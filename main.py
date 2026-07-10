import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Dict, List

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code repo
# and add the `decky-loader/plugin/imports` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky
import asyncio

# Decky may load this module without automatically adding the plugin folder to sys.path.
# Prepend the plugin directory so sibling files like chat_common.py can be imported.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


LOGGING_LEVEL = os.getenv("CHAT_LOGGING_LEVEL", "INFO").upper()
decky.logger.setLevel(getattr(logging, LOGGING_LEVEL, logging.INFO))


from chat_common import ModelApiError, call_model_with_timeout, ModelConfigError


class Plugin:
    HISTORY_FILE = "chat_history.json"

    def _history_path(self) -> Path:
        return Path(decky.DECKY_PLUGIN_RUNTIME_DIR) / self.HISTORY_FILE

    def _read_history(self) -> List[Dict[str, str]]:
        history_path = self._history_path()
        if not history_path.exists():
            return []

        try:
            with history_path.open("r", encoding="utf-8") as history_file:
                loaded = json.load(history_file)
        except (OSError, json.JSONDecodeError):
            decky.logger.warning(
                "[chat] Failed to read chat history, resetting to empty list"
            )
            return []

        if not isinstance(loaded, list):
            return []

        normalized: List[Dict[str, str]] = []
        for item in loaded:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if isinstance(role, str) and isinstance(content, str):
                normalized.append({"role": role, "content": content})
        return normalized

    def _write_history(self, history: List[Dict[str, str]]) -> None:
        history_path = self._history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("w", encoding="utf-8") as history_file:
            json.dump(history, history_file, ensure_ascii=False)

    def _append_history(self, role: str, content: str) -> None:
        history = self._read_history()
        history.append({"role": role, "content": content})
        # Keep history bounded so prompts and file size remain manageable.
        history = history[-200:]
        self._write_history(history)

    async def get_chat_history(self) -> List[Dict[str, str]]:
        return self._read_history()

    async def clear_chat_history(self) -> None:
        self._write_history([])

    async def has_api_key(self) -> bool:
        import chat_common
        return bool(chat_common.GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY"))

    async def set_api_key(self, api_key: str) -> None:
        import chat_common
        
        dotenv_path = Path(PLUGIN_DIR) / ".env"
        lines = []
        if dotenv_path.exists():
            with open(dotenv_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("GEMINI_API_KEY="):
                new_lines.append(f"GEMINI_API_KEY={api_key}\n")
                found = True
            else:
                new_lines.append(line)
        
        if not found:
            # Ensure the file ends with a newline before appending
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"GEMINI_API_KEY={api_key}\n")
            
        with open(dotenv_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            
        os.environ["GEMINI_API_KEY"] = api_key
        chat_common.GEMINI_API_KEY = api_key

    async def send_message(self, text: str):
        # Frontend input arrives here via callable("send_message") in index.tsx.
        cleaned = text.strip()
        if not cleaned:
            # Ignore empty input so no blank rows/log spam are generated.
            return

        self._append_history("user", cleaned)

        history_for_prompt = self._read_history()
        history_lines = []
        for item in history_for_prompt:
            role = item.get("role", "other")
            content = item.get("content", "")
            label = (
                "User"
                if role == "user"
                else "Assistant" if role == "assistant" else "Other"
            )
            history_lines.append(f"{label}: {content}")

        prompt = "\n".join(history_lines)
        start_time = time.perf_counter()

        try:
            # Call the model with a timeout, and log the response.
            response = call_model_with_timeout(prompt) or ""
            decky.logger.debug(f"[chat] model response: {response}")
        except ModelConfigError:
            decky.logger.error(
                f"[chat] GEMINI_API_KEY is not set, cannot call model for input: {cleaned}"
            )
            response = "Gemini API Key is not configured. Please set the GEMINI_API_KEY in the .env file."
        except TimeoutError:
            decky.logger.warning(f"[chat] model call timed out for input: {cleaned}")
            response = "Sorry, the model took too long to respond. Please try again."
        except ModelApiError as e:
            decky.logger.warning(
                f"[chat] model API returned status {e.status_code} for input: {cleaned}"
            )
            response = f"API error code: {e.status_code}"
        except Exception as e:
            decky.logger.error(
                f"[chat] model call failed for input: {cleaned} | error: {e}"
            )
            response = (
                "Sorry, there was an error processing your request. Please try again."
            )

        self._append_history("assistant", response)
        response_time_ms = int((time.perf_counter() - start_time) * 1000)

        await decky.emit("chat_message", response, response_time_ms)

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        # Keep a loop reference for optional background tasks.
        self.loop = asyncio.get_event_loop()
        self._history_path().parent.mkdir(parents=True, exist_ok=True)
        decky.logger.info(f"[chat] Plugin started (log level: {LOGGING_LEVEL})")

    # Function called first during the unload process, utilize this to handle your plugin being stopped, but not
    # completely removed
    async def _unload(self):
        decky.logger.info("[chat] Plugin unloading")
        pass

    # Function called after `_unload` during uninstall, utilize this to clean up processes and other remnants of your
    # plugin that may remain on the system
    async def _uninstall(self):
        decky.logger.info("[chat] Plugin uninstalled")
        pass

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        decky.logger.debug("[chat] Running migration steps")
        # Here's a migration example for logs:
        # - `~/.config/decky-template/template.log` will be migrated to `decky.decky_LOG_DIR/template.log`
        decky.migrate_logs(
            os.path.join(
                decky.DECKY_USER_HOME, ".config", "decky-template", "template.log"
            )
        )
        # Here's a migration example for settings:
        # - `~/homebrew/settings/template.json` is migrated to `decky.decky_SETTINGS_DIR/template.json`
        # - `~/.config/decky-template/` all files and directories under this root are migrated to `decky.decky_SETTINGS_DIR/`
        decky.migrate_settings(
            os.path.join(decky.DECKY_HOME, "settings", "template.json"),
            os.path.join(decky.DECKY_USER_HOME, ".config", "decky-template"),
        )
        # Here's a migration example for runtime data:
        # - `~/homebrew/template/` all files and directories under this root are migrated to `decky.decky_RUNTIME_DIR/`
        # - `~/.local/share/decky-template/` all files and directories under this root are migrated to `decky.decky_RUNTIME_DIR/`
        decky.migrate_runtime(
            os.path.join(decky.DECKY_HOME, "template"),
            os.path.join(decky.DECKY_USER_HOME, ".local", "share", "decky-template"),
        )
