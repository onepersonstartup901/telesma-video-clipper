#!/usr/bin/env python3
"""Video-to-Clips Pipeline — Download, transcribe, cut, upload.

Usage:
  # Download + transcribe only
  python video_clipper.py "https://drive.google.com/file/d/XXXXX/view" --transcribe-only

  # Draft-cut top clip for review
  python video_clipper.py "https://drive.google.com/file/d/XXXXX/view" --draft

  # Cut all clips + upload to Drive
  python video_clipper.py "https://drive.google.com/file/d/XXXXX/view" --cut-and-upload

  # Full pipeline (download → transcribe → wait for /clip → cut all → upload)
  python video_clipper.py "https://drive.google.com/file/d/XXXXX/view"

  # Dry run (test OAuth only)
  python video_clipper.py --dry-run
"""
import argparse
import json
import os
import queue as queue_mod
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

# Add parent dirs so we can import gdrive_utils when run from repo root
_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

import assemblyai as aai

from gdrive_utils import (
    authenticate,
    create_folder,
    download_file,
    extract_file_id,
    get_file_metadata,
    get_parent_folder,
    upload_file,
)
from telegram_utils import (
    notify_step,
    send_clips_summary,
    send_document,
    send_message,
    send_video,
)

# ── Paths & Config ────────────────────────────────────────────────────────
_REPO_ROOT = _DIR
_TMP_BASE = os.path.join(_REPO_ROOT, ".tmp")


def _load_env():
    """Load .env from repo root if present."""
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _slugify(text, max_len=40):
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:max_len].rstrip("_")


def _format_time(seconds):
    """Format seconds as HH:MM:SS or MM:SS."""
    td = timedelta(seconds=int(seconds))
    total = int(td.total_seconds())
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _format_srt_time(seconds):
    """Format seconds as SRT timestamp: HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ── State Management ──────────────────────────────────────────────────────

def _get_work_dir(video_name):
    """Get or create the working directory for a video."""
    slug = _slugify(video_name, max_len=60)
    work_dir = os.path.join(_TMP_BASE, slug)
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(os.path.join(work_dir, "clips"), exist_ok=True)
    return work_dir


def _load_state(work_dir):
    """Load pipeline state from state.json."""
    state_path = os.path.join(work_dir, "state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return {}


def _save_state(work_dir, state):
    """Save pipeline state to state.json."""
    state_path = os.path.join(work_dir, "state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# ── Step 0: Local File Ingest ────────────────────────────────────────────

def step_local_ingest(local_path):
    """Set up work directory from a local video file. Returns (work_dir, state)."""
    print("\n=== Step 0: Local File Ingest ===")

    local_path = os.path.abspath(local_path)
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local file not found: {local_path}")

    video_name = os.path.basename(local_path)
    size_mb = os.path.getsize(local_path) / 1024 / 1024
    print(f"  File: {video_name}")
    print(f"  Size: {size_mb:.1f} MB")

    work_dir = _get_work_dir(os.path.splitext(video_name)[0])
    state = _load_state(work_dir)

    # Skip if already ingested
    if state.get("step") in ("downloaded", "transcribed", "clips_identified", "cut", "uploaded"):
        if os.path.exists(state.get("video_path", "")):
            print(f"  Already ingested: {state['video_path']}")
            return work_dir, state

    # Symlink or copy into work dir
    dest_path = os.path.join(work_dir, video_name)
    if not os.path.exists(dest_path):
        os.symlink(local_path, dest_path)
        print(f"  Linked: {dest_path}")

    notify_step("download_done", video_name, f"Local file ({size_mb:.1f} MB)")

    state.update({
        "step": "downloaded",
        "drive_url": None,
        "drive_file_id": None,
        "drive_parent_id": None,
        "video_name": video_name,
        "video_path": dest_path,
        "local_source": local_path,
    })
    _save_state(work_dir, state)

    return work_dir, state


# ── Step 1: Download ─────────────────────────────────────────────────────

def step_download(drive_url):
    """Download video from Google Drive. Returns (work_dir, state)."""
    print("\n=== Step 1: Download from Google Drive ===")

    service = authenticate()
    file_id = extract_file_id(drive_url)
    meta = get_file_metadata(service, file_id)
    video_name = meta["name"]
    parent_id = get_parent_folder(service, file_id)
    size_mb = int(meta.get("size", 0)) / 1024 / 1024

    print(f"  File: {video_name}")
    print(f"  Size: {size_mb:.1f} MB")

    work_dir = _get_work_dir(os.path.splitext(video_name)[0])
    state = _load_state(work_dir)

    # Skip if already downloaded
    video_path = os.path.join(work_dir, video_name)
    if state.get("step") in ("downloaded", "transcribed", "clips_identified", "cut", "uploaded"):
        if os.path.exists(state.get("video_path", "")):
            print(f"  Already downloaded: {state['video_path']}")
            return work_dir, state

    notify_step("download_start", video_name, f"Size: {size_mb:.1f} MB")

    download_file(service, file_id, video_path)

    state.update({
        "step": "downloaded",
        "drive_url": drive_url,
        "drive_file_id": file_id,
        "drive_parent_id": parent_id,
        "video_name": video_name,
        "video_path": video_path,
    })
    _save_state(work_dir, state)

    notify_step("download_done", video_name, f"{size_mb:.1f} MB downloaded")

    return work_dir, state


# ── Step 2: Transcribe ───────────────────────────────────────────────────

def step_transcribe(work_dir, state):
    """Extract audio, transcribe with AssemblyAI, generate SRT + markdown."""
    print("\n=== Step 2: Transcribe via AssemblyAI ===")

    video_name = state.get("video_name", "Unknown")

    if state.get("step") in ("transcribed", "clips_identified", "cut", "uploaded"):
        print(f"  Already transcribed. SRT: {state.get('srt_path')}")
        return state

    video_path = state["video_path"]
    base_name = os.path.splitext(os.path.basename(video_path))[0]

    # Extract audio as MP3
    audio_path = os.path.join(work_dir, f"{base_name}.mp3")
    if not os.path.exists(audio_path):
        print("  Extracting audio...")
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
            "-y", audio_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ffmpeg error: {result.stderr[-500:]}")
            notify_step("error", video_name, "Audio extraction failed (ffmpeg error)")
            raise RuntimeError("Audio extraction failed")
        print(f"  Audio: {audio_path} ({os.path.getsize(audio_path) / 1024 / 1024:.1f} MB)")
    else:
        print(f"  Audio already extracted: {audio_path}")

    # Transcribe with AssemblyAI
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        notify_step("error", video_name, "ASSEMBLYAI_API_KEY not set in .env")
        raise RuntimeError("ASSEMBLYAI_API_KEY not set in .env")

    aai.settings.api_key = api_key

    notify_step("transcribe_start", video_name, "Uploading audio to AssemblyAI...")

    print("  Uploading to AssemblyAI and transcribing (this may take a few minutes)...")
    config = aai.TranscriptionConfig(
        speaker_labels=True,
        speech_models=["universal-3-pro"],
    )

    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(audio_path, config=config)

    if transcript.status == aai.TranscriptStatus.error:
        notify_step("error", video_name, f"Transcription failed: {transcript.error}")
        raise RuntimeError(f"Transcription failed: {transcript.error}")

    word_count = len(transcript.words)
    utt_count = len(transcript.utterances)
    duration_sec = transcript.words[-1].end / 1000.0 if transcript.words else 0

    print(f"  Transcription complete. {word_count} words, {utt_count} utterances.")

    # Build SRT from utterances (with speaker labels)
    srt_path = os.path.join(work_dir, f"{base_name}.srt")
    _build_srt(transcript, srt_path)
    print(f"  SRT: {srt_path}")

    # Build markdown transcript
    md_path = os.path.join(work_dir, f"{base_name}_transcript.md")
    _build_transcript_md(transcript, md_path, base_name)
    print(f"  Transcript: {md_path}")

    # Upload transcript + SRT to Drive
    service = authenticate()
    parent_id = state.get("drive_parent_id")
    if parent_id:
        print("  Uploading transcript files to Drive...")
        upload_file(service, srt_path, parent_id)
        upload_file(service, md_path, parent_id)

    state.update({
        "step": "transcribed",
        "srt_path": srt_path,
        "transcript_path": md_path,
        "audio_path": audio_path,
        "word_count": word_count,
        "utterance_count": utt_count,
    })
    _save_state(work_dir, state)

    # Count unique speakers
    speakers = set()
    for u in transcript.utterances:
        if u.speaker:
            speakers.add(u.speaker)

    # Telegram: send completion notification + transcript files
    notify_step(
        "transcribe_done", video_name,
        f"Duration: {_format_time(duration_sec)}\n"
        f"Words: {word_count:,}\n"
        f"Utterances: {utt_count}\n"
        f"Speakers: {len(speakers)}\n\n"
        f"Run <code>/clip</code> to identify viral clips."
    )
    send_document(srt_path, caption=f"SRT — {video_name}")
    send_document(md_path, caption=f"Transcript — {video_name}")

    print(f"\n  Transcription complete. Run /clip to identify viral clips.")
    return state


def _build_srt(transcript, srt_path):
    """Build SRT file from AssemblyAI transcript utterances."""
    with open(srt_path, "w") as f:
        for i, utterance in enumerate(transcript.utterances, 1):
            start_sec = utterance.start / 1000.0
            end_sec = utterance.end / 1000.0
            speaker = utterance.speaker or "?"

            f.write(f"{i}\n")
            f.write(f"{_format_srt_time(start_sec)} --> {_format_srt_time(end_sec)}\n")
            f.write(f"[Speaker {speaker}] {utterance.text}\n\n")


def _build_transcript_md(transcript, md_path, title):
    """Build a readable markdown transcript with speaker labels and timestamps."""
    with open(md_path, "w") as f:
        f.write(f"# Transcript: {title}\n\n")

        duration_sec = transcript.words[-1].end / 1000.0 if transcript.words else 0
        f.write(f"**Duration:** {_format_time(duration_sec)}\n")

        # Count unique speakers
        speakers = set()
        for u in transcript.utterances:
            if u.speaker:
                speakers.add(u.speaker)
        f.write(f"**Speakers:** {len(speakers)}\n\n---\n\n")

        for utterance in transcript.utterances:
            start_sec = utterance.start / 1000.0
            speaker = utterance.speaker or "?"
            timestamp = _format_time(start_sec)
            f.write(f"**[{timestamp}] Speaker {speaker}:** {utterance.text}\n\n")


# ── Step 3: Clip Identification (via /clip skill — manual) ───────────────

def _load_clips(work_dir):
    """Load clips.json from work dir."""
    # Find the clips JSON
    for f in os.listdir(work_dir):
        if f.endswith("_clips.json"):
            clips_path = os.path.join(work_dir, f)
            with open(clips_path) as fh:
                data = json.load(fh)
            return data.get("clips", []), clips_path

    raise FileNotFoundError(
        "No *_clips.json found. Run /clip first to identify clips."
    )


# ── Step 4: FFmpeg Cutting ────────────────────────────────────────────────

def _cut_clip(video_path, clip, output_dir, skip_vertical=False):
    """Cut a single clip — horizontal and optionally vertical versions.

    Uses two-pass seeking for frame-accurate cuts:
    - First -ss seeks roughly (fast, keyframe-based)
    - Second -ss refines from that point (accurate)
    """
    clip_id = clip["id"]
    title_slug = _slugify(clip.get("title", f"clip_{clip_id:02d}"))
    base_name = f"clip_{clip_id:02d}_{title_slug}"

    start = clip["start_time"]
    end = clip["end_time"]
    duration = end - start

    # Seek 2 seconds early for keyframe alignment, then refine
    pre_seek = max(0, start - 2)
    fine_seek = start - pre_seek

    horiz_path = os.path.join(output_dir, f"{base_name}.mp4")
    vert_path = os.path.join(output_dir, f"{base_name}_vertical.mp4")

    # Horizontal cut (source resolution, high quality for Drive)
    if not os.path.exists(horiz_path):
        cmd_h = [
            "ffmpeg",
            "-ss", str(pre_seek),
            "-i", video_path,
            "-ss", str(fine_seek),
            "-t", str(duration),
            "-c:v", "libx264", "-c:a", "aac",
            "-crf", "18",
            "-avoid_negative_ts", "make_zero",
            "-y", horiz_path,
        ]
        result = subprocess.run(cmd_h, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ERROR (horizontal): {result.stderr[-300:]}")
            return None, None
    else:
        print(f"    Horizontal already exists, skipping")

    if skip_vertical:
        return horiz_path, None

    # Vertical 9:16 cut (center crop)
    if not os.path.exists(vert_path):
        # Allow per-clip crop_x override
        crop_x = clip.get("crop_x")
        if crop_x is not None:
            crop_filter = f"crop=ih*9/16:ih:{crop_x}:0,scale=1080:1920"
        else:
            crop_filter = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"

        cmd_v = [
            "ffmpeg",
            "-ss", str(pre_seek),
            "-i", video_path,
            "-ss", str(fine_seek),
            "-t", str(duration),
            "-vf", crop_filter,
            "-c:v", "libx264", "-c:a", "aac",
            "-crf", "23",
            "-avoid_negative_ts", "make_zero",
            "-y", vert_path,
        ]
        result = subprocess.run(cmd_v, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    ERROR (vertical): {result.stderr[-300:]}")
            return horiz_path, None
    else:
        print(f"    Vertical already exists, skipping")

    return horiz_path, vert_path


def _make_telegram_copy(clip_path, tg_dir):
    """Create a 720p downscaled copy of a clip for Telegram.

    If the source is already 720p or smaller, copies at CRF 23 without scaling.
    Returns the path to the Telegram-ready file.
    """
    os.makedirs(tg_dir, exist_ok=True)
    out_path = os.path.join(tg_dir, os.path.basename(clip_path))
    if os.path.exists(out_path):
        return out_path

    # Probe source height
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=height", "-of", "csv=p=0", clip_path],
        capture_output=True, text=True,
    )
    try:
        src_height = int(probe.stdout.strip())
    except (ValueError, AttributeError):
        src_height = 9999  # Assume large if probe fails

    vf = "scale=-2:720" if src_height > 720 else None

    cmd = ["ffmpeg", "-i", clip_path]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-c:a", "aac",
        "-crf", "23",
        "-y", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [Telegram copy] ERROR: {result.stderr[-200:]}")
        return clip_path  # Fall back to full-res file
    return out_path


def step_cut_draft(work_dir, state, skip_vertical=False):
    """Cut ONLY the top-scoring clip as a draft for review."""
    print("\n=== Step 4a: Draft Cut (top clip only) ===")

    video_name = state.get("video_name", "Unknown")
    clips, clips_path = _load_clips(work_dir)
    if not clips:
        print("  No clips found in clips.json")
        return state

    # Sort by virality_score descending, take #1
    clips_sorted = sorted(clips, key=lambda c: c.get("virality_score", 0), reverse=True)
    top_clip = clips_sorted[0]

    video_path = state["video_path"]
    output_dir = os.path.join(work_dir, "clips")

    title = top_clip.get("title", "Untitled")
    time_range = f"{_format_time(top_clip['start_time'])}–{_format_time(top_clip['end_time'])}"
    print(f"  Cutting draft: #{top_clip['id']} \"{title}\" ({time_range})")

    notify_step("draft_cut", video_name,
                f"Cutting draft: #{top_clip['id']} \"{title}\"\n"
                f"Time: {time_range}\n"
                f"Score: {top_clip.get('virality_score', '?')}/10")

    h_path, v_path = _cut_clip(video_path, top_clip, output_dir, skip_vertical=skip_vertical)

    if h_path:
        print(f"\n  Draft ready for review:")
        print(f"    Horizontal: {h_path}")
        if v_path:
            print(f"    Vertical:   {v_path}")
        print(f"\n  If it looks good, run with --cut-and-upload to batch all clips.")

        # Send draft clips via Telegram
        send_video(h_path, caption=f"<b>Draft clip (horizontal)</b>\n#{top_clip['id']} {title}\n{time_range}")
        if v_path:
            send_video(v_path, caption=f"<b>Draft clip (vertical 9:16)</b>\n#{top_clip['id']} {title}\n{time_range}")
    else:
        print(f"  Draft cut failed. Check ffmpeg output above.")
        notify_step("error", video_name, f"Draft cut failed for clip #{top_clip['id']}")

    return state


def step_cut_all(work_dir, state, skip_vertical=False, max_workers=4):
    """Cut all clips in parallel using ThreadPoolExecutor.

    ffmpeg processes run concurrently (max_workers at a time).
    Telegram sends happen in a background thread so they don't block cutting.
    """
    print("\n=== Step 4b: Cut All Clips ===")

    video_name = state.get("video_name", "Unknown")
    clips, clips_path = _load_clips(work_dir)
    if not clips:
        print("  No clips found.")
        return state

    video_path = state["video_path"]
    output_dir = os.path.join(work_dir, "clips")
    total = len(clips)

    # Send clips.json to Telegram
    send_document(clips_path, caption=f"Clip definitions — {video_name}")
    send_clips_summary(clips, video_name)

    mode = "horizontal only" if skip_vertical else "horizontal + vertical"
    notify_step("cutting_start", video_name,
                f"Cutting {total} clips in parallel ({mode}, {max_workers} workers)...")

    # ── Background Telegram sender (non-blocking) ──
    tg_queue = queue_mod.Queue()
    tg_done = threading.Event()

    def _telegram_sender():
        while not tg_done.is_set() or not tg_queue.empty():
            try:
                item = tg_queue.get(timeout=0.5)
            except queue_mod.Empty:
                continue
            path, caption = item
            send_video(path, caption=caption)
            tg_queue.task_done()

    tg_thread = threading.Thread(target=_telegram_sender, daemon=True)
    tg_thread.start()

    # ── Parallel ffmpeg cuts ──
    cut_count = 0
    errors = []

    tg_dir = os.path.join(output_dir, "telegram")

    def _cut_one(clip_idx, clip):
        """Wrapper for ThreadPoolExecutor — returns (idx, clip, h, v)."""
        h, v = _cut_clip(video_path, clip, output_dir, skip_vertical=skip_vertical)
        return clip_idx, clip, h, v

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_cut_one, i, clip): i
            for i, clip in enumerate(clips, 1)
        }
        for future in as_completed(futures):
            i, clip, h_path, v_path = future.result()
            title = clip.get("title", f"Clip {clip['id']}")
            start_fmt = _format_time(clip["start_time"])
            end_fmt = _format_time(clip["end_time"])
            duration = clip["end_time"] - clip["start_time"]

            if h_path:
                cut_count += 1
                print(f"  Done {cut_count}/{total}: #{clip['id']} {title} ({start_fmt}–{end_fmt})")
                caption = (
                    f"<b>Clip {cut_count}/{total}</b>\n"
                    f"#{clip['id']} {title}\n"
                    f"{start_fmt}–{end_fmt} ({duration:.0f}s) | "
                    f"{clip.get('virality_score', '?')}/10"
                )
                # Send 720p copy to Telegram, keep full-res for Drive
                tg_path = _make_telegram_copy(h_path, tg_dir)
                tg_queue.put((tg_path, caption))
                if v_path:
                    tg_v = _make_telegram_copy(v_path, tg_dir)
                    tg_queue.put((tg_v, f"{caption}\n(vertical 9:16)"))
            else:
                errors.append(clip["id"])
                print(f"  FAILED: #{clip['id']} {title}")

    # Wait for all Telegram sends to finish
    tg_queue.join()
    tg_done.set()
    tg_thread.join(timeout=5)

    print(f"\n  Cut {cut_count}/{total} clips. Errors: {len(errors)}")
    if errors:
        print(f"  Failed clip IDs: {errors}")

    state["step"] = "cut"
    state["clips_cut"] = cut_count
    _save_state(work_dir, state)

    error_detail = f"\nFailed: {errors}" if errors else ""
    notify_step("cutting_done", video_name,
                f"{cut_count}/{total} clips cut successfully{error_detail}")

    return state


# ── Step 5: Upload to Drive ──────────────────────────────────────────────

def step_upload(work_dir, state):
    """Upload all clips to Google Drive and make the folder publicly accessible.

    For Drive-sourced videos: creates a clips/ subfolder under the original parent.
    For local files: creates a top-level folder named after the video.
    Always sets 'anyone with link can view' permission and sends the link via Telegram.
    """
    print("\n=== Step 5: Upload to Google Drive ===")

    video_name = state.get("video_name", "Unknown")
    parent_id = state.get("drive_parent_id")

    service = authenticate()

    # Create the target folder
    if parent_id:
        # Drive-sourced: create clips/ subfolder under original parent
        clips_folder_id = create_folder(service, "clips", parent_id)
    else:
        # Local file: create a top-level folder named after the video
        base = os.path.splitext(video_name)[0]
        folder_name = f"{base} – Clips"
        clips_folder_id = create_folder(service, folder_name)

    # Upload full-res clip files (skip telegram/ subfolder)
    clips_dir = os.path.join(work_dir, "clips")
    clip_files = sorted([
        f for f in os.listdir(clips_dir)
        if f.endswith(".mp4") and os.path.isfile(os.path.join(clips_dir, f))
    ])

    if not clip_files:
        print("  No clip files to upload.")
        return state

    notify_step("upload_start", video_name, f"Uploading {len(clip_files)} files to Google Drive...")

    print(f"  Uploading {len(clip_files)} clips...")
    for i, filename in enumerate(clip_files, 1):
        filepath = os.path.join(clips_dir, filename)
        print(f"  [{i}/{len(clip_files)}] {filename}")
        upload_file(service, filepath, clips_folder_id)

    # Also upload clips.json for reference
    for f in os.listdir(work_dir):
        if f.endswith("_clips.json"):
            upload_file(service, os.path.join(work_dir, f), clips_folder_id)
            break

    # Make folder public (anyone with link can view)
    service.permissions().create(
        fileId=clips_folder_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
    ).execute()
    print("  Folder set to public (anyone with link).")

    state["step"] = "uploaded"
    state["clips_folder_id"] = clips_folder_id
    state["clips_uploaded"] = len(clip_files)
    _save_state(work_dir, state)

    folder_url = f"https://drive.google.com/drive/folders/{clips_folder_id}?usp=sharing"
    print(f"\n  Upload complete: {len(clip_files)} files to Drive.")
    print(f"  Public link: {folder_url}")

    notify_step("upload_done", video_name,
                f"{len(clip_files)} files uploaded\n\n"
                f"<a href=\"{folder_url}\">Open Drive folder</a>")

    return state


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Video-to-Clips Pipeline")
    parser.add_argument("url", nargs="?", help="Google Drive video URL")
    parser.add_argument("--local", type=str, default=None,
                        help="Path to a local video file (skip Drive download)")
    parser.add_argument("--transcribe-only", action="store_true",
                        help="Download and transcribe only (stop before cutting)")
    parser.add_argument("--draft", action="store_true",
                        help="Cut only the top-scoring clip as a draft")
    parser.add_argument("--cut-and-upload", action="store_true",
                        help="Cut all clips and upload to Drive")
    parser.add_argument("--cut-only", action="store_true",
                        help="Cut all clips without uploading")
    parser.add_argument("--upload-only", action="store_true",
                        help="Upload already-cut clips to Drive")
    parser.add_argument("--no-vertical", action="store_true",
                        help="Skip vertical 9:16 cuts, keep original aspect ratio only")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel ffmpeg workers (default: 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Test OAuth connection only")
    args = parser.parse_args()

    _load_env()

    if args.dry_run:
        print("=== Dry Run: Testing OAuth ===")
        service = authenticate()
        results = service.files().list(pageSize=3, fields="files(id, name)").execute()
        print("Drive connection OK. Recent files:")
        for item in results.get("files", []):
            print(f"  {item['name']}")
        print("\nAssemblyAI key:", "SET" if os.environ.get("ASSEMBLYAI_API_KEY") else "MISSING")
        print("ffmpeg:", end=" ")
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
        print("OK")

        # Test Telegram
        print("Telegram:", end=" ")
        ok = send_message("Video Clipper dry-run — all systems OK!")
        print("OK" if ok else "FAILED (check VIDEO_CLIPPER_BOT_TOKEN / VIDEO_CLIPPER_CHAT_ID)")
        return

    if not args.url and not args.local:
        parser.error("URL or --local <path> required (or use --dry-run)")

    try:
        # Step 1: Download or ingest local file
        if args.local:
            work_dir, state = step_local_ingest(args.local)
        else:
            work_dir, state = step_download(args.url)

        # Step 2: Transcribe
        if state.get("step") == "downloaded" or args.transcribe_only:
            state = step_transcribe(work_dir, state)
            if args.transcribe_only:
                return

        # Step 3: Clips must exist (from /clip skill)
        if args.draft:
            step_cut_draft(work_dir, state, skip_vertical=args.no_vertical)
            return

        if args.upload_only:
            step_upload(work_dir, state)
            return

        if args.cut_and_upload or args.cut_only:
            state = step_cut_all(work_dir, state, skip_vertical=args.no_vertical, max_workers=args.workers)
            if args.cut_and_upload:
                step_upload(work_dir, state)
            return

        # Full pipeline: download → transcribe → stop for /clip
        if state.get("step") == "transcribed":
            print("\n  Pipeline paused. Run /clip to identify clips, then re-run with --cut-and-upload")
            return

        # If clips exist, cut and upload
        if state.get("step") in ("clips_identified", "cut"):
            state = step_cut_all(work_dir, state, skip_vertical=args.no_vertical, max_workers=args.workers)
            step_upload(work_dir, state)

    except Exception as e:
        # Send error to Telegram
        video_name = "Unknown"
        try:
            video_name = state.get("video_name", "Unknown")
        except Exception:
            pass
        notify_step("error", video_name, str(e)[:500])
        raise


if __name__ == "__main__":
    main()
