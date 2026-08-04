"""
Self-update for the Procare Downloader.
=======================================

Checks GitHub Releases on startup and, if a newer version exists, offers to
download it, verify it against the published SHA-256, and swap the running
binary in place. Everything here is best-effort: any failure (offline, rate
limited, bad download, unsupported platform) degrades to a printed notice and
never stops the app from running.

Only the packaged one-file app updates itself. When run from source
(`python procare_download.py`) there is no binary to replace, so we just point
the user at `git pull` / a fresh download.

Security notes:
- The download is verified against the release's `*.zip.sha256` before anything
  is swapped in — the same integrity file we publish for manual downloads.
- The GitHub API/CDN are NOT Procare hosts, so the account bearer token is never
  involved here (this module makes its own plain, unauthenticated requests).
- A programmatic download carries no macOS `com.apple.quarantine` xattr / Windows
  Mark-of-the-Web, so the replaced binary launches without re-triggering the
  Gatekeeper / SmartScreen "unidentified developer" prompt.
"""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import urljoin

try:
    import requests
except ImportError:  # the engine already hard-requires requests; guard anyway
    requests = None

REPO = "eyedocnyc/procare-downloader"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = "procare-media-downloader/updater"

CHECK_TIMEOUT = 6            # seconds for the version check (keep startup snappy)
DOWNLOAD_TIMEOUT = 120       # seconds per download request
MAX_REDIRECTS = 5            # bound the redirect chain we'll follow for a download
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024  # refuse absurdly large bodies (app zip is ~tens of MB)


# --------------------------------------------------------------------------- #
# Version math
# --------------------------------------------------------------------------- #
def parse_version(tag):
    """'v1.9' / '1.9' / '1.10.2' -> (1, 9) / (1, 10, 2). Non-numeric -> ()."""
    if not isinstance(tag, str):
        return ()
    s = tag.strip().lstrip("vV").strip()
    parts = []
    for chunk in s.split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            break                       # stop at the first non-numeric segment
        parts.append(int(chunk))
    return tuple(parts)


def is_newer(latest, current):
    """True iff version string `latest` is strictly newer than `current`.
    Unparseable/empty `latest` is never considered newer (fail safe)."""
    lv, cv = parse_version(latest), parse_version(current)
    if not lv:
        return False
    return lv > cv


# --------------------------------------------------------------------------- #
# Platform / asset selection
# --------------------------------------------------------------------------- #
def platform_asset():
    """Return (zip_asset_name, binary_path_inside_zip) for the current OS, or
    None on platforms we don't publish a binary for (e.g. Linux / source runs)."""
    system = platform.system()
    if system == "Windows":
        return "ProcareDownloader-Windows.zip", "ProcareDownloader-Windows/ProcareDownloader.exe"
    if system == "Darwin":
        return "ProcareDownloader-Mac.zip", "ProcareDownloader-Mac/ProcareDownloader"
    return None


def is_frozen():
    """True when running as the packaged PyInstaller one-file app."""
    return bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------- #
# GitHub queries (best-effort; never raise)
# --------------------------------------------------------------------------- #
def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    return s


def fetch_latest(timeout=CHECK_TIMEOUT, session=None):
    """Return the latest-release JSON dict, or None on any problem."""
    if requests is None:
        return None
    try:
        s = session or _session()
        resp = s.get(RELEASES_API, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def find_asset(release, name):
    """URL of the release asset named `name`, or None."""
    if not isinstance(release, dict):
        return None
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == name:
            url = asset.get("browser_download_url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
    return None


# --------------------------------------------------------------------------- #
# Download + verify
# --------------------------------------------------------------------------- #
def parse_sha256_file(text):
    """Pull the hex digest out of a `sha256sum`-style file ('<hex>  <name>')."""
    if not isinstance(text, str):
        return None
    first = text.strip().split("\n", 1)[0].strip()
    token = first.split()[0] if first else ""
    token = token.lower()
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(session, url, dest, timeout=DOWNLOAD_TIMEOUT,
              max_redirects=MAX_REDIRECTS, max_bytes=MAX_DOWNLOAD_BYTES):
    """Stream `url` to `dest`. Follows redirects manually so we can enforce that
    EVERY hop stays https (a redirect must never downgrade the transport), bounds
    the redirect chain, and caps the total size (via Content-Length and again
    while streaming, so a lying/absent header can't blow past the limit)."""
    for _ in range(max_redirects + 1):
        if not url.lower().startswith("https://"):
            return False
        with session.get(url, stream=True, timeout=timeout, allow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    return False
                url = urljoin(url, loc)   # resolve relative redirects, then re-check https
                continue
            if resp.status_code != 200:
                return False
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                return False
            written = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        fh.close()
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                        return False
                    fh.write(chunk)
            return True
    return False  # too many redirects


def download_and_verify(zip_url, sha_url, tmp_dir, session=None):
    """Download the release zip and its `.sha256`, verify, and return the local
    zip path — or None if anything fails or the checksum doesn't match."""
    if requests is None:
        return None
    s = session or _session()
    zip_path = os.path.join(tmp_dir, "update.zip")
    try:
        if not _download(s, zip_url, zip_path):
            return None
        expected = None
        if sha_url:
            try:
                r = s.get(sha_url, timeout=CHECK_TIMEOUT)
                if r.status_code == 200:
                    expected = parse_sha256_file(r.text)
            except Exception:
                expected = None
        if not expected:
            # No usable checksum -> refuse to install (don't run unverified code).
            return None
        if sha256_of(zip_path).lower() != expected:
            return None
        return zip_path
    except Exception:
        return None


def extract_binary(zip_path, bin_relpath, dest_path):
    """Extract `bin_relpath` from the zip to `dest_path`. Returns dest_path or
    None. Tolerates the binary living at a different depth than expected."""
    try:
        wanted = os.path.basename(bin_relpath)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            member = bin_relpath if bin_relpath in names else next(
                (n for n in names if not n.endswith("/") and os.path.basename(n) == wanted), None)
            if member is None:
                return None
            with zf.open(member) as src, open(dest_path, "wb") as out:
                shutil.copyfileobj(src, out)
        return dest_path
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Applying the update (platform-specific swap of the running binary)
# --------------------------------------------------------------------------- #
def apply_update(new_bin, current_exe):
    """Replace the running binary with `new_bin` and relaunch. Returns False if
    it couldn't (caller then falls back to opening the releases page)."""
    try:
        if platform.system() == "Windows":
            return _apply_windows(new_bin, current_exe)
        return _apply_posix(new_bin, current_exe)
    except Exception:
        return False


def _swap_file(new_bin, target):
    """Replace the file at `target` with `new_bin` (pure file ops, no relaunch).

    Backs `target` up to `<target>.bak`, stages the new binary in the SAME
    directory (so the final swap is an atomic same-filesystem `os.replace` —
    `new_bin` lives in a temp dir that may be on a different filesystem), and
    marks it executable. Returns the backup path (or None if the backup step
    failed). Split out from the relaunch so it can be unit-tested."""
    backup = target + ".bak"
    try:
        shutil.copy2(target, backup)
    except Exception:
        backup = None
    staging = target + ".new"
    shutil.copy2(new_bin, staging)
    os.replace(staging, target)
    os.chmod(target, 0o755)
    return backup


def _apply_posix(new_bin, current_exe):
    """macOS: swap the running binary's file in place, then re-exec."""
    backup = _swap_file(new_bin, current_exe)
    print("\nUpdate installed. Restarting...\n")
    sys.stdout.flush()
    try:
        # Relaunch preserving the original args so a CLI invocation continues as
        # the user asked (double-click has none). The new binary is up to date,
        # so its own startup check finds nothing newer — no re-prompt loop.
        os.execv(current_exe, [current_exe] + sys.argv[1:])
    except Exception:
        # Couldn't re-exec, but the swap succeeded — just ask them to reopen.
        print("Update installed — please reopen the app to use the new version.")
        if backup:
            print(f"(previous version kept at: {backup})")
        sys.exit(0)
    return True


def _windows_script(target, new_bin, backup, arg_str):
    """Build the batch that swaps a running .exe after the parent exits. The retry
    loop is BOUNDED (waits ~30 tries for the file to free, then gives up leaving
    the original in place) so it can never spin forever, and it relaunches with
    the preserved args. Pure string builder so it can be unit-tested."""
    return f"""@echo off
setlocal
set "TARGET={target}"
set "NEWBIN={new_bin}"
set "BACKUP={backup}"
set /a tries=0
rem give the parent process a moment to fully exit
ping 127.0.0.1 -n 3 >nul
:retry
set /a tries+=1
if exist "%BACKUP%" del /f /q "%BACKUP%" >nul 2>&1
move /y "%TARGET%" "%BACKUP%" >nul 2>&1
if not errorlevel 1 goto swap
if %tries% GEQ 30 goto done
ping 127.0.0.1 -n 2 >nul
goto retry
:swap
move /y "%NEWBIN%" "%TARGET%" >nul 2>&1
if errorlevel 1 (
  rem swap failed after we moved the old exe aside -> restore it
  move /y "%BACKUP%" "%TARGET%" >nul 2>&1
  goto done
)
start "" "%TARGET%" {arg_str}
:done
del /f /q "%~f0" >nul 2>&1
"""


def _apply_windows(new_bin, current_exe):
    """Windows can't overwrite a running .exe, so hand off to a batch script that
    waits for us to exit, swaps the file (old kept as .bak), relaunches, then
    deletes itself. The script goes to a uniquely-named temp file."""
    backup = current_exe + ".bak"
    # Preserve the original CLI args on relaunch (double-click has none). Strip any
    # embedded quotes so a stray value can't break out of the batch quoting; args
    # here are plain flags/values (the password is never on argv).
    arg_str = " ".join('"%s"' % a.replace('"', "") for a in sys.argv[1:])
    fd, bat = tempfile.mkstemp(prefix="procare_update_", suffix=".bat")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_windows_script(current_exe, new_bin, backup, arg_str))
    DETACHED = 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(["cmd", "/c", bat], creationflags=DETACHED, close_fds=True)
    print("\nUpdate downloaded. The app will restart on the new version...\n")
    sys.stdout.flush()
    sys.exit(0)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def _prompt_yes(question):
    """Ask a yes/no question, default yes. Only call when stdin is a TTY."""
    try:
        ans = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("", "y", "yes")


def self_update(current_version, assume_yes=False, ask=None):
    """Check for a newer release and, with the user's ok, install it. Best-effort:
    swallows all errors so a failed or skipped update never blocks the real work.

    `ask`: optional callable (question: str) -> bool, replacing the default
    TTY input()-based confirm. Used by GUI launches (no console to prompt on)
    to show a messagebox instead -- see procare_download._gui_ask_yes_no."""
    try:
        _self_update(current_version, assume_yes=assume_yes, ask=ask)
    except Exception:
        pass  # never let the updater crash the app


def _self_update(current_version, assume_yes=False, ask=None):
    release = fetch_latest()
    if not release:
        return
    latest = release.get("tag_name") or ""
    if not is_newer(latest, current_version):
        return

    latest_disp = latest.lstrip("vV")
    print(f"\nA newer version is available: v{latest_disp} "
          f"(you have v{current_version}).")

    asset = platform_asset()
    if not is_frozen() or asset is None:
        # Source run or unsupported platform -> can't self-replace a binary.
        print("  Update it with your usual method (re-download the latest release,")
        print(f"  or 'git pull' if you run from source):\n  {RELEASES_PAGE}\n")
        return

    ask_fn = ask or _prompt_yes
    interactive = True if ask is not None else sys.stdin.isatty()
    if not assume_yes and not interactive:
        # Don't block a scripted run waiting on input.
        print(f"  Run interactively to update, or download it:\n  {RELEASES_PAGE}\n")
        return
    question = "Download and install it now?" if ask is not None else "Download and install it now? [Y/n]: "
    if not assume_yes and not ask_fn(question):
        print("  Skipped. Continuing with the current version.\n")
        return

    zip_name, bin_relpath = asset
    zip_url = find_asset(release, zip_name)
    sha_url = find_asset(release, zip_name + ".sha256")
    if not zip_url:
        print("  Couldn't find the download for your system; opening the page instead.")
        _open_page()
        return

    print("  Downloading and verifying...")
    tmp_dir = tempfile.mkdtemp(prefix="procare_update_")
    zip_path = download_and_verify(zip_url, sha_url, tmp_dir)
    if not zip_path:
        print("  Download or checksum verification failed; opening the page instead.")
        _open_page()
        return

    current_exe = sys.executable
    new_bin = os.path.join(tmp_dir, "ProcareDownloader.new")
    if not extract_binary(zip_path, bin_relpath, new_bin):
        print("  Couldn't read the update package; opening the page instead.")
        _open_page()
        return

    if not apply_update(new_bin, current_exe):
        print("  Couldn't install the update automatically; opening the page instead.")
        _open_page()
        return


def _open_page():
    try:
        import webbrowser
        webbrowser.open(RELEASES_PAGE)
    except Exception:
        pass
    print(f"  {RELEASES_PAGE}\n")
