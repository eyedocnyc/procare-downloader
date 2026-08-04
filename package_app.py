"""Assemble a shareable package from the PyInstaller build.

Detects the current OS and produces, under dist/:
  - ProcareDownloader-Windows/  + ProcareDownloader-Windows.zip   (on Windows)
  - ProcareDownloader-Mac/      + ProcareDownloader-Mac.zip       (on macOS)

Each contains the standalone app plus a quick-start README. Used by both the
local build scripts and the GitHub Actions workflow so every channel ships the
same thing.
"""
import hashlib
import os
import platform
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
IS_WIN = platform.system() == "Windows"
BIN = "ProcareDownloader.exe" if IS_WIN else "ProcareDownloader"
OSNAME = "Windows" if IS_WIN else "Mac"

README_WIN = """\
PROCARE DOWNLOADER  (Windows)
=============================

Save ALL of your child's photos and videos from Procare, and build a
browsable "scrapbook" of the whole year that opens in any web browser.

No installation needed.

HOW TO USE
----------
1. Double-click  ProcareDownloader.exe
   (Windows may show "Windows protected your PC" because the app isn't
    signed. Click "More info" then "Run anyway".)
2. Choose option 1 (download everything + build the scrapbook).
3. Type your school's name (optional), then your Procare email and
   password. Your password is hidden as you type and is never saved.
4. It downloads a year of photos/videos (can take a while the first time).
   When done, your scrapbook opens automatically.

GOOD TO KNOW
------------
- Everything is saved in a new "procare_media" folder next to the app.
- "Open Scrapbook.html" is the front page of your scrapbook.
- Safe to re-run: it skips what you already downloaded.
- Keep the folder together - the scrapbook links to the photo/video files
  inside it. To move or share it, move the WHOLE procare_media folder.
"""

README_MAC = """\
PROCARE DOWNLOADER  (Mac)
=========================

Save ALL of your child's photos and videos from Procare, and build a
browsable "scrapbook" of the whole year that opens in any web browser.

No installation needed.

HOW TO USE
----------
1. Double-click  ProcareDownloader

   The FIRST time, macOS will likely block it ("cannot be opened because
   it is from an unidentified developer"). To allow it:
     - Right-click (or Control-click) ProcareDownloader  ->  Open  ->  Open
     - or: System Settings -> Privacy & Security -> "Open Anyway"
   You only need to do this once.

2. A Terminal window opens. Choose option 1 (download everything + build
   the scrapbook).
3. Type your school's name (optional), then your Procare email and
   password. Your password is hidden as you type and is never saved.
4. It downloads a year of photos/videos (can take a while the first time).
   When done, your scrapbook opens automatically.

GOOD TO KNOW
------------
- Everything is saved in a new "procare_media" folder next to the app.
- "Open Scrapbook.html" is the front page of your scrapbook.
- Safe to re-run: it skips what you already downloaded.
- Keep the folder together - the scrapbook links to the photo/video files
  inside it. To move or share it, move the WHOLE procare_media folder.
"""


def main():
    src = os.path.join(DIST, BIN)
    if not os.path.exists(src):
        raise SystemExit(f"Build first (PyInstaller). Missing: {src}")
    stage = os.path.join(DIST, f"ProcareDownloader-{OSNAME}")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    dst = os.path.join(stage, BIN)
    shutil.copy2(src, dst)
    if not IS_WIN:
        os.chmod(dst, 0o755)  # preserved in the zip so it stays runnable

    with open(os.path.join(stage, "READ ME FIRST.txt"), "w", encoding="utf-8") as fh:
        fh.write(README_WIN if IS_WIN else README_MAC)

    archive = shutil.make_archive(stage, "zip", root_dir=DIST,
                                  base_dir=f"ProcareDownloader-{OSNAME}")
    print(f"Created: {archive}  ({os.path.getsize(archive)/1_048_576:.0f} MB)")

    # SHA-256 checksum so downloads can be verified (the apps are unsigned).
    h = hashlib.sha256()
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with open(archive + ".sha256", "w", encoding="utf-8") as fh:
        fh.write(f"{digest}  {os.path.basename(archive)}\n")
    print(f"SHA-256: {digest}")


if __name__ == "__main__":
    main()
