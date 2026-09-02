from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd


@dataclass(frozen=True)
class RemoteObjectRef:
    bucket: str
    key: str
    local_path: Path
    expected_size: int | None = None
    expected_sha256: str | None = None


@dataclass
class ReferenceBundle:
    backend: str
    official_basins: gpd.GeoDataFrame
    official_rivers: gpd.GeoDataFrame
    toponym_db_path: Path | None
    map_assets_public_base: str | None = None
    map_assets_version: str | None = None
    metadata: dict[str, Any] | None = None
    toponym_ref: RemoteObjectRef | None = None


_METRICS_LOCK = threading.Lock()
_METRICS: dict[str, int] = {
    "head_requests": 0,
    "get_requests": 0,
    "downloaded_bytes": 0,
    "cache_hits": 0,
    "lazy_downloads": 0,
}
_TOPONYM_LOCK = threading.Lock()


def _metric_add(name: str, value: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = int(_METRICS.get(name, 0)) + int(value)


def get_reference_backend_metrics() -> dict[str, int]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def _load_project_dotenv(root: Path) -> None:
    path = root / ".env"
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


def _required_env(*keys: str) -> dict[str, str]:
    values = {key: os.getenv(key, "").strip() for key in keys}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError("Environment variable belum diisi: " + ", ".join(missing))
    return values


def _r2_endpoint_url() -> str:
    explicit = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    account = _required_env("R2_ACCOUNT_ID")["R2_ACCOUNT_ID"]
    return f"https://{account}.r2.cloudflarestorage.com"


@lru_cache(maxsize=1)
def _r2_client():
    values = _required_env("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("DATA_BACKEND=r2 membutuhkan boto3.") from exc
    return boto3.client(
        "s3",
        endpoint_url=_r2_endpoint_url(),
        aws_access_key_id=values["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )


def _cache_root() -> Path:
    base = os.getenv("FLOOD_REFERENCE_CACHE_DIR", "").strip()
    return Path(base) if base else Path(tempfile.gettempdir()) / "penelusuran-banjir-reference"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(ref: RemoteObjectRef) -> Path:
    client = _r2_client()
    path = ref.local_path
    if path.exists() and path.stat().st_size > 0:
        size_ok = ref.expected_size is None or path.stat().st_size == ref.expected_size
        if size_ok:
            _metric_add("cache_hits")
            return path

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.unlink(missing_ok=True)
    try:
        _metric_add("get_requests")
        client.download_file(ref.bucket, ref.key, str(tmp))
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Gagal download R2 {ref.bucket}/{ref.key}: {exc}") from exc
    if not tmp.exists() or tmp.stat().st_size <= 0:
        raise RuntimeError(f"R2 object kosong: {ref.bucket}/{ref.key}")
    if ref.expected_size is not None and tmp.stat().st_size != ref.expected_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Ukuran R2 object tidak sesuai: {ref.bucket}/{ref.key}")
    if ref.expected_sha256 and os.getenv("R2_VERIFY_DOWNLOAD_SHA256", "0").lower() in {"1", "true", "yes"}:
        if _sha256(tmp) != ref.expected_sha256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"SHA256 R2 object tidak sesuai: {ref.bucket}/{ref.key}")
    tmp.replace(path)
    _metric_add("downloaded_bytes", path.stat().st_size)
    return path


def _read_manifest(bucket: str) -> dict[str, Any]:
    key = os.getenv("R2_MANIFEST_KEY", "manifest.json").strip() or "manifest.json"
    try:
        _metric_add("get_requests")
        response = _r2_client().get_object(Bucket=bucket, Key=key)
        payload = json.loads(response["Body"].read().decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Gagal membaca manifest R2 {bucket}/{key}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("manifest.json R2 tidak valid.")
    return payload


def _remote_ref(bucket: str, manifest: dict[str, Any], key: str, local_path: Path) -> RemoteObjectRef:
    objects = manifest.get("objects") or {}
    meta = objects.get(key) if isinstance(objects, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    size = meta.get("size")
    return RemoteObjectRef(
        bucket=bucket,
        key=key,
        local_path=local_path,
        expected_size=int(size) if size is not None else None,
        expected_sha256=str(meta.get("sha256") or "").strip() or None,
    )


def _load_local(root: Path) -> ReferenceBundle:
    reference_dir = root / "data" / "reference"
    official_path = reference_dir / "official_reference.gpkg"
    if not official_path.exists():
        raise RuntimeError(
            f"Data referensi tidak ditemukan: {official_path}. Folder /data sengaja tidak dibundel; pasang data referensi secara terpisah."
        )
    basins = gpd.read_file(official_path, layer="official_basins").reset_index(drop=True)
    rivers = gpd.read_file(official_path, layer="official_rivers").reset_index(drop=True)
    if basins.empty or rivers.empty:
        raise RuntimeError("Layer referensi resmi kosong.")
    toponym = reference_dir / "toponim.sqlite"
    return ReferenceBundle(
        backend="local",
        official_basins=basins,
        official_rivers=rivers,
        toponym_db_path=toponym if toponym.exists() else None,
        map_assets_public_base=os.getenv("R2_MAP_ASSETS_PUBLIC_BASE", "").strip().rstrip("/") or None,
        map_assets_version=os.getenv("R2_MAP_ASSETS_VERSION", "").strip() or None,
        metadata={"reference_source": str(official_path)},
    )


def _load_r2() -> ReferenceBundle:
    # Official cartographic assets are shared with Delineasi DTA and intentionally
    # live in a separate bucket.  Flood-routing runtime objects use flood-routing.
    bucket = os.getenv("R2_REFERENCE_BUCKET", "dta-map-assets").strip() or "dta-map-assets"
    prefix = os.getenv("R2_REFERENCE_PREFIX", "").strip().strip("/")
    def key(name: str) -> str:
        return f"{prefix}/{name}" if prefix else name

    namespace = hashlib.sha1(f"{bucket}/{prefix}".encode("utf-8")).hexdigest()[:10]
    root = _cache_root() / namespace
    basin_key = key(os.getenv("R2_OFFICIAL_BASINS_KEY", "official_basins.geojson").strip() or "official_basins.geojson")
    river_key = key(os.getenv("R2_OFFICIAL_RIVERS_KEY", "official_rivers.geojson").strip() or "official_rivers.geojson")
    basin_path = _download(RemoteObjectRef(bucket=bucket, key=basin_key, local_path=root / "official_basins.geojson"))
    river_path = _download(RemoteObjectRef(bucket=bucket, key=river_key, local_path=root / "official_rivers.geojson"))
    basins = gpd.read_file(basin_path).reset_index(drop=True)
    rivers = gpd.read_file(river_path).reset_index(drop=True)
    if basins.empty or rivers.empty:
        raise RuntimeError("Layer referensi resmi R2 kosong.")

    # Toponym is optional for flood routing.  It may be hosted separately without
    # forcing the shared dta-map-assets bucket to contain private/non-map objects.
    toponym_key = os.getenv("R2_TOPONYM_KEY", "").strip()
    toponym_ref = None
    toponym_path = None
    if toponym_key:
        toponym_bucket = os.getenv("R2_TOPONYM_BUCKET", bucket).strip() or bucket
        toponym_ref = RemoteObjectRef(bucket=toponym_bucket, key=toponym_key, local_path=root / "toponim.sqlite")
        toponym_path = toponym_ref.local_path

    return ReferenceBundle(
        backend="r2",
        official_basins=basins,
        official_rivers=rivers,
        toponym_db_path=toponym_path,
        map_assets_public_base=os.getenv("R2_MAP_ASSETS_PUBLIC_BASE", "").strip().rstrip("/") or None,
        map_assets_version=os.getenv("R2_MAP_ASSETS_VERSION", "").strip() or None,
        metadata={"reference_source": f"{bucket}/{basin_key} + {bucket}/{river_key}", "reference_bucket": bucket},
        toponym_ref=toponym_ref,
    )


def load_reference_bundle(root: Path) -> ReferenceBundle:
    _load_project_dotenv(root)
    backend = os.getenv("DATA_BACKEND", "local").strip().lower() or "local"
    if backend == "local":
        return _load_local(root)
    if backend == "r2":
        return _load_r2()
    raise RuntimeError("DATA_BACKEND harus 'local' atau 'r2'.")


def ensure_toponym_db_path(bundle: ReferenceBundle) -> Path:
    if bundle.backend == "r2" and bundle.toponym_ref is not None:
        with _TOPONYM_LOCK:
            existed = bundle.toponym_ref.local_path.exists()
            path = _download(bundle.toponym_ref)
            if not existed:
                _metric_add("lazy_downloads")
            return path
    if bundle.toponym_db_path is None or not bundle.toponym_db_path.exists():
        raise RuntimeError("Database toponim tidak tersedia.")
    return bundle.toponym_db_path
