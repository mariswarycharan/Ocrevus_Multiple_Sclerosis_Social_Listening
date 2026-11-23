"""
Modular, dynamic multi-platform LLM client and router.

Supported platforms and underlying Python libraries:
    - "groq"       : Official `groq` Python SDK (`Groq`)
    - "openai"     : Official `openai` Python SDK (`OpenAI`, supports OpenAI & NVIDIA NIM)
    - "gemini"     : Official `openai` Python SDK (`OpenAI` via Gemini OpenAI-compatible endpoint)
    - "openrouter" : Official `openai` Python SDK (`OpenAI` via OpenRouter endpoint)
    - "opencode"   : Python standard library (`urllib.request`) for OpenCode Zen REST API

All API keys and optional endpoint overrides are loaded from `.env` or environment
variables. Every request is stateless by design (no conversation history retained).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Environment / .env Loading
# ---------------------------------------------------------------------------

DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env_file(env_path: str | Path | None = None, override: bool = False) -> bool:
    """
    Load environment variables from a `.env` file.

    Uses `python-dotenv` when installed and falls back to a built-in parser
    when `python-dotenv` is unavailable, so API keys always load cleanly.

    Args:
        env_path: Optional path to the `.env` file. Defaults to `.env` beside this module.
        override: If True, overwrite existing environment variables.

    Returns:
        True if the `.env` file existed and was processed, False otherwise.
    """
    target_path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if not target_path.is_file():
        return False

    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=target_path, override=override)
        return True
    except ImportError:
        pass

    # Pure-Python fallback parser when python-dotenv is not installed
    with target_path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and (override or key not in os.environ):
                os.environ[key] = value
    return True


# Load `.env` once when the module is imported
load_env_file()


# ---------------------------------------------------------------------------
# Internal Helpers: Keys, SDK Imports, Error Formatting, and Retry Engine
# ---------------------------------------------------------------------------

def _get_api_keys(env_prefix: str) -> list[str]:
    """
    Collect all configured API keys for a given platform prefix (e.g. 'GROQ').

    Checks `<PREFIX>_API_KEY_1`, `<PREFIX>_API_KEY_2`, ..., `<PREFIX>_API_KEY_10`
    and `<PREFIX>_API_KEY`, returning non-empty unique keys in priority order.
    """
    candidate_names = [f"{env_prefix}_API_KEY_{idx}" for idx in range(1, 11)]
    candidate_names.append(f"{env_prefix}_API_KEY")

    keys: list[str] = []
    seen: set[str] = set()
    for name in candidate_names:
        val = os.getenv(name, "").strip()
        if val and val not in seen:
            keys.append(val)
            seen.add(val)
    return keys


def _import_client_class(module_name: str, class_name: str) -> tuple[Any | None, str | None]:
    """Safely import an LLM SDK client class with a clear installation message on failure."""
    try:
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name), None
    except ImportError:
        return None, (
            f"Missing required Python library '{module_name}'. "
            f"Install it with: pip install {module_name}"
        )
    except Exception as exc:
        return None, f"Failed to import '{class_name}' from '{module_name}': {exc}"


def _build_fresh_messages(
    prompt: str,
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    """
    Build a fresh, stateless messages list containing only the current request.
    No conversation history is ever persisted across calls.
    """
    messages: list[dict[str, str]] = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": prompt})
    return messages


def _format_exception(exc: Exception) -> str:
    """Format exceptions consistently with useful status/category context when available."""
    exc_name = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return f"{exc_name} (HTTP {status_code}): {exc}"
    return f"{exc_name}: {exc}"


def _validate_call_inputs(
    prompt: str,
    model: str,
    max_retries: int,
    retry_delay: float,
    temperature: float,
    top_p: float,
) -> str | None:
    """Validate common LLM call parameters and return an error message if invalid."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "Invalid input: 'prompt' must be a non-empty string."
    if not isinstance(model, str) or not model.strip():
        return "Invalid input: 'model' must be a non-empty string."
    if not isinstance(max_retries, int) or max_retries < 1:
        return "Invalid input: 'max_retries' must be an integer >= 1."
    if retry_delay < 0:
        return "Invalid input: 'retry_delay' must be >= 0."
    if not (0.0 <= temperature <= 2.0):
        return "Invalid input: 'temperature' must be between 0.0 and 2.0."
    if not (0.0 < top_p <= 1.0):
        return "Invalid input: 'top_p' must be in (0.0, 1.0]."
    return None


def _execute_with_retry(
    platform_label: str,
    model: str,
    api_keys: list[str],
    env_prefix: str,
    request_fn: Callable[[str, int], str],
    max_retries: int,
    retry_delay: float,
    api_key_index: int = 0,
) -> tuple[str | None, str | None]:
    """
    Execute an LLM request with automatic API-key rotation and retry handling.

    Starts with key at `api_key_index` (e.g. 0 for Key 1, 1 for Key 2) and rotates
    across remaining keys on subsequent retry attempts.

    Returns:
        (raw_response_text, error_message)
        - On success: (str, None)
        - On total failure: (None, "<last error message>")
    """
    if not api_keys:
        error_msg = (
            f"No API key configured for {platform_label}. "
            f"Please set {env_prefix}_API_KEY_1 (or {env_prefix}_API_KEY) in your .env file."
        )
        print(f"❌ {error_msg}")
        return None, error_msg

    last_error: str | None = None

    for attempt in range(1, max_retries + 1):
        # Rotate starting from api_key_index:
        # When 3+ keys exist and alternating between Key 1 & Key 2 (indices 0 and 1),
        # keep standard retries strictly between Key 1 & Key 2 so Key 3 remains a dedicated fallback.
        if len(api_keys) > 2 and api_key_index in (0, 1):
            key_idx = (api_key_index + attempt - 1) % 2
        elif len(api_keys) > 2 and api_key_index >= 2:
            key_idx = min(api_key_index, len(api_keys) - 1)
        else:
            key_idx = (api_key_index + attempt - 1) % len(api_keys)

        active_key = api_keys[key_idx]
        key_label = f"Key {key_idx + 1}"
        try:
            print(
                f"\n📝 [Attempt {attempt}/{max_retries}] "
                f"Sending prompt to {platform_label} (model: {model}, {key_label}, stateless request)",
                flush=True,
            )
            response_content = request_fn(active_key, attempt)

            if response_content is None or not str(response_content).strip():
                raise ValueError(f"Empty response content received from {platform_label}.")

            preview = str(response_content).strip().replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "..."
            print(f"✅ {platform_label} response (attempt {attempt}, {key_label}): {preview}", flush=True)
            return response_content, None

        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = str(exc)
            last_error = f"HTTP {exc.code}: {error_body}"
            print(f"❌ Attempt {attempt}/{max_retries} failed for {platform_label} ({key_label}): {last_error}", flush=True)

        except urllib.error.URLError as exc:
            last_error = f"Network/URL error: {exc.reason}"
            print(f"❌ Attempt {attempt}/{max_retries} failed for {platform_label} ({key_label}): {last_error}", flush=True)

        except TimeoutError:
            last_error = f"Request timed out while waiting for {platform_label}."
            print(f"❌ Attempt {attempt}/{max_retries} failed for {platform_label} ({key_label}): {last_error}", flush=True)

        except Exception as exc:
            last_error = _format_exception(exc)
            print(f"❌ Attempt {attempt}/{max_retries} failed for {platform_label} ({key_label}): {last_error}", flush=True)

        if attempt < max_retries and retry_delay > 0:
            if len(api_keys) > 2 and api_key_index in (0, 1):
                next_key_idx = (api_key_index + attempt) % 2
            elif len(api_keys) > 2 and api_key_index >= 2:
                next_key_idx = min(api_key_index, len(api_keys) - 1)
            else:
                next_key_idx = (api_key_index + attempt) % len(api_keys)
            print(f"🔄 Retrying (fallback attempt {attempt + 1}) using Key {next_key_idx + 1} in {retry_delay}s...", flush=True)
            time.sleep(retry_delay)

    return None, last_error


def _call_openai_compatible_sdk(
    *,
    platform_label: str,
    sdk_module: str,
    sdk_class_name: str,
    env_prefix: str,
    prompt: str,
    model: str,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 2.0,
    temperature: float = 0.0,
    top_p: float = 0.95,
    use_json_mode: bool = False,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    Shared caller for SDKs following the OpenAI Chat Completions interface
    (`groq.Groq` and `openai.OpenAI` for OpenAI, NVIDIA NIM, Gemini, OpenRouter).
    """
    validation_error = _validate_call_inputs(
        prompt=prompt,
        model=model,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
    )
    if validation_error:
        print(f"❌ {platform_label}: {validation_error}")
        return None, validation_error

    client_cls, import_error = _import_client_class(sdk_module, sdk_class_name)
    if import_error:
        print(f"❌ {platform_label}: {import_error}")
        return None, import_error

    api_keys = _get_api_keys(env_prefix)
    if api_key and api_key.strip():
        custom_key = api_key.strip()
        api_keys = [custom_key] + [k for k in api_keys if k != custom_key]
        api_key_index = 0

    fresh_messages = _build_fresh_messages(prompt, system_prompt=system_prompt)
    if not system_prompt:
        assert len(fresh_messages) == 1, "Stateless call must contain only the current prompt."

    request_kwargs: dict[str, Any] = {
        "model": model,
        "messages": fresh_messages,
        "temperature": temperature,
        "top_p": top_p,
    }
    if use_json_mode:
        request_kwargs["response_format"] = {"type": "json_object"}

    def _do_request(api_key: str, _attempt: int) -> str:
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if timeout is not None:
            client_kwargs["timeout"] = timeout

        client = client_cls(**client_kwargs)
        completion = client.chat.completions.create(**request_kwargs)

        if not getattr(completion, "choices", None):
            raise ValueError(f"No choices returned by {platform_label}: {completion}")

        content = completion.choices[0].message.content
        if content is None:
            raise ValueError(f"Null message content in response: {completion}")
        return content

    return _execute_with_retry(
        platform_label=platform_label,
        model=model,
        api_keys=api_keys,
        env_prefix=env_prefix,
        request_fn=_do_request,
        max_retries=max_retries,
        retry_delay=retry_delay,
        api_key_index=api_key_index,
    )


# ---------------------------------------------------------------------------
# Dedicated Platform Functions
# ---------------------------------------------------------------------------

def call_groq_llm(
    prompt: str,
    model: str = "llama-3.3-70b-versatile",
    *,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 2.0,
    temperature: float = 0.0,
    top_p: float = 0.95,
    use_json_mode: bool = True,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    Call a Groq-hosted LLM using the `groq` Python SDK.

    Example models: "llama-3.3-70b-versatile", "openai/gpt-oss-120b".
    Reads GROQ_API_KEY_1 / GROQ_API_KEY_2 (or GROQ_API_KEY) from environment/.env.
    """
    return _call_openai_compatible_sdk(
        platform_label="Groq",
        sdk_module="groq",
        sdk_class_name="Groq",
        env_prefix="GROQ",
        prompt=prompt,
        model=model,
        system_prompt=system_prompt,
        api_key_index=api_key_index,
        api_key=api_key,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
        use_json_mode=use_json_mode,
        timeout=timeout,
    )


def call_openai_llm(
    prompt: str,
    model: str = "z-ai/glm-5.2",
    *,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 5.0,
    temperature: float = 0.0,
    top_p: float = 0.95,
    use_json_mode: bool = False,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    Call OpenAI or an OpenAI-compatible provider (e.g., NVIDIA NIM) via the `openai` SDK.

    Example models: "z-ai/glm-5.2", "nvidia/nemotron-3-super-120b-a12b", "gpt-4o-mini".
    Reads OPENAI_API_KEY_1 / OPENAI_API_KEY_2 and optional OPENAI_BASE_URL from .env.
    """
    resolved_base_url = base_url
    if resolved_base_url is None:
        env_base_url = os.getenv("OPENAI_BASE_URL")
        if env_base_url is not None:
            resolved_base_url = env_base_url.strip() or None
        else:
            keys = _get_api_keys("OPENAI")
            if keys and keys[0].startswith("nvapi-"):
                resolved_base_url = "https://integrate.api.nvidia.com/v1"

    return _call_openai_compatible_sdk(
        platform_label="OpenAI",
        sdk_module="openai",
        sdk_class_name="OpenAI",
        env_prefix="OPENAI",
        prompt=prompt,
        model=model,
        base_url=resolved_base_url,
        system_prompt=system_prompt,
        api_key_index=api_key_index,
        api_key=api_key,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
        use_json_mode=use_json_mode,
        timeout=timeout,
    )


def call_gemini_llm(
    prompt: str,
    model: str = "gemini-3-flash-preview",
    *,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 5.0,
    temperature: float = 0.0,
    top_p: float = 0.95,
    use_json_mode: bool = False,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    Call Google Gemini via Google's OpenAI-compatible endpoint using the `openai` SDK.

    Stateless by design: every call sends only the current prompt without history.
    Reads GEMINI_API_KEY_1 / GEMINI_API_KEY_2 / GEMINI_API_KEY_3 and GEMINI_BASE_URL from environment/.env.
    Supports alternating starting key via api_key_index (0 for Key 1, 1 for Key 2, 2 for Fallback Key 3).
    """
    resolved_base_url = (
        base_url
        or os.getenv("GEMINI_BASE_URL", "").strip()
        or "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    return _call_openai_compatible_sdk(
        platform_label="Gemini",
        sdk_module="openai",
        sdk_class_name="OpenAI",
        env_prefix="GEMINI",
        prompt=prompt,
        model=model,
        base_url=resolved_base_url,
        system_prompt=system_prompt,
        api_key_index=api_key_index,
        api_key=api_key,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
        use_json_mode=use_json_mode,
        timeout=timeout,
    )


def call_openrouter_llm(
    prompt: str,
    model: str = "google/gemma-4-31b-it",
    *,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 2.0,
    temperature: float = 0.0,
    top_p: float = 0.9,
    use_json_mode: bool = True,
    timeout: float | None = None,
) -> tuple[str | None, str | None]:
    """
    Call an OpenRouter-hosted model via the OpenAI-compatible endpoint using `openai` SDK.

    Reads OPENROUTER_API_KEY_1 / OPENROUTER_API_KEY_2 and OPENROUTER_BASE_URL from .env.
    """
    resolved_base_url = (
        base_url
        or os.getenv("OPENROUTER_BASE_URL", "").strip()
        or "https://openrouter.ai/api/v1"
    )
    return _call_openai_compatible_sdk(
        platform_label="OpenRouter",
        sdk_module="openai",
        sdk_class_name="OpenAI",
        env_prefix="OPENROUTER",
        prompt=prompt,
        model=model,
        base_url=resolved_base_url,
        system_prompt=system_prompt,
        api_key_index=api_key_index,
        api_key=api_key,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
        use_json_mode=use_json_mode,
        timeout=timeout,
    )


def call_opencode_llm(
    prompt: str,
    model: str = "deepseek-v4-flash-free",
    *,
    api_url: str | None = None,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float = 2.0,
    temperature: float = 0.0,
    top_p: float = 0.95,
    disable_thinking: bool = True,
    timeout: float = 180.0,
) -> tuple[str | None, str | None]:
    """
    Call the OpenCode (Zen) Chat Completions API using Python's `urllib.request`.

    Reads OPENCODE_API_KEY_1 / OPENCODE_API_KEY_2 and OPENCODE_API_URL from .env.
    """
    validation_error = _validate_call_inputs(
        prompt=prompt,
        model=model,
        max_retries=max_retries,
        retry_delay=retry_delay,
        temperature=temperature,
        top_p=top_p,
    )
    if validation_error:
        print(f"❌ OpenCode: {validation_error}")
        return None, validation_error

    resolved_url = (
        api_url
        or os.getenv("OPENCODE_API_URL", "").strip()
        or "https://opencode.ai/zen/v1/chat/completions"
    )
    api_keys = _get_api_keys("OPENCODE")
    if api_key and api_key.strip():
        custom_key = api_key.strip()
        api_keys = [custom_key] + [k for k in api_keys if k != custom_key]
        api_key_index = 0

    payload: dict[str, Any] = {
        "model": model,
        "messages": _build_fresh_messages(prompt, system_prompt=system_prompt),
        "temperature": temperature,
        "top_p": top_p,
    }
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}

    encoded_payload = json.dumps(payload).encode("utf-8")

    def _do_request(active_api_key: str, _attempt: int) -> str:
        request = urllib.request.Request(
            resolved_url,
            data=encoded_payload,
            headers={
                "Authorization": f"Bearer {active_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "OpenCode-Zen-Python/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
        response_json = json.loads(raw_body)
        choices = response_json.get("choices")
        if not choices or not isinstance(choices, list):
            raise ValueError(f"Unexpected OpenCode response structure: {response_json}")
        content = choices[0].get("message", {}).get("content")
        if content is None:
            raise ValueError(f"Missing message content in OpenCode response: {response_json}")
        return content

    return _execute_with_retry(
        platform_label="OpenCode",
        model=model,
        api_keys=api_keys,
        env_prefix="OPENCODE",
        request_fn=_do_request,
        max_retries=max_retries,
        retry_delay=retry_delay,
        api_key_index=api_key_index,
    )


# ---------------------------------------------------------------------------
# Unified Platform Registry & Dynamic Router
# ---------------------------------------------------------------------------

PLATFORM_CONFIGS: dict[str, dict[str, Any]] = {
    "groq": {
        "handler": call_groq_llm,
        "library": "groq (Groq)",
        "default_model": "llama-3.3-70b-versatile",
        "default_retry_delay": 2.0,
        "default_top_p": 0.95,
        "default_json_mode": True,
    },
    "openai": {
        "handler": call_openai_llm,
        "library": "openai (OpenAI / NVIDIA NIM)",
        "default_model": "z-ai/glm-5.2",
        "default_retry_delay": 5.0,
        "default_top_p": 0.95,
        "default_json_mode": False,
    },
    "gemini": {
        "handler": call_gemini_llm,
        "library": "openai (Gemini OpenAI-compatible)",
        "default_model": "gemini-3-flash-preview",
        "default_retry_delay": 5.0,
        "default_top_p": 0.95,
        "default_json_mode": False,
    },
    "openrouter": {
        "handler": call_openrouter_llm,
        "library": "openai (OpenRouter)",
        "default_model": "google/gemma-4-31b-it",
        "default_retry_delay": 2.0,
        "default_top_p": 0.9,
        "default_json_mode": True,
    },
    "opencode": {
        "handler": call_opencode_llm,
        "library": "urllib.request (OpenCode Zen REST API)",
        "default_model": "deepseek-v4-flash-free",
        "default_retry_delay": 2.0,
        "default_top_p": 0.95,
        "default_json_mode": None,
    },
}


def list_supported_platforms() -> dict[str, dict[str, Any]]:
    """Return metadata for all supported LLM platforms (library and defaults)."""
    return {
        name: {
            "library": cfg["library"],
            "default_model": cfg["default_model"],
            "default_retry_delay": cfg["default_retry_delay"],
            "default_top_p": cfg["default_top_p"],
            "default_json_mode": cfg["default_json_mode"],
        }
        for name, cfg in PLATFORM_CONFIGS.items()
    }


def call_llm(
    prompt: str,
    platform: str = "gemini",
    model: str | None = None,
    *,
    system_prompt: str | None = None,
    api_key_index: int = 0,
    api_key: str | None = None,
    max_retries: int = 2,
    retry_delay: float | None = None,
    temperature: float = 0.0,
    top_p: float | None = None,
    use_json_mode: bool | None = None,
    timeout: float | None = None,
    **extra_kwargs: Any,
) -> tuple[str | None, str | None]:
    """
    Unified dynamic LLM router function.

    Routes the prompt to the selected platform and model using `PLATFORM_CONFIGS`.
    Any optional setting passed as None falls back to that platform's recommended default.

    Args:
        prompt: The complete user prompt sent to the model.
        platform: Target platform ("groq", "openai", "gemini", "openrouter", "opencode").
        model: Model identifier for the chosen platform (None uses platform default).
        system_prompt: Optional system instruction message.
        max_retries: Total attempts including primary attempt + fallback retries.
        retry_delay: Delay in seconds between failed attempts.
        temperature: Sampling temperature (0.0 for deterministic classification).
        top_p: Nucleus sampling parameter.
        use_json_mode: Whether to request JSON object output (for supported platforms).
        timeout: Optional request timeout in seconds.
        **extra_kwargs: Platform-specific keyword arguments (e.g., `base_url`, `disable_thinking`).

    Returns:
        (raw_response_text, error_message)
    """
    if not isinstance(platform, str) or not platform.strip():
        err = "Invalid input: 'platform' must be a non-empty string."
        print(f"❌ {err}")
        return None, err

    normalized_platform = platform.strip().lower()
    if normalized_platform not in PLATFORM_CONFIGS:
        supported = ", ".join(PLATFORM_CONFIGS.keys())
        err = f"Unsupported LLM platform '{platform}'. Supported platforms: {supported}"
        print(f"❌ {err}")
        return None, err

    config = PLATFORM_CONFIGS[normalized_platform]
    handler = config["handler"]

    resolved_model = model.strip() if isinstance(model, str) and model.strip() else config["default_model"]
    resolved_delay = config["default_retry_delay"] if retry_delay is None else retry_delay
    resolved_top_p = config["default_top_p"] if top_p is None else top_p

    call_kwargs: dict[str, Any] = {
        "prompt": prompt,
        "model": resolved_model,
        "system_prompt": system_prompt,
        "api_key_index": api_key_index,
        "api_key": api_key,
        "max_retries": max_retries,
        "retry_delay": resolved_delay,
        "temperature": temperature,
        "top_p": resolved_top_p,
    }

    if config["default_json_mode"] is not None:
        call_kwargs["use_json_mode"] = (
            config["default_json_mode"] if use_json_mode is None else use_json_mode
        )

    if timeout is not None:
        call_kwargs["timeout"] = timeout

    call_kwargs.update(extra_kwargs)

    try:
        return handler(**call_kwargs)
    except TypeError as exc:
        err = f"Invalid argument passed to '{normalized_platform}' handler: {exc}"
        print(f"❌ {err}")
        return None, err
    except Exception as exc:
        err = f"Unexpected error in LLM router for '{normalized_platform}': {_format_exception(exc)}"
        print(f"❌ {err}")
        return None, err

