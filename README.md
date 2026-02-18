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

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in your API keys
cp .env.example .env

# Place your Google Cloud OAuth client secret JSON in this directory
# (filename must match: client_secret_*.json)

# Test everything works
python video_clipper.py --dry-run
```

### Google Drive OAuth

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the Google Drive API
3. Create an OAuth 2.0 Client ID (Desktop app)
4. Download the client secret JSON into this directory
5. On first run, a browser window will open for OAuth consent

### Telegram Bot (optional)

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot)
3. Add `VIDEO_CLIPPER_BOT_TOKEN` and `VIDEO_CLIPPER_CHAT_ID` to `.env`

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
└── .tmp/                     # Work directories (gitignored)
    └── <video_slug>/
        ├── state.json
        ├── *.srt
        ├── *_transcript.md
        ├── *_clips.json
        └── clips/
            └── clip_01_*.mp4
```
