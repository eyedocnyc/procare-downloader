#!/usr/bin/env python3
"""
Procare Media Downloader
========================

Bulk-downloads ALL photos and videos from a Procare (Procare Connect) parent
account and timestamps each file with its original capture date.

- Photos: capture date written into EXIF (DateTimeOriginal / DateTimeDigitized).
- Every file: OS modified/created time set to the capture date (so videos, which
  have no EXIF, still sort correctly in Photos / Google Photos / Explorer).
- Files are organized into monthly folders: procare_media/YYYY-MM/

Usage:
    python procare_download.py --email you@example.com
    python procare_download.py --email you@example.com --out "D:/Daycare Photos"
    python procare_download.py --email you@example.com --since 2024-09-01

You will be prompted for your password (it is never printed or saved).

This talks to the same private API the Procare web app uses. It does not use any
official/public API and may break if Procare changes their backend.
"""

import argparse
import getpass
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Run:  pip install -r requirements.txt")

try:
    import piexif
    HAVE_PIEXIF = True
except ImportError:
    HAVE_PIEXIF = False  # photos still download; EXIF write is skipped with a warning

# The self-updater compares this against the latest GitHub release. It MUST equal
# the release tag (build.yml enforces APP_VERSION == the vX.Y tag on release), so
# bump it in the same change you intend to tag.
APP_VERSION = "1.14"

import updater  # noqa: E402  (top-level so PyInstaller bundles it automatically)


# Procare changed domains over time; try the current one first, then the legacy one.
BASE_URLS = [
    "https://api-school.procareconnect.com/api/web/",
    "https://api-school.kinderlime.com/api/web/",
]

# The web app authenticates through this service, not the per-host endpoints
# below. It resolves the account's home API host (returned under "sites") and
# issues the bearer token. We try it first because the legacy POST
# /api/web/auth/ now returns HTTP 500 for ordinary parent ("carer") accounts.
ONLINE_AUTH_URL = "https://online-auth.procareconnect.com/sessions/"

# Only these hosts ever receive the account's bearer token. Media downloads go to
# CDN/S3 hosts on signed URLs that authorize themselves, so they must NOT carry the
# Authorization header (that would leak the token to CloudFront/S3). We authenticate
# only when the destination host is exactly one of these; everything else (media) is
# fetched with a separate, unauthenticated session. This is belt-and-suspenders with
# requests' own behavior of dropping auth on cross-host redirects.
PROCARE_AUTH_HOSTS = {
    "api-school.procareconnect.com",
    "api-school.kinderlime.com",
}


def is_procare_host(url):
    """True iff `url` is https and its host is EXACTLY an allowlisted Procare API
    host. Exact hostname match (not suffix) so a look-alike like
    `api-school.procareconnect.com.attacker.test` is rejected; the query string
    can't affect the decision because urlsplit parses the host separately."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return host in PROCARE_AUTH_HOSTS

# Videos come from their own simple paginated endpoint.
VIDEO_PATH = "parent/videos/"
# Some daycares post everything through the daily-activities feed; others (or
# some rooms within the same school) upload straight into the gallery and never
# create an activity record at all. So we always query BOTH: the feed (each
# activity item carries an activity_type and the real media object under the
# (Procare-misspelled) "activiable" key) and the bare gallery endpoints below,
# then merge + dedup (see collect_gallery / distribute_gallery). Some accounts
# 400 on the bare gallery endpoints (older backends) - that's treated as
# "nothing here", not an error.
GALLERY_PHOTO_PATH = "parent/photos/"
ACTIVITIES_PATH = "parent/daily_activities/"
KIDS_PATH = "parent/kids/"
# We don't filter the activity feed by type: photos can be attached to many
# activity types (learning, observation, incident, note, kudos, ...), not just
# photo_activity. We extract images from every activity and skip videos by ext.
VIDEO_ACTIVITY_TYPES = {"video_activity"}

# Media file extensions, classified so we can label each file correctly.
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".3gp", ".3gpp", ".mkv")
# Key-name fragments whose URLs are decorations, not content photos.
SKIP_URL_KEY_FRAGMENTS = ("avatar", "icon", "logo", "profile",
                          "staff", "teacher", "signature", "badge")
# URL-PATH fragments that mark a decoration (e.g. a teacher's profile picture
# that some activities expose under a generic "photo_url" key).
SKIP_URL_PATH_FRAGMENTS = ("/profile_pics/", "profilepic", "/avatars/",
                           "/avatar/", "/logos/")
# When one photo is offered in several resolutions, prefer the full-size one and
# avoid the smaller variants. Used to score URL key names.
PHOTO_KEY_PREFER = ("original", "full", "large", "main", "hires", "hi_res",
                    "highres", "high", "standard", "display", "orig")
PHOTO_KEY_AVOID = ("thumb", "small", "medium", "mini", "preview", "low", "tiny")
# The activities feed limits how much it returns per query, so we walk the
# timeline one month at a time. EARLIEST is how far back to start when --since
# isn't given (covers any realistic daycare enrollment history).
ACTIVITY_EARLIEST_DEFAULT = date(2018, 1, 1)

# Candidate keys for the media URL and the capture timestamp, in priority order.
URL_KEYS = ["main_url", "video_file_url", "url", "photo_url", "image_url", "file_url"]
DATE_KEYS = ["created_at", "activity_time", "captured_at", "taken_at", "updated_at"]

# Status codes that mean "these credentials are wrong", not "this host is wrong".
# Procare uses 422 (with an errors body); 401/403 are here for future-proofing.
AUTH_REJECTED_CODES = (401, 403, 422)

REQUEST_TIMEOUT = 60
RETRIES = 4
POLITE_DELAY = 0.25  # seconds between requests, to be gentle on the API


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def auth_error_message(resp):
    """The server's own explanation of a rejected login, or None if it didn't give one.

    Procare answers a bad email/password with HTTP 422 and a body like
    {"errors": ["Email and password did not match."]} -- NOT 401/403. Treating
    that as a transport error sends us on to the next base URL and buries the
    real reason under whatever that host happens to say.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        errs = payload.get("errors") or payload.get("error")
        if isinstance(errs, str):
            return errs
        if isinstance(errs, (list, tuple)) and errs:
            return "; ".join(str(e) for e in errs if e)
    return None


def _fail_login(detail):
    """Exit with a clear, non-retryable credential-failure message.

    Not retryable on purpose: parent accounts lock after repeated failed logins
    and only the daycare can unlock them, so we must not loop on a bad password.
    """
    sys.exit(f"Login failed: {detail}\n"
             "Check the email and password you use on schools.procareconnect.com.\n"
             "Careful: repeated failed logins lock the account, and only your "
             "daycare can unlock it.")


def session_token_and_base(payload):
    """Pull (auth_token, api_base) out of an online-auth /sessions/ response.

    `api_base` is the account's home host (from the "sites" list) with the
    "/api/web/" suffix the rest of this tool expects. Returns (None, None) if the
    response carries no usable token or site (e.g. an MFA-gated login)."""
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = data.get("auth_token")
    if not (isinstance(token, str) and token):
        user = data.get("user")
        token = user.get("auth_token") if isinstance(user, dict) else None
    base = None
    sites = data.get("sites")
    if isinstance(sites, list) and sites:
        # Prefer the account's default site; fall back to the first listed.
        site = next((s for s in sites if isinstance(s, dict) and s.get("is_default")), sites[0])
        base_url = (site.get("base_url") or "").rstrip("/") if isinstance(site, dict) else ""
        if base_url:
            base = base_url + "/api/web/"
    if not (isinstance(token, str) and token) or not base:
        return None, None
    return token, base


def allow_auth_host(api_base):
    """Permit the bearer token to reach a resolved Procare API host.

    Multi-tenant schools live on `api-school.<school>.procareconnect.com`; add the
    resolved host to the allowlist so `download_file` will authenticate to it.
    Only genuine Procare/Kinderlime hosts qualify — a look-alike such as
    `api-school.procareconnect.com.evil.test` fails the suffix check."""
    host = (urlsplit(api_base).hostname or "").lower()
    if host.endswith(".procareconnect.com") or host.endswith(".kinderlime.com"):
        PROCARE_AUTH_HOSTS.add(host)


def _online_auth(session, email, password, errors):
    """Authenticate via online-auth — the flow the web app itself uses.

    Returns the resolved API base ("…/api/web/") on success, or None so the
    caller can fall back to the legacy hosts. Exits on a definitive rejection
    (bad password, or an MFA/SSO account this tool can't handle)."""
    try:
        resp = session.post(
            ONLINE_AUTH_URL,
            json={"email": email, "password": password,
                  "role": "carer", "platform": "web", "preserve_sites": True},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        errors.append(f"{ONLINE_AUTH_URL}\n      could not reach this host ({type(e).__name__})")
        return None
    if resp.status_code in AUTH_REJECTED_CODES:
        _fail_login(auth_error_message(resp) or "email or password is incorrect")
    if resp.status_code >= 400:
        errors.append(f"{ONLINE_AUTH_URL}\n      returned HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        payload = resp.json()
    except ValueError:
        errors.append(f"{ONLINE_AUTH_URL}\n      unexpected login response: {resp.text[:200]}")
        return None

    token, base = session_token_and_base(payload)
    if not (token and base):
        # A session with no usable token/host is almost always an MFA/SSO account:
        # authentication half-succeeded but isn't cleared for regular requests.
        access = payload.get("access_to") if isinstance(payload, dict) else None
        if access and access != "regular_requests":
            _fail_login("this account needs two-factor authentication or single "
                        "sign-on, which this tool can't do. Only plain email + "
                        "password accounts work.")
        errors.append(f"{ONLINE_AUTH_URL}\n      login response carried no usable token/host")
        return None

    allow_auth_host(base)
    session.headers.update({"Authorization": f"Bearer {token}"})
    return base


def _legacy_auth(session, email, password, errors):
    """The pre-online-auth flow: POST /api/web/auth/ to each known host.

    Returns (base, user_dict) on success, or None. Kept as a fallback for
    backends where online-auth is unavailable."""
    for base in BASE_URLS:
        try:
            resp = session.post(
                base + "auth/",
                json={"email": email, "password": password},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            errors.append(f"{base}\n      could not reach this host ({type(e).__name__})")
            continue

        if resp.status_code == 404:
            errors.append(f"{base}\n      returned 404 (wrong domain)")
            continue
        # 401/403 is the textbook answer; 422 is what Procare actually sends.
        if resp.status_code in AUTH_REJECTED_CODES:
            _fail_login(auth_error_message(resp) or "email or password is incorrect")
        if resp.status_code >= 400:
            errors.append(f"{base}\n      returned HTTP {resp.status_code}: {resp.text[:200]}")
            continue

        try:
            user = resp.json()["user"]
            token = user["auth_token"]
        except (ValueError, KeyError, TypeError):
            errors.append(f"{base}\n      unexpected login response: {resp.text[:200]}")
            continue

        session.headers.update({"Authorization": f"Bearer {token}"})
        return base, user
    return None


def authenticate(session, email, password):
    """Log in and return (api_base, payload).

    Uses the web app's own flow first (online-auth resolves the account's API
    host and issues the token), then falls back to the legacy per-host
    /api/web/auth/ endpoint. Exits with a clear message if every path fails."""
    errors = []
    base = _online_auth(session, email, password, errors)
    if base:
        return base, None

    result = _legacy_auth(session, email, password, errors)
    if result:
        return result

    # Report every endpoint we tried, not just the last -- the legacy kinderlime
    # host no longer resolves, so its DNS error would otherwise mask the real
    # failure (e.g. the primary host's 500).
    detail = "\n".join(f"  - {e}" for e in errors)
    sys.exit(f"Authentication failed. Tried {len(errors)} endpoint(s):\n{detail}")


# --------------------------------------------------------------------------- #
# Field extraction (defensive against API drift)
# --------------------------------------------------------------------------- #
def find_media_url(item):
    for key in URL_KEYS:
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    # Fallback: first http(s) string anywhere in the item.
    for val in item.values():
        if isinstance(val, str) and val.startswith("http") and _looks_like_media(val):
            return val
    return None


def _looks_like_media(url):
    clean = url.split("?")[0].lower()
    return clean.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".heic", ".mp4", ".mov", ".m4v", ".webp")
    )


def find_video_url(item):
    """Find the actual video URL on a video item (NOT its poster image).

    Video URLs often lack a file extension, so we identify them by field name
    first, then by a video extension.
    """
    for key in ("video_file_url", "video_url", "video"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for k, v in item.items():
        if isinstance(v, str) and v.startswith("http") and "video" in str(k).lower():
            return v
    for v in item.values():
        if isinstance(v, str) and v.startswith("http") and media_kind(v) == "video":
            return v
    return None


def find_capture_dt(item):
    if not isinstance(item, dict):
        return None
    for key in DATE_KEYS:
        if key in item:
            dt = _parse_dt(item[key])
            if dt:
                return dt
    # Fallback: scan only STRING values for a date (never bare ints — an `id`
    # like 50 must not be mistaken for "50 seconds after epoch").
    for val in item.values():
        if isinstance(val, str):
            dt = _parse_dt(val)
            if dt:
                return dt
    return None


# Plausible epoch range so a small integer id isn't read as a 1970 timestamp.
_EPOCH_MIN = 1_104_537_600   # 2005-01-01
_EPOCH_MAX = 4_102_444_800   # 2100-01-01


def _parse_dt(val):
    if not val:
        return None
    if isinstance(val, (int, float)):
        try:
            ts = float(val)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            if not (_EPOCH_MIN <= ts <= _EPOCH_MAX):
                return None  # implausible as a date (likely an id/count)
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(val, str):
        s = val.strip()
        # Normalize ISO 8601 with trailing Z.
        s_iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s_iso)
            return dt.replace(tzinfo=None)  # store naive local-ish; we only need date/time
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[: len(fmt) + 4], fmt)
            except ValueError:
                continue
    return None


def ext_from_url(url, default):
    clean = url.split("?")[0]
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", clean)
    if m:
        return "." + m.group(1).lower()
    return default


# --------------------------------------------------------------------------- #
# Download + timestamp
# --------------------------------------------------------------------------- #
def fetch_json(session, url, params, label="", reauth=None, quiet=False, retries=None):
    """`quiet` suppresses the HTTP-error print — use it for endpoints that are
    expected to 400/404 on some accounts (e.g. the bare gallery endpoints on
    backends that don't support them) so a normal run doesn't look like it hit
    an error when there's simply nothing there. `retries` overrides the default
    attempt count (the gallery walk uses a smaller one so a flaky month doesn't
    cost the full ~15s of 5xx backoff)."""
    attempts = retries or RETRIES
    reauthed = False
    for attempt in range(attempts):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            # Token expired mid-run: log in again once and retry immediately.
            if resp.status_code in (401, 403) and reauth and not reauthed:
                reauthed = True
                print("  (session expired — signing in again...)")
                reauth()
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if not quiet:
                print(f"  ! HTTP {resp.status_code} on {label or url} "
                      f"(params={params}); stopping this feed.")
            return None
        except requests.RequestException as e:
            if attempt == attempts - 1:
                if not quiet:
                    print(f"  ! Network error on {label or url}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def get_kids_meta(session, base):
    """Return [{'id':..., 'name':...}] for each child on the account."""
    payload = fetch_json(session, base + KIDS_PATH, {}, "parent/kids")
    if payload is None:
        return []
    if isinstance(payload, dict):
        lst = payload.get("kids")
        if not isinstance(lst, list):
            lst = extract_items(payload)
    elif isinstance(payload, list):
        lst = payload
    else:
        lst = []
    kids = []
    for k in lst or []:
        if isinstance(k, dict) and k.get("id") is not None:
            name = (k.get("name")
                    or " ".join(p for p in (k.get("first_name"), k.get("last_name")) if p)
                    or "").strip()
            kids.append({"id": k["id"], "name": name,
                         "first_name": (k.get("first_name") or "").strip()})
    return kids


def get_kids(session, base):
    """Return a list of kid_ids on the account (needed for the photos feed)."""
    return [k["id"] for k in get_kids_meta(session, base)]


def extract_items(payload):
    """The list may be the top-level array or wrapped under a common key."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("photos", "videos", "daily_activities", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        # Single wrapping list value.
        for val in payload.values():
            if isinstance(val, list):
                return val
    return []


def sniff_ext(head):
    """Return the real file extension from magic bytes, or None if unknown."""
    if not head:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":  # ISO base media: mp4 / mov / heic
        brand = head[8:12]
        if brand[:2] == b"qt":
            return ".mov"
        if brand in (b"heic", b"heif", b"mif1", b"hevc"):
            return ".heic"
        return ".mp4"
    return None


# Head-byte signatures of the error pages a CDN sometimes serves with HTTP 200
# instead of a real error status (an HTML "access denied"/"expired" page, or a
# JSON error body). These are never valid photo/video content, so we reject them.
_ERROR_PAGE_PREFIXES = (b"<!doctype", b"<!DOCTYPE", b"<html", b"<HTML", b"<?xml", b"{")


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _looks_like_error_page(content_type, head):
    """True if a 200 response is really an HTML/JSON/text error page, not media.
    We reject only clearly-bad content types / signatures; anything else is
    accepted (so video formats sniff_ext doesn't recognize still download)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("text/") or ct in ("application/json", "application/xml"):
        return True
    stripped = head.lstrip()[:16]
    return any(stripped.startswith(p) for p in _ERROR_PAGE_PREFIXES)


def download_file(session, media_session, url, dest):
    """Download `url` to `dest`. Returns (ok, head_bytes).

    `session` is the authenticated Procare session; `media_session` is a separate
    UNauthenticated session. We send the bearer token ONLY to allowlisted Procare
    hosts (`is_procare_host`) — Procare-proxied media needs it — and fetch signed
    CDN/S3 URLs with the unauthenticated session so the token never leaks off-domain.
    `requests` also drops auth on cross-host redirects, so this stays correct when a
    Procare URL redirects to S3. Non-https media URLs are refused outright.

    head_bytes are the first bytes of the file so the caller can verify the real type.
    """
    if not (isinstance(url, str) and url.lower().startswith("https://")):
        # Refuse plaintext http:// (and anything non-URL): no media should be
        # fetched over a channel that could expose a signed link or be tampered.
        return False, b""
    use_session = session if is_procare_host(url) else media_session
    for attempt in range(RETRIES):
        try:
            with use_session.get(url, stream=True, timeout=REQUEST_TIMEOUT,
                                 allow_redirects=True) as resp:
                if resp.status_code != 200:
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return False, b""

                expected = resp.headers.get("Content-Length")
                expected = int(expected) if expected and expected.isdigit() else None
                content_type = resp.headers.get("Content-Type")

                tmp = dest + ".part"
                written = 0
                head = b""
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            if not head:
                                head = chunk[:16]
                            fh.write(chunk)
                            written += len(chunk)

                # An HTML/JSON error page served with a 200 is a complete, wrong
                # response — retrying the same (e.g. expired-signature) URL just
                # returns it again, so fail fast without burning the backoff.
                if _looks_like_error_page(content_type, head):
                    _remove_quiet(tmp)
                    return False, b""

                # A truncated/empty body, by contrast, is often a transient hiccup
                # worth retrying.
                if written == 0 or (expected is not None and written != expected):
                    _remove_quiet(tmp)
                    if attempt < RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return False, b""

                os.replace(tmp, dest)
                return True, head
        except requests.RequestException:
            if attempt == RETRIES - 1:
                return False, b""
            time.sleep(2 ** attempt)
    return False, b""


def apply_timestamp(path, dt):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg") and HAVE_PIEXIF:
        try:
            stamp = dt.strftime("%Y:%m:%d %H:%M:%S")
            try:
                exif = piexif.load(path)
            except Exception:
                exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
            exif.setdefault("Exif", {})
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
            exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
            exif.setdefault("0th", {})
            exif["0th"][piexif.ImageIFD.DateTime] = stamp
            piexif.insert(piexif.dump(exif), path)
        except Exception as e:
            print(f"    (EXIF write skipped: {e})")
    # Set filesystem times for everything (this is what fixes video sorting).
    try:
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# Saving + feed loops
# --------------------------------------------------------------------------- #
def media_stem(dt, label, ident):
    """The filename stem (no extension) used for a media file. Shared by the
    downloader and the scrapbook so both agree on names."""
    return f"{dt.strftime('%Y-%m-%d_%H%M%S')}_{label}_{ident}"


def find_local_media(out_dir, dt, label, ident):
    """Return the path to an already-downloaded media file (any extension), or None."""
    ident = str(ident)
    month_dir = os.path.join(out_dir, dt.strftime("%Y-%m"))
    stem = media_stem(dt, label, ident)
    matches = [p for p in glob.glob(os.path.join(glob.escape(month_dir), stem + ".*"))
               if not p.endswith(".part")]
    if matches:
        return matches[0]
    # Fallback: the same label+ident in any month (the timestamp/month recorded
    # at download time may differ slightly from the lookup), since ident is unique.
    pat = os.path.join(glob.escape(out_dir), "*", f"*_{label}_{glob.escape(ident)}.*")
    matches = [p for p in glob.glob(pat) if not p.endswith(".part")]
    return matches[0] if matches else None


def save_media(session, media_session, url, dt, label, ident, out_dir, since_dt,
               stats, default_ext, seen=None, overwrite=False, until_dt=None):
    """Download one media item into its monthly folder and timestamp it.

    `session` is the authenticated Procare session; `media_session` is the
    separate unauthenticated one used for off-domain (CDN) media — see
    `download_file`. `seen` is a set used to dedup the same file across feeds
    within one run (e.g. a video that appears in both the gallery and the
    activity feed). `since_dt`/`until_dt` bound which capture dates are kept.
    """
    if not url:
        stats["failed"] += 1
        return
    if dt is None:
        dt = datetime.now()
    if (since_dt and dt < since_dt) or (until_dt and dt > until_dt):
        stats["skipped_old"] += 1
        return

    # Fallback identity chain: given ident -> URL filename -> deterministic
    # SHA of the URL. Never the randomized hash() (broke re-run idempotency)
    # and never the literal "None" (distinct id-less items would collide).
    ident = str(ident or id_from_url(url) or stable_media_ident(url))
    key = f"{label}:{ident}"
    if seen is not None and key in seen:
        stats["skipped_exist"] += 1
        return

    month_dir = os.path.join(out_dir, dt.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    stem = media_stem(dt, label, ident)

    # A previous run may have saved this with any extension; match on the stem.
    if not overwrite:
        if find_local_media(out_dir, dt, label, ident):
            stats["skipped_exist"] += 1
            if seen is not None:
                seen.add(key)
            return

    tmp = os.path.join(month_dir, stem + ".part")
    ok, head = download_file(session, media_session, url, tmp)
    if not ok:
        stats["failed"] += 1
        print(f"  ! failed: {url[:80]}")
        time.sleep(POLITE_DELAY)
        return

    # Name the file by its REAL type (magic bytes), so we never save e.g. a PNG
    # poster as .mp4. Fall back to the URL/default extension if unrecognized.
    real_ext = sniff_ext(head) or ext_from_url(url, default_ext)
    dest = os.path.join(month_dir, stem + real_ext)
    os.replace(tmp, dest)

    apply_timestamp(dest, dt)
    stats["downloaded"] += 1
    if seen is not None:
        seen.add(key)
    print(f"  + {os.path.relpath(dest, out_dir)}")
    time.sleep(POLITE_DELAY)


# Keys a gallery item MIGHT use to name the child(ren) it belongs to. Observed on
# real accounts: the gallery returns NONE of these (items are account-wide and
# child-agnostic), but we read them if present so the code stays correct if Procare
# ever starts tagging gallery media per child.
GALLERY_KID_LIST_KEYS = ("kid_ids", "student_ids", "child_ids")
GALLERY_KID_SINGLE_KEYS = ("kid_id", "student_id", "child_id")
GALLERY_KID_OBJ_KEYS = ("kids", "students", "participants")


def gallery_item_kids(item):
    """Explicit child association on a gallery item, as a list of id strings, or []
    if the item names no child (the common case — gallery media is account-wide)."""
    for k in GALLERY_KID_LIST_KEYS:
        v = item.get(k)
        if isinstance(v, list) and v:
            return [str(x) for x in v if x is not None]
    for k in GALLERY_KID_SINGLE_KEYS:
        v = item.get(k)
        if v is not None:
            return [str(v)]
    for k in GALLERY_KID_OBJ_KEYS:
        v = item.get(k)
        if isinstance(v, list):
            ids = [str(x.get("id")) for x in v if isinstance(x, dict) and x.get("id") is not None]
            if ids:
                return ids
    return []


# The gallery endpoints are date-capped server-side (Procare now hides media
# older than ~1 year from the default view), so — like the activities feed — we
# must pass an explicit date-range filter to reach older items and walk the
# timeline month-by-month. The filter is keyed by the resource name and takes a
# "YYYY-MM-DD HH:MM" datetime, verified against a live account's dashboard request:
#   parent/photos/?filters[photo][datetime_from]=2024-08-07 00:00
#                 &filters[photo][datetime_to]=2024-08-07 23:59
# Videos use the analogous filters[video][...].
GALLERY_ENDPOINTS = (("video", VIDEO_PATH, "video"), ("photo", GALLERY_PHOTO_PATH, "photo"))
# Safety bound for the gallery pagination: stop after this many pages of one
# query (and also if a page repeats the previous page's items) so a backend that
# ignores the `page` param can never spin the loop forever. Fewer retries too, so
# a flaky month costs a couple of seconds, not the full 5xx backoff.
GALLERY_MAX_PAGES = 500
GALLERY_RETRIES = 2


def gallery_query_params(resource, win_from, win_to, kid_id=None, page=1):
    """Build the query for one gallery page/window. `resource` is the filter key
    ("photo"/"video"); `win_from`/`win_to` are ISO dates (YYYY-MM-DD)."""
    params = {
        "page": page,
        f"filters[{resource}][datetime_from]": f"{win_from} 00:00",
        f"filters[{resource}][datetime_to]": f"{win_to} 23:59",
    }
    if kid_id:
        params["kid_id"] = kid_id
    return params


def _gallery_items_to_entries(items, kind):
    """Turn one page of gallery items into (url, dt, ident, kind, assoc_kids)."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # For videos, grab the real video URL (not the poster image).
        url = find_video_url(item) if kind == "video" else find_media_url(item)
        if not url:
            continue
        # Video URLs are randomized 'open-uri' names that change on every request
        # (see collect_media_entries) - identify by the item's own resource id
        # instead, so it dedups against the same video seen via the activity feed.
        ident = item.get("id") if kind == "video" and item.get("id") is not None \
            else (id_from_url(url) or stable_media_ident(url))
        out.append((url, find_capture_dt(item), str(ident), kind, gallery_item_kids(item)))
    return out


def _paginate_gallery(session, base, path, kind, base_params, reauth=None):
    """Page through one gallery query (a fixed `base_params` plus an incrementing
    `page`) and return its entries. Stops on an empty page, an error, the
    GALLERY_MAX_PAGES cap, or a page that repeats the previous one's items — the
    last two guard against a backend that ignores `page` and would otherwise loop
    forever."""
    entries, prev_ids = [], None
    for page in range(1, GALLERY_MAX_PAGES + 1):
        params = dict(base_params, page=page)
        payload = fetch_json(session, base + path, params, path,
                             reauth=reauth, quiet=True, retries=GALLERY_RETRIES)
        if payload is None:
            break
        items = extract_items(payload)
        if not items:
            break
        page_ids = [it.get("id") for it in items if isinstance(it, dict)]
        if page_ids and page_ids == prev_ids:   # endpoint ignoring `page` -> stop
            break
        prev_ids = page_ids
        entries.extend(_gallery_items_to_entries(items, kind))
        time.sleep(POLITE_DELAY)
    return entries


def fetch_gallery_media(session, base, kid_id, start_date, end_date, reauth=None, progress=None):
    """Fetch photos & videos posted straight into the gallery, bypassing the
    daily-activities feed entirely. Some daycares (or some rooms) only use the
    gallery and never create an activity record, and Procare now moves media
    older than ~1 year out of the activity feed into the gallery.

    Two passes per endpoint (results merged, deduped downstream by (kind, ident)):
    an **unfiltered** pass — for backends that return the whole gallery without a
    date filter — and a **date-windowed** pass walking `start_date`..`end_date`
    month-by-month, which is what reaches media the date-capped backends hide
    (issue #1). Doing both means neither kind of account regresses.

    Returns [(url, dt, ident, kind, assoc_kids), ...]; `assoc_kids` is the list of
    child ids the item explicitly names (usually empty — the gallery is
    account-wide). The caller attributes + dedups (collect_gallery /
    distribute_gallery). A 400 on an endpoint just yields nothing for it.
    """
    entries = []
    kid_params = {"kid_id": kid_id} if kid_id else {}
    windows = list(month_windows(start_date, end_date))
    for kind, path, resource in GALLERY_ENDPOINTS:
        if progress:
            progress(None)                        # the unfiltered pass
        entries.extend(_paginate_gallery(session, base, path, kind, dict(kid_params), reauth))
        for win_from, win_to in windows:
            if progress:
                progress(win_from[:7])            # YYYY-MM label for this window
            base_params = gallery_query_params(resource, win_from, win_to, kid_id)
            entries.extend(_paginate_gallery(session, base, path, kind, base_params, reauth))
    return entries


def gallery_step_count(kid_ids, start_date, end_date):
    """Total progress steps the gallery walk will take: per child, per endpoint,
    one unfiltered pass plus one per month window."""
    months = len(list(month_windows(start_date, end_date)))
    return max(len(kid_ids), 1) * len(GALLERY_ENDPOINTS) * (1 + months)


def _gallery_progress(total):
    """Return a callback for the gallery walk that prints one in-place updating
    line ('Gallery   42%  (2019-03)'). Called once per step with a YYYY-MM label
    (or None for the unfiltered pass)."""
    state = {"done": 0}

    def cb(label):
        state["done"] += 1
        pct = min(100, int(state["done"] * 100 / total)) if total else 100
        sys.stdout.write(f"\r  Gallery {pct:3d}%  ({label or 'recent'})    ")
        sys.stdout.flush()
    return cb


def collect_gallery(session, base, kid_ids, start_date, end_date, reauth=None, progress=None):
    """Query the gallery once per child id (walking `start_date`..`end_date`
    month-by-month) and collapse the results per media item.

    Returns {(kind, ident): {"url", "dt", "assoc": set(explicit kid ids),
    "returned_for": set(kid ids whose query returned this item)}}. `returned_for`
    is the signal that lets us tell a genuinely per-child gallery (item comes back
    for only one kid) from an account-wide one (same item for every kid)."""
    meta = {}
    for kid_id in kid_ids:
        for url, dt, ident, kind, assoc in fetch_gallery_media(
                session, base, kid_id, start_date, end_date, reauth=reauth, progress=progress):
            m = meta.setdefault((kind, ident),
                                {"url": url, "dt": dt, "assoc": set(), "returned_for": set()})
            m["assoc"].update(assoc)
            if kid_id is not None:
                m["returned_for"].add(kid_id)
    return meta


def distribute_gallery(meta, sections, since_dt, until_dt):
    """Attach gallery media to the right child section(s), returning any
    child-agnostic gallery-only records for a shared bucket.

    Rules (see plan #2): skip items already present via an activity record (they're
    correctly attributed there); attribute an item to the child(ren) it explicitly
    names, else to the single child whose query returned it (a per-child gallery),
    else treat it as account-wide. Account-wide items go to the sole child when
    there's only one, otherwise to the returned "Shared Gallery" list. Never
    duplicated within a section, never dumped on an arbitrary child."""
    if not sections:
        return []
    by_kid = {s.get("kid_id"): s for s in sections}
    real_kids = [k for k in by_kid if k is not None]
    n_kids = len(real_kids)
    # (kind, ident) already downloaded/shown via activities, across ALL children.
    known = set()
    section_idents = {}
    for s in sections:
        ids = existing_media_idents(s["records"])
        section_idents[id(s)] = set(ids)
        known |= ids

    def _range_for(section):
        return section.get("since", since_dt), section.get("until", until_dt)

    shared = []
    shared_seen = set()
    for (kind, ident), m in meta.items():
        if (kind, ident) in known:
            continue
        assoc = [k for k in m["assoc"] if k in by_kid]
        if assoc:
            targets = assoc
        elif m["returned_for"] and len(m["returned_for"]) < max(n_kids, 1):
            # Returned for a strict subset of kids -> the endpoint discriminates,
            # so attribute to exactly those kids.
            targets = list(m["returned_for"])
        else:
            targets = []  # account-wide / child-agnostic

        if targets:
            for k in targets:
                s = by_kid[k]
                if (kind, ident) in section_idents[id(s)]:
                    continue
                lo, hi = _range_for(s)
                if not in_range(m["dt"], lo, hi):
                    continue
                section_idents[id(s)].add((kind, ident))
                s["records"].append(gallery_entry_to_record(m["url"], m["dt"], ident, kind, k))
        elif n_kids <= 1:
            # Single child (or no child profiles): no ambiguity — it's theirs.
            s = sections[0]
            if (kind, ident) in section_idents[id(s)]:
                continue
            lo, hi = _range_for(s)
            if not in_range(m["dt"], lo, hi):
                continue
            section_idents[id(s)].add((kind, ident))
            s["records"].append(gallery_entry_to_record(m["url"], m["dt"], ident, kind,
                                                         s.get("kid_id")))
        else:
            # Multiple children, no way to attribute -> shared bucket. Keep if it
            # falls in ANY child's selected date window.
            if (kind, ident) in shared_seen:
                continue
            if not any(in_range(m["dt"], *_range_for(s)) for s in sections):
                continue
            shared_seen.add((kind, ident))
            shared.append(gallery_entry_to_record(m["url"], m["dt"], ident, kind, None))
    return shared


def gallery_entry_to_record(url, dt, ident, kind, kid_id=None):
    """Wrap a bare gallery photo/video into an activity-shaped dict so it flows
    through the same download/scrapbook code path as feed-sourced media.

    The media URL is placed under a key `collect_media_urls` will recognize for
    this kind: videos under `video_file_url` (their open-uri URLs have no file
    extension, so they're only detectable by key name), photos under `main_url`."""
    url_key = "video_file_url" if kind == "video" else "main_url"
    return {
        "id": f"gallery-{kind}-{ident}",
        "activity_type": "photo_activity" if kind == "photo" else "video_activity",
        "activity_date": dt.strftime("%Y-%m-%d") if dt else None,
        "activity_time": dt.isoformat() if dt else None,
        "kid_ids": [kid_id] if kid_id else [],
        "comment": None,
        "activiable": {"id": ident, url_key: url},
    }


def existing_media_idents(records):
    """(kind, ident) pairs for every media item already present in `records`."""
    return {(kind, ident) for r in records for _, _, ident, kind in collect_media_entries(r)}


def month_windows(start_date, end_date):
    """Yield (from_str, to_str) covering each calendar month in the range."""
    cur = date(start_date.year, start_date.month, 1)
    while cur <= end_date:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        win_to = min(nxt - timedelta(days=1), end_date)
        win_from = max(cur, start_date)
        yield win_from.isoformat(), win_to.isoformat()
        cur = nxt


def media_kind(u):
    """Return 'photo' or 'video' for a media URL, or None if it isn't media."""
    if not (isinstance(u, str) and u.startswith("http")):
        return None
    clean = u.split("?")[0].lower()
    if clean.endswith(IMAGE_EXTS):
        return "photo"
    if clean.endswith(VIDEO_EXTS):
        return "video"
    return None


def id_from_url(url):
    """Stable id from the URL's filename (ignores signed/expiring query params)."""
    path = url.split("?")[0]
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    return stem or None


def stable_media_ident(url):
    """Deterministic identity for a media item that has no usable id and whose
    URL filename yields nothing (e.g. an odd proxy URL). A SHA-256 of the URL
    with its signed/expiring query stripped, so it's stable across runs — never
    Python's per-process-randomized hash() and never the literal string "None"."""
    normalized = (url or "").split("?")[0]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def collect_media_urls(obj, _depth=0, _seen=None):
    """Recursively gather content media URLs (photos AND videos) from a media obj.

    Returns a list of (url, kind). Skips URLs whose key name looks like an
    avatar/thumbnail/icon so we only keep media a teacher actually attached.
    """
    if _seen is None:
        _seen = set()
    found = []
    if _depth > 5:
        return found
    if isinstance(obj, dict):
        # If this object holds a video, its image fields are just the poster -
        # capture the video and skip the still images at THIS level.
        has_video = any(
            isinstance(v, str) and v.startswith("http")
            and ("video" in str(k).lower() or media_kind(v) == "video")
            for k, v in obj.items()
        )
        photo_candidates = []  # (key_lower, url) - usually sizes of ONE photo
        for key, val in obj.items():
            kl = str(key).lower()
            if isinstance(val, str) and val.startswith("http"):
                if any(b in kl for b in SKIP_URL_KEY_FRAGMENTS):
                    continue
                if any(b in val.lower() for b in SKIP_URL_PATH_FRAGMENTS):
                    continue
                if "video" in kl or media_kind(val) == "video":
                    if val not in _seen:
                        _seen.add(val)
                        found.append((val, "video"))
                elif media_kind(val) == "photo" and not has_video:
                    photo_candidates.append((kl, val))
            elif isinstance(val, (dict, list)):
                found.extend(collect_media_urls(val, _depth + 1, _seen))
        # Multiple image URLs on one object are resolution variants of the same
        # photo - keep only the highest-resolution one. Distinct photos arrive
        # as separate objects in a list, handled by the recursion above.
        if photo_candidates:
            best_url = max(photo_candidates, key=lambda kv: _photo_score(kv[0]))[1]
            if best_url not in _seen:
                _seen.add(best_url)
                found.append((best_url, "photo"))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(collect_media_urls(item, _depth + 1, _seen))
    return found


def _photo_score(key_lower):
    """Higher score = more likely the full-resolution URL for a photo."""
    score = 0
    if any(f in key_lower for f in PHOTO_KEY_PREFER):
        score += 10
    if any(f in key_lower for f in PHOTO_KEY_AVOID):
        score -= 10
    return score


def collect_media_entries(it):
    """Return [(url, dt, ident, kind), ...] for every photo/video in an activity.

    Works across all activity types: scans the media object (`activiable`) for
    any attached media, not just a single primary photo.
    """
    # Only read media from the activity's own media object (`activiable`). The
    # item-level `photo_url` is unreliable - for learning activities it points
    # to a teacher's profile picture, not content.
    media = it.get("activiable")
    if not isinstance(media, dict):
        media = it.get("activable") if isinstance(it.get("activable"), dict) else None
    if media is None:
        return []
    outer_dt = find_capture_dt(it) or find_capture_dt(media)
    resource_id = media.get("id")
    entries = []
    for url, kind in collect_media_urls(media):
        if kind == "video":
            # Video URLs are randomized 'open-uri' names that change on every
            # request, so they are NOT a stable identity. Use the activity's
            # resource id (a UUID) instead, so the same video always maps to the
            # same filename across feeds and re-runs.
            ident = resource_id or id_from_url(url) or stable_media_ident(url)
        else:
            # Photo URLs carry a stable file UUID in the path.
            ident = id_from_url(url) or stable_media_ident(url)
        entries.append((url, outer_dt, str(ident), kind))
    return entries


def record_dedup_key(it):
    """A stable de-dup key for an activity record. Uses the API `id` when present;
    otherwise a deterministic SHA-256 over the record's identifying fields, so that
    multiple id-less records don't all collapse into a single `None` key (which
    would silently drop every id-less activity but the first)."""
    rid = it.get("id")
    if rid is not None:
        return rid
    media_ids = sorted(ident for _, _, ident, _ in collect_media_entries(it))
    canonical = json.dumps({
        "type": it.get("activity_type"),
        "when": it.get("activity_time") or it.get("activity_date"),
        "kids": sorted(str(k) for k in (it.get("kid_ids") or [])),
        "media": media_ids,
        "comment": it.get("comment"),
    }, sort_keys=True, default=str)
    return "sha:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def fetch_all_records(session, base, kids, start_date, end_date,
                      debug=False, out_dir=None, reauth=None):
    """Walk the daily-activities feed (JSON only, no downloads) and return the
    list of activity records, deduped by id. Walks month-by-month because the
    feed caps how much it returns per query."""
    url = base + ACTIVITIES_PATH
    records, record_ids = [], set()
    type_counts, type_samples = {}, {}
    for kid_id in kids:
        for win_from, win_to in month_windows(start_date, end_date):
            page = 1
            while True:
                params = {
                    "kid_id": kid_id,
                    "filters[daily_activity][date_from]": win_from,
                    "filters[daily_activity][date_to]": win_to,
                    "page": page,
                }
                payload = fetch_json(session, url, params, ACTIVITIES_PATH, reauth=reauth)
                if payload is None:
                    break
                items = payload.get("daily_activities") if isinstance(payload, dict) else None
                if items is None:
                    items = extract_items(payload)
                if not items:
                    break
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    atype = it.get("activity_type", "unknown")
                    type_counts[atype] = type_counts.get(atype, 0) + 1
                    if debug:
                        type_samples.setdefault(atype, it)
                    rid = record_dedup_key(it)
                    if rid not in record_ids:
                        record_ids.add(rid)
                        records.append(it)
                page += 1
                time.sleep(POLITE_DELAY)

    if debug and type_counts:
        summary = ", ".join(f"{t}:{c}" for t, c in sorted(type_counts.items()))
        print(f"  [debug] Activity types seen: {summary}")
    if debug and out_dir and type_samples:
        dump_path = os.path.join(out_dir, "debug_activities.json")
        try:
            # Scrub signed/expiring URLs from the samples just like feed.json — the
            # debug dump embeds real activity objects with signed media links.
            payload = scrub_signed_urls({"counts": type_counts, "samples": type_samples})
            write_private_json(dump_path, payload)
            print(f"  [debug] wrote one sample of each activity type to {dump_path}")
        except Exception as e:
            print(f"  [debug] could not write dump: {e}")
    return records


def scrub_signed_urls(obj):
    """Recursively strip the query string AND fragment from every http(s) URL
    (deep-copies the data).

    feed.json / debug dumps embed full-resolution media URLs whose query carries
    time-limited signatures or tokens (`Signature=`, `X-Amz-*`, `token=`, ...). We
    drop the ENTIRE query+fragment — unconditionally, regardless of parameter name
    or capitalization — so a shared archive can never hand out a working link to
    the media. This is safe because the scrapbook only needs the URL's path (via
    `id_from_url`, which already ignores the query) to find the local file."""
    if isinstance(obj, dict):
        return {k: scrub_signed_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_signed_urls(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        return obj.split("?", 1)[0].split("#", 1)[0]
    return obj


def write_private_json(path, data):
    """Write `data` as JSON, restricting the file to the owner on POSIX. These
    JSON files (feed.json, debug dump) hold a child's activity history, so we
    keep them out of a group-/world-readable default umask where we can."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def in_range(dt, since_dt, until_dt):
    """True if dt falls within the (optional) since/until bounds."""
    if dt is None:
        return True
    if since_dt and dt < since_dt:
        return False
    if until_dt and dt > until_dt:
        return False
    return True


def class_spans(records):
    """Map each class/room name -> [first_date, last_date, count] from the feed.
    Class only appears on attendance records (activiable.section.name)."""
    spans = {}
    for it in records:
        if not isinstance(it, dict):
            continue
        act = it.get("activiable")
        sec = act.get("section") if isinstance(act, dict) else None
        name = sec.get("name").strip() if isinstance(sec, dict) and sec.get("name") else None
        if not name:
            continue
        d = it.get("activity_date")
        if not d:
            dt = find_capture_dt(it)
            d = dt.strftime("%Y-%m-%d") if dt else None
        if not d:
            continue
        d = d[:10]
        s = spans.setdefault(name, [d, d, 0])
        s[0] = min(s[0], d)
        s[1] = max(s[1], d)
        s[2] += 1
    return spans


def download_records(session, media_session, records, out_dir, since_dt, until_dt,
                     stats, seen=None, overwrite=False, kinds_filter=None):
    """Download the photos/videos attached to the given activity records."""
    total = len(records)
    for idx, it in enumerate(records):
        for media_url, dt, ident, kind in collect_media_entries(it):
            if kinds_filter and kind not in kinds_filter:
                continue
            default_ext = ".mp4" if kind == "video" else ".jpg"
            save_media(session, media_session, media_url, dt, kind, ident, out_dir,
                       since_dt, stats, default_ext, seen=seen, overwrite=overwrite,
                       until_dt=until_dt)
        if total and (idx + 1) % 200 == 0:
            print(f"  ...scanned {idx + 1}/{total} activities "
                  f"(downloaded {stats['downloaded']}, skipped {stats['skipped_exist']})")


def make_zip(out_dir):
    """Bundle the whole output folder into a single .zip beside it."""
    base = os.path.join(os.path.dirname(out_dir), "Procare Scrapbook")
    print("\nZipping everything into one file (this can take a while)...")
    archive = shutil.make_archive(base, "zip", root_dir=out_dir)
    size_mb = os.path.getsize(archive) / (1024 * 1024)
    print(f"Created: {archive}  ({size_mb:,.0f} MB)")


def announce_scrapbook(out_dir, pages):
    landing = os.path.join(out_dir, "Open Scrapbook.html")
    print(f"\nScrapbook built: {pages} month page(s).")
    print(f"Open this file to view it:\n  {landing}")
    # Try to open it automatically (best-effort; ignore if headless).
    try:
        if sys.platform.startswith("win"):
            os.startfile(landing)  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", landing], check=False)
    except Exception:
        pass


def build_parser():
    ap = argparse.ArgumentParser(description="Download all photos & videos from Procare, "
                                             "and optionally build a browsable scrapbook.")
    ap.add_argument("--email", help="Procare account email")
    ap.add_argument("--out", default="procare_media", help="Output directory (default: procare_media)")
    ap.add_argument("--since", help="Only include media on/after this date (YYYY-MM-DD)")
    ap.add_argument("--until", help="Only include media on/before this date (YYYY-MM-DD)")
    ap.add_argument("--scrapbook", action="store_true",
                    help="After downloading, build a browsable HTML scrapbook of the whole feed")
    ap.add_argument("--scrapbook-only", action="store_true",
                    help="Don't download media; just (re)build the scrapbook from feed.json "
                         "(or fetch the feed if feed.json is missing)")
    ap.add_argument("--zip", action="store_true",
                    help="Bundle the whole output folder into a single Procare Scrapbook.zip")
    ap.add_argument("--debug", action="store_true",
                    help="Dump one sample of each activity type to debug_activities.json")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-download and overwrite files that already exist (e.g. to "
                         "replace corrupted ones)")
    ap.add_argument("--videos-only", action="store_true",
                    help="Only process videos (skip the photo/activity scan)")
    ap.add_argument("--school", help="School name to show on the scrapbook "
                                     "(auto-detected from your account if omitted)")
    ap.add_argument("--class-name", dest="class_name",
                    help="Class/room name to show on the scrapbook "
                         "(auto-detected from the feed if omitted)")
    ap.add_argument("--version", action="version", version=f"Procare Downloader v{APP_VERSION}",
                    help="Print the version and exit")
    ap.add_argument("--no-update-check", action="store_true",
                    help="Don't check GitHub for a newer version on startup")
    return ap


def guided(args):
    """Interactive menu for when the program is launched with no arguments
    (e.g. the .exe is double-clicked). Mutates and returns `args`."""
    args._interactive = True
    print("=" * 52)
    print("  Procare Photo, Video & Scrapbook Downloader")
    print(f"  version {APP_VERSION}")
    print("=" * 52)
    print()
    print("What would you like to do?")
    print("  [1] Download photos & videos AND build the scrapbook  (recommended)")
    print("  [2] Download photos & videos only")
    print("  [3] Rebuild the scrapbook only (no re-downloading)")
    choice = input("Type 1, 2, or 3 then press Enter (default 1): ").strip() or "1"
    if choice == "2":
        args.scrapbook = False
    elif choice == "3":
        args.scrapbook_only = True
    else:
        args.scrapbook = True
    print()
    return args


def _parse_ymd(value):
    """Parse a YYYY-MM-DD string to a datetime, or None (blank/invalid)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(f"  (couldn't read the date '{value}', ignoring it)")
        return None


def choose_scope(records):
    """Interactive menu: choose how much to download. Always prompts.
    Returns (since_dt, until_dt, class_name)."""
    items = sorted(class_spans(records).items(), key=lambda kv: kv[1][0])
    print("What would you like to download?")
    print("  [1] Everything (all available history)   (default)")
    for i, (name, (d0, d1, _)) in enumerate(items, start=2):
        print(f'  [{i}] Just "{name}"  ({d0} to {d1})')
    custom = len(items) + 2
    print(f"  [{custom}] A custom date range")
    choice = input("Pick a number then Enter (default 1): ").strip() or "1"
    print()

    if choice.isdigit():
        n = int(choice)
        if 2 <= n < custom and items:                      # a specific class
            name, (d0, d1, _) = items[n - 2]
            return (_parse_ymd(d0),
                    _parse_ymd(d1).replace(hour=23, minute=59, second=59), name)
        if n == custom:                                    # custom date range
            since = _parse_ymd(input("  Start date (YYYY-MM-DD, blank = earliest): "))
            until = _parse_ymd(input("  Finish date (YYYY-MM-DD, blank = today): "))
            if until:
                until = until.replace(hour=23, minute=59, second=59)
            print()
            return since, until, None
    # default: everything (use the single class name for the title if there is one)
    return None, None, (items[0][0] if len(items) == 1 else None)


def main():
    parser = build_parser()
    args = parser.parse_args()
    # Offer to self-update to the latest release before doing anything else.
    # This never raises and no-ops from source / when offline / when up to date.
    if not args.no_update_check:
        try:
            updater.self_update(APP_VERSION)
        except Exception:
            pass
    # No command-line arguments (e.g. double-clicked .exe) -> friendly menu.
    guided_mode = len(sys.argv) == 1
    if guided_mode:
        args = guided(args)
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
    except SystemExit as e:
        # sys.exit(str) prints straight to stderr and the window would vanish
        # before a double-click user ever reads it -- show it and keep going.
        if e.code is not None:
            print(e.code)
    except Exception as e:
        print(f"\nSomething went wrong: {e}")
    # When double-clicked, keep the window open so the user can read the result.
    if guided_mode:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def run(args):
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    # feed.json lives inside the Scrapbook folder to keep the root tidy; also
    # look in the old root location for backward compatibility.
    feed_path = os.path.join(out_dir, "Scrapbook", "feed.json")
    legacy_feed = os.path.join(out_dir, "feed.json")
    read_feed = feed_path if os.path.exists(feed_path) else legacy_feed
    want_scrapbook = args.scrapbook or args.scrapbook_only

    # Fast path: rebuild the scrapbook from a saved feed.json with no login.
    if args.scrapbook_only and os.path.exists(read_feed):
        print("Rebuilding scrapbook from existing feed.json (no login needed)...")
        with open(read_feed, encoding="utf-8") as fh:
            data = json.load(fh)
        import scrapbook
        sections = data.get("sections")
        if sections is None:  # legacy feed.json (single merged scrapbook)
            kids = data.get("kids") or []
            who = ", ".join(n for n in (scrapbook.first_name(k) for k in kids) if n) or "My Child"
            sections = [{"name": who, "class_name": args.class_name or data.get("class_name"),
                         "folder": "", "records": data.get("activities", [])}]
        pages = scrapbook.build_scrapbook(sections, out_dir,
                                          school=args.school or data.get("school"))
        announce_scrapbook(out_dir, pages)
        if args.zip:
            make_zip(out_dir)
        return

    _warn_if_low_disk_space(out_dir)

    email = args.email or input("Procare email: ").strip()
    password = getpass.getpass("Procare password (input hidden): ")

    def parse_date(value, flag):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"{flag} must be in YYYY-MM-DD format, e.g. 2024-09-01")

    since_dt = parse_date(args.since, "--since")
    until_dt = parse_date(args.until, "--until")
    if until_dt:  # make --until inclusive of the whole day
        until_dt = until_dt.replace(hour=23, minute=59, second=59)
    interactive = getattr(args, "_interactive", False)

    if not HAVE_PIEXIF:
        print("Note: 'piexif' not installed — photos will download but EXIF dates won't be "
              "embedded (file modified-times are still set). Install with: pip install piexif\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "procare-media-downloader/1.0"})
    # A SEPARATE, unauthenticated session for media downloads. Signed CDN/S3 URLs
    # authorize themselves; sending them the bearer token would leak it off-domain.
    # `download_file` uses the authed `session` only for allowlisted Procare hosts.
    media_session = requests.Session()
    media_session.headers.update({"User-Agent": "procare-media-downloader/1.0"})

    print("Logging in...")
    base, _ = authenticate(session, email, password)
    print(f"Authenticated. Saving to: {out_dir}\n")
    school = args.school  # shown only if explicitly provided; not auto-detected

    def reauth():
        """Re-login if the session token expires during a long run."""
        authenticate(session, email, password)

    kids_meta = get_kids_meta(session, base)
    kids = [k["id"] for k in kids_meta]
    print(f"Found {len(kids)} child profile(s) on the account.\n")
    if not kids_meta:
        # No child profiles at all: still run the normal single-section pipeline
        # below (activity feed walk finds nothing, since kids=[]) so the
        # account-wide gallery - fetched unconditionally per section - is what
        # actually supplies the media.
        print("  No child profiles found; using the account-wide gallery.\n")
        kids_meta = [{"id": None, "name": "", "first_name": ""}]

    stats = {"downloaded": 0, "skipped_exist": 0, "skipped_old": 0, "failed": 0}
    download = not args.scrapbook_only  # scrapbook-only doesn't re-download media

    # Decide how much of the feed to walk. For the interactive class picker we
    # need the full history (to list every class); otherwise just the range.
    want_picker = interactive and not args.since and not args.until
    walk_start = ACTIVITY_EARLIEST_DEFAULT if want_picker else (
        since_dt.date() if since_dt else ACTIVITY_EARLIEST_DEFAULT)
    walk_end = date.today() if want_picker else (
        until_dt.date() if until_dt else date.today())

    print("Reading the activity feed — this walks your whole history month-by-month,")
    print("so it can take several minutes for years of history (it isn't frozen)...")
    all_records = fetch_all_records(session, base, kids, walk_start, walk_end,
                                    debug=args.debug, out_dir=out_dir, reauth=reauth)

    import scrapbook

    # Build one "section" per child from the ACTIVITY feed. Each child gets its own
    # date-range choice, its own class name, and (with siblings) its own subfolder.
    multi = len(kids_meta) > 1
    sections, used_folders = [], set()
    for kid in kids_meta:
        kid_id = kid.get("id")
        who = scrapbook.first_name(kid) or "My Child"
        kid_records = ([r for r in all_records if kid_id in (r.get("kid_ids") or [])]
                       if multi else all_records)

        c_since, c_until = since_dt, until_dt
        picked = None
        if want_picker:
            if multi:
                print(f"\n--- {who} ---")
            c_since, c_until, picked = choose_scope(kid_records)
            if c_since or c_until:
                lo = c_since.strftime("%Y-%m-%d") if c_since else "the beginning"
                hi = c_until.strftime("%Y-%m-%d") if c_until else "today"
                print(f"{who}: {lo} to {hi}\n")

        sel = [r for r in kid_records if in_range(find_capture_dt(r), c_since, c_until)]
        cls = args.class_name or picked or scrapbook.detect_class_name(sel)

        folder = ""
        if multi:
            folder = scrapbook.safe_name(who)
            if folder in used_folders:
                folder = f"{folder} ({kid_id[:6]})" if kid_id else f"{folder} (2)"
            used_folders.add(folder)

        sections.append({"name": who, "class_name": cls, "folder": folder, "kid_id": kid_id,
                         "records": sel, "since": c_since, "until": c_until})

    # Fold in the gallery. Daycares that upload straight to the gallery (bypassing
    # activities) expose media here; the gallery is account-wide and child-agnostic
    # (it ignores kid_id), so distribute_gallery attributes each item where it can,
    # dedups against what the activity feed already found, and routes anything it
    # can't attribute to a shared bucket instead of an arbitrary child.
    # Walk the same date range as the activity feed so gallery media older than
    # Procare's ~1-year activity cap is reachable (the endpoints are date-filtered).
    gallery_kids = [k.get("id") for k in kids_meta]
    total_steps = gallery_step_count(gallery_kids, walk_start, walk_end)
    print("\nChecking the photo/video gallery for older media. This walks your whole")
    print("history month-by-month, so it can take several minutes to tens of minutes")
    print("for years of history — it is NOT frozen. You can safely stop and re-run")
    print("later (already-downloaded files are skipped), or use a date range to limit it.")
    gallery_meta = collect_gallery(session, base, gallery_kids, walk_start, walk_end,
                                   reauth=reauth, progress=_gallery_progress(total_steps))
    print()   # finish the \r progress line
    shared_records = distribute_gallery(gallery_meta, sections, since_dt, until_dt)
    if shared_records:
        # Only appears with siblings (single-child galleries fold into that child).
        folder = scrapbook.safe_name("Shared Gallery")
        used_folders.add(folder)
        sections.append({"name": "Shared Gallery", "class_name": None, "folder": folder,
                         "kid_id": None, "records": shared_records,
                         "since": since_dt, "until": until_dt, "shared": True})
        print(f"\n{len(shared_records)} gallery item(s) not tied to a specific child "
              f"-> 'Shared Gallery'.")

    if download:
        kinds_filter = {"video"} if args.videos_only else None
        for s in sections:
            m_dir = scrapbook.media_root(out_dir, s["folder"])
            os.makedirs(m_dir, exist_ok=True)
            if multi:
                print(f"\nDownloading {s['name']}'s media — {len(s['records'])} item(s)...")
            else:
                print(f"Downloading media from {len(s['records'])} item(s)...")
            # Fresh dedup set per section so shared media lands in each folder.
            download_records(session, media_session, s["records"], m_dir, s["since"],
                             s["until"], stats, seen=set(), overwrite=args.overwrite,
                             kinds_filter=kinds_filter)
        ranged = any(s["since"] or s["until"] for s in sections)
        _print_download_summary(stats, out_dir, ranged)

    if want_scrapbook:
        os.makedirs(os.path.dirname(feed_path), exist_ok=True)
        feed_data = {"generated_at": datetime.now().isoformat(), "school": school,
                     "sections": [{"name": s["name"], "class_name": s["class_name"],
                                   "folder": s["folder"], "records": s["records"]}
                                  for s in sections]}
        # Strip signed/expiring query strings and keep the file owner-only (POSIX).
        write_private_json(feed_path, scrub_signed_urls(feed_data))
        pages = scrapbook.build_scrapbook(
            [{"name": s["name"], "class_name": s["class_name"], "folder": s["folder"],
              "records": s["records"], "shared": s.get("shared", False)} for s in sections],
            out_dir, school=school)
        announce_scrapbook(out_dir, pages)

    if args.zip:
        make_zip(out_dir)


LOW_DISK_SPACE_BYTES = 2 * 1024 ** 3  # 2 GB — years of full-res photos/video add up fast


def _warn_if_low_disk_space(out_dir):
    try:
        free = shutil.disk_usage(out_dir).free
    except OSError:
        return  # e.g. no such path yet on some platforms -- not worth failing over
    if free < LOW_DISK_SPACE_BYTES:
        free_gb = free / 1024 ** 3
        print(f"\nHeads up: only {free_gb:.1f} GB free on this drive. Downloading years of")
        print("full-resolution photos and videos can use many GB -- free up space first")
        print("if the run fails partway through.")


def _print_download_summary(stats, out_dir, ranged):
    print("\nDownload summary:")
    print(f"  Downloaded:        {stats['downloaded']}")
    print(f"  Skipped (existing):{stats['skipped_exist']:>4}")
    if ranged:
        print(f"  Skipped (out of range): {stats['skipped_old']}")
    print(f"  Failed:            {stats['failed']}")
    print(f"  Files are in: {out_dir}  (organized by month)")
    nothing_found = not any(stats[k] for k in ("downloaded", "skipped_exist", "skipped_old"))
    if nothing_found:
        print("\nNothing matched that selection. If this doesn't look right, try")
        print("running again with 'Everything' or a wider date range.")


if __name__ == "__main__":
    main()
