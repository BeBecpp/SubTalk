# Setup

Python 3.11+ (3.12 recommended) and Git on every machine. All commands run from the repository
folder in PowerShell. Dependencies are pinned in `uv.lock` (resolved only from package versions
published before 2026-10-24).

## 1. Development laptop (mock mode)

```powershell
git clone https://github.com/BeBecpp/SubTalk.git
cd SubTalk
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
scripts\start.ps1
```

* Dashboard: <http://127.0.0.1:8000/operator> - passcode `dev-operator` (mock mode only).
* Connect captions with the URL `mock` (built-in strict mock of Zoom's caption endpoint).
* `config/default.yaml` keeps everything safe: `mode: mock`, mock ASR, mock translator,
  `allow_real_zoom: false`, `allow_event_target: false`. Nothing is paid, nothing leaves the laptop.
* To try real Claude translation on the laptop: put `ANTHROPIC_API_KEY=...` in `.env` and create
  `config/local.yaml` with `translate: {engine: claude}`.
* To test the audio path without a GPU: `asr: {engine: scripted}` in `config/local.yaml`, then
  `.venv\Scripts\subtalk.exe replay some_recording.wav` (or run the sender against this laptop).

## 2. Computer B - production engine (NVIDIA GPU)

Prerequisites: a current NVIDIA driver. faster-whisper uses CTranslate2 4.x, which needs the CUDA 12
runtime libraries (cuBLAS, cuDNN 9). If `subtalk doctor` reports missing CUDA DLLs, install them
with `.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` and follow the
faster-whisper README note on adding their `bin` folders to `PATH`.

```powershell
git clone https://github.com/BeBecpp/SubTalk.git
cd SubTalk
git checkout v1.0.0                      # the frozen release tag (see Rollback)
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Engine
copy config\production.example.yaml config\local.yaml   # review it
notepad .env                             # ANTHROPIC_API_KEY, SUBTALK_AUDIO_TOKEN
.venv\Scripts\subtalk.exe set-passcode --role operator
.venv\Scripts\subtalk.exe set-passcode --role speaker
.venv\Scripts\subtalk.exe glossary-check
.venv\Scripts\subtalk.exe doctor         # first run downloads the Whisper model into models\
scripts\start.ps1
```

Optional, elevated PowerShell:

* `scripts\firewall-lan.ps1` - allow port 8000 from the local subnet only.
* `scripts\install-autostart.ps1` - start SubTalk at logon (Task Scheduler, restarts on failure).

Windows settings for the event week (you change these, not SubTalk): High-performance power plan,
sleep off, Windows Update paused until 8 Nov, no GPU driver updates.

Private event files live in `event\` (gitignored):

| File | Purpose |
|---|---|
| `event/event_names.json` | Approved Latin spellings of names (copy the format of `resources/glossary/event_names.example.json`, set `approved: true` after the family confirms) |
| `event/glossary_approvals.json` | `{"reviewer": "...", "approved_term_ids": [...]}` - terms a Turkish JW reviewer signed off |
| `event/phrases.yaml` | Optional override of the manual phrase deck |

## 3. Computer A - audio sender only

```powershell
git clone https://github.com/BeBecpp/SubTalk.git
cd SubTalk
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Sender   # numpy, sounddevice, websockets only
notepad .env                              # SUBTALK_AUDIO_TOKEN - same value as on Computer B
scripts\start-sender.ps1 -List            # find the mixer input
scripts\start-sender.ps1 -Engine ws://<Computer-B-IP>:8000/ws/audio -Device "<mixer name>"
```

The sender opens the input in **shared mode** and never requests exclusive access, never changes
default devices and never plays audio, so Zoom's microphone routing is untouched. Open the
dashboard on Computer A at `http://<Computer-B-IP>:8000/operator`.

## Updating and rollback

```powershell
git fetch --tags
git checkout v1.0.1                       # new release
.venv\Scripts\python.exe -m uv sync --frozen --extra asr --extra dev
# rollback = check out the previous tag and sync again (models\, event\, secrets\, .env are untouched)
git checkout v1.0.0
.venv\Scripts\python.exe -m uv sync --frozen --extra asr --extra dev
```

Freeze the event build on Thursday 5 Nov: tag it, deploy it, and do not update after the dress rehearsal.
