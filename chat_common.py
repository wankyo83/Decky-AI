import os
import re
import sys
import importlib
import asyncio
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
MODEL = os.getenv("GOOGLE_MODEL", "gemini-3.5-flash")
SEARCH_MODEL = os.getenv("GOOGLE_SEARCH_MODEL", "gemini-2.5-flash")
MODEL_TIMEOUT_SECONDS = int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.1"))
NUM_HISTORY_MESSAGES = int(os.getenv("NUM_HISTORY_MESSAGES", "10"))
ENABLE_GOOGLE_SEARCH = os.getenv("ENABLE_GOOGLE_SEARCH", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


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


async def call_model(
    prompt: str,
    timeout_seconds: int = MODEL_TIMEOUT_SECONDS,
    temperature: float = MODEL_TEMPERATURE,
    model: str = MODEL,
    enable_google_search: bool = ENABLE_GOOGLE_SEARCH,
    api_key: str | None = None,
    media_bytes: bytes | None = None,
    media_mime_type: str | None = None,
    system_instruction: str | None = None,
) -> Optional[str]:
    """Call Gemini asynchronously so a hidden or closed panel can cancel the request."""
    actual_key = api_key or GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not actual_key:
        raise ModelConfigError("GEMINI_API_KEY is not set in environment variables")

    tools = (
        [types.Tool(google_search=types.GoogleSearch())]
        if enable_google_search
        else None
    )
    config_kwargs: Dict[str, object] = {
        "tools": tools,
        "system_instruction": system_instruction or """
        You are Decky AI, a concise and helpful general-purpose assistant that
        also specializes in video games.
        Answer in the same language as the user.
        Use the current game name only when the user's question is related to gaming.
        For ordinary conversation, answer naturally without forcing a game connection.
        If the user asks for weather without naming a location, ask which city or region.
        If a search tool is available, use it for current game facts and strategies.
        Also use the search tool for current weather, news, prices, exchange rates,
        schedules, and other time-sensitive facts. If no search tool is available,
        clearly say when information may be outdated.
        Do not expose internal reasoning.
        Include sources when they are available.
        """,
    }
    # Gemini 3.5 uses its own thinking controls; Google recommends omitting the
    # older sampling knobs during migration.
    if model != "gemini-3.5-flash":
        config_kwargs["temperature"] = temperature
    config = types.GenerateContentConfig(**config_kwargs)

    try:
        contents: object = prompt
        if media_bytes is not None:
            if not media_mime_type:
                raise ModelConfigError("A MIME type is required for media input")
            contents = [
                prompt,
                types.Part.from_bytes(data=media_bytes, mime_type=media_mime_type),
            ]

        async with genai.Client(api_key=actual_key).aio as client:
            response = await asyncio.wait_for(
                client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout_seconds,
            )
    except asyncio.CancelledError:
        # Closing the Decky panel cancels the underlying async HTTP request.
        raise
    except asyncio.TimeoutError as error:
        raise TimeoutError(
            f"Model request timed out after {timeout_seconds} seconds"
        ) from error
    except Exception as error:
        status_code = _extract_api_status_code(error)
        if status_code is not None and status_code != 200:
            raise ModelApiError(status_code, str(error)) from error
        raise

    final_text = response.text or ""

    # Extract web sources from grounding metadata (Google Search tool output).
    sources: List[str] = []
    search_was_used = False
    if response.candidates:
        candidate = response.candidates[0]
        metadata = getattr(candidate, "grounding_metadata", None)
        if metadata:
            search_was_used = bool(
                getattr(metadata, "web_search_queries", None)
                or getattr(metadata, "search_entry_point", None)
                or getattr(metadata, "grounding_chunks", None)
            )
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

    if enable_google_search and not search_was_used:
        raise ModelApiError(
            424,
            "Gemini returned an answer without using Google Search grounding",
        )

    if sources:
        unique_sources = list(dict.fromkeys(sources))
        final_text += (
            "\n\n**실제 Google 검색 확인됨**\n\n**출처:**\n"
            + "\n".join(unique_sources)
        )

    return final_text
