# Deck Muse

Deck Muse is a Decky plugin that gives in-game help and advice while you play.

It uses a Google Gemini-compatible model (default: Gemma 4 variant) with web search grounding and returns concise answers with sources.

*This app was developed with the help of Github Co-Pilot, particularly for the React parts as this was new to me.*

## Features

- Ask gameplay and strategy questions from the Decky Quick Access panel
- Context-aware prompt prefills using the currently running game
- Chat history persistence across plugin reloads
- Source links included in model responses when available

## Requirements

- Steam Deck with Decky Loader installed: https://decky.xyz/ (tested on v3.2.3 stable)
- A Gemini API key: https://ai.google.dev/gemini-api/docs/api-key
- For local development:
   - Node.js v16.14+
   - pnpm 9
   - Python 3

## Install from Release Zip (Steam Deck)

1. On the Steam Deck, switch to Desktop Mode.
2. Download the latest Deck Muse release zip.
3. Extract the zip.
4. Rename `.env_example` to `.env`.
5. Edit `.env` and set `GEMINI_API_KEY`.
6. Re-compress the plugin folder back into a zip.
7. Return to Gaming Mode.
8. Open Quick Access Menu (three-dot button) and open Decky.
9. In Decky, enable Developer Mode:
	- General -> Other -> Developer Mode
10. Open the Developer menu, choose Install Plugin from Zip File, and select your zip.
11. The plugin should install and then be visible in Decky

## Configuration

Set values in `.env` or `.env_config`:

- `GEMINI_API_KEY` (required)
- `GOOGLE_MODEL` (default: `gemma-4-26b-a4b-it`)
- `MODEL_TIMEOUT_SECONDS` (default: `60`)
- `MODEL_TEMPERATURE` (default: `0.1`)
- `NUM_HISTORY_MESSAGES` (default: `10`)
- `CHAT_LOGGING_LEVEL` (default: `INFO`)

## Local Development

1. Install Node.js v16.14+ and confirm it is available:

	```bash
	node --version
	```

2. Install pnpm 9 (if needed):

	```bash
	sudo npm i -g pnpm@9
	```

3. Install dependencies:

	```bash
	pnpm i
	```

4. Build frontend + vendor Python dependencies:

	```bash
	pnpm run build_all
	```

5. Create distributable zip:

	```bash
	pnpm run zip:app
	```

Useful commands:

- `pnpm run py:deps:vendor` to refresh vendored Python dependencies

## Troubleshooting

- Error about missing API key:
  - Ensure `.env` exists and includes `GEMINI_API_KEY=...`
- Plugin installs but does not answer:
  - Check network access and verify model/env settings

## License

BSD-3-Clause. See `LICENSE`.
