#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "[ERROR] File .env belum ada. Salin .env.example menjadi .env."
  exit 1
fi

VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ] || ! "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
  rm -rf "$ROOT/.venv"
  echo "[1/3] Membuat virtual environment..."
  python3 -m venv "$ROOT/.venv"
  VENV_PY="$ROOT/.venv/bin/python"
  echo "[2/3] Menginstal dependency..."
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r "$ROOT/requirements.txt"
fi

echo "[3/3] Menjalankan Penelusuran Banjir..."
echo "Buka http://127.0.0.5:8000"
exec "$VENV_PY" -m api.app
