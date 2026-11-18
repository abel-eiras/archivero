#!/usr/bin/env bash
set -euo pipefail
echo "=== Compilando Archivero para macOS / Linux ==="
python3 -m pip install --upgrade pip pyinstaller -r requirements.txt
pyinstaller --clean --noconfirm packaging/facturas_farmacia.spec

echo "=== Generando hashes SHA-256 para GitHub Releases ==="
cd dist
shasum -a 256 Archivero ArchiveroUpdater > SHA256SUMS.txt
cat SHA256SUMS.txt
