# Video-to-Clips Pipeline

## Goal

Repeatable workflow: Google Drive link → word-for-word transcript → top 15 viral clips → horizontal + vertical cuts → upload back to Drive.

## Prerequisites

- `ASSEMBLYAI_API_KEY` in `.env` (get from assemblyai.com/dashboard, $50 free credits)
- `VIDEO_CLIPPER_BOT_TOKEN` + `VIDEO_CLIPPER_CHAT_ID` in `.env` (Telegram bot for pipeline updates)
- Google Drive OAuth: first run opens browser for consent, saves token to `workflows/video_clipper/gdrive_token.json`
- ffmpeg 8.0+ (Homebrew)
- Python packages: `assemblyai`, `google-api-python-client`, `google-auth-oauthlib`

## Scripts

| Script | Purpose |
|--------|---------|
| `workflows/video_clipper/video_clipper.py` | Main pipeline: download, transcribe, cut, upload |
| `workflows/video_clipper/gdrive_utils.py` | Google Drive OAuth + file operations |
| `workflows/video_clipper/telegram_utils.py` | Telegram bot: messages, file/video uploads |
| `workflows/video_clipper/clipping_agent_skills.md` | Clip selection criteria + JSON schema |
| `.claude/commands/clip.md` | `/clip` skill for interactive clip identification |

## Typical Workflow

```bash
# Step 1: Download + transcribe
.venv/bin/python workflows/video_clipper/video_clipper.py \
  "https://drive.google.com/file/d/XXXXX/view" --transcribe-only

# Step 2: Identify clips interactively
/clip

# Step 3: Draft-cut top clip for review
.venv/bin/python workflows/video_clipper/video_clipper.py \
  "https://drive.google.com/file/d/XXXXX/view" --draft

# Step 4: Cut all + upload
.venv/bin/python workflows/video_clipper/video_clipper.py \
  "https://drive.google.com/file/d/XXXXX/view" --cut-and-upload
```

## CLI Flags

| Flag | Behavior |
|------|----------|
| (no flags) | Full pipeline — pauses after transcription for `/clip` |
| `--transcribe-only` | Download + transcribe, stop |
| `--draft` | Cut only the #1 virality clip for review |
| `--cut-and-upload` | Cut all clips + upload to Drive |
| `--cut-only` | Cut all clips without uploading |
| `--upload-only` | Upload already-cut clips |
| `--dry-run` | Test OAuth + ffmpeg without processing |

## File Structure

```
.tmp/video_clipper/<video_name>/
  <name>.mp4                    # Downloaded video
  <name>.mp3                    # Extracted audio
  <name>.srt                    # SRT with speaker labels
  <name>_transcript.md          # Full text transcript
  <name>_clips.json             # Clip definitions from /clip
  state.json                    # Pipeline state for resumability
  clips/
    clip_01_title_slug.mp4
    clip_01_title_slug_vertical.mp4
```

## Resumability

`state.json` tracks pipeline progress. Steps: `downloaded` → `transcribed` → `clips_identified` → `cut` → `uploaded`. Re-running any command skips completed steps automatically.

## Telegram Bot Integration

Every pipeline step sends real-time updates to Telegram:

| Event | What's sent |
|-------|-------------|
| Download start/done | Status message with file size |
| Transcribe start/done | Status + SRT file + transcript .md file |
| Clips identified | Ranked clips summary table |
| Draft cut | Draft clip video (horizontal + vertical) |
| All clips cut | Each clip video as it's cut (H + V) + clips.json |
| Upload done | Completion message with Drive folder link |
| Errors | Error details at any step |

Env vars: `VIDEO_CLIPPER_BOT_TOKEN`, `VIDEO_CLIPPER_CHAT_ID`

Telegram file size limit is 50 MB per upload. Clips larger than 50 MB are sent as documents instead of inline video.

## Known Constraints

- AssemblyAI: $0.15/hr transcription. $50 free credits = ~333 hours of video.
- Google Drive OAuth: token expires after ~1 hour, auto-refreshes via refresh_token.
- Vertical crop: Center-crop by default. For side-by-side layouts, set `crop_x` in clips.json per clip.
- Large SRTs: If >3000 lines, the `/clip` skill processes in time-range chunks.

## Changelog

- 2026-02-18: Initial creation. AssemblyAI for transcription, `/clip` for interactive clip selection.
- 2026-02-18: Added Telegram bot integration — real-time updates, transcript delivery, clip video delivery at every step.
