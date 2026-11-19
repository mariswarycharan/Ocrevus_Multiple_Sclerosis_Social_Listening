"""
Main pipeline for SLE Social Media Topic Modeling & Classification.

Modular architecture:
    - `llm_client.py`                         : Multi-platform LLM router & SDK callers
    - `prompts/topic_classification_prompt.md` : External system/classification prompt
    - `.env`                                  : API keys and endpoint configurations

Enhanced with:
    - Alternating 2-API-Key Strategy: Odd batches -> API Key 1, Even batches -> API Key 2
    - Fast pair execution with 2s inter-request delay and 10s rate-limit pacing between pairs
    - Dynamic Solo Retry Protocol with 10s recovery pause and alternate API key rotation
    - Strict Sequential Row-Order Preservation: Batch N is always applied before Batch N+1
    - Continuous Auto-Saving and Atomic Excel Checkpointing (disaster recovery on interrupt/crash)
    - Beautiful Rich Terminal Dashboard with Live Progress Bar, Countdown Pacing, and Status Logging
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from llm_client import call_llm, list_supported_platforms

# Initialize global Rich Console
console = Console()


# ==============================================================================
# GLOBAL CONFIGURATION — Platform, Delays, Batching, and File Paths
# ==============================================================================

# LLM Platform & Model (Gemini model as requested)
LLM_PLATFORM = "gemini"
LLM_MODEL = "gemini-3.5-flash-lite"  # Active Gemini model on OpenAI-compatible endpoint eg. gemini-3.5-flash-lite

# LLM sampling & retry parameters
LLM_TEMPERATURE = 0.0
LLM_TOP_P = None
LLM_USE_JSON_MODE = False
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 5.0

# File & dataset configuration
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILENAME = str(BASE_DIR / "Data" / "Sampled_Twitter_Data_500.xlsx")
SHEET_NAME = "Sheet1"
POST_ID_COLUMN = "Post_ID"
TEXT_COLUMN = "Message"
OUTPUT_FILENAME = str(BASE_DIR / "Output" / "Sample_Output_Topic_Modelling.xlsx")
PROMPT_FILE_PATH = str(BASE_DIR / "prompts" / "MS_ocrevus_topic_classification_system_prompt.md")

# Optional row subsetting for targeted testing (set to None to classify the entire dataset)
ROW_INDICES: list[int] | None = [446]  # e.g., [0, 1, 2, 3, 4] to test the first 5 rows

# Batching & Rate-Limiting Configuration
BATCH_SIZE = 10                      # Number of posts per LLM request
START_BATCH = 1                     # 1-indexed batch number to start/resume from

# Time Delays (as specified for Gemini free tier rate-limit optimization)
INTER_REQUEST_DELAY_SECONDS = 2.0   # Delay between 1st & 2nd request in a pair (1-3s configurable)
PAIR_DELAY_SECONDS = 10.0           # Pacing delay after each 2-batch pair before the next pair (10s)
RETRY_SOLO_DELAY_SECONDS = 10.0     # Recovery pause before retrying a failed batch alone (10s)
MAX_SOLO_RETRIES = 2                # Number of solo retry attempts for a failed batch
AUTO_SAVE_INTERVAL = 1              # Save Excel workbook after every pair (1 = continuous auto-save)


# ==============================================================================
# BATCH DATA STRUCTURE
# ==============================================================================

class BatchItem:
    """Encapsulates a single batch slice, its assigned API keys, state, and results."""

    def __init__(self, batch_number: int, start_idx: int, end_idx: int, batch_df: pd.DataFrame):
        self.batch_number = batch_number
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.batch_df = batch_df

        self.posts_to_classify = ""
        self.post_id_map: dict[Any, str] = {}

        # Odd batches -> Key 1 (index 0); Even batches -> Key 2 (index 1)
        self.primary_key_index = 0 if (batch_number % 2 == 1) else 1
        self.alternate_key_index = 1 if (batch_number % 2 == 1) else 0

        self.is_fully_cached = False
        self.raw_response: str | None = None
        self.parsed_results: dict[str, Any] | None = None
        self.error_reason: str | None = None
        self.parse_error: str | None = None
        self.success = False

    @property
    def primary_key_label(self) -> str:
        return f"API Key {self.primary_key_index + 1}"

    @property
    def alternate_key_label(self) -> str:
        return f"API Key {self.alternate_key_index + 1}"


# ==============================================================================
# PROMPT LOADING & PREPARATION
# ==============================================================================

def load_system_prompt(prompt_path: str | Path) -> str:
    """Load the classification system prompt from an external Markdown or text file."""
    path = Path(prompt_path)
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt file is empty: {path}")
    return content


def build_classification_prompt(prompt_template: str, posts_to_classify: str) -> str:
    """Attach the current batch of social media posts to the loaded system prompt."""
    if "{posts_to_classify}" in prompt_template:
        return prompt_template.replace("{posts_to_classify}", posts_to_classify)
    return f"{prompt_template.rstrip()}\n\n{posts_to_classify}"


# ==============================================================================
# DATA LOADING & CHECKPOINT RESUMPTION
# ==============================================================================

def load_input_data(
    file_path: str | Path,
    sheet_name: str,
    text_column: str,
    post_id_column: str = "post_id",
    row_indices: list[int] | None = None,
) -> pd.DataFrame:
    """
    Load the input CSV or Excel file, apply optional row filtering, and initialize output columns.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Input data file not found: {path}")

    console.print(f"[cyan]📄 Loading dataset from '[bold]{path}[/bold]'...[/cyan]")
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path, sheet_name=sheet_name)

    # df = df.head(10)

    if post_id_column not in df.columns:
        raise KeyError(
            f"Expected post ID column '{post_id_column}' not found in '{path}'. "
            f"Available columns: {list(df.columns)}"
        )

    if text_column not in df.columns:
        raise KeyError(
            f"Expected text column '{text_column}' not found in '{path}'. "
            f"Available columns: {list(df.columns)}"
        )

    if row_indices is not None:
        invalid_indices = [idx for idx in row_indices if idx < 0 or idx >= len(df)]
        if invalid_indices:
            raise IndexError(
                f"Row indices out of range for dataset of length {len(df)}: {invalid_indices}"
            )
        df = df.iloc[row_indices].copy()
    else:
        df = df.copy()

    # Preserve original post IDs from data; ensure post_id is accessible
    if "post_id" not in df.columns:
        df["post_id"] = df[post_id_column]

    # Initialize classification result columns
    if "topics" not in df.columns:
        df["topics"] = ""
    if "theme" not in df.columns:
        df["theme"] = ""
    if "justification" not in df.columns:
        df["justification"] = ""

    console.print(f"[green]✅ Dataset loaded successfully: {len(df)} total rows ready for classification.[/green]")
    return df


def load_existing_checkpoint(
    output_filename: str | Path,
    df: pd.DataFrame,
    classification_cache: dict[str, dict[str, str]],
    start_batch: int,
    batch_size: int,
    text_column: str,
    post_id_column: str = "post_id",
) -> int:
    """
    If resuming (start_batch > 1) and output Excel exists, pre-load previously completed
    rows into df and deduplication cache so prior results are preserved.
    """
    out_path = Path(output_filename)
    if start_batch <= 1 or not out_path.is_file():
        return 0

    try:
        console.print(f"[yellow]🔄 Resumption detected (Batch {start_batch}): Loading prior results from '{out_path}'...[/yellow]")
        existing_df = pd.read_excel(out_path, sheet_name="Master Data")
        if post_id_column not in existing_df.columns or "topics" not in existing_df.columns:
            return 0

        prior_count = 0
        cutoff_idx = (start_batch - 1) * batch_size
        prior_post_ids = set(df.iloc[:cutoff_idx][post_id_column].astype(str).str.strip().tolist())

        for _, row in existing_df.iterrows():
            pid = row.get(post_id_column)
            if pd.isna(pid):
                continue
            clean_pid_str = str(pid).strip()
            if clean_pid_str in prior_post_ids:
                topic = str(row.get("topics", "") or "")
                theme = str(row.get("theme", "") or "")
                just = str(row.get("justification", "") or "")
                if topic:
                    if clean_pid_str.isdigit():
                        mask = (df[post_id_column] == int(clean_pid_str)) | (df[post_id_column].astype(str) == clean_pid_str)
                    else:
                        mask = df[post_id_column].astype(str) == clean_pid_str
                    df.loc[mask, "topics"] = topic
                    df.loc[mask, "theme"] = theme
                    df.loc[mask, "justification"] = just
                    txt = str(row.get(text_column, "") or "").strip()
                    if txt:
                        classification_cache[txt] = {
                            "topic": topic,
                            "theme": theme,
                            "justification": just,
                        }
                    prior_count += 1

        console.print(f"[green]✔ Restored {prior_count} previously classified rows from checkpoint.[/green]")
        return prior_count
    except Exception as exc:
        console.print(f"[dim yellow]Notice: Could not load prior checkpoint ({exc}). Starting fresh from Batch {start_batch}.[/dim yellow]")
        return 0


# ==============================================================================
# JSON RESPONSE PARSING
# ==============================================================================

def parse_json_response(response_content: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """
    Robustly clean and parse an LLM's raw text output into a Python dictionary.
    Handles markdown code fences, surrounding commentary, and trailing commas.
    """
    if not response_content or not str(response_content).strip():
        return None, "Empty response content from LLM."

    cleaned = str(response_content).strip()

    try:
        # 1. Strip markdown code fences (```json ... ``` or ``` ... ```)
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        # 2. Extract the outermost JSON object if commentary surrounds the JSON
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            cleaned = cleaned[start_idx : end_idx + 1]

        # 3. Remove trailing commas before closing } or ] (invalid in standard JSON)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

        # 4. Parse JSON string into Python object
        parsed = json.loads(cleaned)

        # 5. Ensure top-level structure is a dictionary
        if not isinstance(parsed, dict):
            return None, f"Parsed JSON is not an object/dict (got {type(parsed).__name__})."

        return parsed, None

    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    except Exception as exc:
        return None, f"Unexpected error while parsing JSON: {exc}"


# ==============================================================================
# FAILURE RECORDING & DATAFRAME UPDATES
# ==============================================================================

def record_batch_failure(
    failure_records: list[dict[str, Any]],
    post_id_map: dict[Any, str],
    text_column: str,
    post_id_column: str = "post_id",
    batch_number: int = 1,
    *,
    error_reason: str | None = None,
    llm_raw_response: str | None = None,
    parse_error: str | None = None,
) -> None:
    """Append failure metadata for every uncached post in a failed batch."""
    for post_id, post_text in post_id_map.items():
        record: dict[str, Any] = {
            post_id_column: post_id,
            text_column: post_text,
            "batch_number": batch_number,
        }
        if error_reason is not None:
            record["error_reason"] = error_reason
        if llm_raw_response is not None or parse_error is not None:
            record["llm_raw_response"] = llm_raw_response
            record["parse_error"] = parse_error
        failure_records.append(record)


def apply_batch_results(
    df: pd.DataFrame,
    batch_results: dict[str, Any],
    post_id_map: dict[Any, str],
    classification_cache: dict[str, dict[str, str]],
    text_column: str,
    post_id_column: str = "post_id",
    batch_number: int = 1,
    raw_response: str = "",
    json_parse_failed_records: list[dict[str, Any]] | None = None,
) -> bool:
    """
    Validate parsed JSON batch results, update the deduplication cache, and write
    assigned topics, themes, and justifications into the master DataFrame in exact post order.
    """
    try:
        for post_id, data in batch_results.items():
            if not isinstance(data, dict):
                raise ValueError(
                    f"Expected dict for post_id '{post_id}', got {type(data).__name__}"
                )

            topic = data.get("topic", "No topic assigned")
            theme = data.get("theme", "No theme assigned")
            justification = data.get("justification", "No justification provided.")

            clean_str_pid = str(post_id).strip()
            numeric_pid = int(clean_str_pid) if clean_str_pid.isdigit() else None

            original_text = (
                post_id_map.get(post_id)
                or (post_id_map.get(numeric_pid) if numeric_pid is not None else None)
                or post_id_map.get(clean_str_pid)
            )

            if original_text:
                classification_cache[original_text] = {
                    "topic": topic,
                    "theme": theme,
                    "justification": justification,
                }

            # In-place DataFrame assignment preserving exact post ID
            if numeric_pid is not None:
                mask = (df[post_id_column] == numeric_pid) | (df[post_id_column].astype(str) == clean_str_pid)
            else:
                mask = df[post_id_column].astype(str) == clean_str_pid

            df.loc[mask, "topics"] = topic
            df.loc[mask, "theme"] = theme
            df.loc[mask, "justification"] = justification

        # Backfill any duplicate texts within this same batch
        for pid, post_text in post_id_map.items():
            if post_text in classification_cache:
                cached = classification_cache[post_text]
                clean_pid_str = str(pid).strip()
                if clean_pid_str.isdigit():
                    num_pid = int(clean_pid_str)
                    mask = (df[post_id_column] == num_pid) | (df[post_id_column].astype(str) == clean_pid_str)
                else:
                    mask = df[post_id_column].astype(str) == clean_pid_str

                df.loc[mask, "topics"] = cached["topic"]
                df.loc[mask, "theme"] = cached["theme"]
                df.loc[mask, "justification"] = cached["justification"]

        return True

    except Exception as exc:
        console.print(f"[bold red]❌ Batch {batch_number}: Error applying parsed results: {exc}[/bold red]")
        if json_parse_failed_records is not None:
            record_batch_failure(
                failure_records=json_parse_failed_records,
                post_id_map=post_id_map,
                text_column=text_column,
                post_id_column=post_id_column,
                batch_number=batch_number,
                llm_raw_response=raw_response,
                parse_error=f"Error applying parsed results: {exc}",
            )
        return False


# ==============================================================================
# ATOMIC EXCEL SAVING & CONTINUOUS AUTO-SAVE
# ==============================================================================

def save_results_to_excel(
    df: pd.DataFrame,
    llm_call_failed_records: list[dict[str, Any]],
    json_parse_failed_records: list[dict[str, Any]],
    output_filename: str | Path,
    text_column: str,
    post_id_column: str = "post_id",
    preview: bool = True,
    is_checkpoint: bool = False,
) -> None:
    """
    Atomic Excel export writing to a temporary file before renaming, ensuring that
    even if an interruption or crash occurs mid-write, data is never corrupted.
    """
    output_path = Path(output_filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    llm_call_failed_df = pd.DataFrame(llm_call_failed_records)
    json_parse_failed_df = pd.DataFrame(json_parse_failed_records)

    llm_failed_columns = [post_id_column, text_column, "batch_number", "error_reason"]
    json_failed_columns = [
        post_id_column,
        text_column,
        "batch_number",
        "llm_raw_response",
        "parse_error",
    ]

    temp_path = output_path.with_suffix(".tmp.xlsx")

    def _write_workbook(target: Path) -> None:
        with pd.ExcelWriter(target, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Master Data", index=False)

            if not llm_call_failed_df.empty:
                llm_call_failed_df.to_excel(writer, sheet_name="LLM Call Failed", index=False)
            else:
                pd.DataFrame(columns=llm_failed_columns).to_excel(
                    writer, sheet_name="LLM Call Failed", index=False
                )

            if not json_parse_failed_df.empty:
                json_parse_failed_df.to_excel(writer, sheet_name="JSON Parsing Failed", index=False)
            else:
                pd.DataFrame(columns=json_failed_columns).to_excel(
                    writer, sheet_name="JSON Parsing Failed", index=False
                )

    try:
        _write_workbook(temp_path)
        if temp_path.exists():
            temp_path.replace(output_path)
    except Exception:
        # Fallback to direct write if file replace encounters Windows handle lock
        _write_workbook(output_path)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    if preview:
        console.print("\n[bold green]✅ Classification complete! Previewing classified results:[/bold green]")
        # preview_df = df[[text_column, "topics", "theme", "justification"]].head(15)
        # print(preview_df.to_string(index=False))

        summary_table = Table(title="Execution Summary", border_style="cyan")
        summary_table.add_column("Category", style="bold white")
        summary_table.add_column("Count / Status", style="green")

        classified_count = (df["topics"].str.strip() != "").sum()
        summary_table.add_row("Total Master Posts", str(len(df)))
        summary_table.add_row("Successfully Classified", f"{classified_count} posts")
        summary_table.add_row("LLM Call Failed", f"{len(llm_call_failed_df)} posts")
        summary_table.add_row("JSON Parsing Failed", f"{len(json_parse_failed_df)} posts")
        summary_table.add_row("Output Workbook", str(output_path.resolve()))
        console.print(summary_table)

    elif is_checkpoint:
        completed = (df["topics"].str.strip() != "").sum()
        console.print(f"[dim green]💾 Auto-saved checkpoint: {completed}/{len(df)} rows saved to '{output_path.name}'[/dim green]")


# ==============================================================================
# RATE-LIMIT / TIME DELAY PACING
# ==============================================================================

def time_delay(
    delay_seconds: float,
    reason: str = "",
    progress: Progress | None = None,
    task_id: Any = None,
) -> None:
    """
    Wait for delay_seconds with clear terminal logging and live countdown.
    """
    if delay_seconds <= 0:
        return

    reason_text = f" ({reason})" if reason else ""
    console.print(f"[dim yellow]⏳ Rate-limit pacing: waiting {delay_seconds:.1f}s{reason_text}...[/dim yellow]")

    start_time = time.time()
    while True:
        elapsed = time.time() - start_time
        remaining = delay_seconds - elapsed
        if remaining <= 0:
            break
        if progress is not None and task_id is not None:
            progress.update(task_id, status=f"Pacing pause: {remaining:.1f}s remaining")
        time.sleep(min(0.2, remaining))

    if progress is not None and task_id is not None:
        progress.update(task_id, status="Active")


# ==============================================================================
# SINGLE BATCH API CALL & SOLO RETRY PROTOCOL
# ==============================================================================

def execute_batch_call(
    batch: BatchItem,
    key_index: int,
    system_prompt_template: str,
) -> bool:
    """
    Execute an LLM request and parse JSON for a single batch using the given API key index.
    """
    full_prompt = build_classification_prompt(system_prompt_template, batch.posts_to_classify)
    key_label = f"API Key {key_index + 1}"

    console.print(
        f"[cyan]🚀 [Batch {batch.batch_number}] Sending prompt via [bold]{key_label}[/bold] "
        f"({len(batch.post_id_map)} posts)...[/cyan]"
    )

    raw_response, call_error = call_llm(
        prompt=full_prompt,
        platform=LLM_PLATFORM,
        model=LLM_MODEL,
        api_key_index=key_index,
        max_retries=MAX_RETRIES,
        retry_delay=RETRY_DELAY_SECONDS,
        temperature=LLM_TEMPERATURE,
        top_p=LLM_TOP_P,
        use_json_mode=LLM_USE_JSON_MODE,
    )

    batch.raw_response = raw_response
    if raw_response is None:
        batch.error_reason = call_error or "LLM call failed after retries"
        batch.success = False
        console.print(f"[bold red]❌ Batch {batch.batch_number}: LLM request failed on {key_label}. Error: {batch.error_reason}[/bold red]")
        return False

    parsed, parse_error = parse_json_response(raw_response)
    batch.parsed_results = parsed
    if parsed is None:
        batch.parse_error = parse_error or "JSON parsing error"
        batch.success = False
        console.print(f"[bold red]❌ Batch {batch.batch_number}: JSON parse failed on {key_label}. Error: {batch.parse_error}[/bold red]")
        return False

    batch.success = True
    batch.error_reason = None
    batch.parse_error = None
    console.print(f"[green]✔ [Batch {batch.batch_number}] Response received & parsed successfully via {key_label}![/green]")
    return True


def solo_retry_batch(
    batch: BatchItem,
    system_prompt_template: str,
    progress: Progress,
    task_id: Any,
) -> bool:
    """
    Dynamic Solo Retry Protocol:
      1. Wait RETRY_SOLO_DELAY_SECONDS (10s) recovery pause.
      2. Retry the failed batch alone using its alternate API key.
      3. If needed, attempt once more before marking as failed.
    """
    console.print(
        f"\n[bold yellow]⚠️ SOLO RETRY PROTOCOL ACTIVATED for Batch {batch.batch_number}[/bold yellow]\n"
        f"[yellow]Reason: {batch.error_reason or batch.parse_error}[/yellow]\n"
        f"[yellow]Row-order protection: Waiting {RETRY_SOLO_DELAY_SECONDS}s before retrying Batch {batch.batch_number} alone...[/yellow]"
    )

    time_delay(
        RETRY_SOLO_DELAY_SECONDS,
        reason=f"Solo retry cooldown for Batch {batch.batch_number}",
        progress=progress,
        task_id=task_id,
    )

    for attempt in range(1, MAX_SOLO_RETRIES + 1):
        # First solo attempt uses alternate key; subsequent attempts rotate
        target_key = batch.alternate_key_index if attempt % 2 == 1 else batch.primary_key_index
        key_label = f"API Key {target_key + 1}"

        console.print(
            f"[bold yellow]🔄 Solo retry attempt {attempt}/{MAX_SOLO_RETRIES} for Batch {batch.batch_number} "
            f"using alternate {key_label}...[/bold yellow]"
        )
        progress.update(task_id, status=f"Retrying Batch {batch.batch_number} ({key_label})")

        success = execute_batch_call(
            batch=batch,
            key_index=target_key,
            system_prompt_template=system_prompt_template,
        )

        if success:
            console.print(f"[bold green]🎉 Batch {batch.batch_number} RECOVERED successfully on solo retry ({key_label})![/bold green]")
            return True

        if attempt < MAX_SOLO_RETRIES:
            time.sleep(5.0)

    console.print(f"[bold red]❌ Batch {batch.batch_number} could not be recovered after solo retries. Marking failed.[/bold red]")
    return False


# ==============================================================================
# BATCH COMMIT (STRICT SEQUENTIAL ORDER PRESERVATION)
# ==============================================================================

def commit_batch_results(
    batch: BatchItem,
    df: pd.DataFrame,
    classification_cache: dict[str, dict[str, str]],
    llm_call_failed_records: list[dict[str, Any]],
    json_parse_failed_records: list[dict[str, Any]],
    text_column: str,
    post_id_column: str = "post_id",
) -> None:
    """
    Commit batch results to the DataFrame or record failure.
    Called strictly in sequential order (Batch 1, then Batch 2, then Batch 3...)
    so that rows in Master Data and failure sheets remain perfectly aligned.
    """
    if batch.is_fully_cached:
        console.print(f"[dim cyan]⚡ Batch {batch.batch_number}: Reused classifications from deduplication cache[/dim cyan]")
        return

    if batch.success and batch.parsed_results is not None:
        apply_batch_results(
            df=df,
            batch_results=batch.parsed_results,
            post_id_map=batch.post_id_map,
            classification_cache=classification_cache,
            text_column=text_column,
            post_id_column=post_id_column,
            batch_number=batch.batch_number,
            raw_response=batch.raw_response or "",
            json_parse_failed_records=json_parse_failed_records,
        )
        console.print(f"[bold green]✔ Batch {batch.batch_number} committed ({len(batch.parsed_results)} posts applied to Master Data)[/bold green]")
    elif batch.raw_response is None:
        console.print(f"[bold red]❌ Batch {batch.batch_number} recorded into 'LLM Call Failed' tab[/bold red]")
        record_batch_failure(
            failure_records=llm_call_failed_records,
            post_id_map=batch.post_id_map,
            text_column=text_column,
            post_id_column=post_id_column,
            batch_number=batch.batch_number,
            error_reason=batch.error_reason,
        )
    else:
        console.print(f"[bold red]❌ Batch {batch.batch_number} recorded into 'JSON Parsing Failed' tab[/bold red]")
        record_batch_failure(
            failure_records=json_parse_failed_records,
            post_id_map=batch.post_id_map,
            text_column=text_column,
            post_id_column=post_id_column,
            batch_number=batch.batch_number,
            llm_raw_response=batch.raw_response,
            parse_error=batch.parse_error,
        )


# ==============================================================================
# MAIN PIPELINE ORCHESTRATION
# ==============================================================================

def main() -> None:
    """Run the end-to-end SLE social media topic modeling classification pipeline."""
    supported = list_supported_platforms()
    if LLM_PLATFORM.strip().lower() not in supported:
        console.print(
            f"[bold red]❌ Invalid LLM_PLATFORM '{LLM_PLATFORM}'. "
            f"Choose from: {', '.join(supported.keys())}[/bold red]"
        )
        return

    platform_info = supported[LLM_PLATFORM.strip().lower()]
    active_model = LLM_MODEL or platform_info["default_model"]

    # 1. Load the external system prompt
    try:
        system_prompt_template = load_system_prompt(PROMPT_FILE_PATH)
    except Exception as exc:
        console.print(f"[bold red]❌ Could not load system prompt: {exc}[/bold red]")
        return

    # 2. Load and prepare the input DataFrame
    try:
        df = load_input_data(
            file_path=INPUT_FILENAME,
            sheet_name=SHEET_NAME,
            text_column=TEXT_COLUMN,
            post_id_column=POST_ID_COLUMN,
            row_indices=ROW_INDICES,
        )
    except Exception as exc:
        console.print(f"\n[bold red]❌ An error occurred while loading the file: {exc}[/bold red]")
        console.print("[bold red]❌ Script stopped because the DataFrame could not be loaded.[/bold red]")
        return

    # 3. Validate batching parameters
    if BATCH_SIZE < 1 or START_BATCH < 1:
        console.print("[bold red]❌ Error: BATCH_SIZE and START_BATCH must both be >= 1.[/bold red]")
        return

    start_index = (START_BATCH - 1) * BATCH_SIZE
    if start_index >= len(df):
        console.print(
            f"[bold red]❌ Error: Start batch {START_BATCH} is beyond the end of the file. "
            f"The file only has {len(df)} rows.[/bold red]"
        )
        return

    # 4. Initialize tracking structures
    classification_cache: dict[str, dict[str, str]] = {}
    llm_call_failed_records: list[dict[str, Any]] = []
    json_parse_failed_records: list[dict[str, Any]] = []

    # Restore existing checkpoint if resuming from start_batch > 1
    load_existing_checkpoint(
        output_filename=OUTPUT_FILENAME,
        df=df,
        classification_cache=classification_cache,
        start_batch=START_BATCH,
        batch_size=BATCH_SIZE,
        text_column=TEXT_COLUMN,
        post_id_column=POST_ID_COLUMN,
    )

    # 5. Build all BatchItem objects
    batches: list[BatchItem] = []
    for idx in range(start_index, len(df), BATCH_SIZE):
        batch_num = idx // BATCH_SIZE + 1
        batch_slice = df.iloc[idx : idx + BATCH_SIZE]
        batch_item = BatchItem(
            batch_number=batch_num,
            start_idx=idx,
            end_idx=min(idx + BATCH_SIZE, len(df)),
            batch_df=batch_slice,
        )

        seen_in_batch: set[str] = set()
        for row_idx, row in batch_slice.iterrows():
            raw_cell = row[TEXT_COLUMN]
            text = "" if pd.isna(raw_cell) else str(raw_cell).strip()

            if text in classification_cache:
                cached = classification_cache[text]
                df.at[row_idx, "topics"] = cached["topic"]
                df.at[row_idx, "theme"] = cached["theme"]
                df.at[row_idx, "justification"] = cached["justification"]
                continue

            raw_pid = row[POST_ID_COLUMN]
            if isinstance(raw_pid, float) and raw_pid.is_integer():
                pid: Any = int(raw_pid)
            elif str(raw_pid).strip().isdigit():
                pid = int(raw_pid)
            else:
                pid = str(raw_pid).strip()

            batch_item.post_id_map[pid] = text

            if text not in seen_in_batch:
                batch_item.posts_to_classify += (
                    f"--- Post ID: {pid} --- \n\n"
                    f"Post content: \n{text}\n\n"
                    f"---\n\n"
                )
                seen_in_batch.add(text)

        if not batch_item.posts_to_classify.strip():
            batch_item.is_fully_cached = True
            batch_item.success = True

        batches.append(batch_item)

    # Group batches into pairs: (Batch 1, Batch 2), (Batch 3, Batch 4)...
    batch_pairs: list[list[BatchItem]] = []
    for i in range(0, len(batches), 2):
        batch_pairs.append(batches[i : i + 2])

    total_batches = len(batches)
    total_pairs = len(batch_pairs)

    # Display Rich Configuration Dashboard Banner
    console.print(
        Panel.fit(
            f"[bold cyan]SLE Social Media Topic Modeling Pipeline[/bold cyan]\n"
            f"[white]Platform:[/] [green]{LLM_PLATFORM}[/]  |  [white]Model:[/] [green]{active_model}[/]\n"
            f"[white]Dataset Rows:[/] {len(df)}  |  [white]Batch Size:[/] {BATCH_SIZE} posts\n"
            f"[white]Total Batches:[/] {total_batches}  |  [white]Batch Pairs:[/] {total_pairs}  |  [white]Start Batch:[/] {START_BATCH}\n"
            f"[white]Alternating Strategy:[/] Odd Batches -> [bold magenta]API Key 1[/bold magenta]  |  Even Batches -> [bold cyan]API Key 2[/bold cyan]\n"
            f"[white]Pacing Config:[/] Inter-request: {INTER_REQUEST_DELAY_SECONDS}s  |  Pair Delay: {PAIR_DELAY_SECONDS}s  |  Solo Retry Delay: {RETRY_SOLO_DELAY_SECONDS}s\n"
            f"[white]Continuous Auto-Save:[/] Enabled -> [bold green]{OUTPUT_FILENAME}[/bold green]",
            title="[bold yellow]System Configuration[/bold yellow]",
            border_style="cyan",
        )
    )

    # Setup Graceful Interruption Handler (Disaster Recovery)
    interrupted = False

    def handle_signal(sig, frame):
        nonlocal interrupted
        interrupted = True
        console.print("\n[bold yellow]⚠️ Interruption signal received (Ctrl+C). Performing emergency auto-save...[/bold yellow]")
        save_results_to_excel(
            df=df,
            llm_call_failed_records=llm_call_failed_records,
            json_parse_failed_records=json_parse_failed_records,
            output_filename=OUTPUT_FILENAME,
            text_column=TEXT_COLUMN,
            post_id_column=POST_ID_COLUMN,
            preview=False,
            is_checkpoint=True,
        )
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handle_signal)
    except Exception:
        pass

    # Setup Rich Progress Bar
    progress = Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(bar_width=35, style="bright_black", complete_style="green", finished_style="bold green"),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("[yellow]{task.fields[status]}[/yellow]"),
        console=console,
    )

    try:
        with progress:
            progress_task = progress.add_task(
                "Topic Modeling",
                total=total_batches,
                completed=0,
                status="Initializing",
            )

            for pair_idx, pair in enumerate(batch_pairs):
                if interrupted:
                    break

                batch_A = pair[0]
                batch_B = pair[1] if len(pair) > 1 else None

                pair_desc = (
                    f"Batch {batch_A.batch_number}"
                    if batch_B is None
                    else f"Batch {batch_A.batch_number} ({batch_A.primary_key_label}) & Batch {batch_B.batch_number} ({batch_B.primary_key_label})"
                )
                console.print(f"\n[bold blue]━━━ [Pair {pair_idx + 1}/{total_pairs}] Processing {pair_desc} ━━━[/bold blue]")

                # --------------------------------------------------------------
                # STEP 1: Execute Batch A
                # --------------------------------------------------------------
                if not batch_A.is_fully_cached:
                    progress.update(
                        progress_task,
                        status=f"Running Batch {batch_A.batch_number} ({batch_A.primary_key_label})",
                    )
                    execute_batch_call(
                        batch=batch_A,
                        key_index=batch_A.primary_key_index,
                        system_prompt_template=system_prompt_template,
                    )
                else:
                    progress.update(
                        progress_task,
                        status=f"Batch {batch_A.batch_number} (Cached)",
                    )

                # --------------------------------------------------------------
                # STEP 2: Inter-request Delay & Execute Batch B
                # --------------------------------------------------------------
                if batch_B is not None:
                    if not batch_B.is_fully_cached:
                        # Apply short delay between the two requests in the pair
                        if not batch_A.is_fully_cached:
                            time_delay(
                                INTER_REQUEST_DELAY_SECONDS,
                                reason=f"Inter-request gap before Batch {batch_B.batch_number}",
                                progress=progress,
                                task_id=progress_task,
                            )

                        progress.update(
                            progress_task,
                            status=f"Running Batch {batch_B.batch_number} ({batch_B.primary_key_label})",
                        )
                        execute_batch_call(
                            batch=batch_B,
                            key_index=batch_B.primary_key_index,
                            system_prompt_template=system_prompt_template,
                        )
                    else:
                        progress.update(
                            progress_task,
                            status=f"Batch {batch_B.batch_number} (Cached)",
                        )

                # --------------------------------------------------------------
                # STEP 3: Order Preservation & Dynamic Solo Retries
                # --------------------------------------------------------------
                # Case A: Batch A failed -> Hold Batch B, wait 10s, retry Batch A alone
                if not batch_A.is_fully_cached and not batch_A.success:
                    held_notice = f"Holding Batch {batch_B.batch_number} in memory" if batch_B else "Holding queue"
                    console.print(
                        f"[bold yellow]⚠️ Batch {batch_A.batch_number} failed. {held_notice} to strictly preserve row order.[/bold yellow]"
                    )
                    solo_retry_batch(
                        batch=batch_A,
                        system_prompt_template=system_prompt_template,
                        progress=progress,
                        task_id=progress_task,
                    )

                # Case B: Batch B failed -> Retry Batch B alone
                if batch_B is not None and not batch_B.is_fully_cached and not batch_B.success:
                    console.print(
                        f"[bold yellow]⚠️ Batch {batch_B.batch_number} failed. Initiating solo retry for Batch {batch_B.batch_number}...[/bold yellow]"
                    )
                    solo_retry_batch(
                        batch=batch_B,
                        system_prompt_template=system_prompt_template,
                        progress=progress,
                        task_id=progress_task,
                    )

                # --------------------------------------------------------------
                # STEP 4: Strict Sequential Commit (Batch A, then Batch B)
                # --------------------------------------------------------------
                commit_batch_results(
                    batch=batch_A,
                    df=df,
                    classification_cache=classification_cache,
                    llm_call_failed_records=llm_call_failed_records,
                    json_parse_failed_records=json_parse_failed_records,
                    text_column=TEXT_COLUMN,
                    post_id_column=POST_ID_COLUMN,
                )
                progress.advance(progress_task, 1)

                if batch_B is not None:
                    commit_batch_results(
                        batch=batch_B,
                        df=df,
                        classification_cache=classification_cache,
                        llm_call_failed_records=llm_call_failed_records,
                        json_parse_failed_records=json_parse_failed_records,
                        text_column=TEXT_COLUMN,
                        post_id_column=POST_ID_COLUMN,
                    )
                    progress.advance(progress_task, 1)

                # --------------------------------------------------------------
                # STEP 5: Continuous Auto-Save Checkpoint
                # --------------------------------------------------------------
                save_results_to_excel(
                    df=df,
                    llm_call_failed_records=llm_call_failed_records,
                    json_parse_failed_records=json_parse_failed_records,
                    output_filename=OUTPUT_FILENAME,
                    text_column=TEXT_COLUMN,
                    post_id_column=POST_ID_COLUMN,
                    preview=False,
                    is_checkpoint=True,
                )

                # --------------------------------------------------------------
                # STEP 6: Rate-Limit Pacing Delay (10s) Between Pairs
                # --------------------------------------------------------------
                has_next_pair = pair_idx < (total_pairs - 1)
                pair_made_api_call = (not batch_A.is_fully_cached) or (
                    batch_B is not None and not batch_B.is_fully_cached
                )
                if has_next_pair and pair_made_api_call:
                    time_delay(
                        PAIR_DELAY_SECONDS,
                        reason="10s rate-limit pacing before next batch pair",
                        progress=progress,
                        task_id=progress_task,
                    )

            progress.update(progress_task, status="Complete")

    except KeyboardInterrupt:
        console.print("\n[bold yellow]⚠️ Process interrupted by user (Ctrl+C). Saving current progress...[/bold yellow]")
    except Exception as exc:
        console.print(f"\n[bold red]❌ Unexpected pipeline exception: {exc}. Saving current progress...[/bold red]")
    finally:
        # Final export with full preview and summary table
        save_results_to_excel(
            df=df,
            llm_call_failed_records=llm_call_failed_records,
            json_parse_failed_records=json_parse_failed_records,
            output_filename=OUTPUT_FILENAME,
            text_column=TEXT_COLUMN,
            post_id_column=POST_ID_COLUMN,
            preview=True,
            is_checkpoint=False,
        )


if __name__ == "__main__":
    main()
