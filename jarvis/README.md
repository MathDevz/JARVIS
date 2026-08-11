# JARVIS — local desktop assistant

**Already installed? Double-click `START_JARVIS.bat`, then open http://127.0.0.1:8742**

Read `START_HERE.txt` if you are stuck.

A privacy-first desktop agent inspired by JARVIS. It runs entirely on your computer: wake-word, speech, planning, computer control, vision, memory, and the control UI. Cloud AI is **not required** and is disabled by default.

This is not a chatbot that tells you what to click. Every computer action goes through a named tool with validation, logging, risk levels, and optional confirmation.

## What it can do

Voice or type (wake word defaults to **JARVIS**):

- `JARVIS, open Discord.`
- `JARVIS, search YouTube for Minecraft Create tutorials.`
- `JARVIS, open Chrome and go to my dashboard.`
- `JARVIS, organize these files into folders.`
- `JARVIS, tell me what is currently on my screen.`
- `JARVIS, launch Minecraft and wait until it finishes loading.`
- `JARVIS, type this message into Discord: on my way.`
- `JARVIS, check my CPU and GPU usage.`
- `JARVIS, run this command in PowerShell Get-Process`

It plans multi-step work, checks whether each step succeeded, and tries a recovery instead of blindly continuing. **JARVIS stop** or `Ctrl+Shift+Escape` immediately aborts computer control.

## Architecture

```
voice  →  NLU / local LLM  →  planner  →  permission gate  →  tools  →  OS
                ↑                               ↓
             memory                         event bus
                ↑                               ↓
             settings  ←——————  desktop / web HUD
```

Modules (all under `jarvis/`):

| Module | Role |
| --- | --- |
| `core` | Config, events, state, logging, emergency stop, hardware probe |
| `security` | Risk levels, confirmation, command/path validation |
| `voice` | Local mic loop, wake word, STT, TTS |
| `ai` | Ollama / llama.cpp / heuristic planner, task plan, agent loop |
| `tools` | One function per capability — never raw OS access from the model |
| `platform` | Windows first; Linux adapter for the same tool API |
| `vision` | Screenshot, Tesseract OCR, OpenCV UI-element heuristics, optional local VLM |
| `memory` | SQLite conversation + durable preferences, editable in the UI |
| `ui` | Local FastAPI HUD (conversation, plan, tools, settings) |

## Requirements

- Windows 10/11 (primary) or Linux (implemented)
- Python 3.10+
- Optional but recommended:
  - [Ollama](https://ollama.com) with a local model (`ollama pull llama3.2`)
  - [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for reading the screen
  - `faster-whisper` or `vosk` for fully local speech recognition
  - A microphone (native via `sounddevice`, or the browser mic in the HUD)

No GPU is required. The heuristic planner handles the example commands without any LLM. A local model improves open-ended requests.

## Install (Windows)

```powershell
cd jarvis
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# optional speech / hotkeys
pip install sounddevice pyttsx3 pynput
# optional local STT (pick one)
pip install faster-whisper
# or: pip install vosk   + download a small model into %APPDATA%\jarvis\models\vosk
```

Install Ollama, then:

```powershell
ollama pull llama3.2
```

Launch:

```powershell
python -m jarvis
```

Open http://127.0.0.1:8742

Single command (no UI):

```powershell
python -m jarvis --once "check my CPU and GPU usage"
python -m jarvis --cli
python -m jarvis --probe
```

`scripts/setup_windows.ps1` does the venv + pip install for you.

## Install (Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m jarvis --probe
python -m jarvis
```

Window/mouse/keyboard tools need an X11 display plus `wmctrl` and `xdotool`. File, terminal, memory, and system tools work headless.

## Configuration

Defaults live in `config/default.yaml`. User overrides are written to:

- Windows: `%APPDATA%\jarvis\config.yaml`
- Linux: `~/.config/jarvis/config.yaml`

Or set `JARVIS_CONFIG` to a file path. Environment overrides use `JARVIS_<section>_<key>`, e.g. `JARVIS_LLM_MODEL=phi3`.

Important keys:

- `wake_word.phrase` — default `jarvis`
- `llm.backend` — `auto` | `ollama` | `openai_compat` | `heuristic`
- `llm.allow_cloud` — **false**. The core refuses well-known cloud hosts unless you flip this.
- `permissions.require_confirmation_for` — default `dangerous`
- `permissions.trusted_tools` / `trusted_apps`
- `permissions.allow_file_delete` / `allow_install` — off by default
- `web.bookmarks.dashboard` — used by “go to my dashboard”
- `hotkeys.emergency_stop` — default `ctrl+shift+escape`
- `paths.workspace_dir` — default folder for “organize these files”

Memories, logs, and screenshots are stored under the platform user-data directory (or `paths.data_dir`). You can inspect, edit, and delete memories in **Settings → Memory**.

## Voice & privacy

- The native listen loop keeps a short in-memory PCM buffer.
- Wake-word matching is local (phrase / aliases). Nothing is uploaded.
- Only the post-wake utterance is sent to local STT.
- If no native STT is installed, the HUD can use the **browser** speech recognizer on the same machine, or you can type.
- TTS uses Windows SAPI (`pyttsx3`), espeak, or local browser voices.

## Tools & safety

The model cannot run arbitrary Python or open a raw shell. It may only call registered tools:

`apps.*` `windows.*` `input.*` `files.*` `terminal.run` `system.*` `media.control` `web.*` `screen.*` `memory.*` `control.wait`

- Paths are resolved and blocked from writing `/etc`, `C:\Windows`, etc.
- Shell strings are scanned for destructive patterns (`rm -rf`, `Remove-Item -Recurse`, `format`, `Invoke-Expression`, package installs, …). Those are **dangerous** and need confirmation.
- Emergency stop is a process-wide flag checked before and during tool execution.

## Tests

```bash
pip install pytest
python -m pytest -q
```

## Hardware probe

On first launch JARVIS records CPU, RAM, GPU/CUDA, Ollama, Tesseract, and speech libraries, then picks backends. Read it in the HUD under **Settings → Hardware** or `python -m jarvis --probe`.

## Project layout

```
jarvis/           Python package
config/           default.yaml
tests/            unit + integration tests
scripts/          Windows setup
```
