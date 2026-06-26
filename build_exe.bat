@echo off
REM Rebuilds the standalone Windows executable (dist\ProcareDownloader.exe)
REM and assembles a shareable zip. Run this on a Windows machine with Python.
setlocal
cd /d "%~dp0"

echo Installing build tool (PyInstaller)...
python -m pip install --quiet --disable-pip-version-check pyinstaller requests piexif

echo Building ProcareDownloader.exe ...
python -m PyInstaller --onefile --console --name ProcareDownloader ^
  --hidden-import scrapbook --hidden-import piexif --noconfirm procare_download.py

echo Assembling shareable package...
python package_app.py

echo.
echo Done. Share this file:  dist\ProcareDownloader-Windows.zip
pause
