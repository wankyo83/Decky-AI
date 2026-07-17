import os
import sys
import json
import time
import logging
import shutil
import signal
import tempfile
import re
import html
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code repo
# and add the `decky-loader/plugin/imports` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky
import asyncio
import configparser
import httpx
from urllib.parse import quote_plus

# Decky may load this module without automatically adding the plugin folder to sys.path.
# Prepend the plugin directory so sibling files like chat_common.py can be imported.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


LOGGING_LEVEL = os.getenv("CHAT_LOGGING_LEVEL", "INFO").upper()
decky.logger.setLevel(getattr(logging, LOGGING_LEVEL, logging.INFO))

AUTO_WEB_PATTERN = re.compile(
    r"(?:날씨|기온|미세먼지|비\s*(?:와|오|올)|오늘\s*뉴스|최신\s*뉴스|환율|주가|"
    r"실시간|현재\s*(?:가격|시간|순위|점수)|weather|forecast|latest\s+news|"
    r"exchange\s+rate|stock\s+price)",
    re.IGNORECASE,
)

GAME_SEARCH_PATTERN = re.compile(
    r"(?:게임|공략|스킬|트리|빌드|보스|퀘스트|임무|퍼즐|장비|무기|방어구|"
    r"캐릭터|레벨|어디로|walkthrough|guide|skill|build|boss|quest|puzzle)",
    re.IGNORECASE,
)


from chat_common import (
    MODEL,
    NUM_HISTORY_MESSAGES,
    SEARCH_MODEL,
    ModelApiError,
    ModelConfigError,
    call_model,
)


class Plugin:
    HISTORY_FILE = "chat_history.json"
    SECRETS_FILE = "secrets.env"

    def __init__(self) -> None:
        self._panel_session: Optional[str] = None
        self._request_task: Optional[asyncio.Task] = None
        self._request_kind: Optional[str] = None
        self._media_process: Optional[asyncio.subprocess.Process] = None
        self._recording_path: Optional[Path] = None
        self._recording_timer: Optional[asyncio.Task] = None
        self._chapter_task: Optional[asyncio.Task] = None

    def _history_path(self) -> Path:
        return Path(decky.DECKY_PLUGIN_RUNTIME_DIR) / self.HISTORY_FILE

    def _secrets_path(self) -> Path:
        return Path(decky.DECKY_PLUGIN_SETTINGS_DIR) / self.SECRETS_FILE

    def _apply_api_key(self, api_key: str) -> None:
        import chat_common

        os.environ["GEMINI_API_KEY"] = api_key
        chat_common.GEMINI_API_KEY = api_key

    def _load_saved_api_key(self) -> bool:
        secrets_path = self._secrets_path()
        if not secrets_path.is_file():
            return False
        try:
            for line in secrets_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    if api_key:
                        self._apply_api_key(api_key)
                        return True
        except OSError as error:
            decky.logger.warning(f"[settings] Could not load saved API key: {error}")
        return False

    async def _call_gemini_resilient(
        self,
        prompt: str,
        model: str,
        enable_google_search: bool,
        media_bytes: Optional[bytes] = None,
        media_mime_type: Optional[str] = None,
    ) -> str:
        """Call Gemini without disguising a failed web search as a normal answer."""
        try:
            return await call_model(
                prompt,
                model=model,
                enable_google_search=enable_google_search,
                media_bytes=media_bytes,
                media_mime_type=media_mime_type,
            ) or ""
        except ModelApiError as error:
            # A non-grounded answer is not a valid substitute for a requested
            # live search. Never silently fall back when search was requested.
            if (
                error.status_code != 404
                or model == MODEL
                or enable_google_search
            ):
                raise
            decky.logger.warning(
                f"[model] {model} returned 404; retrying with {MODEL} without search grounding"
            )
            return await call_model(
                prompt,
                model=MODEL,
                enable_google_search=False,
                media_bytes=media_bytes,
                media_mime_type=media_mime_type,
            ) or ""

    async def _youtube_walkthrough_links(self, query: str) -> str:
        """Find a few direct YouTube results without requiring a YouTube API key."""
        search_url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
        try:
            async with httpx.AsyncClient(
                timeout=8.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DeckyAI/1.0"},
            ) as client:
                response = await client.get(search_url)
                response.raise_for_status()
            video_ids = list(dict.fromkeys(
                re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', response.text)
            ))[:3]
            if video_ids:
                links = [
                    f"- [YouTube 공략 영상 {index}](https://www.youtube.com/watch?v={video_id})"
                    for index, video_id in enumerate(video_ids, start=1)
                ]
                return "\n\n**YouTube 공략:**\n" + "\n".join(links)
        except Exception as error:
            decky.logger.warning(f"[youtube] Search failed: {error}")
        return f"\n\n[YouTube에서 이 공략 검색하기]({search_url})"

    async def get_youtube_chapters(self, video_id: str) -> List[Dict[str, object]]:
        """Return official chapters, or generate approximate ones from timed captions."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return []

        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            async with httpx.AsyncClient(
                timeout=8.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DeckyAI/1.0",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
                },
            ) as client:
                response = await client.get(watch_url)
                response.raise_for_status()
        except Exception as error:
            decky.logger.warning(f"[youtube] Chapter fetch failed: {error}")
            return []

        page = response.text
        chapters: List[Dict[str, object]] = []

        # Most creator-authored chapters are timestamp lines in shortDescription.
        description_match = re.search(
            r'"shortDescription":"((?:\\.|[^"\\])*)"', page
        )
        if description_match:
            try:
                description = json.loads(f'"{description_match.group(1)}"')
                for line in description.splitlines():
                    match = re.match(
                        r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*[-–—:|]?\s*(.+?)\s*$",
                        line,
                    )
                    if not match:
                        continue
                    parts = [int(value) for value in match.group(1).split(":")]
                    seconds = (
                        parts[0] * 3600 + parts[1] * 60 + parts[2]
                        if len(parts) == 3
                        else parts[0] * 60 + parts[1]
                    )
                    chapters.append({
                        "seconds": seconds,
                        "timestamp": match.group(1),
                        "title": html.unescape(match.group(2)).strip(),
                        "generated": False,
                    })
            except (json.JSONDecodeError, ValueError):
                pass

        # Some videos expose chapter renderers even when the description is sparse.
        if not chapters:
            renderer_pattern = re.compile(
                r'"chapterRenderer"\s*:\s*\{.*?"title"\s*:\s*\{\s*"simpleText"\s*:\s*"((?:\\.|[^"\\])*)".*?'
                r'"timeRangeStartMillis"\s*:\s*(\d+)',
                re.DOTALL,
            )
            for title_raw, millis_raw in renderer_pattern.findall(page):
                try:
                    title = json.loads(f'"{title_raw}"')
                    seconds = int(millis_raw) // 1000
                except (json.JSONDecodeError, ValueError):
                    continue
                hours, remainder = divmod(seconds, 3600)
                minutes, secs = divmod(remainder, 60)
                timestamp = (
                    f"{hours}:{minutes:02d}:{secs:02d}"
                    if hours else f"{minutes:02d}:{secs:02d}"
                )
                chapters.append({
                    "seconds": seconds,
                    "timestamp": timestamp,
                    "title": html.unescape(title).strip(),
                    "generated": False,
                })

        unique: Dict[int, Dict[str, object]] = {}
        for chapter in chapters:
            seconds = int(chapter["seconds"])
            if chapter["title"] and seconds not in unique:
                unique[seconds] = chapter
        official = [unique[key] for key in sorted(unique)][:40]
        if official:
            return official

        # No official chapters: use YouTube's timed captions as a lightweight,
        # bandwidth-friendly alternative to downloading and analyzing the video.
        if self._chapter_task and not self._chapter_task.done():
            self._chapter_task.cancel()
        task = asyncio.create_task(self._generate_chapters_from_captions(page))
        self._chapter_task = task
        try:
            return await task
        finally:
            if self._chapter_task is task:
                self._chapter_task = None

    async def cancel_youtube_chapters(self) -> None:
        """Stop caption-based chapter generation when the video modal closes."""
        task = self._chapter_task
        self._chapter_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _generate_chapters_from_captions(
        self, watch_page: str
    ) -> List[Dict[str, object]]:
        marker = '"captionTracks":'
        marker_index = watch_page.find(marker)
        if marker_index < 0:
            return []

        try:
            tracks, _ = json.JSONDecoder().raw_decode(
                watch_page[marker_index + len(marker):]
            )
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(tracks, list) or not tracks:
            return []

        preferred = sorted(
            (track for track in tracks if isinstance(track, dict)),
            key=lambda track: (
                0 if track.get("languageCode") == "ko" else
                1 if track.get("languageCode") == "en" else
                2 if track.get("kind") != "asr" else 3
            ),
        )
        if not preferred:
            return []
        caption_url = html.unescape(str(preferred[0].get("baseUrl", "")))
        if not caption_url:
            return []
        caption_url += ("&" if "?" in caption_url else "?") + "fmt=json3"

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(caption_url)
                response.raise_for_status()
                caption_data = response.json()
        except Exception as error:
            decky.logger.warning(f"[youtube] Caption fetch failed: {error}")
            return []

        events = caption_data.get("events", [])
        timed_lines: List[Tuple[int, str]] = []
        max_seconds = 0
        for event in events:
            if not isinstance(event, dict) or "tStartMs" not in event:
                continue
            seconds = int(event.get("tStartMs", 0)) // 1000
            text = "".join(
                str(segment.get("utf8", ""))
                for segment in event.get("segs", [])
                if isinstance(segment, dict)
            )
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                timed_lines.append((seconds, text))
                max_seconds = max(max_seconds, seconds)
        if len(timed_lines) < 5:
            return []

        # Merge densely timed caption fragments so the Gemini request stays small
        # even for long walkthrough videos.
        bucket_seconds = 30 if max_seconds <= 7200 else 60 if max_seconds <= 14400 else 120
        buckets: Dict[int, List[str]] = {}
        for seconds, text in timed_lines:
            bucket = (seconds // bucket_seconds) * bucket_seconds
            buckets.setdefault(bucket, []).append(text)

        transcript_lines: List[str] = []
        for seconds in sorted(buckets):
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            timestamp = (
                f"{hours}:{minutes:02d}:{secs:02d}"
                if hours else f"{minutes:02d}:{secs:02d}"
            )
            merged = " ".join(buckets[seconds])[:240]
            transcript_lines.append(f"{timestamp} {merged}")

        transcript = "\n".join(transcript_lines)
        if len(transcript) > 70000:
            transcript = transcript[:70000]
        prompt = (
            "다음은 YouTube 게임 공략 영상의 시간 정보가 포함된 자막이다. "
            "자막에 실제로 나타나는 진행 변화만 이용해 유용한 챕터 6~20개를 만들어라. "
            "첫 항목은 0초로 하고 시간순으로 정렬하라. 제목은 짧은 한국어로 작성하라. "
            "JSON 배열만 출력하라. 형식: "
            '[{"seconds":0,"title":"시작"},{"seconds":90,"title":"첫 번째 퍼즐"}]\n\n'
            + transcript
        )
        try:
            model_text = await call_model(
                prompt,
                model=MODEL,
                enable_google_search=False,
                system_instruction=(
                    "You create approximate video chapters only from the supplied timed "
                    "captions. Return valid JSON only and never invent unsupported events."
                ),
            ) or ""
            array_match = re.search(r"\[.*\]", model_text, re.DOTALL)
            parsed = json.loads(array_match.group(0)) if array_match else []
        except asyncio.CancelledError:
            raise
        except Exception as error:
            decky.logger.warning(f"[youtube] AI chapter generation failed: {error}")
            return []

        generated: Dict[int, Dict[str, object]] = {}
        for item in parsed if isinstance(parsed, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                seconds = max(0, int(item.get("seconds", 0)))
            except (TypeError, ValueError):
                continue
            title = str(item.get("title", "")).strip()[:80]
            if not title or seconds > max_seconds + bucket_seconds:
                continue
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            timestamp = (
                f"{hours}:{minutes:02d}:{secs:02d}"
                if hours else f"{minutes:02d}:{secs:02d}"
            )
            generated.setdefault(seconds, {
                "seconds": seconds,
                "timestamp": timestamp,
                "title": title,
                "generated": True,
            })
        return [generated[key] for key in sorted(generated)][:20]

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
        secrets_path = self._secrets_path()
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text(f"GEMINI_API_KEY={api_key}\n", encoding="utf-8")

        # The key is a secret. On SteamOS, restrict the file to the deck user.
        try:
            os.chmod(secrets_path, 0o600)
        except OSError:
            decky.logger.warning("[settings] Could not restrict secrets.env permissions")

        self._apply_api_key(api_key)

    async def import_api_key_from_ini(self, file_path: str) -> Dict[str, object]:
        """Import a Gemini key from an INI file explicitly selected by the user."""
        try:
            ini_path = Path(file_path).expanduser()
            if ini_path.suffix.lower() != ".ini":
                return {"ok": False, "message": ".ini 파일만 선택할 수 있습니다."}
            if not ini_path.is_file():
                return {"ok": False, "message": "선택한 파일을 찾을 수 없습니다."}
            if ini_path.stat().st_size > 64 * 1024:
                return {"ok": False, "message": "INI 파일이 너무 큽니다."}
            raw_text = ini_path.read_text(encoding="utf-8-sig")

            api_key = ""
            parser = configparser.ConfigParser()
            try:
                parser.read_string(raw_text)
                for section in parser.sections():
                    for key_name in ("GEMINI_API_KEY", "API_KEY", "api_key"):
                        if parser.has_option(section, key_name):
                            api_key = parser.get(section, key_name).strip().strip('"\'')
                            break
                    if api_key:
                        break
            except configparser.Error:
                # Also accept a simple GEMINI_API_KEY=... file without a section.
                pass

            if not api_key:
                for line in raw_text.splitlines():
                    cleaned = line.strip()
                    if not cleaned or cleaned.startswith(("#", ";")) or "=" not in cleaned:
                        continue
                    key_name, value = cleaned.split("=", 1)
                    if key_name.strip().upper() in ("GEMINI_API_KEY", "API_KEY"):
                        api_key = value.strip().strip('"\'')
                        break

            if not api_key:
                return {"ok": False, "message": "파일에서 GEMINI_API_KEY 값을 찾지 못했습니다."}
            if any(character.isspace() for character in api_key):
                return {"ok": False, "message": "API 키에 공백이 포함되어 있습니다."}
            is_standard_key = bool(re.fullmatch(r"AIza[A-Za-z0-9_-]{35}", api_key))
            is_auth_key = bool(re.fullmatch(r"AQ\.[A-Za-z0-9_-]{40,200}", api_key))
            if not (is_standard_key or is_auth_key):
                return {
                    "ok": False,
                    "message": "Gemini API 키 형식이 아닙니다. AI Studio에서 복사한 AQ. 또는 AIza로 시작하는 전체 키를 사용하세요.",
                }

            await self.set_api_key(api_key)
            decky.logger.info(f"[settings] Gemini API key imported from {ini_path.name}")
            return {"ok": True, "message": f"{ini_path.name}에서 API 키를 불러왔습니다."}
        except (OSError, UnicodeError) as error:
            decky.logger.warning(f"[settings] Could not read selected INI: {error}")
            return {"ok": False, "message": "선택한 INI 파일을 읽지 못했습니다."}
        except Exception as error:
            decky.logger.error(f"[settings] API key import failed: {error}")
            return {"ok": False, "message": "API 키 저장 중 오류가 발생했습니다."}

    async def _cancel_active_request(self, preserve_chat: bool = False) -> None:
        task = self._request_task
        if task is None or task.done():
            self._request_task = None
            self._request_kind = None
            return
        if preserve_chat and self._request_kind == "chat":
            decky.logger.debug("[chat] Keeping text request alive during panel transition")
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            decky.logger.info("[chat] Active model request cancelled")
        finally:
            if self._request_task is task:
                self._request_task = None
                self._request_kind = None

    async def _stop_media_process(self) -> None:
        process = self._media_process
        self._media_process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=1.5)
        except (ProcessLookupError, asyncio.TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _cancel_recording(self, remove_file: bool = True) -> None:
        timer = self._recording_timer
        self._recording_timer = None
        if timer and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        await self._stop_media_process()
        path = self._recording_path
        self._recording_path = None
        if remove_file and path:
            path.unlink(missing_ok=True)

    async def _recording_time_limit(self) -> None:
        try:
            await asyncio.sleep(30)
            await self._stop_media_process()
            decky.logger.info("[voice] Recording stopped at the 30 second limit")
        except asyncio.CancelledError:
            return

    async def open_panel(self, session_id: str) -> None:
        if self._panel_session and self._panel_session != session_id:
            await self._cancel_active_request(preserve_chat=True)
        self._panel_session = session_id
        decky.logger.debug(f"[lifecycle] Panel opened: {session_id}")

    async def close_panel(self, session_id: str) -> None:
        # Ignore a late cleanup message from an older React mount.
        if self._panel_session != session_id:
            return
        self._panel_session = None
        # Text questions are short, bounded API calls and must survive Decky's
        # temporary hidden state while the Ask Assistant modal changes views.
        # Capture and voice work still stop immediately when the panel closes.
        await self._cancel_active_request(preserve_chat=True)
        await self._cancel_recording()
        decky.logger.debug(f"[lifecycle] Panel closed: {session_id}")

    async def start_voice_recording(self, session_id: str) -> None:
        # A Decky modal can briefly hide QAM; an explicit user action reopens the session.
        self._panel_session = session_id
        if self._request_task and not self._request_task.done():
            raise RuntimeError("An AI request is already running")
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record is not available on this SteamOS installation")

        await self._cancel_recording()
        runtime_dir = Path(decky.DECKY_PLUGIN_RUNTIME_DIR)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="decky-ai-voice-", suffix=".wav", dir=runtime_dir)
        os.close(fd)
        self._recording_path = Path(raw_path)
        voice_env = self._screen_capture_environment()
        self._media_process = await asyncio.create_subprocess_exec(
            "pw-record",
            "--rate=48000",
            "--channels=1",
            "--format=s16",
            "--channel-map=mono",
            raw_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=voice_env,
        )
        # pw-record can start successfully as a process and then immediately fail
        # to connect to the Steam user's PipeWire microphone. Detect that here.
        await asyncio.sleep(0.2)
        if self._media_process.returncode is not None:
            process = self._media_process
            self._media_process = None
            _, error = await process.communicate()
            detail = re.sub(
                r"\s+", " ", error.decode("utf-8", errors="ignore")
            ).strip()[-220:]
            self._recording_path.unlink(missing_ok=True)
            self._recording_path = None
            raise RuntimeError(
                "SteamOS 마이크에 연결하지 못했습니다. "
                f"진단: {detail or f'pw-record 종료 코드 {process.returncode}'}"
            )
        self._recording_timer = asyncio.create_task(self._recording_time_limit())
        decky.logger.info("[voice] Recording started")

    async def stop_voice_recording(self, session_id: str, game_name: str = "") -> str:
        if self._panel_session != session_id:
            await self._cancel_recording()
            return ""
        path = self._recording_path
        if path is None:
            raise RuntimeError("Voice recording is not active")

        timer = self._recording_timer
        self._recording_timer = None
        if timer and not timer.done():
            timer.cancel()
        recording_process = self._media_process
        await self._stop_media_process()
        self._recording_path = None
        try:
            audio = path.read_bytes()
            if len(audio) < 1024:
                detail = ""
                if recording_process and recording_process.stderr:
                    error = await recording_process.stderr.read()
                    detail = re.sub(
                        r"\s+", " ", error.decode("utf-8", errors="ignore")
                    ).strip()[-220:]
                raise RuntimeError(
                    "마이크 음성이 녹음되지 않았습니다. "
                    f"진단: {detail or '녹음 파일이 비어 있음'}"
                )
            transcription_prompt = f"""
            Transcribe this short voice input for a gaming assistant.
            Current game: {game_name.strip() or 'Unknown game'}

            Requirements:
            - The speaker will usually speak Korean, possibly mixed with English game names.
            - Use the current game as context to resolve similar-sounding words.
            - Prefer common gaming terms when supported by the audio, such as 스킬 트리,
              빌드, 퀘스트, 장비, 특성, 캐릭터, 보스, 공략, 레벨, 무기, 방어구.
            - For example, do not turn clearly spoken '스킬 트리 추천해줘' into an
              unrelated phrase such as '숲 캐릭터 추천해줘'.
            - Preserve the user's intended question; do not answer it.
            - Return only one cleaned transcription sentence with no quotation marks,
              explanation, label, or markdown.
            """
            task = asyncio.create_task(call_model(
                transcription_prompt,
                model=MODEL,
                media_bytes=audio,
                media_mime_type="audio/wav",
                system_instruction=(
                    "You are a highly accurate multilingual speech-to-text engine for "
                    "video-game questions. Output only the transcription. Use acoustic "
                    "evidence first and game context only to resolve ambiguity."
                ),
            ))
            self._request_task = task
            self._request_kind = "voice"
            transcript = (await task or "").strip()
            return transcript if self._panel_session == session_id else ""
        finally:
            path.unlink(missing_ok=True)
            if self._request_task and self._request_task.done():
                self._request_task = None
                self._request_kind = None

    async def cancel_voice_recording(self, session_id: str) -> None:
        """Discard an unfinished recording when the composer is closed."""
        if self._panel_session not in (None, session_id):
            return
        await self._cancel_recording()
        decky.logger.info("[voice] Recording cancelled from composer")

    async def _run_capture_command(
        self,
        command: List[str],
        output_path: Path,
        timeout: float,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[bytes], str]:
        """Run one capture process and always leave it cancellable by panel close."""
        output_path.unlink(missing_ok=True)
        try:
            self._media_process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            process = self._media_process
            try:
                _, error = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._stop_media_process()
                return None, "시간 초과"
            finally:
                if process.returncode is not None and self._media_process is process:
                    self._media_process = None

            detail = error.decode("utf-8", errors="ignore").strip()[-240:]
            if process.returncode != 0 or not output_path.is_file():
                return None, detail or "출력 파일 없음"
            frame = output_path.read_bytes()
            if len(frame) < 20_000:
                return None, "캡처된 이미지가 비어 있음"
            return frame, ""
        except FileNotFoundError:
            return None, "명령을 찾을 수 없음"

    def _screen_capture_environment(self) -> Dict[str, str]:
        """Connect capture tools to the Steam user's Game Mode session."""
        env = os.environ.copy()
        user_home = Path(decky.DECKY_USER_HOME)
        try:
            user_id = user_home.stat().st_uid
        except OSError:
            user_id = 1000
        runtime_dir = f"/run/user/{user_id}"
        env.update({
            "HOME": str(user_home),
            "XDG_RUNTIME_DIR": runtime_dir,
            "XDG_SESSION_TYPE": "wayland",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
            "PIPEWIRE_REMOTE": "pipewire-0",
        })
        return env

    async def _capture_pipewire_frame(
        self, output_path: Path
    ) -> Tuple[Optional[bytes], str]:
        """Capture Game Mode through PipeWire, tolerating GStreamer EOS quirks."""
        env = self._screen_capture_environment()
        last_detail = "PipeWire 캡처 실패"

        for attempt in range(1, 4):
            output_path.unlink(missing_ok=True)
            if attempt > 1:
                await asyncio.sleep(0.3)
            try:
                self._media_process = await asyncio.create_subprocess_exec(
                    "gst-launch-1.0", "-q", "-e",
                    "pipewiresrc", "do-timestamp=true", "num-buffers=5", "!",
                    "videoconvert", "!",
                    "pngenc", "snapshot=true", "!",
                    "filesink", f"location={output_path}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                process = self._media_process
                try:
                    _, error = await asyncio.wait_for(
                        process.communicate(), timeout=2.75
                    )
                except asyncio.TimeoutError:
                    # Some SteamOS GStreamer builds write a valid frame but wait for
                    # EOS indefinitely. Ask them to finish, then inspect the file.
                    try:
                        process.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    try:
                        _, error = await asyncio.wait_for(
                            process.communicate(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        process.kill()
                        _, error = await process.communicate()
                finally:
                    if self._media_process is process:
                        self._media_process = None

                last_detail = error.decode("utf-8", errors="ignore").strip()[-240:]
                if output_path.is_file():
                    frame = output_path.read_bytes()
                    if len(frame) >= 20_000:
                        decky.logger.info(
                            f"[screen] PipeWire capture succeeded on attempt {attempt}"
                        )
                        return frame, ""
                    last_detail = f"캡처 이미지가 너무 작음 ({len(frame)} bytes)"
            except FileNotFoundError:
                return None, "gst-launch-1.0 명령을 찾을 수 없음"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_detail = str(error)

        return None, last_detail

    async def _capture_with_gamescope_hotkey(
        self, env: Dict[str, str]
    ) -> Tuple[Optional[bytes], str]:
        """Ask gamescope for its built-in Super+S screenshot without user input."""
        before = {
            path: path.stat().st_mtime
            for path in Path("/tmp").glob("gamescope_*.png")
            if path.is_file()
        }
        command: Optional[List[str]] = None
        if shutil.which("wtype"):
            command = ["wtype", "-M", "LOGO", "s", "-m", "LOGO"]
        elif shutil.which("xdotool"):
            env.setdefault("DISPLAY", ":0")
            command = ["xdotool", "key", "Super+s"]
        if command is None:
            return None, "wtype/xdotool 명령 없음"

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, error = await asyncio.wait_for(process.communicate(), timeout=2.0)
            if process.returncode != 0:
                detail = error.decode("utf-8", errors="ignore").strip()[-180:]
                return None, detail or f"종료 코드 {process.returncode}"

            for _ in range(10):
                await asyncio.sleep(0.2)
                candidates = sorted(
                    (path for path in Path("/tmp").glob("gamescope_*.png") if path.is_file()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                for candidate in candidates[:3]:
                    modified = candidate.stat().st_mtime
                    if modified <= before.get(candidate, 0):
                        continue
                    frame = candidate.read_bytes()
                    if len(frame) >= 20_000:
                        candidate.unlink(missing_ok=True)
                        decky.logger.info("[screen] Captured with gamescope Super+S")
                        return frame, ""
            return None, "새 gamescope 스크린샷이 생성되지 않음"
        except asyncio.TimeoutError:
            return None, "명령 시간 초과"
        except Exception as error:
            return None, str(error)

    async def _capture_game_frame(self) -> Tuple[bytes, str]:
        runtime_dir = Path(decky.DECKY_PLUGIN_RUNTIME_DIR)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        jpg_path = runtime_dir / "decky-ai-screen.jpg"
        png_path = runtime_dir / "decky-ai-screen.png"
        failures: List[str] = []
        try:
            if shutil.which("gst-launch-1.0"):
                frame, detail = await self._capture_pipewire_frame(png_path)
                if frame:
                    decky.logger.info("[screen] Captured with gamescope PipeWire")
                    return frame, "image/png"
                failures.append(f"PipeWire: {detail}")

            capture_env = self._screen_capture_environment()
            frame, detail = await self._capture_with_gamescope_hotkey(capture_env)
            if frame:
                return frame, "image/png"
            failures.append(f"gamescope: {detail}")

            if shutil.which("grim"):
                frame, detail = await self._run_capture_command(
                    ["grim", str(png_path)], png_path, 5.0, capture_env
                )
                if frame:
                    decky.logger.info("[screen] Captured with grim fallback")
                    return frame, "image/png"
                failures.append(f"grim: {detail}")

            spectacle = shutil.which("spectacle")
            if spectacle:
                frame, detail = await self._run_capture_command(
                    [spectacle, "-b", "-n", "-f", "-o", str(png_path)],
                    png_path,
                    7.0,
                    capture_env,
                )
                if frame:
                    decky.logger.info("[screen] Captured with Spectacle fallback")
                    return frame, "image/png"
                failures.append(f"Spectacle: {detail}")

            recent = self._find_recent_steam_screenshot(max_age_seconds=300)
            if recent is not None:
                frame = recent.read_bytes()
                if len(frame) >= 20_000:
                    mime_type = "image/png" if recent.suffix.lower() == ".png" else "image/jpeg"
                    decky.logger.info(
                        f"[screen] Using recent Steam screenshot: {recent.name}"
                    )
                    return frame, mime_type

            safe_failures = [
                re.sub(r"\s+", " ", failure).strip()[-220:]
                for failure in failures
                if failure.strip()
            ]
            failure_summary = " | ".join(safe_failures) or "사용 가능한 캡처 명령 없음"
            decky.logger.warning("[screen] All capture methods failed: " + failure_summary)
            raise RuntimeError(
                "게임 화면 자동 캡처에 실패했습니다.\n\n"
                f"진단: {failure_summary}"
            )
        finally:
            jpg_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)

    def _find_recent_steam_screenshot(self, max_age_seconds: int) -> Optional[Path]:
        """Find only recent Steam screenshots without recursively scanning the disk."""
        user_home = Path(decky.DECKY_USER_HOME)
        roots = (
            user_home / ".local" / "share" / "Steam" / "userdata",
            user_home / ".steam" / "steam" / "userdata",
            user_home / ".steam" / "root" / "userdata",
        )
        newest: Optional[Path] = None
        newest_mtime = 0.0
        now = time.time()

        for root in roots:
            if not root.is_dir():
                continue
            try:
                screenshot_dirs = root.glob("*/760/remote/*/screenshots")
                for screenshot_dir in screenshot_dirs:
                    if not screenshot_dir.is_dir():
                        continue
                    for candidate in screenshot_dir.iterdir():
                        if candidate.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                            continue
                        try:
                            modified = candidate.stat().st_mtime
                        except OSError:
                            continue
                        if now - modified <= max_age_seconds and modified > newest_mtime:
                            newest = candidate
                            newest_mtime = modified
            except OSError as error:
                decky.logger.debug(f"[screen] Could not inspect {root}: {error}")

        return newest

    async def analyze_game_screen(
        self,
        text: str,
        game_name: str,
        puzzle_mode: bool,
        session_id: str,
    ) -> Dict[str, object]:
        # Treat this explicit button press as an active panel session.
        self._panel_session = session_id
        if self._request_task and not self._request_task.done():
            raise RuntimeError("An AI request is already running")

        cleaned = text.strip() or (
            "화면에 보이는 퍼즐의 정확한 해결 순서를 알려줘."
            if puzzle_mode else "이 화면을 분석해서 지금 어디로 가야 하는지 알려줘."
        )
        self._append_history("user", f"[화면 분석] {cleaned}")
        start_time = time.perf_counter()

        async def capture_and_ask() -> str:
            base_youtube_query = " ".join(filter(None, [
                game_name.strip(), "walkthrough guide",
            ]))
            try:
                frame, frame_mime_type = await self._capture_game_frame()
            except asyncio.CancelledError:
                raise
            except Exception as capture_error:
                if not puzzle_mode:
                    raise
                decky.logger.warning(
                    f"[youtube] Screen capture unavailable; using game-name search: {capture_error}"
                )
                message = (
                    "화면 캡처를 분석하지 못해 실행 중인 게임 이름으로 YouTube 공략을 찾았습니다."
                )
                return message + await self._youtube_walkthrough_links(base_youtube_query)
            video_instruction = (
                " 실행 중인 게임 이름을 가장 중요한 단서로 사용해서 화면의 장소, 임무, 보스, "
                "퍼즐 이름을 최대한 식별해. 답변 마지막 줄에는 YouTube 검색에 적합한 핵심어만 "
                "[YOUTUBE_QUERY: 영어 또는 원어 검색어] 형식으로 작성해."
                if puzzle_mode else ""
            )
            prompt = (
                f"현재 게임: {game_name.strip() or '알 수 없음'}\n"
                f"사용자 질문: {cleaned}\n"
                "첨부된 현재 게임 화면만 근거로 보이는 요소를 먼저 식별하고, 짧고 구체적인 단계로 답해. "
                "장면 식별이 불확실하면 그 사실을 명시해."
                f"{video_instruction}"
            )
            try:
                answer = await self._call_gemini_resilient(
                    prompt,
                    model=MODEL,
                    enable_google_search=False,
                    media_bytes=frame,
                    media_mime_type=frame_mime_type,
                )
            except asyncio.CancelledError:
                raise
            except ModelApiError as primary_error:
                if not puzzle_mode:
                    raise
                # 3.5 free-tier RPM/quota can temporarily return 429. A 2.5
                # vision call may still be available, so try it without Search.
                if primary_error.status_code == 429:
                    try:
                        answer = await call_model(
                            prompt,
                            model=SEARCH_MODEL,
                            enable_google_search=False,
                            media_bytes=frame,
                            media_mime_type=frame_mime_type,
                        ) or ""
                    except asyncio.CancelledError:
                        raise
                    except Exception as fallback_error:
                        decky.logger.warning(
                            f"[youtube] 2.5 vision fallback failed: {fallback_error}"
                        )
                        answer = (
                            "Gemini 화면 분석 사용량이 일시적으로 제한되어 "
                            "실행 중인 게임 이름으로 YouTube 공략을 찾았습니다."
                        )
                else:
                    answer = (
                        f"Gemini 화면 분석을 사용할 수 없어(오류 {primary_error.status_code}) "
                        "실행 중인 게임 이름으로 YouTube 공략을 찾았습니다."
                    )
            except (ModelConfigError, TimeoutError) as model_error:
                if not puzzle_mode:
                    raise
                decky.logger.warning(f"[youtube] Vision analysis unavailable: {model_error}")
                answer = (
                    "화면을 AI로 식별하지 못해 실행 중인 게임 이름으로 YouTube 공략을 찾았습니다."
                )
            except Exception as model_error:
                if not puzzle_mode:
                    raise
                decky.logger.warning(f"[youtube] Vision analysis failed: {model_error}")
                answer = (
                    "화면 분석 중 오류가 발생해 실행 중인 게임 이름으로 YouTube 공략을 찾았습니다."
                )
            if puzzle_mode:
                query_match = re.search(
                    r"\[YOUTUBE_QUERY:\s*(.+?)\s*\]", answer, re.IGNORECASE
                )
                scene_query = query_match.group(1).strip() if query_match else ""
                answer = re.sub(
                    r"\s*\[YOUTUBE_QUERY:\s*.+?\s*\]\s*", "", answer,
                    flags=re.IGNORECASE,
                ).strip()
                youtube_query = " ".join(filter(None, [
                    game_name.strip(), scene_query, "walkthrough guide",
                ])) or base_youtube_query
                answer += await self._youtube_walkthrough_links(youtube_query)
            return answer

        task = asyncio.create_task(capture_and_ask())
        self._request_task = task
        self._request_kind = "screen"
        try:
            response = await task
            if not response.strip():
                response = (
                    "Gemini가 빈 응답을 반환했습니다. API 키의 모델 사용 권한과 "
                    "무료 사용량을 확인해 주세요."
                )
        except asyncio.CancelledError:
            return {"text": "", "response_time_ms": 0}
        except ModelConfigError:
            response = "Gemini API 키가 설정되지 않았습니다. 설정에서 INI 파일을 다시 불러오세요."
        except TimeoutError:
            response = "화면 분석 요청 시간이 초과됐습니다. 네트워크를 확인하고 다시 시도해 주세요."
        except ModelApiError as error:
            response = (
                "429 오류: 분당/일일 무료 사용량 소진"
                if error.status_code == 429
                else f"Gemini API가 요청을 거부했습니다. 오류 코드: {error.status_code}"
            )
        except RuntimeError as error:
            response = str(error)
        except Exception as error:
            decky.logger.error(f"[screen] Analysis failed: {error}")
            response = "화면 분석 중 알 수 없는 오류가 발생했습니다. Decky 로그를 확인해 주세요."
        finally:
            await self._stop_media_process()
            if self._request_task is task:
                self._request_task = None
                self._request_kind = None

        self._append_history("assistant", response)
        return {
            "text": response,
            "response_time_ms": int((time.perf_counter() - start_time) * 1000),
        }

    async def send_message(
        self,
        text: str,
        game_name: str,
        question_mode: str,
        session_id: str,
    ):
        # Frontend input arrives here via callable("send_message") in index.tsx.
        cleaned = text.strip()
        if not cleaned:
            # Ignore empty input so no blank rows/log spam are generated.
            return {"text": "", "response_time_ms": 0}
        # Closing the composer modal can briefly mark QAM hidden. The send action itself
        # is authoritative; a later genuine panel close will still cancel this request.
        self._panel_session = session_id

        start_time = time.perf_counter()

        # Plain Google mode deliberately bypasses Gemini, so it remains available
        # when the Gemini API is out of quota or a model endpoint returns 404.
        if question_mode == "google":
            include_game = bool(game_name.strip() and GAME_SEARCH_PATTERN.search(cleaned))
            search_query = " ".join(filter(None, [
                game_name.strip() if include_game else "",
                cleaned,
            ]))
            search_url = "https://www.google.com/search?q=" + quote_plus(search_query)
            response = (
                "Gemini API를 사용하지 않는 일반 Google 검색입니다.\n\n"
                f"검색어: `{search_query}`\n\n"
                f"[Google 검색 결과 열기]({search_url})"
            )
            self._append_history("user", cleaned)
            self._append_history("assistant", response)
            return {
                "text": response,
                "response_time_ms": int((time.perf_counter() - start_time) * 1000),
            }

        # New UI modes select an exact model. Legacy "general" retains automatic
        # routing for users upgrading while an old composer modal is still mounted.
        automatic_web_search = (
            question_mode == "general" and bool(AUTO_WEB_PATTERN.search(cleaned))
        )
        use_web_search = question_mode in ("gemini25", "web") or automatic_web_search

        history_for_prompt = self._read_history()[-NUM_HISTORY_MESSAGES:]
        history_lines = [
            f"Current game: {game_name.strip() or 'Unknown'}",
            f"Question mode: {'Web search' if use_web_search else 'General conversation'}",
            "Use the current game only if the question is about a game.",
            "For a weather question without a location, ask for the city or region first.",
            "",
        ]
        for item in history_for_prompt:
            role = item.get("role", "other")
            content = item.get("content", "")
            label = (
                "User"
                if role == "user"
                else "Assistant" if role == "assistant" else "Other"
            )
            history_lines.append(f"{label}: {content}")

        history_lines.append(f"User: {cleaned}")

        if use_web_search:
            history_lines.extend([
                "",
                "MANDATORY: Invoke the Google Search tool before answering.",
                "Use current search results, not model memory. Cite the sources used.",
                "If the tool cannot be invoked, do not guess.",
            ])

        prompt = "\n".join(history_lines)
        request_task = asyncio.create_task(
            self._call_gemini_resilient(
                prompt,
                model=SEARCH_MODEL if use_web_search else MODEL,
                enable_google_search=use_web_search,
            )
        )
        self._request_task = request_task
        self._request_kind = "chat"

        try:
            response = await request_task or ""
            if not response.strip():
                response = (
                    "Gemini가 빈 응답을 반환했습니다. API 키의 모델 사용 권한과 "
                    "무료 사용량을 확인해 주세요."
                )
            decky.logger.debug(f"[chat] model response: {response}")
        except asyncio.CancelledError:
            # Closing the panel intentionally stops work and leaves no partial history.
            decky.logger.info("[chat] Request stopped because the panel closed")
            return {
                "text": "요청이 중간에 취소되었습니다. Decky AI 창을 연 상태에서 다시 시도해 주세요.",
                "response_time_ms": 0,
            }
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
            if e.status_code == 429:
                response = "429 오류: 분당/일일 무료 사용량 소진"
            elif e.status_code == 424:
                response = (
                    "Google 실시간 검색이 실제로 실행되지 않아 답변을 표시하지 않았습니다. "
                    "Gemini API의 Google Search 사용 권한과 무료 한도를 확인해 주세요."
                )
            elif e.status_code == 404 and use_web_search:
                response = (
                    f"2.5 Flash 모델({SEARCH_MODEL})을 API에서 찾지 못했습니다(404). "
                    "현재 API 프로젝트에서 이 모델이 제공되지 않거나 Google의 모델 접근 정책이 "
                    "변경된 상태일 수 있습니다. 3.5 Flash 또는 Google 검색을 선택해 주세요."
                )
            else:
                response = f"API error code: {e.status_code}"
        except Exception as e:
            decky.logger.error(
                f"[chat] model call failed for input: {cleaned} | error: {e}"
            )
            response = (
                "Sorry, there was an error processing your request. Please try again."
            )
        finally:
            if self._request_task is request_task:
                self._request_task = None
                self._request_kind = None

        self._append_history("user", cleaned)
        self._append_history("assistant", response)
        response_time_ms = int((time.perf_counter() - start_time) * 1000)

        return {"text": response, "response_time_ms": response_time_ms}

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        self._history_path().parent.mkdir(parents=True, exist_ok=True)
        self._secrets_path().parent.mkdir(parents=True, exist_ok=True)
        self._load_saved_api_key()
        decky.logger.info(
            f"[chat] Decky AI started idle (log level: {LOGGING_LEVEL})"
        )

    # Function called first during the unload process, utilize this to handle your plugin being stopped, but not
    # completely removed
    async def _unload(self):
        self._panel_session = None
        await self._cancel_active_request()
        await self.cancel_youtube_chapters()
        await self._cancel_recording()
        decky.logger.info("[chat] Plugin unloading")

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
