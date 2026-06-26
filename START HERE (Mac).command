#!/bin/bash
cd "$(dirname "$0")"

echo "================================================"
echo "  Procare Photo, Video and Scrapbook Downloader"
echo "================================================"
echo

# --- Find Python ---
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
fi
if [ -z "$PYTHON" ]; then
  echo "Python is not installed yet (you only need to do this once)."
  echo
  echo "  1) Go to:  https://www.python.org/downloads/"
  echo "  2) Run the installer."
  echo "  3) Then double-click this file again."
  echo
  read -p "Press Enter to close. "
  exit 1
fi

echo "Installing the small helpers this tool needs (one-time, ~1 minute)..."
$PYTHON -m pip install -r requirements.txt --quiet --disable-pip-version-check
echo

echo "What would you like to do?"
echo
echo "  [1] Download photos & videos AND build the scrapbook   (recommended)"
echo "  [2] Download photos & videos only"
echo "  [3] Rebuild the scrapbook only (no re-downloading)"
echo
read -p "Type 1, 2, or 3 then press Enter (default is 1): " choice
choice=${choice:-1}

# Optional school name for the scrapbook (the class/room is detected for you).
SCHOOL_ARG=()
if [ "$choice" != "2" ]; then
  read -p "Your school's name for the scrapbook (press Enter to skip): " school
  [ -n "$school" ] && SCHOOL_ARG=(--school "$school")
fi

echo
echo "You'll be asked for your Procare email and password next."
echo "(Your password is hidden as you type and is never saved.)"
echo

if [ "$choice" = "2" ]; then
  $PYTHON procare_download.py
elif [ "$choice" = "3" ]; then
  $PYTHON procare_download.py --scrapbook-only "${SCHOOL_ARG[@]}"
else
  $PYTHON procare_download.py --scrapbook "${SCHOOL_ARG[@]}"
fi

echo
echo "Finished. You can close this window."
read -p "Press Enter to close. "
