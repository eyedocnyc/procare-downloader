# Procare Downloader & Scrapbook

Save **all** of your child's photos and videos from the Procare (Procare Connect)
parent app onto your own computer — with the **original date** on every file — and
build a browsable **scrapbook** of the whole year (teacher notes, learning
activities, photos, and videos) that opens in any web browser.

Procare has no "download everything" button for parents. This tool logs in with your
parent email/password, pulls everything your account can see, and saves it into
**monthly folders**, plus generates an HTML scrapbook:

```
procare_media/
  Open Scrapbook.html              <- the scrapbook front page (open this)
  2025-06 (June 2025).html         <- one page per month
  2025-06/
    2025-06-30_115800_photo_a561a1e6...jpg
    2025-06-30_104227_video_open-uri...mp4
  assets/scrapbook.css
  feed.json                         <- raw archive of the full feed
```

- **Photos** keep their capture date in EXIF metadata.
- **Every file** (incl. videos) gets its file date set to the capture date, so they
  sort correctly everywhere.
- The **scrapbook** shows each day's teacher notes and learning activities with the
  photos and videos embedded inline; routine logs (meals, naps, sign in/out,
  bathroom) are condensed into a compact "daily log" line per day.
- The scrapbook is titled with your child and **class/room name** (auto-detected from
  the feed, e.g. *"Ava's Year in Emerald Lilies"*) and shows your **school name** at the
  top. The class is detected automatically; the school is auto-detected from your account
  or you can set it with `--school` (the launchers ask for it).

---

## Easiest way (no commands)

1. **Install Python once** — [python.org/downloads](https://www.python.org/downloads/).
   On Windows, tick **"Add Python to PATH"** during install.
2. **Double-click the launcher:**
   - Windows: **`START HERE (Windows).bat`**
   - Mac: **`START HERE (Mac).command`** (first time: right-click → Open)
3. Choose option **1**, then type your Procare **email** and **password** (hidden, never saved).
4. When it finishes, your **`Open Scrapbook.html`** opens automatically.

If your child has been in more than one class/room over time, the guided menu shows the
classes it finds (with their date ranges) and lets you pick one — so you can make a
scrapbook for just that class/year. (You can also set an exact range with `--since`/`--until`.)

See **`READ ME FIRST.txt`** for the same steps in plain language.

---

## Running it manually (terminal)

```
cd path/to/procare-downloader
pip install -r requirements.txt
python procare_download.py --scrapbook
```

It asks for your password at a hidden prompt, downloads everything, then builds the scrapbook.

### Options

| Option | What it does | Example |
|--------|--------------|---------|
| `--email`         | Your Procare login email (asks if omitted) | `--email you@example.com` |
| `--out`           | Where to save (default: `procare_media`) | `--out "D:\Daycare"` |
| `--scrapbook`     | Build the HTML scrapbook after downloading | |
| `--scrapbook-only`| Rebuild the scrapbook **without** re-downloading (uses `feed.json` if present) | |
| `--zip`           | Bundle the whole folder into one `Procare Scrapbook.zip` | |
| `--school`        | School name shown on the scrapbook (auto-detected if omitted) | `--school "The Brunswick School"` |
| `--class-name`    | Class/room name shown on the scrapbook (auto-detected from the feed if omitted) | `--class-name "Emerald Lilies"` |
| `--since`         | Only include media on/after this date | `--since 2024-09-01` |
| `--until`         | Only include media on/before this date | `--until 2025-06-30` |
| `--overwrite`     | Re-download files that already exist | |
| `--videos-only`   | Only process videos | |
| `--debug`         | Save one sample of each activity type to `debug_activities.json` | |

Examples:
```
# Everything + scrapbook + a single shareable zip:
python procare_download.py --scrapbook --zip

# Just rebuild the scrapbook after the media is already downloaded:
python procare_download.py --scrapbook-only
```

---

## Good to know

- **Safe to re-run.** It skips files you already have and continues where it left off.
- **Your password is never stored** — only used to log in for that run.
- **Keep the folder together.** The scrapbook pages link to the media files beside them.
  To share or back up, move/zip the **whole** `procare_media` folder (or use `--zip`).
- **Videos & metadata.** Video files can't hold EXIF, so the tool sets their file date
  instead (enough for Photos/Google Photos to sort them). For in-file video metadata,
  optionally install [ExifTool](https://exiftool.org/) — not required.
- **This uses Procare's private app API**, not an official public one. If Procare changes
  their system the tool may need an update.

## Troubleshooting

- **"Login failed: email or password is incorrect."** Verify at
  [schools.procareconnect.com](https://schools.procareconnect.com/). If you sign in with
  Google/Apple, set a regular Procare password first (Forgot password).
- **`python` or `pip` "not recognized."** Python wasn't added to PATH — reinstall and tick
  that box, or try `py` instead of `python`.
- **A video won't play / a file looks wrong.** Re-run with `--overwrite` to refetch it.
- **Scrapbook shows "media file not found."** Run a normal download first (or `--overwrite`)
  so the referenced files exist, then rebuild with `--scrapbook-only`.

## Sharing it as a single Windows app (no Python needed)

For non-technical people, hand them a single **`ProcareDownloader.exe`** — no Python, no
setup. They double-click it, pick option 1, and follow the prompts (it shows a friendly
menu and keeps the window open when finished).

- **Ready-made package:** `dist/ProcareDownloader-Windows.zip` (the exe + a quick-start
  `READ ME FIRST.txt`). Send that.
- **Rebuild it** (on any Windows machine with Python): double-click **`build_exe.bat`**, or:
  ```
  python -m pip install pyinstaller
  python -m PyInstaller --onefile --console --name ProcareDownloader ^
    --hidden-import scrapbook --hidden-import piexif procare_download.py
  python package_exe.py
  ```
  Output: `dist/ProcareDownloader.exe` and `dist/ProcareDownloader-Windows.zip`.

Notes:
- First launch shows Windows SmartScreen ("Windows protected your PC") because the exe is
  unsigned — click **More info → Run anyway**.

### Mac single-file app

A Mac binary can only be built **on a Mac** (PyInstaller doesn't cross-compile). On any Mac
with Python, double-click **`build_mac.command`** (or run it in Terminal). It produces
`dist/ProcareDownloader` and a shareable `dist/ProcareDownloader-Mac.zip`.

- First launch on Mac is blocked by Gatekeeper (unsigned): **right-click → Open → Open**,
  or System Settings → Privacy & Security → **Open Anyway**. One time only.

### Cloud build (no Mac needed) — GitHub Actions

`.github/workflows/build.yml` builds **both** the Mac and Windows apps on GitHub's runners,
so you don't need a Mac at all.

1. Create a repo on github.com. Make it **public** if you want share links anyone can use
   without a GitHub account.
2. Push this folder to it:
   ```
   git remote add origin https://github.com/<you>/procare-downloader.git
   git push -u origin main
   ```
3. Get the apps, two ways:
   - **Actions tab → "Build apps" → Run workflow.** Download the `ProcareDownloader-macOS`
     and `ProcareDownloader-Windows` artifacts. (Artifacts need a GitHub login to download.)
   - **Publish a Release (best for sharing):** tag a version and push it —
     ```
     git tag v1.0
     git push origin v1.0
     ```
     The workflow attaches both zips to a GitHub **Release**. On a public repo those are
     direct download links you can send to anyone (no login needed).

The Mac binary is built on Intel (`macos-13`) so it runs on Intel Macs natively and on Apple
Silicon via Rosetta (macOS auto-prompts to install it on first launch).
