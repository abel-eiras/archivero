@echo off
REM Script de compilacion de ejecutable Windows (prioridad absoluta segun especificacion)
setlocal
echo === Compilando Archivero para Windows ===
python -m pip install --upgrade pip pyinstaller -r requirements.txt
pyinstaller --clean --noconfirm packaging\facturas_farmacia.spec

echo === Generando hashes SHA-256 para GitHub Releases ===
certutil -hashfile dist\Archivero.exe SHA256 > dist\SHA256SUMS.txt
certutil -hashfile dist\ArchiveroUpdater.exe SHA256 >> dist\SHA256SUMS.txt
echo Compilacion completada en carpeta dist\
endlocal
