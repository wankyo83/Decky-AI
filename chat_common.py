import os
import re
import sys
import importlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Dict, List, Optional


def _prefer_local_py_modules() -> Path:
    """Prepend ./py_modules so local vendored deps win over bundled/system packages."""
    project_root = Path(__file__).resolve().parent
    local_modules = project_root / "py_modules"
    if not local_modules.is_dir():
        raise RuntimeError(f"Expected local dependency folder at {local_modules}")

    local_modules_str = str(local_modules)
    sys.path = [local_modules_str] + [
        entry for entry in sys.path if entry != local_modules_str
    ]
    return local_modules


def _force_local_import(module_name: str, local_modules: Path):
    """Reload module from py_modules if something else preloaded it first."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        module_file = getattr(existing, "__file__", None)
        if module_file:
            resolved = Path(module_file).resolve()
            if local_modules not in resolved.parents:
                del sys.modules[module_name]

    importlib.invalidate_caches()
    imported = importlib.import_module(module_name)
    module_file = getattr(imported, "__file__", None)
    if not module_file:
        raise RuntimeError(f"Could not validate import path for module {module_name}")

    resolved = Path(module_file).resolve()
    if local_modules not in resolved.parents:
        raise RuntimeError(
            f"{module_name} was not imported from local py_modules. Resolved path: {resolved}"
        )
    return imported


LOCAL_PY_MODULES = _prefer_local_py_modules()

# Ensure pydantic_core gets Sentinel from vendored typing_extensions, not setuptools vendor.
_force_local_import("typing_extensions", LOCAL_PY_MODULES)

import dotenv

plugin_root = Path(__file__).resolve().parent
dotenv.load_dotenv(dotenv_path=plugin_root / ".env")
dotenv.load_dotenv(dotenv_path=plugin_root / ".env_config")


from google import genai
from google.genai import types

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)
MODEL = os.getenv("GOOGLE_MODEL", "gemma-4-26b-a4b-it")
MODEL_TIMEOUT_SECONDS = int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.1"))
NUM_HISTORY_MESSAGES = int(os.getenv("NUM_HISTORY_MESSAGES", "10"))


# Guardrail: fail fast if google.genai resolves outside vendored py_modules.
GENAI_MODULE_PATH = Path(genai.__file__ or "").resolve()
if LOCAL_PY_MODULES not in GENAI_MODULE_PATH.parents:
    raise RuntimeError(
        "google.genai was not imported from local py_modules. "
        f"Resolved path: {GENAI_MODULE_PATH}"
    )


class ModelConfigError(RuntimeError):
    """Raised when model configuration is invalid (for example, missing API key)."""


def build_prompt(history_text: str) -> str:
    """Model prompt with instructions and chat history."""
    return (
        "You are a helpful gaming companion. Provide concise, tactical help. "
        "Always use google search tool to look up information.\n"
        "DO NOT use internal reasoning or thinking process. Give the answer immediately once the research is complete.\n\n"
        f"Chat history:\n{history_text}\n\nAssistant:"
    )


def _run_with_timeout(callable_fn, timeout_seconds: int):
    """Run a blocking function with a timeout, using ThreadPoolExecutor to avoid blocking the event loop."""
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(callable_fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as error:
        future.cancel()
        raise TimeoutError(
            f"Model request timed out after {timeout_seconds} seconds"
        ) from error
    finally:
        # Do not wait for stuck network calls when timing out.
        executor.shutdown(wait=False, cancel_futures=True)


def serialize_history(
    history: List[Dict[str, str]],
    message: str,
    num_messages: int = NUM_HISTORY_MESSAGES,
) -> str:
    """Serialize recent chat history and new message into a prompt string."""
    lines = []
    for item in history[-num_messages:]:
        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        if role == "user":
            label = "User"
        elif role == "assistant":
            label = "Assistant"
        else:
            label = "Other"
        lines.append(f"{label}: {content}")
    lines.append(f"User: {message}")
    return "\n".join(lines)


def _extract_api_status_code(error: Exception) -> Optional[int]:
    """Best-effort extraction of an HTTP/API status code from SDK exceptions."""
    for attr in ("status_code", "http_status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(error, "response", None)
    if response is not None:
        response_code = getattr(response, "status_code", None)
        if isinstance(response_code, int):
            return response_code

    details = str(error)
    match = re.search(r"\b([1-5]\d{2})\b", details)
    if match:
        return int(match.group(1))

    return None


class ModelApiError(Exception):
    """Raised when the model API returns a non-200 status code."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Model API error code: {status_code}")


def call_model_with_timeout(
    prompt: str,
    timeout_seconds: int = MODEL_TIMEOUT_SECONDS,
    temperature: float = MODEL_TEMPERATURE,
    model: str = MODEL,
    GEMINI_API_KEY: str | None = GEMINI_API_KEY,
) -> Optional[str]:
    """Call the Gemini model with a timeout and raise on non-200 API status codes."""
    if not GEMINI_API_KEY:
        raise ModelConfigError("GEMINI_API_KEY is not set in environment variables")

    client = genai.Client(api_key=GEMINI_API_KEY)

    def call_model():

        search_tool = types.Tool(google_search=types.GoogleSearch())

        config = types.GenerateContentConfig(
            tools=[search_tool],
            system_instruction="""
            Provide direct game tips only.
            If the question is not about computer games, say you can only answer questions about computer games.
            DO NOT use <|think|> tags or provide internal reasoning.
            Always use the google search tool to look up information then summarize it in your response.
            Include a 'Sources' section at the end with URLs for any references used.
            If a URL is unavailable, acknowledge the original author or publication.
            """,
            temperature=temperature,
        )

        response = client.models.generate_content(
            model=model, contents=prompt, config=config
        )

        final_text = response.text or ""

        # Extract web sources from grounding metadata (google search tool output).
        sources: List[str] = []
        if response.candidates:
            candidate = response.candidates[0]
            metadata = getattr(candidate, "grounding_metadata", None)
            grounding_chunks = (
                getattr(metadata, "grounding_chunks", None) if metadata else None
            )

            if grounding_chunks:
                for chunk in grounding_chunks:
                    web = getattr(chunk, "web", None)
                    if not web:
                        continue

                    title = getattr(web, "title", None) or "Source"
                    url = getattr(web, "uri", None)
                    if url:
                        sources.append(f"- [{title}]({url})")

        if sources:
            unique_sources = list(dict.fromkeys(sources))
            final_text += "\n\n**Sources:**\n" + "\n".join(unique_sources)

        return final_text

    try:
        return _run_with_timeout(call_model, timeout_seconds)
    except TimeoutError:
        raise
    except Exception as error:
        status_code = _extract_api_status_code(error)
        if status_code is not None and status_code != 200:
            raise ModelApiError(status_code, str(error)) from error
        raise
