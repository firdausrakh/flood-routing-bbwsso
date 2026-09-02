"""Storage backend for precomputed HEC-HMS runtime objects.

Local mode reads ``data/hms`` directly.  R2 mode lazily materializes exactly
those same relative objects into a process-local cache, so the routing reader
keeps one storage-neutral contract.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
_LOCAL_HMS_ROOT = ROOT_DIR / "data" / "hms"
_DOWNLOAD_LOCK = threading.Lock()
_METRICS_LOCK = threading.Lock()
_METRICS = {"get_requests": 0, "downloaded_bytes": 0, "cache_hits": 0}


def _load_project_dotenv() -> None:
    path = ROOT_DIR / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_project_dotenv()


def backend_name() -> str:
    return (os.getenv("DATA_BACKEND", "local").strip().lower() or "local")


def hms_root() -> Path:
    if backend_name() == "local":
        return _LOCAL_HMS_ROOT
    base = os.getenv("FLOOD_HMS_CACHE_DIR", "").strip()
    root = Path(base) if base else Path(tempfile.gettempdir()) / "penelusuran-banjir-hms"
    bucket = os.getenv("R2_RUNTIME_BUCKET", "flood-routing").strip() or "flood-routing"
    prefix = os.getenv("R2_HMS_PREFIX", "").strip().strip("/")
    namespace = hashlib.sha1(f"{bucket}/{prefix}".encode("utf-8")).hexdigest()[:10]
    return root / namespace


def _metric(name: str, amount: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = int(_METRICS.get(name, 0)) + int(amount)


def backend_metrics() -> dict[str, int | str]:
    with _METRICS_LOCK:
        return {"backend": backend_name(), **dict(_METRICS)}


def _required(name: str, default: str | None = None) -> str:
    value = (os.getenv(name, default or "") or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable belum diisi: {name}")
    return value


def _endpoint() -> str:
    explicit = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"https://{_required('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"


@lru_cache(maxsize=1)
def _client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise RuntimeError("DATA_BACKEND=r2 membutuhkan boto3.") from exc
    return boto3.client(
        "s3",
        endpoint_url=_endpoint(),
        aws_access_key_id=_required("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )


def _safe_relative(key: str | Path) -> Path:
    text = str(key).replace("\\", "/").lstrip("/")
    rel = Path(text)
    if not text or ".." in rel.parts:
        raise RuntimeError(f"Path runtime HMS tidak valid: {key}")
    return rel


def _r2_key(rel: Path) -> str:
    prefix = os.getenv("R2_HMS_PREFIX", "").strip().strip("/")
    raw = rel.as_posix()
    return f"{prefix}/{raw}" if prefix else raw


def ensure_hms_object(key: str | Path) -> Path:
    """Return a local path for one object relative to the HMS runtime root."""
    rel = _safe_relative(key)
    root = hms_root()
    path = root / rel
    if backend_name() == "local":
        return path
    if backend_name() != "r2":
        raise RuntimeError("DATA_BACKEND harus 'local' atau 'r2'.")
    if path.exists() and path.stat().st_size > 0:
        _metric("cache_hits")
        return path
    with _DOWNLOAD_LOCK:
        if path.exists() and path.stat().st_size > 0:
            _metric("cache_hits")
            return path
        bucket = _required("R2_RUNTIME_BUCKET", "flood-routing")
        remote_key = _r2_key(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.unlink(missing_ok=True)
        try:
            _metric("get_requests")
            _client().download_file(bucket, remote_key, str(tmp))
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Gagal download R2 {bucket}/{remote_key}: {exc}") from exc
        if not tmp.exists() or tmp.stat().st_size <= 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"R2 object kosong: {bucket}/{remote_key}")
        tmp.replace(path)
        _metric("downloaded_bytes", path.stat().st_size)
        return path
