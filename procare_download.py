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
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Run:  pip install -r requirements.txt")

try:
    import piexif
    HAVE_PIEXIF = True
except ImportError:
    HAVE_PIEXIF = False  # photos still download; EXIF write is skipped with a warning


# Procare changed domains over time; try the current one first, then the legacy one.
BASE_URLS = [
    "https://api-school.procareconnect.com/api/web/",
    "https://api-school.kinderlime.com/api/web/",
]

# Videos come from their own simple paginated endpoint.
VIDEO_PATH = "parent/videos/"
# Photos must be pulled from the daily-activities feed (the bare parent/photos/
# endpoint now returns HTTP 400). Each activity item carries an activity_type and
# the real media object under the (Procare-misspelled) "activiable" key.
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

REQUEST_TIMEOUT = 60
RETRIES = 4
POLITE_DELAY = 0.25  # seconds between requests, to be gentle on the API


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def authenticate(session, email, password):
    """Try each base URL; on success set the auth header and return (base, user_dict)."""
    last_error = None
    for base in BASE_URLS:
        try:
            resp = session.post(
                base + "auth/",
                json={"email": email, "password": password},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            last_error = f"Could not reach {base}: {e}"
            continue

        if resp.status_code == 404:
            last_error = f"{base} returned 404 (wrong domain), trying next."
            continue
        if resp.status_code in (401, 403):
            sys.exit("Login failed: email or password is incorrect.")
        if resp.status_code >= 400:
            last_error = f"{base} returned HTTP {resp.status_code}: {resp.text[:200]}"
            continue

        try:
            user = resp.json()["user"]
            token = user["auth_token"]
        except (ValueError, KeyError, TypeError):
            last_error = f"Unexpected login response from {base}: {resp.text[:200]}"
            continue

        session.headers.update({"Authorization": f"Bearer {token}"})
        return base, user

    sys.exit(f"Authentication failed. Last error:\n  {last_error}")


def find_school_name(obj, _depth=0):
    """Best-effort: dig a school name out of the login `user` payload."""
    if _depth > 4 or not isinstance(obj, dict):
        return None
    # Any key mentioning "school": a name string, a {name:...}, or a [{name:...}].
    for key, val in obj.items():
        kl = str(key).lower()
        if "school" not in kl:
            continue
        if isinstance(val, str):
            if "name" in kl and val.strip():   # school_name / current_school_name
                return val.strip()
        elif isinstance(val, dict):
            nm = val.get("name")
            if isinstance(nm, str) and nm.strip():
                return nm.strip()
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and isinstance(item.get("name"), str) \
                        and item["name"].strip():
                    return item["name"].strip()
    # Recurse a little for deeper/nested shapes.
    for val in obj.values():
        if isinstance(val, dict):
            found = find_school_name(val, _depth + 1)
            if found:
                return found
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    found = find_school_name(item, _depth + 1)
                    if found:
                        return found
    return None


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
def fetch_json(session, url, params, label=""):
    for attempt in range(RETRIES):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            print(f"  ! HTTP {resp.status_code} on {label or url} "
                  f"(params={params}); stopping this feed.")
            return None
        except requests.RequestException as e:
            if attempt == RETRIES - 1:
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


def download_file(session, url, dest):
    """Download `url` to `dest`. Returns (ok, head_bytes).

    head_bytes are the first bytes of the file so the caller can verify the
    real type. We keep the session's auth header: Procare-proxied media URLs
    need it, and `requests` automatically drops it on cross-host redirects
    (e.g. to S3), so signed CDN links still work.
    """
    for attempt in range(RETRIES):
        try:
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT,
                             allow_redirects=True) as resp:
                if resp.status_code != 200:
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return False, b""

                expected = resp.headers.get("Content-Length")
                expected = int(expected) if expected and expected.isdigit() else None

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

                # Reject truncated downloads and obviously-too-small error bodies.
                bad = (written == 0
                       or (expected is not None and written != expected))
                if bad:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
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


def save_media(session, url, dt, label, ident, out_dir, since_dt, stats,
               default_ext, seen=None, overwrite=False):
    """Download one media item into its monthly folder and timestamp it.

    `seen` is a set used to dedup the same file across feeds within one run
    (e.g. a video that appears in both the gallery and the activity feed).
    """
    if not url:
        stats["failed"] += 1
        return
    if dt is None:
        dt = datetime.now()
    if since_dt and dt < since_dt:
        stats["skipped_old"] += 1
        return

    ident = str(ident or id_from_url(url) or abs(hash(url)) % 10**8)
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
    ok, head = download_file(session, url, tmp)
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


def process_simple_feed(session, base, label, path, out_dir, since_dt, stats,
                        seen=None, overwrite=False):
    """Paginated feed where each item is itself the media (e.g. parent/videos/)."""
    default_ext = ".jpg" if label == "photo" else ".mp4"
    page = 1
    while True:
        payload = fetch_json(session, base + path, {"page": page}, path)
        if payload is None:
            break
        items = extract_items(payload)
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            # For videos, grab the real video URL (not the poster image).
            url = find_video_url(item) if label == "video" else None
            if not url:
                url = find_media_url(item)
            # Identify by URL filename so the same file dedups against the
            # activity feed (which only ever sees the URL).
            save_media(session, url, find_capture_dt(item), label,
                       id_from_url(url) if url else None,
                       out_dir, since_dt, stats, default_ext, seen=seen, overwrite=overwrite)
        print(f"  ...{label}s page {page} done "
              f"(downloaded {stats['downloaded']}, skipped {stats['skipped_exist']})")
        page += 1
        time.sleep(POLITE_DELAY)


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
            ident = resource_id or id_from_url(url)
        else:
            # Photo URLs carry a stable file UUID in the path.
            ident = id_from_url(url)
        entries.append((url, outer_dt, str(ident), kind))
    return entries


def process_activities(session, base, kids, out_dir, since_dt, stats,
                       seen=None, debug=False, overwrite=False, records=None,
                       download=True, kinds_filter=None):
    """Download photos AND videos from the daily-activities feed (all types).

    Walks the timeline month-by-month (the feed caps how much it returns per
    query). Media can be attached to any activity type (learning, observation,
    incident, note, ...), so we scan every activity for attached photos/videos
    rather than filtering to photo_activity only. Videos here dedup against the
    dedicated videos feed via `seen`.

    If `records` (a list) is given, every activity is appended to it (deduped by
    `id`) so the caller can build the scrapbook from the full feed.
    """
    start_date = since_dt.date() if since_dt else ACTIVITY_EARLIEST_DEFAULT
    end_date = date.today()
    url = base + ACTIVITIES_PATH
    type_counts = {}          # activity_type -> count (for visibility/debug)
    type_samples = {}         # activity_type -> one raw item (debug only)
    record_ids = set()        # dedup activity records across kids

    for kid_id in kids:
        for win_from, win_to in month_windows(start_date, end_date):
            page = 1
            month_found = 0
            while True:
                params = {
                    "kid_id": kid_id,
                    "filters[daily_activity][date_from]": win_from,
                    "filters[daily_activity][date_to]": win_to,
                    "page": page,
                }
                payload = fetch_json(session, url, params, ACTIVITIES_PATH)
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
                    if records is not None:
                        rid = it.get("id")
                        if rid not in record_ids:
                            record_ids.add(rid)
                            records.append(it)
                    if not download:
                        continue
                    for media_url, dt, ident, kind in collect_media_entries(it):
                        if kinds_filter and kind not in kinds_filter:
                            continue
                        default_ext = ".mp4" if kind == "video" else ".jpg"
                        save_media(session, media_url, dt, kind, ident,
                                   out_dir, since_dt, stats, default_ext,
                                   seen=seen, overwrite=overwrite)
                        month_found += 1
                page += 1
                time.sleep(POLITE_DELAY)
            if month_found:
                print(f"  ...activity media (child {kid_id}) {win_from[:7]}: {month_found} found "
                      f"(downloaded {stats['downloaded']}, skipped {stats['skipped_exist']})")

    # Show what activity types exist, so missing photos are easy to diagnose.
    if type_counts:
        summary = ", ".join(f"{t}:{c}" for t, c in sorted(type_counts.items()))
        print(f"\n  Activity types seen: {summary}")
    if debug and type_samples:
        dump_path = os.path.join(out_dir, "debug_activities.json")
        try:
            import json
            with open(dump_path, "w", encoding="utf-8") as fh:
                json.dump({"counts": type_counts, "samples": type_samples},
                          fh, indent=2, default=str)
            print(f"  [debug] wrote one sample of each activity type to {dump_path}")
        except Exception as e:
            print(f"  [debug] could not write dump: {e}")


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
    ap.add_argument("--since", help="Only download media on/after this date, format YYYY-MM-DD")
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
    return ap


def guided(args):
    """Interactive menu for when the program is launched with no arguments
    (e.g. the .exe is double-clicked). Mutates and returns `args`."""
    print("=" * 52)
    print("  Procare Photo, Video & Scrapbook Downloader")
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
    if choice != "2":
        s = input("Your school's name for the scrapbook (press Enter to skip): ").strip()
        if s:
            args.school = s
    print()
    return args


def main():
    parser = build_parser()
    args = parser.parse_args()
    # No command-line arguments (e.g. double-clicked .exe) -> friendly menu.
    if len(sys.argv) == 1:
        args = guided(args)
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
    # When double-clicked, keep the window open so the user can read the result.
    if len(sys.argv) == 1:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def run(args):
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    feed_path = os.path.join(out_dir, "feed.json")
    want_scrapbook = args.scrapbook or args.scrapbook_only

    # Fast path: rebuild the scrapbook from a saved feed.json with no login.
    if args.scrapbook_only and os.path.exists(feed_path):
        print("Rebuilding scrapbook from existing feed.json (no login needed)...")
        with open(feed_path, encoding="utf-8") as fh:
            data = json.load(fh)
        import scrapbook
        pages = scrapbook.build_scrapbook(
            data.get("activities", []), data.get("kids", []), out_dir,
            school=args.school or data.get("school"),
            class_name=args.class_name or data.get("class_name"))
        announce_scrapbook(out_dir, pages)
        if args.zip:
            make_zip(out_dir)
        return

    email = args.email or input("Procare email: ").strip()
    password = getpass.getpass("Procare password (input hidden): ")

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            sys.exit("--since must be in YYYY-MM-DD format, e.g. 2024-09-01")

    if not HAVE_PIEXIF:
        print("Note: 'piexif' not installed — photos will download but EXIF dates won't be "
              "embedded (file modified-times are still set). Install with: pip install piexif\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "procare-media-downloader/1.0"})

    print("Logging in...")
    base, user = authenticate(session, email, password)
    print(f"Authenticated. Saving to: {out_dir}\n")
    school = args.school or find_school_name(user)

    kids_meta = get_kids_meta(session, base)
    kids = [k["id"] for k in kids_meta]
    print(f"Found {len(kids)} child profile(s) on the account.\n")

    stats = {"downloaded": 0, "skipped_exist": 0, "skipped_old": 0, "failed": 0}
    seen = set()              # dedups within this run
    records = [] if want_scrapbook else None
    download = not args.scrapbook_only  # scrapbook-only doesn't re-download media

    # Everything (photos AND videos) comes from the activity feed: it's the only
    # source with a STABLE id per item, so downloads and the scrapbook always
    # agree on filenames. (The gallery video endpoint returns randomized URLs
    # that change per request, which can't be matched reliably.)
    if kids:
        kinds_filter = {"video"} if args.videos_only else None
        if download:
            print("Fetching photos & videos from the activity feed...")
        else:
            print("Fetching the activity feed...")
        process_activities(session, base, kids, out_dir, since_dt, stats,
                            seen=seen, debug=args.debug, overwrite=args.overwrite,
                            records=records, download=download, kinds_filter=kinds_filter)
    elif download:
        # No child profiles: fall back to the simple gallery endpoints.
        print("  No child profiles found; falling back to the gallery endpoints.")
        process_simple_feed(session, base, "video", VIDEO_PATH, out_dir, since_dt, stats,
                            seen=seen, overwrite=args.overwrite)
        process_simple_feed(session, base, "photo", "parent/photos/", out_dir,
                            since_dt, stats, seen=seen, overwrite=args.overwrite)

    if download:
        print("\nDownload summary:")
        print(f"  Downloaded:        {stats['downloaded']}")
        print(f"  Skipped (existing):{stats['skipped_exist']:>4}")
        if since_dt:
            print(f"  Skipped (too old): {stats['skipped_old']}")
        print(f"  Failed:            {stats['failed']}")
        print(f"  Files are in: {out_dir}  (organized by month, e.g. {datetime.now():%Y-%m}/)")

    if want_scrapbook:
        import scrapbook
        class_name = args.class_name or scrapbook.detect_class_name(records or [])
        with open(feed_path, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": datetime.now().isoformat(),
                       "school": school, "class_name": class_name,
                       "kids": kids_meta, "activities": records or []},
                      fh, indent=2, default=str)
        pages = scrapbook.build_scrapbook(records or [], kids_meta, out_dir,
                                          school=school, class_name=class_name)
        announce_scrapbook(out_dir, pages)

    if args.zip:
        make_zip(out_dir)


if __name__ == "__main__":
    main()
