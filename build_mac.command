#!/bin/bash
# Builds a single-file Mac app (dist/ProcareDownloader) and a shareable zip.
# MUST be run on a Mac (double-click it, or run in Terminal). Needs Python 3.
cd "$(dirname "$0")"

PYTHON=""
command -v python3 >/dev/null 2>&1 && PYTHON=python3
if [ -z "$PYTHON" ]; then
  echo "Python 3 is required to BUILD the app (not to run it)."
  echo "Install it from https://www.python.org/downloads/ then run this again."
  read -p "Press Enter to close. "
  exit 1
fi

echo "Installing build tools..."
$PYTHON -m pip install --quiet --disable-pip-version-check pyinstaller requests piexif

echo "Building ProcareDownloader (this takes a minute)..."
$PYTHON -m PyInstaller --onefile --console --name ProcareDownloader \
  --hidden-import scrapbook --hidden-import piexif --noconfirm procare_download.py

echo "Assembling shareable package..."
$PYTHON package_app.py

echo
echo "Done. Share this file:  dist/ProcareDownloader-Mac.zip"
read -p "Press Enter to close. "
