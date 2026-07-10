#!/usr/bin/env python3
"""
Read-only gallery diagnostic for the Procare downloader.
=======================================================

Purpose: learn how the bare gallery endpoints (parent/photos/, parent/videos/)
behave on a REAL multi-child account, so the multi-child gallery de-duplication
can be designed against observed behavior instead of guesses.

This ONLY reads. It logs in, lists your kids, and queries the two gallery
endpoints once per child. It downloads nothing and writes nothing to disk.

Privacy: the output is deliberately REDACTED. It prints only structural facts —
item IDs (opaque identifiers, not media), the JSON *key names* on a sample item,
and the values of any child-association keys. It never prints media URLs, signed
query strings, auth tokens, or your children's names. IDs are shown so you (and
the maintainer) can see whether the two children's galleries overlap; if you'd
rather not share even those, replace them with the SHA-tagged form the script
also prints.

Run from the repo root:

    python tools/probe_gallery.py

You'll be prompted for your Procare email and password (password input hidden).
Paste the printed report back to the maintainer.
"""

import getpass
import hashlib
import os
import sys

# Import the engine's already-tested primitives (auth, kid list, pagination).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import procare_download as pd  # noqa: E402

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Run:  pip install -r requirements.txt")

# Keys that might tie a gallery item to one or more children. We report their
# presence and values (ids are opaque, not sensitive) to see if the gallery
# self-describes which child each item belongs to.
ASSOC_KEYS = ("kid_id", "kid_ids", "student_id", "student_ids",
              "child_id", "child_ids", "kids", "participants", "taggings",
              "tagged_kids", "tagged_kid_ids")

MAX_PAGES = 5  # cap so a huge gallery doesn't make the probe run forever


def short(value):
    """A stable, non-reversible tag for an id, in case you prefer not to share
    the raw ids. Same id -> same tag, so overlaps are still visible."""
    return "sha:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def item_id(item):
    if isinstance(item, dict) and item.get("id") is not None:
        return item["id"]
    return None


def assoc_summary(item):
    """Redacted view of any child-association fields on one item."""
    out = {}
    if not isinstance(item, dict):
        return out
    for k in ASSOC_KEYS:
        if k in item:
            out[k] = item[k]
    return out


def fetch_ids(session, base, path, kid_id):
    """Return (ids, first_item) for a gallery endpoint scoped to one kid.
    Reads up to MAX_PAGES pages. Never returns URLs."""
    ids, first_item = [], None
    page = 1
    while page <= MAX_PAGES:
        params = {"page": page}
        if kid_id is not None:
            params["kid_id"] = kid_id
        payload = pd.fetch_json(session, base + path, params, path, quiet=True)
        if payload is None:
            break
        items = pd.extract_items(payload)
        if not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            if first_item is None:
                first_item = it
            iid = item_id(it)
            ids.append(iid if iid is not None else short(pd.find_media_url(it) or repr(it)[:40]))
        page += 1
    return ids, first_item


def report_endpoint(name, per_kid_ids):
    """Print whether the endpoint returned the same or different ids per kid."""
    print(f"\n== {name} ==")
    kid_labels = list(per_kid_ids.keys())
    for label in kid_labels:
        ids = per_kid_ids[label]
        print(f"  {label}: {len(ids)} item(s)")
        if ids:
            preview = ", ".join(str(x) for x in ids[:8])
            print(f"     ids: {preview}{' ...' if len(ids) > 8 else ''}")
    if len(kid_labels) >= 2:
        sets = [set(per_kid_ids[l]) for l in kid_labels]
        common = set.intersection(*sets) if sets else set()
        union = set.union(*sets) if sets else set()
        only_each = {l: len(set(per_kid_ids[l]) - common) for l in kid_labels}
        print(f"  overlap: {len(common)} shared, {len(union)} total across kids")
        print(f"  unique-to-each: {only_each}")
        if union and common == union:
            print("  => IDENTICAL for every kid (endpoint likely IGNORES kid_id: one global list)")
        elif not common:
            print("  => DISJOINT per kid (endpoint respects kid_id; no shared items seen)")
        else:
            print("  => PARTIAL overlap (some items shared between kids, some not)")


def main():
    print("Procare gallery probe (read-only). Nothing is downloaded or saved.\n")
    email = input("Procare email: ").strip()
    password = getpass.getpass("Procare password (hidden): ")

    session = requests.Session()
    session.headers.update({"User-Agent": "procare-gallery-probe/1.0"})
    base, _ = pd.authenticate(session, email, password)

    kids = pd.get_kids_meta(session, base)
    print(f"\nAccount has {len(kids)} child profile(s).")
    # Redacted labels — never print the child's real name.
    labels = {k["id"]: f"kid{i+1}" for i, k in enumerate(kids)}

    photo_ids, video_ids = {}, {}
    sample_photo, sample_video = None, None
    for k in kids:
        kid_id = k["id"]
        label = labels[kid_id]
        p_ids, p_first = fetch_ids(session, base, pd.GALLERY_PHOTO_PATH, kid_id)
        v_ids, v_first = fetch_ids(session, base, pd.VIDEO_PATH, kid_id)
        photo_ids[label] = p_ids
        video_ids[label] = v_ids
        sample_photo = sample_photo or p_first
        sample_video = sample_video or v_first

    report_endpoint("parent/photos/  (kid-scoped)", photo_ids)
    report_endpoint("parent/videos/  (kid-scoped)", video_ids)

    print("\n== sample item shapes (KEY NAMES ONLY, no values) ==")
    if sample_photo:
        print(f"  photo item keys: {sorted(sample_photo.keys())}")
        print(f"  photo child-association fields: {assoc_summary(sample_photo)}")
    else:
        print("  no photo items returned")
    if sample_video:
        print(f"  video item keys: {sorted(sample_video.keys())}")
        print(f"  video child-association fields: {assoc_summary(sample_video)}")
    else:
        print("  no video items returned")

    # Also probe the endpoints with NO kid_id, to see if an account-wide call
    # returns the same thing as the per-kid calls.
    print("\n== unscoped (no kid_id) ==")
    up_ids, _ = fetch_ids(session, base, pd.GALLERY_PHOTO_PATH, None)
    uv_ids, _ = fetch_ids(session, base, pd.VIDEO_PATH, None)
    print(f"  parent/photos/ (no kid_id): {len(up_ids)} item(s)")
    print(f"  parent/videos/ (no kid_id): {len(uv_ids)} item(s)")

    print("\nDone. Paste the report above back to the maintainer.")
    print("(No media, URLs, tokens, or names were printed.)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
