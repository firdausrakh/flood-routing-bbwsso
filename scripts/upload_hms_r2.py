"""Upload precomputed data/hms to Cloudflare R2 without duplicating map assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HMS = ROOT / "data" / "hms"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required(name: str, default: str | None = None) -> str:
    value = (os.getenv(name, default or "") or "").strip()
    if not value:
        raise SystemExit(f"[ERROR] Environment variable belum diisi: {name}")
    return value


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise SystemExit("[ERROR] boto3 belum terpasang. Jalankan run.bat/preprocess_hms_r2.bat.") from exc
    endpoint = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
    if not endpoint:
        endpoint = f"https://{required('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=required("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required("R2_SECRET_ACCESS_KEY"),
        region_name="auto", config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    if not (HMS / "index.json").exists():
        raise SystemExit("[ERROR] data/hms/index.json belum ada. Jalankan preprocessing lebih dulu.")
    bucket = required("R2_RUNTIME_BUCKET", "flood-routing")
    prefix = os.getenv("R2_HMS_PREFIX", "").strip().strip("/")
    files = sorted(p for p in HMS.rglob("*") if p.is_file())
    s3 = None if args.dry_run else client()
    objects = {}
    for path in files:
        rel = path.relative_to(HMS).as_posix()
        key = f"{prefix}/{rel}" if prefix else rel
        objects[rel] = {"key": key, "size": path.stat().st_size, "sha256": sha256(path)}
        print(f"[UPLOAD] {rel} -> r2://{bucket}/{key}")
        if s3:
            content_type = "application/json" if path.suffix in {".json", ".geojson"} else "application/octet-stream"
            cache_control = "public,max-age=31536000,immutable" if rel != "index.json" else "no-cache"
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type, "CacheControl": cache_control})
    # Naming data is small and deployment-specific, so it may live with the
    # flood-routing runtime. Official basin/river assets are never duplicated.
    toponym = ROOT / "data" / "reference" / "toponim.sqlite"
    if toponym.exists():
        toponym_key = os.getenv("R2_TOPONYM_KEY", "reference/toponim.sqlite").strip() or "reference/toponim.sqlite"
        toponym_bucket = os.getenv("R2_TOPONYM_BUCKET", bucket).strip() or bucket
        print(f"[UPLOAD] reference/toponim.sqlite -> r2://{toponym_bucket}/{topononym_key}")
        if s3:
            s3.upload_file(str(toponim), toponym_bucket, toponym_key, ExtraArgs={"ContentType": "application/vnd.sqlite3", "CacheControl": "public,max-age=31536000,immutable"})

    manifest = {"schema_version": 1, "bucket": bucket, "prefix": prefix, "objects": objects}
    manifest_key = f"{prefix}/runtime-manifest.json" if prefix else "runtime-manifest.json"
    if s3:
        s3.put_object(Bucket=bucket, Key=manifest_key, Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), ContentType="application/json", CacheControl="no-cache")
    print(f"[SELESAI] {len(files)} file runtime HMS {'akan diunggah' if args.dry_run else 'diunggah'}; official basin/rivers tetap memakai bucket dta-map-assets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
