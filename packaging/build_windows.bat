@echo off
REM Script de compilacion de ejecutable Windows (prioridad absoluta segun especificacion)
setlocal
echo === Compilando Archivero para Windows ===
python -m pip install --upgrade pip pyinstaller -r requirements.txt
pyinstaller --clean --noconfirm packaging\facturas_farmacia.spec

echo === Generando hashes SHA-256 para GitHub Releases ===
REM Formato "<hash>  <nombre>" (el que lee el actualizador automatico)
powershell -NoProfile -Command "$l=@(); foreach($f in 'Archivero.exe','ArchiveroUpdater.exe'){ $h=(Get-FileHash ('dist\'+$f) -Algorithm SHA256).Hash.ToLower(); $l+=\"$h  $f\" }; $l | Set-Content -Path dist\SHA256SUMS.txt -Encoding ASCII"
echo Compilacion completada en carpeta dist\
endlocal
