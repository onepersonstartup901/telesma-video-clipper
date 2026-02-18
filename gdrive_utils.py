"""Google Drive utilities — OAuth, download, upload, folder ops.

Uses installed-app OAuth2 flow with an OAuth client secret JSON.
Token is saved to gdrive_token.json (gitignored).
"""
import io
import os
import re
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Scopes: read + write files in Drive
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Paths
_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = _DIR
_TOKEN_PATH = os.path.join(_DIR, "gdrive_token.json")

# Find the client secret JSON in repo root
_CLIENT_SECRET_PATTERN = re.compile(r"^client_secret_.*\.json$")


def _find_client_secret():
    """Find the OAuth client secret JSON in the repo root."""
    for f in os.listdir(_REPO_ROOT):
        if _CLIENT_SECRET_PATTERN.match(f):
            return os.path.join(_REPO_ROOT, f)
    raise FileNotFoundError(
        "No client_secret_*.json found in repo root. "
        "Download one from Google Cloud Console."
    )


def authenticate():
    """Authenticate with Google Drive via OAuth2 installed-app flow.

    Returns a googleapiclient service object.
    """
    creds = None
    if os.path.exists(_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secret = _find_client_secret()
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save token for next run
        with open(_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def extract_file_id(url):
    """Extract Google Drive file ID from various URL formats.

    Supports:
      - https://drive.google.com/file/d/XXXX/view
      - https://drive.google.com/open?id=XXXX
      - https://docs.google.com/document/d/XXXX/edit
      - Raw file ID string
    """
    # /d/FILE_ID/ pattern
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # ?id=FILE_ID pattern
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # Assume raw file ID if no slashes
    if "/" not in url and len(url) > 10:
        return url
    raise ValueError(f"Cannot extract file ID from: {url}")


def get_file_metadata(service, file_id):
    """Get file name, mime type, size, and parent folder."""
    meta = (
        service.files()
        .get(fileId=file_id, fields="id,name,mimeType,size,parents")
        .execute()
    )
    return meta


def get_parent_folder(service, file_id):
    """Get the parent folder ID of a file."""
    meta = get_file_metadata(service, file_id)
    parents = meta.get("parents", [])
    return parents[0] if parents else None


def download_file(service, file_id, dest_path, progress_callback=None):
    """Download a file from Google Drive with progress logging.

    Handles large files (>2GB) via chunked download.
    """
    request = service.files().get_media(fileId=file_id)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status and progress_callback:
                progress_callback(status.progress())
            elif status:
                pct = int(status.progress() * 100)
                print(f"  Download: {pct}%")

    print(f"  Downloaded: {dest_path} ({os.path.getsize(dest_path) / 1024 / 1024:.1f} MB)")
    return dest_path


def create_folder(service, folder_name, parent_id=None):
    """Create a folder in Drive. Deduplicates by name within parent.

    Returns the folder ID.
    """
    # Check if folder already exists
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id,name)").execute()
    existing = results.get("files", [])
    if existing:
        print(f"  Folder '{folder_name}' already exists: {existing[0]['id']}")
        return existing[0]["id"]

    # Create new folder
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    print(f"  Created folder '{folder_name}': {folder['id']}")
    return folder["id"]


def upload_file(service, local_path, parent_id=None, mime_type=None):
    """Upload a file to Google Drive with resumable upload.

    Returns the uploaded file's metadata dict.
    """
    filename = os.path.basename(local_path)

    if mime_type is None:
        # Auto-detect common types
        ext = os.path.splitext(filename)[1].lower()
        mime_map = {
            ".mp4": "video/mp4",
            ".mp3": "audio/mpeg",
            ".srt": "text/plain",
            ".md": "text/markdown",
            ".json": "application/json",
            ".txt": "text/plain",
            ".csv": "text/csv",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

    metadata = {"name": filename}
    if parent_id:
        metadata["parents"] = [parent_id]

    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True)
    file_obj = (
        service.files()
        .create(body=metadata, media_body=media, fields="id,name,webViewLink")
        .execute()
    )

    size_mb = os.path.getsize(local_path) / 1024 / 1024
    print(f"  Uploaded: {filename} ({size_mb:.1f} MB) → {file_obj.get('webViewLink', file_obj['id'])}")
    return file_obj


if __name__ == "__main__":
    # Quick test: authenticate and list root files
    svc = authenticate()
    results = svc.files().list(pageSize=5, fields="files(id, name)").execute()
    items = results.get("files", [])
    print("Drive connection OK. Recent files:")
    for item in items:
        print(f"  {item['name']} ({item['id']})")
