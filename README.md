# Procare Downloader & Scrapbook

Download **all** of your child's photos and videos from the [Procare](https://www.procaresoftware.com/)
(Procare Connect) parent app to your own computer — each file keeps its **original date** — and
build a browsable **scrapbook** of the whole year (teacher notes, learning activities, photos,
and videos) that opens in any web browser.

Procare gives parents no "download everything" button, and access to a year of memories can
disappear when a child leaves or a subscription lapses. This tool saves it all, locally.

> **Unofficial.** Not affiliated with or endorsed by Procare. It uses the same private API the
> Procare web/mobile app uses, so it may break if Procare changes their service. Use it only for
> your **own** child's account. Provided as-is, with no warranty.
>
> **Private by design.** Everything runs on your computer. Your password is entered at a hidden
> prompt, used only to log in, and is **never saved or sent anywhere** except to Procare.

---

## Download & run (no Python needed)

Most people should just grab the prebuilt app:

### ➡️ [**Download the latest release**](https://github.com/eyedocnyc/procare-downloader/releases/latest)

- **Windows:** download `ProcareDownloader-Windows.zip`, unzip, double-click `ProcareDownloader.exe`.
  First launch shows *"Windows protected your PC"* (the app is unsigned) → **More info → Run anyway**.
- **Mac:** download `ProcareDownloader-Mac.zip`, unzip, double-click `ProcareDownloader`.
  First launch is blocked by Gatekeeper → **right-click → Open → Open** (one time only).

Then just follow the prompts:

1. Choose **option 1** (download everything + build the scrapbook).
2. Enter your school's name (optional) and your Procare **email** and **password**.
3. It downloads your media (the first run can take a while) and opens your scrapbook when done.

Everything is saved next to the app in a `procare_media` folder. Open **`Open Scrapbook.html`** to
browse it.

---

## What you get

- **All photos and videos**, full resolution, organized into monthly folders.
- **Correct dates:** photos keep their capture date in EXIF; every file's date is set to when it
  was taken, so they sort correctly in Photos, Google Photos, or your file browser.
- **A scrapbook** — one HTML page per month plus a front page — showing each day's teacher notes
  and learning activities with the photos and videos embedded inline. Routine logs (meals, naps,
  sign in/out, bathroom) are condensed into a compact "daily log" line per day. The scrapbook is
  titled with your child and class (e.g. *"Maya's Year in Emerald Lilies"*).

```
procare_media/
  Open Scrapbook.html              <- open this
  2025-06 (June 2025).html         <- one page per month
  2025-06/                         <- that month's photos & videos
  assets/scrapbook.css
  feed.json                        <- raw archive of the full activity feed
```

**Multiple classes/years?** When run interactively, the app lists the classes it finds (with date
ranges) and lets you pick one — handy for making a scrapbook for a single class or school year.

---

## Run from source (advanced)

Requires [Python 3.9+](https://www.python.org/downloads/).

```bash
git clone https://github.com/eyedocnyc/procare-downloader.git
cd procare-downloader
pip install -r requirements.txt
python procare_download.py --scrapbook
```

### Options

| Option | What it does |
|--------|--------------|
| `--email` | Your Procare login email (asks if omitted) |
| `--out` | Output directory (default `procare_media`) |
| `--scrapbook` | Build the HTML scrapbook after downloading |
| `--scrapbook-only` | Rebuild the scrapbook without re-downloading (uses `feed.json`) |
| `--zip` | Bundle the whole output folder into one `.zip` |
| `--school "Name"` | School name shown on the scrapbook (auto-detected if omitted) |
| `--class-name "Name"` | Class/room name (auto-detected from the feed if omitted) |
| `--since YYYY-MM-DD` | Only include media on/after this date |
| `--until YYYY-MM-DD` | Only include media on/before this date |
| `--overwrite` | Re-download files that already exist |
| `--videos-only` | Only process videos |
| `--debug` | Save one sample of each activity type to `debug_activities.json` |

Running with **no arguments** (or double-clicking the app) starts a friendly guided menu.

---

## Building the apps

Prebuilt apps are produced automatically by [GitHub Actions](.github/workflows/build.yml): pushing
a version tag (`git tag v1.1 && git push origin v1.1`) builds the Windows and Mac apps and
publishes them to a **Release**. The Mac binary is built on Intel so it runs on Intel Macs natively
and on Apple Silicon via Rosetta.

To build locally instead:

- **Windows:** double-click `build_exe.bat` (needs Python). Produces
  `dist/ProcareDownloader.exe` and `dist/ProcareDownloader-Windows.zip`.
- **Mac:** double-click `build_mac.command` (needs Python; must be run on a Mac). Produces
  `dist/ProcareDownloader` and `dist/ProcareDownloader-Mac.zip`.

Both call `package_app.py` to assemble the shareable zip.

---

## Troubleshooting

- **"Login failed: email or password is incorrect."** Verify at
  [schools.procareconnect.com](https://schools.procareconnect.com/). If you sign in with
  Google/Apple, set a regular Procare password first ("Forgot password").
- **A video won't play.** Re-run with `--overwrite` to refetch it.
- **Scrapbook shows "media file not found."** Do a normal download first, then `--scrapbook-only`.
- **From source: `python`/`pip` not recognized.** Python isn't on your PATH — reinstall and tick
  "Add Python to PATH", or try `py` instead of `python`.

## Notes & limitations

- Uses Procare's private API (no official public API exists for this); it may break if Procare
  changes their backend.
- Only downloads what your own parent account can see — photos your child is tagged in, plus the
  activity feed for your child.
- The apps are unsigned, hence the one-time SmartScreen/Gatekeeper prompts. Code signing requires
  paid developer certificates and isn't worth it for a personal tool.

## License

Provided as-is for personal use, with no warranty. Not affiliated with Procare Solutions.
