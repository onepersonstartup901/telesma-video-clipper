# Telesma Video Clipper

Video-to-Clips pipeline that turns long-form video into short viral clips.

**Pipeline:** Video → AssemblyAI transcribe → identify viral moments → parallel ffmpeg cuts → auto-upload to Google Drive (public link) → Telegram notifications

## Features

- **Speaker-labeled transcription** via AssemblyAI (SRT + Markdown)
- **Parallel ffmpeg cuts** — 4 workers by default, configurable with `--workers`
- **Background Telegram notifications** — non-blocking, sends each clip as it's cut
- **Auto-upload to Google Drive** with public sharing link
- **Resume-safe** — state tracked in `state.json`, skips completed steps on re-run
- **Original or vertical (9:16)** aspect ratio cuts

## First-Time Setup (macOS)

### 1. Install system dependencies

```bash
# Install Homebrew (skip if you already have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python, ffmpeg, and Node.js
brew install python@3.12 ffmpeg node
```

### 2. Clone and set up the project

```bash
git clone https://github.com/onepersonstartup901/telesma-video-clipper.git
cd telesma-video-clipper

# Create virtual environment and install Python packages
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. API keys

```bash
cp .env.example .env
```

Edit `.env` and fill in your `ASSEMBLYAI_API_KEY` (get one free at [assemblyai.com/dashboard](https://www.assemblyai.com/dashboard) — $50 free credits = ~333 hours of transcription).

### 4. Google Drive OAuth

You should have received a `client_secret_*.json` file. Place it in this directory (the repo root). The filename must match the pattern `client_secret_*.json`.

Then run a dry-run to trigger the browser OAuth consent:

```bash
.venv/bin/python video_clipper.py --dry-run
```

A browser window will open asking you to authorize Google Drive access. After you approve, a `gdrive_token.json` is saved locally and you won't need to do this again.

### 5. Install Claude Code (for AI clip selection)

```bash
npm install -g @anthropic-ai/claude-code
```

Then open Claude Code inside the project:

```bash
cd telesma-video-clipper
claude
```

The `/clip` slash command is auto-detected from `.claude/commands/clip.md`. Type `/clip` after transcribing a video to interactively identify viral moments.

### 6. Telegram Bot (optional)

Pipeline notifications via Telegram. Skip this if you don't need them — the pipeline works without it.

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot)
3. Add `VIDEO_CLIPPER_BOT_TOKEN` and `VIDEO_CLIPPER_CHAT_ID` to `.env`

### 7. Verify everything works

```bash
.venv/bin/python video_clipper.py --dry-run
```

You should see confirmation that OAuth, ffmpeg, and (optionally) Telegram are working.

## Usage

```bash
# From Google Drive
python video_clipper.py "https://drive.google.com/file/d/XXXXX/view"

# From a local file
python video_clipper.py --local "path/to/video.mp4"

# Transcribe only (stop before cutting)
python video_clipper.py --local "video.mp4" --transcribe-only

# Cut all clips + upload to Drive (original aspect ratio)
python video_clipper.py --local "video.mp4" --cut-and-upload --no-vertical

# Cut only (no upload)
python video_clipper.py --local "video.mp4" --cut-only --no-vertical

# Upload already-cut clips
python video_clipper.py --local "video.mp4" --upload-only

# Control parallelism
python video_clipper.py --local "video.mp4" --cut-and-upload --workers 8
```

## Pipeline Steps

| Step | What happens |
|------|-------------|
| **1. Ingest** | Download from Google Drive or symlink local file |
| **2. Transcribe** | Extract audio → AssemblyAI transcription → SRT + Markdown |
| **3. Identify clips** | Manual step — use the `/clip` skill or edit `*_clips.json` |
| **4. Cut** | Parallel ffmpeg cuts (horizontal + optional vertical 9:16) |
| **5. Upload** | Upload to Google Drive, set public permissions, send link via Telegram |

## Clip Identification

After transcription, the pipeline pauses for clip identification. Create a `*_clips.json` file in the work directory (`.tmp/<video_slug>/`) with this format:

```json
{
  "clips": [
    {
      "id": 1,
      "title": "Clip Title",
      "start_time": 311.0,
      "end_time": 451.0,
      "hook_quote": "Opening line...",
      "virality_score": 9,
      "platform": "TikTok",
      "category": "story"
    }
  ]
}
```

See [clip_command.md](clip_command.md) and [clipping_agent_skills.md](clipping_agent_skills.md) for the AI-assisted clip identification criteria.

## CLI Flags

| Flag | Description |
|------|-------------|
| `--local <path>` | Use a local video file (skip Drive download) |
| `--transcribe-only` | Stop after transcription |
| `--draft` | Cut only the top-scoring clip for review |
| `--cut-only` | Cut all clips without uploading |
| `--cut-and-upload` | Cut all clips and upload to Drive |
| `--upload-only` | Upload already-cut clips |
| `--no-vertical` | Skip 9:16 vertical cuts, keep original aspect ratio |
| `--workers N` | Number of parallel ffmpeg workers (default: 4) |
| `--dry-run` | Test OAuth + API connections |

## File Structure

```
.
├── video_clipper.py          # Main pipeline script
├── gdrive_utils.py           # Google Drive OAuth + upload/download
├── telegram_utils.py         # Telegram bot notifications
├── clip_command.md           # /clip skill prompt for AI clip identification
├── clipping_agent_skills.md  # Scoring criteria for viral moments
├── video_clipper.md          # Pipeline directive / SOP
├── requirements.txt
├── .env.example
├── .claude/
│   └── commands/
│       └── clip.md           # Claude Code slash command (auto-detected)
└── .tmp/                     # Work directories (gitignored)
    └── <video_slug>/
        ├── state.json
        ├── *.srt
        ├── *_transcript.md
        ├── *_clips.json
        └── clips/
            └── clip_01_*.mp4
```
