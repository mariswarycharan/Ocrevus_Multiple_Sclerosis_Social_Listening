"""
find_duplicates.py  (v2 - content-based fuzzy grouping)

Purpose
-------
Split a social-media (Sprinklr / Twitter) export into UNIQUE posts and
DUPLICATE posts based ONLY on the post content (the Message column), and tag
every row with a Group ID so each duplicate can be traced back to the unique
post it repeats.

Output workbook (3 tabs)
------------------------
1. "Raw Data"  : the entire original data, untouched.
2. "Unique"    : one representative row per group (the FIRST occurrence).
                 Extra columns:  Group_ID, Repeated_Count, Source_Row
3. "Duplicate" : every other row of each group.
                 Extra columns:  Group_ID, Match_Type, Similarity_Score,
                                 Unique_Source_Row, Source_Row

   Group_ID is numbered 1..N in the order groups first appear in the data.
   Unique tab Group_ID 5  <->  Duplicate tab rows with Group_ID 5.
   Repeated_Count = number of rows in the Duplicate tab for that Group_ID.
   Source_Row = row number in the source sheet (header = row 1).

How "duplicate" is decided
--------------------------
Step 1  NORMALISE the text (comparison only - your data is never changed):
        HTML entities, unicode form, case, "RT @user:" prefix, URLs (every
        repost gets a different t.co link), zero-width chars, extra spaces.
Step 2  EXACT match on the normalised text (instant hash lookup).
Step 3  FUZZY match: whole-text similarity >= SIMILARITY_THRESHOLD (95)
        using rapidfuzz.fuzz.ratio. Every member is compared against its
        group's REPRESENTATIVE (first occurrence), so groups cannot "drift"
        by chaining A~B~C~D.
Step 4  CONTENT-CHANGE GUARD: even if the text is >= 95 % similar, if one
        contiguous block of more than MAX_CHANGED_WORDS words was added /
        removed / replaced (e.g. a quote-repost with the user's own comment,
        or a changed last line), the post is treated as NEW content and
        stays UNIQUE.

Speed: candidates are pre-filtered by text length (a >= 95 % match is
mathematically impossible outside a ~+-10 % length window), so 10K-100K rows
run in seconds to a couple of minutes.

How to use
----------
Edit the paths / settings below and run:
    python find_duplicates.py
or override the paths from the command line:
    python find_duplicates.py input.xlsx output.xlsx

Requires:  pip install pandas openpyxl rapidfuzz
"""

from __future__ import annotations

import argparse
import html
import math
import re
import sys
import time
import unicodedata
from collections import Counter

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Indel


# =========================================================================
# EDIT THESE PATHS
# =========================================================================
INPUT_FILE_PATH = r"Sprinklr_MS_Raw_Data\QuickSearch (2)\Whatarethemostrecentposts_b8e0b3b6-bd01-47ac-8402-977a724376b1_1.xlsx"  # .csv or .xlsx/.xls/.xlsm
OUTPUT_FILE_PATH = "Output/Cleaned_unique_twitter_data_MS_new_logic.xlsx"   # the 3-tab result file
SHEET_NAME = 0   # Excel input only: sheet name (e.g. "Sheet1") or 0 = first sheet
# =========================================================================

# Column that holds the post content. (Matching is case/space-insensitive on
# the header name, so "message " or "MESSAGE" will also be found.)
MESSAGE_COLUMN = "Message"

# -------------------------------------------------------------------------
# MATCHING SETTINGS
# -------------------------------------------------------------------------
SIMILARITY_THRESHOLD = 100.0   # % whole-text similarity needed to call two posts duplicates

# Content-change guard (see Step 4 above). A contiguous block of MORE than this
# many words that differs between two posts means "different content" -> unique.
#   0-1 = very strict (almost any edit makes the post unique)
#   2   = default (typo / tiny tag changes still duplicate; a new sentence/comment does not)
#   None = disable the guard (pure 95 % similarity rule)
MAX_CHANGED_WORDS = 2

# Texts shorter than this (after normalisation) are only matched EXACTLY.
# (At 95 % a short text allows ~0 edits anyway; this just avoids false matches.)
MIN_LEN_FOR_FUZZY = 25

# Rows whose Message is empty / whitespace-only:
#   "unique" -> every blank row is its own group (never called a duplicate)  [safest]
#   "group"  -> all blank rows are put together in one group
BLANK_MESSAGE_POLICY = "unique"

# -------------------------------------------------------------------------
# NORMALISATION (used for COMPARISON ONLY - output keeps original text)
# -------------------------------------------------------------------------
LOWERCASE = True              # case-insensitive comparison
STRIP_RT_PREFIX = True        # "RT @someone: text" is compared as "text"
REMOVE_URLS = True            # reposts get different shortened links
REMOVE_MENTIONS = False       # set True to ignore @handles everywhere in the text
REMOVE_PUNCTUATION_EMOJI = False  # set True to compare only letters/digits/spaces

# -------------------------------------------------------------------------
# OUTPUT SETTINGS
# -------------------------------------------------------------------------
RAW_SHEET, UNIQUE_SHEET, DUPLICATE_SHEET = "Raw Data", "Unique", "Duplicate"
COL_GROUP_ID = "Group_ID"
COL_REPEATED = "Repeated_Count"
COL_SOURCE_ROW = "Source_Row"
COL_MATCH_TYPE = "Match_Type"
COL_SIMILARITY = "Similarity_Score"
COL_UNIQUE_ROW = "Unique_Source_Row"
ADD_GROUP_ID_TO_RAW_SHEET = False   # True = also add Group_ID to the Raw Data tab
PROGRESS_EVERY = 2000               # print progress every N rows (0 = silent)

EXCEL_MAX_ROWS = 1_048_575          # Excel limit (excluding header row)


# =========================================================================
# LOADING
# =========================================================================
def load_data(input_path: str, sheet_name) -> pd.DataFrame:
    """Load .csv or .xlsx/.xls/.xlsm as text (nothing is converted or lost)."""
    lower = input_path.lower()
    if lower.endswith(".csv"):
        last_err = None
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return pd.read_csv(input_path, dtype=str, keep_default_na=False, encoding=enc)
            except UnicodeDecodeError as err:
                last_err = err
        raise ValueError(f"Could not decode CSV file: {last_err}")
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        return pd.read_excel(input_path, sheet_name=sheet_name, dtype=str, keep_default_na=False)
    raise ValueError(f"Unsupported file type: {input_path}. Use .csv or .xlsx/.xls/.xlsm")


def resolve_message_column(df: pd.DataFrame) -> str:
    """Return the real header name of the message column (tolerant of case/spaces)."""
    if MESSAGE_COLUMN in df.columns:
        return MESSAGE_COLUMN
    wanted = str(MESSAGE_COLUMN).strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == wanted:
            return col
    import difflib
    close = difflib.get_close_matches(str(MESSAGE_COLUMN), [str(c) for c in df.columns], n=3)
    raise ValueError(
        f"Message column '{MESSAGE_COLUMN}' not found.\n"
        f"Closest matches: {close}\nColumns in file: {list(df.columns)}"
    )


# =========================================================================
# NORMALISATION
# =========================================================================
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_RT_PREFIX_RE = re.compile(r"^\s*rt\s+@\w+\s*:\s*", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise_text(value) -> str:
    """Return the comparison form of a message (see module docstring, Step 1)."""
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    if LOWERCASE:
        text = text.casefold()

    # Fallback form (URLs kept) in case the post is ONLY a link / only an RT tag.
    fallback = _WS_RE.sub(" ", text).strip()

    if STRIP_RT_PREFIX:
        while True:
            new = _RT_PREFIX_RE.sub("", text, count=1)
            if new == text:
                break
            text = new
    if REMOVE_URLS:
        text = _URL_RE.sub(" ", text)
    if REMOVE_MENTIONS:
        text = _MENTION_RE.sub(" ", text)
    if REMOVE_PUNCTUATION_EMOJI:
        text = _NON_WORD_RE.sub(" ", text)

    text = _WS_RE.sub(" ", text).strip()
    return text if text else fallback


# =========================================================================
# MATCHING ENGINE
# =========================================================================
def _is_minor_difference(a: str, b: str) -> bool:
    """
    Content-change guard. True if the two (already >=95 % similar) texts differ
    only by small edits; False if any single contiguous block of more than
    MAX_CHANGED_WORDS words was added / removed / replaced.
    """
    if MAX_CHANGED_WORDS is None:
        return True
    src = dst = 0
    for op in Indel.opcodes(a.split(), b.split()):
        if op.tag == "equal":
            if max(src, dst) > MAX_CHANGED_WORDS:
                return False
            src = dst = 0
        else:  # insert / delete / replace form one contiguous changed region
            src += op.src_end - op.src_start
            dst += op.dest_end - op.dest_start
    return max(src, dst) <= MAX_CHANGED_WORDS


class DuplicateGrouper:
    """
    Assigns rows (in order) to groups. The first row of every group is its
    representative; later rows join the group whose representative they match.
    """

    def __init__(self, threshold: float):
        self.threshold = threshold
        t = threshold / 100.0
        # Length window: fuzz.ratio >= t requires  min/max length >= t / (2 - t)
        self._lo_factor = t / (2.0 - t)
        self._hi_factor = (2.0 - t) / t
        self._exact: dict[str, tuple[int, float, str]] = {}      # norm -> (gid, score, type)
        self._buckets: dict[int, tuple[list[str], list[int]]] = {}  # len -> (rep texts, gids)
        self._rep_text: list[str] = []                           # index = gid - 1
        self._blank_gid: int | None = None
        self.guard_rejections = 0   # near-matches kept unique because content changed

    @property
    def n_groups(self) -> int:
        return len(self._rep_text)

    def _new_group(self, norm: str) -> int:
        self._rep_text.append(norm)
        gid = len(self._rep_text)
        self._exact[norm] = (gid, 100.0, "Exact")
        if len(norm) >= MIN_LEN_FOR_FUZZY:
            texts, gids = self._buckets.setdefault(len(norm), ([], []))
            texts.append(norm)
            gids.append(gid)
        return gid

    def assign(self, norm: str) -> tuple[int, float, str, bool]:
        """Return (group_id, similarity, match_type, is_new_group)."""
        # ---- blank messages ------------------------------------------------
        if not norm:
            if BLANK_MESSAGE_POLICY == "group":
                if self._blank_gid is None:
                    self._rep_text.append("")
                    self._blank_gid = len(self._rep_text)
                    return self._blank_gid, 100.0, "Blank", True
                return self._blank_gid, 100.0, "Blank", False
            self._rep_text.append("")
            return len(self._rep_text), 100.0, "Blank", True

        # ---- exact (normalised) match -------------------------------------
        hit = self._exact.get(norm)
        if hit is not None:
            return hit[0], hit[1], hit[2], False

        # ---- fuzzy match ---------------------------------------------------
        q = len(norm)
        if q >= MIN_LEN_FOR_FUZZY:
            lo = int(q * self._lo_factor)
            hi = int(math.ceil(q * self._hi_factor))
            cand_texts: list[str] = []
            cand_gids: list[int] = []
            for length in range(lo, hi + 1):
                bucket = self._buckets.get(length)
                if bucket:
                    cand_texts.extend(bucket[0])
                    cand_gids.extend(bucket[1])
            if cand_texts:
                found = process.extract(
                    norm, cand_texts, scorer=fuzz.ratio,
                    score_cutoff=self.threshold, limit=None,
                )
                # best score first; ties -> earliest group
                found.sort(key=lambda m: (-m[1], cand_gids[m[2]]))
                for rep_text, score, idx in found:
                    if _is_minor_difference(rep_text, norm):
                        gid = cand_gids[idx]
                        self._exact[norm] = (gid, round(score, 2), "Fuzzy")
                        return gid, round(score, 2), "Fuzzy", False
                    self.guard_rejections += 1

        # ---- nothing matched -> brand-new group ---------------------------
        return self._new_group(norm), 100.0, "Exact", True


def assign_groups(df: pd.DataFrame, message_col: str):
    """Run the grouper over all rows. Returns parallel lists + the grouper."""
    messages = df[message_col].tolist()
    total = len(messages)
    grouper = DuplicateGrouper(SIMILARITY_THRESHOLD)

    group_ids, scores, match_types, is_rep = [], [], [], []
    start = time.time()
    for i, raw in enumerate(messages, 1):
        gid, score, mtype, new = grouper.assign(normalise_text(raw))
        group_ids.append(gid)
        scores.append(score)
        match_types.append(mtype)
        is_rep.append(new)
        if PROGRESS_EVERY and (i % PROGRESS_EVERY == 0 or i == total):
            print(f"  processed {i:,}/{total:,} rows | groups so far: {grouper.n_groups:,} "
                  f"| {time.time() - start:,.1f}s")
    return group_ids, scores, match_types, is_rep, grouper


# =========================================================================
# BUILD UNIQUE / DUPLICATE TABLES
# =========================================================================
def _free_name(name: str, existing) -> str:
    """Avoid clobbering an existing column that already has this name."""
    existing = set(map(str, existing))
    if name not in existing:
        return name
    n = 2
    while f"{name}_{n}" in existing:
        n += 1
    return f"{name}_{n}"


def split_unique_duplicate(df_raw: pd.DataFrame, message_col: str):
    group_ids, scores, match_types, is_rep, grouper = assign_groups(df_raw, message_col)

    cols = list(df_raw.columns)
    c_gid = _free_name(COL_GROUP_ID, cols)
    c_rep = _free_name(COL_REPEATED, cols)
    c_src = _free_name(COL_SOURCE_ROW, cols)
    c_mt = _free_name(COL_MATCH_TYPE, cols)
    c_sim = _free_name(COL_SIMILARITY, cols)
    c_urow = _free_name(COL_UNIQUE_ROW, cols)

    work = df_raw.copy()
    work["_gid"] = group_ids
    work["_score"] = scores
    work["_mtype"] = match_types
    work["_is_rep"] = is_rep
    work["_src"] = range(2, len(work) + 2)          # Excel row number (header = row 1)

    # Source row of each group's representative
    rep_row_by_gid = dict(zip(work.loc[work["_is_rep"], "_gid"], work.loc[work["_is_rep"], "_src"]))
    dup_counts = Counter(work.loc[~work["_is_rep"], "_gid"])

    uniq = work.loc[work["_is_rep"]].copy()
    dups = work.loc[~work["_is_rep"]].copy()

    # ---------------- Unique tab ----------------
    uniq[c_rep] = uniq["_gid"].map(lambda g: dup_counts.get(g, 0)).astype(int)
    uniq[c_gid] = uniq["_gid"].astype(int)
    uniq[c_src] = uniq["_src"].astype(int)
    uniq = uniq.sort_values(c_gid, kind="stable")
    df_unique = uniq[[c_gid, c_rep, c_src] + cols].reset_index(drop=True)

    # ---------------- Duplicate tab ----------------
    dups[c_gid] = dups["_gid"].astype(int)
    dups[c_mt] = dups["_mtype"]
    dups[c_sim] = dups["_score"]
    dups[c_urow] = dups["_gid"].map(rep_row_by_gid).astype(int)
    dups[c_src] = dups["_src"].astype(int)
    dups = dups.sort_values([c_gid, c_src], kind="stable")
    df_duplicate = dups[[c_gid, c_mt, c_sim, c_urow, c_src] + cols].reset_index(drop=True)

    meta = {"gid_col": c_gid, "rep_col": c_rep, "match_col": c_mt,
            "grouper": grouper, "group_ids": group_ids}
    return df_unique, df_duplicate, meta


# =========================================================================
# VALIDATION
# =========================================================================
def run_sanity_checks(df_raw, df_unique, df_duplicate, meta) -> list[str]:
    """Returns a list of problems (empty list = everything is consistent)."""
    problems = []
    g, r = meta["gid_col"], meta["rep_col"]

    if len(df_unique) + len(df_duplicate) != len(df_raw):
        problems.append(f"Unique ({len(df_unique)}) + Duplicate ({len(df_duplicate)}) "
                        f"!= Raw ({len(df_raw)})")
    expected = list(range(1, len(df_unique) + 1))
    if df_unique[g].tolist() != expected:
        problems.append("Unique-tab Group_IDs are not exactly 1..N with no gaps/repeats")
    orphan = set(df_duplicate[g]) - set(df_unique[g])
    if orphan:
        problems.append(f"{len(orphan)} Duplicate-tab Group_IDs have no Unique row")
    if int(df_unique[r].sum()) != len(df_duplicate):
        problems.append("Sum of Repeated_Count != number of Duplicate rows")
    recount = df_duplicate.groupby(g).size()
    mism = df_unique.set_index(g)[r].loc[recount.index] != recount
    if mism.any():
        problems.append("Repeated_Count does not match Duplicate-tab row counts for some groups")
    return problems


# =========================================================================
# OUTPUT
# =========================================================================
def _sanitise_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Remove control characters Excel cannot store (they would crash openpyxl)."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(
                lambda v: ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v)
    return out


def _format_sheet(ws, df: pd.DataFrame) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    ws.freeze_panes = "A2"
    if len(df.columns):
        ws.auto_filter.ref = f"A1:{get_column_letter(len(df.columns))}{len(df) + 1}"
    for cell in ws[1]:
        cell.font = Font(bold=True, name="Arial")
    for i, col in enumerate(df.columns, 1):
        body = df[col].astype(str).str.split("\n").str[0].str.len()
        longest = max(int(body.max()) if len(body) else 0, len(str(col)))
        ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 60)


def write_output(df_raw, df_unique, df_duplicate, output_path: str) -> None:
    for name, d in ((RAW_SHEET, df_raw), (UNIQUE_SHEET, df_unique), (DUPLICATE_SHEET, df_duplicate)):
        if len(d) > EXCEL_MAX_ROWS:
            raise ValueError(f"'{name}' has {len(d):,} rows - more than Excel allows per sheet.")

    sheets = [(RAW_SHEET, _sanitise_for_excel(df_raw)),
              (UNIQUE_SHEET, _sanitise_for_excel(df_unique)),
              (DUPLICATE_SHEET, _sanitise_for_excel(df_duplicate))]
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, d in sheets:
            d.to_excel(writer, sheet_name=name, index=False)
            _format_sheet(writer.sheets[name], d)


# =========================================================================
# MAIN
# =========================================================================
def main(input_path: str = INPUT_FILE_PATH, output_path: str = OUTPUT_FILE_PATH) -> None:
    t0 = time.time()
    print(f"Reading: {input_path}")
    df_raw = load_data(input_path, SHEET_NAME)
    message_col = resolve_message_column(df_raw)
    total = len(df_raw)
    print(f"Total rows read: {total:,}   | grouping on column: '{message_col}'")
    print(f"Similarity threshold: {SIMILARITY_THRESHOLD}%   | max changed-block words: {MAX_CHANGED_WORDS}")

    df_unique, df_duplicate, meta = split_unique_duplicate(df_raw, message_col)

    problems = run_sanity_checks(df_raw, df_unique, df_duplicate, meta)
    n_u, n_d = len(df_unique), len(df_duplicate)
    print(f"\nUnique rows (one per group):     {n_u:,}")
    print(f"Duplicate rows (extra in group): {n_d:,}")
    print(f"Sum (Unique + Duplicate):        {n_u + n_d:,}")
    if problems:
        print("\nERROR - sanity checks failed, nothing was written:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    print("Check passed: Unique + Duplicate == Total rows; Group_IDs and Repeated_Count are consistent.")

    mt = df_duplicate[meta["match_col"]].value_counts().to_dict()
    print(f"Duplicates by match type: {mt}")
    print(f"Near-matches kept UNIQUE because content changed (guard): {meta['grouper'].guard_rejections:,}")
    if total:
        print(f"Reduction: {n_d / total:.1%} of rows were duplicates")
    top = df_unique.nlargest(5, meta["rep_col"])[[meta["gid_col"], meta["rep_col"]]]
    if len(top) and top[meta["rep_col"]].max() > 0:
        print("Most repeated groups (Group_ID: repeats): " +
              ", ".join(f"{int(a)}: {int(b)}" for a, b in top.values))

    df_raw_out = df_raw
    if ADD_GROUP_ID_TO_RAW_SHEET:
        df_raw_out = df_raw.copy()
        df_raw_out.insert(0, _free_name(COL_GROUP_ID, df_raw.columns), meta["group_ids"])

    write_output(df_raw_out, df_unique, df_duplicate, output_path)
    print(f"\nDone in {time.time() - t0:,.1f}s. Output written to: {output_path}")
    print(f"Sheets: '{RAW_SHEET}', '{UNIQUE_SHEET}', '{DUPLICATE_SHEET}'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Split posts into Unique / Duplicate with Group IDs.")
    ap.add_argument("input", nargs="?", default=INPUT_FILE_PATH, help="input .csv/.xlsx (default: INPUT_FILE_PATH)")
    ap.add_argument("output", nargs="?", default=OUTPUT_FILE_PATH, help="output .xlsx (default: OUTPUT_FILE_PATH)")
    args = ap.parse_args()
    main(args.input, args.output)