#!/usr/bin/env bash
set -euo pipefail
echo "=== Compilando FarmaFacturas para macOS / Linux ==="
python3 -m pip install --upgrade pip pyinstaller -r requirements.txt
pyinstaller --clean --noconfirm packaging/facturas_farmacia.spec

echo "=== Generando hashes SHA-256 para GitHub Releases ==="
cd dist
shasum -a 256 FarmaFacturas FarmaFacturasUpdater > SHA256SUMS.txt
cat SHA256SUMS.txt
