"""Telegram Bot utilities for Video Clipper pipeline.

Sends status updates, files (transcripts, clips JSON), and video clips
to a Telegram chat. Uses raw urllib — no extra dependencies.

Env vars: VIDEO_CLIPPER_BOT_TOKEN, VIDEO_CLIPPER_CHAT_ID
"""
import json
import mimetypes
import os
import urllib.request
import uuid


def _get_config():
    """Read bot token and chat ID from env."""
    token = os.environ.get("VIDEO_CLIPPER_BOT_TOKEN", "")
    chat_id = os.environ.get("VIDEO_CLIPPER_CHAT_ID", "")
    return token, chat_id


def _api_url(token, method):
    return f"https://api.telegram.org/bot{token}/{method}"


# ── Text Messages ─────────────────────────────────────────────────────────

def send_message(text, parse_mode="HTML"):
    """Send a text message to the configured Telegram chat.

    Returns True on success, False on failure (never raises).
    """
    token, chat_id = _get_config()
    if not token or not chat_id:
        print(f"[Telegram] No config — skipping: {text[:80]}")
        return False

    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode()

    req = urllib.request.Request(
        _api_url(token, "sendMessage"),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[Telegram] sendMessage failed: {e}")
        return False


# ── File Uploads (documents, videos) ─────────────────────────────────────

def _multipart_encode(fields, files):
    """Build a multipart/form-data body manually (no requests library).

    fields: dict of {name: value} for text fields
    files: list of (field_name, filename, filepath) tuples
    """
    boundary = uuid.uuid4().hex
    lines = []

    for key, val in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        lines.append(b"")
        lines.append(str(val).encode())

    for field_name, filename, filepath in files:
        mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"'.encode()
        )
        lines.append(f"Content-Type: {mime}".encode())
        lines.append(b"")
        with open(filepath, "rb") as f:
            lines.append(f.read())

    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def send_document(filepath, caption=None):
    """Send a file as a Telegram document (up to 50 MB).

    Good for: transcripts (.md, .srt), clips JSON, CSVs.
    Returns True on success.
    """
    token, chat_id = _get_config()
    if not token or not chat_id:
        print(f"[Telegram] No config — skipping document: {filepath}")
        return False

    filename = os.path.basename(filepath)
    size_mb = os.path.getsize(filepath) / 1024 / 1024

    if size_mb > 50:
        print(f"[Telegram] File too large for document upload ({size_mb:.1f} MB): {filename}")
        send_message(f"File too large to send via Telegram ({size_mb:.1f} MB): {filename}")
        return False

    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption[:1024]  # Telegram caption limit
        fields["parse_mode"] = "HTML"

    files = [("document", filename, filepath)]
    body, content_type = _multipart_encode(fields, files)

    req = urllib.request.Request(
        _api_url(token, "sendDocument"),
        data=body,
        headers={"Content-Type": content_type},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())
        if result.get("ok"):
            print(f"  [Telegram] Sent document: {filename}")
            return True
        else:
            print(f"  [Telegram] sendDocument error: {result}")
            return False
    except Exception as e:
        print(f"  [Telegram] sendDocument failed: {e}")
        return False


def send_video(filepath, caption=None):
    """Send a video file via Telegram (up to 50 MB).

    For clips >50 MB, falls back to send_document.
    Returns True on success.
    """
    token, chat_id = _get_config()
    if not token or not chat_id:
        print(f"[Telegram] No config — skipping video: {filepath}")
        return False

    filename = os.path.basename(filepath)
    size_mb = os.path.getsize(filepath) / 1024 / 1024

    if size_mb > 50:
        print(f"  [Telegram] Video too large ({size_mb:.1f} MB), sending as document...")
        return send_document(filepath, caption=caption)

    fields = {"chat_id": chat_id, "supports_streaming": "true"}
    if caption:
        fields["caption"] = caption[:1024]
        fields["parse_mode"] = "HTML"

    files = [("video", filename, filepath)]
    body, content_type = _multipart_encode(fields, files)

    req = urllib.request.Request(
        _api_url(token, "sendVideo"),
        data=body,
        headers={"Content-Type": content_type},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)  # Videos can take a while
        result = json.loads(resp.read())
        if result.get("ok"):
            print(f"  [Telegram] Sent video: {filename}")
            return True
        else:
            print(f"  [Telegram] sendVideo error: {result}")
            return False
    except Exception as e:
        print(f"  [Telegram] sendVideo failed: {e}")
        return False


# ── Pipeline-Specific Helpers ─────────────────────────────────────────────

def notify_step(step_name, video_name, details=""):
    """Send a formatted pipeline step notification."""
    emoji_map = {
        "download_start": "arrow_down",
        "download_done": "white_check_mark",
        "transcribe_start": "studio_microphone",
        "transcribe_done": "white_check_mark",
        "clips_identified": "scissors",
        "draft_cut": "clapper",
        "cutting_start": "hourglass_flowing_sand",
        "cutting_done": "white_check_mark",
        "upload_start": "arrow_up",
        "upload_done": "white_check_mark",
        "error": "x",
    }

    step_labels = {
        "download_start": "Downloading from Google Drive",
        "download_done": "Download complete",
        "transcribe_start": "Transcribing with AssemblyAI",
        "transcribe_done": "Transcription complete",
        "clips_identified": "Clips identified",
        "draft_cut": "Draft clip cut",
        "cutting_start": "Cutting all clips",
        "cutting_done": "All clips cut",
        "upload_start": "Uploading to Google Drive",
        "upload_done": "Upload complete",
        "error": "Error",
    }

    label = step_labels.get(step_name, step_name)
    msg = f"<b>Video Clipper</b>\n<b>{label}</b>\n{video_name}"
    if details:
        msg += f"\n\n{details}"

    return send_message(msg)


def send_clips_summary(clips, video_name):
    """Send a formatted summary of identified clips."""
    if not clips:
        return send_message(f"<b>Video Clipper</b>\nNo clips identified for {video_name}")

    lines = [f"<b>Video Clipper — {len(clips)} Clips Identified</b>"]
    lines.append(f"<i>{video_name}</i>\n")

    for clip in clips:
        score = clip.get("virality_score", "?")
        title = clip.get("title", "Untitled")
        start = clip.get("start_time", 0)
        end = clip.get("end_time", 0)

        # Format timestamps
        s_min, s_sec = divmod(int(start), 60)
        e_min, e_sec = divmod(int(end), 60)
        duration = end - start

        lines.append(
            f"<b>#{clip['id']}</b> [{score}/10] {title}\n"
            f"  {s_min}:{s_sec:02d}–{e_min}:{e_sec:02d} ({duration:.0f}s) | {clip.get('platform', '?')}"
        )

    msg = "\n".join(lines)
    return send_message(msg)


if __name__ == "__main__":
    # Quick test
    _dir = os.path.dirname(os.path.abspath(__file__))

    # Load .env from same directory
    env_path = os.path.join(_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    ok = send_message("Telegram utils test — connection OK!")
    print(f"Test message: {'OK' if ok else 'FAILED'}")
