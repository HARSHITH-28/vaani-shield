"""Short-lived, owner-bound storage for assistant audio uploads/artifacts."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from threading import Event, RLock, Thread
import time
from uuid import UUID, uuid4

from services.principal import CustomerPrincipal


MAX_AUDIO_SIZE = 15 * 1024 * 1024
UPLOAD_TTL = timedelta(minutes=15)
ARTIFACT_TTL = timedelta(minutes=15)
UPLOAD_DIR = Path(tempfile.gettempdir()) / "vaanishield-audio-uploads"

_UPLOADS: dict[UUID, dict] = {}
_ARTIFACTS: dict[UUID, dict] = {}
_LOCK = RLock()
_CLEANUP_STOP = Event()
_CLEANUP_THREAD: Thread | None = None

_FORMATS = {
    ".wav": ({"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}, "audio/wav"),
    ".mp3": ({"audio/mpeg", "audio/mp3", "application/octet-stream"}, "audio/mpeg"),
    ".ogg": ({"audio/ogg", "application/ogg", "application/octet-stream"}, "audio/ogg"),
    ".webm": ({"audio/webm", "application/octet-stream"}, "audio/webm"),
}


class AudioUploadError(ValueError):
    """Safe client error for invalid, unknown, or inaccessible uploads."""


class AudioTooLargeError(AudioUploadError):
    pass


class UnsupportedAudioFormatError(AudioUploadError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _safe_unlink(path: Path, root: Path):
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        if resolved_path.parent == resolved_root:
            resolved_path.unlink(missing_ok=True)
    except (OSError, RuntimeError):
        # Cleanup is best-effort; never follow a path outside our owned directory.
        return


def _matches_container(suffix: str, content: bytes) -> bool:
    if suffix == ".wav":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    if suffix == ".ogg":
        return content.startswith(b"OggS")
    if suffix == ".webm":
        return content.startswith(b"\x1a\x45\xdf\xa3")
    if suffix == ".mp3":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    return False


def store_audio_upload(
    principal: CustomerPrincipal, filename: str | None, content_type: str | None, content: bytes
):
    if not isinstance(principal, CustomerPrincipal):
        raise AudioUploadError("Authenticated audio upload is required.")
    if not content:
        raise AudioUploadError("Audio file is empty.")
    if len(content) > MAX_AUDIO_SIZE:
        raise AudioTooLargeError("Audio file exceeds the 15 MB limit.")

    suffix = Path(filename or "").suffix.lower()
    mime = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    supported = _FORMATS.get(suffix)
    if (
        supported is None
        or mime not in supported[0]
        or not _matches_container(suffix, content)
    ):
        raise UnsupportedAudioFormatError("Unsupported or invalid audio format.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid4()
    path = UPLOAD_DIR / f"{upload_id.hex}{suffix}"
    try:
        with path.open("xb") as audio_file:
            audio_file.write(content)
        expires_at = _now() + UPLOAD_TTL
        with _LOCK:
            cleanup_expired_uploads()
            _UPLOADS[upload_id] = {
                "customer_key": principal.customer_key,
                "path": path,
                "media_type": supported[1],
                "size_bytes": len(content),
                "expires_at": expires_at,
            }
        return {
            "audio_upload_id": upload_id,
            "media_type": supported[1],
            "size_bytes": len(content),
            "expires_at": expires_at,
        }
    except Exception:
        _safe_unlink(path, UPLOAD_DIR)
        raise


def cleanup_expired_uploads():
    now = _now()
    with _LOCK:
        for upload_id, record in list(_UPLOADS.items()):
            if record["expires_at"] <= now:
                _UPLOADS.pop(upload_id, None)
                _safe_unlink(record["path"], UPLOAD_DIR)


@contextmanager
def consume_audio_upload(principal: CustomerPrincipal, upload_id: UUID):
    with _LOCK:
        cleanup_expired_uploads()
        record = _UPLOADS.get(upload_id)
        if record is None or record["customer_key"] != principal.customer_key:
            raise AudioUploadError("Audio upload is unavailable or expired.")
        _UPLOADS.pop(upload_id, None)
    path = record["path"]
    try:
        if not path.is_file() or path.parent.resolve() != UPLOAD_DIR.resolve():
            raise AudioUploadError("Audio upload is unavailable or expired.")
        yield path
    finally:
        _safe_unlink(path, UPLOAD_DIR)


def register_speech_artifact(
    principal: CustomerPrincipal,
    path: Path,
    root: Path,
    media_type: str,
    language: str,
):
    artifact_root = root.resolve()
    if not path.is_file() or path.resolve().parent != artifact_root:
        raise ValueError("Generated speech artifact is unavailable.")
    artifact_id = uuid4()
    expires_at = _now() + ARTIFACT_TTL
    with _LOCK:
        cleanup_expired_artifacts()
        _ARTIFACTS[artifact_id] = {
            "customer_key": principal.customer_key,
            "path": path,
            "root": artifact_root,
            "media_type": media_type,
            "language": language,
            "expires_at": expires_at,
        }
    return artifact_id, expires_at


def cleanup_expired_artifacts():
    now = _now()
    with _LOCK:
        for artifact_id, record in list(_ARTIFACTS.items()):
            if record["expires_at"] <= now:
                _ARTIFACTS.pop(artifact_id, None)
                _safe_unlink(record["path"], record["root"])


def get_speech_artifact(principal: CustomerPrincipal, artifact_id: UUID):
    with _LOCK:
        cleanup_expired_artifacts()
        record = _ARTIFACTS.get(artifact_id)
        if record is None or record["customer_key"] != principal.customer_key:
            return None
        if not record["path"].is_file():
            _ARTIFACTS.pop(artifact_id, None)
            return None
        return dict(record)


def delete_speech_artifact(artifact_id: UUID):
    with _LOCK:
        record = _ARTIFACTS.pop(artifact_id, None)
    if record:
        _safe_unlink(record["path"], record["root"])


def start_cleanup_worker():
    """Start periodic expiry cleanup for abandoned uploads/artifacts."""
    global _CLEANUP_THREAD
    if _CLEANUP_THREAD and _CLEANUP_THREAD.is_alive():
        return
    _CLEANUP_STOP.clear()
    _cleanup_orphaned_files()

    def run():
        while not _CLEANUP_STOP.wait(60):
            cleanup_expired_uploads()
            cleanup_expired_artifacts()

    _CLEANUP_THREAD = Thread(target=run, name="vaanishield-audio-cleanup", daemon=True)
    _CLEANUP_THREAD.start()


def stop_cleanup_worker():
    _CLEANUP_STOP.set()
    if _CLEANUP_THREAD and _CLEANUP_THREAD.is_alive():
        _CLEANUP_THREAD.join(timeout=2)
    with _LOCK:
        uploads = list(_UPLOADS.values())
        artifacts = list(_ARTIFACTS.values())
        _UPLOADS.clear()
        _ARTIFACTS.clear()
    for record in uploads:
        _safe_unlink(record["path"], UPLOAD_DIR)
    for record in artifacts:
        _safe_unlink(record["path"], record["root"])


def _cleanup_orphaned_files():
    # The upload directory belongs exclusively to this short-lived store.
    if UPLOAD_DIR.exists():
        for path in UPLOAD_DIR.iterdir():
            if path.is_file():
                _safe_unlink(path, UPLOAD_DIR)
    # Timbre writes under the shared legacy audio directory; only remove the
    # clearly namespaced temporary outputs owned by this adapter.
    try:
        from services import gnani_speech
        audio_root = Path(gnani_speech.AUDIO_DIR)
        if audio_root.exists():
            for path in audio_root.glob("assistant_*.wav"):
                _safe_unlink(path, audio_root)
    except OSError:
        return
