#!/usr/bin/env python3
"""Self-contained regression tests for the Procare downloader + scrapbook.

Run directly (no pytest needed):

    python tests/test_core.py

Exits non-zero if anything fails. Covers the behavior that's easy to break:
media identity, poster/avatar suppression, full-res selection, date parsing,
class detection / date-range filtering, and the scrapbook folder layout for
single vs. multiple children (including per-child media isolation).
"""
import builtins
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import procare_download as pd          # noqa: E402
import scrapbook as sb                 # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def photo_activity(kid, d, pid, caption="pic"):
    return {"activity_type": "photo_activity", "id": f"{kid}-{pid}", "activity_date": d,
            "activity_time": f"{d}T10:00:00-04:00", "kid_ids": [kid], "comment": caption,
            "activiable": {"id": pid, "main_url": f"https://cdn/photos/files/{pid}/main/{pid}.jpg",
                           "thumb_url": f"https://cdn/photos/files/{pid}/thumb/{pid}.jpg"}}


def video_activity(kid, d, vid):
    return {"activity_type": "video_activity", "id": f"{kid}-{vid}", "activity_date": d,
            "activity_time": f"{d}T11:00:00-04:00", "kid_ids": [kid],
            "activiable": {"id": vid, "is_video": True,
                           "video_file_url": f"https://cdn/attachments/files/{vid}/original/open-uri-x",
                           "main_url": f"https://cdn/photos/files/{vid}/main/open-uri-poster"}}


def attend(kid, d, cls):
    return {"activity_type": "sign_in_activity", "id": f"si-{kid}-{d}", "activity_date": d,
            "activity_time": f"{d}T08:00:00-04:00", "kid_ids": [kid],
            "activiable": {"section": {"name": cls}}}


def plant(media_dir, rec, ext=".jpg"):
    for url, dt, ident, kind in pd.collect_media_entries(rec):
        md = os.path.join(media_dir, dt.strftime("%Y-%m"))
        os.makedirs(md, exist_ok=True)
        open(os.path.join(md, pd.media_stem(dt, kind, ident) + ext), "wb").write(b"\xff\xd8\xff\x00")


def link_resolves(page_path, src):
    p = urllib.parse.unquote(src)
    return os.path.exists(os.path.normpath(os.path.join(os.path.dirname(page_path), p)))


def first_media_src(html):
    m = re.search(r'src="([^"]+\.(?:jpg|jpeg|png|mp4|mov|svg))"', html)
    return m.group(1) if m else None


def mock_input(answers, fn):
    it = iter(answers)
    orig = builtins.input
    builtins.input = lambda *a, **k: next(it)
    try:
        return fn()
    finally:
        builtins.input = orig


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_date_parsing():
    assert pd._parse_dt("2025-06-30T11:58:00.000-04:00").hour == 11
    assert pd._parse_dt("2025-06-30").year == 2025
    assert pd._parse_dt(50) is None                       # small int is not a date
    assert pd.find_capture_dt({"id": 50}) is None         # id must not be read as a date
    assert pd.find_capture_dt({"activity_time": "2025-01-02T03:04:05Z"}).month == 1


def test_media_helpers():
    assert pd.media_kind("https://x/a.JPG") == "photo"
    assert pd.media_kind("https://x/a.MP4") == "video"
    assert pd.media_kind("https://x/a.pdf") is None
    assert pd.sniff_ext(b"\x89PNG\r\n\x1a\n") == ".png"
    assert pd.sniff_ext(b"\x00\x00\x00\x18ftypmp42") == ".mp4"
    assert pd.id_from_url("https://x/p/abc123.jpg?sig=1") == "abc123"


def test_photo_full_res_and_thumb_suppressed():
    entries = pd.collect_media_entries(photo_activity("k1", "2025-06-01", "p1"))
    assert len(entries) == 1 and entries[0][3] == "photo"
    assert "/main/" in entries[0][0] and "thumb" not in entries[0][0]


def test_video_stable_id_and_poster_suppressed():
    entries = pd.collect_media_entries(video_activity("k1", "2025-06-01", "vid9"))
    assert len(entries) == 1 and entries[0][3] == "video"
    assert entries[0][2] == "vid9"                        # resource id, not the open-uri name
    assert "/attachments/" in entries[0][0]               # the real video, not the poster


def test_profile_pic_excluded():
    learning = {"activity_type": "learning_activity", "id": "l1", "activity_date": "2025-06-01",
                "activity_time": "2025-06-01T09:00:00-04:00", "kid_ids": ["k1"],
                "comment": "lesson", "activiable": {"id": "x", "urls": []},
                "photo_url": "https://cdn/profile_pics/files/t/main/teacher.jpg"}
    assert pd.collect_media_entries(learning) == []


def test_class_spans_and_range():
    recs = [attend("k1", "2024-09-03", "Daffodils"), attend("k1", "2025-01-10", "Daffodils"),
            attend("k1", "2025-09-05", "Emerald Lilies")]
    spans = pd.class_spans(recs)
    assert spans["Daffodils"][:2] == ["2024-09-03", "2025-01-10"]
    since, until = datetime(2025, 9, 1), datetime(2026, 6, 30, 23, 59, 59)
    assert pd.in_range(datetime(2025, 10, 1), since, until) is True
    assert pd.in_range(datetime(2024, 1, 1), since, until) is False


def test_choose_scope_prompts_even_single_class():
    recs = [attend("k1", "2025-09-03", "Emerald Lilies"), attend("k1", "2026-06-20", "Emerald Lilies")]
    # default -> everything, but title class still returned
    assert mock_input([""], lambda: pd.choose_scope(recs)) == (None, None, "Emerald Lilies")
    # pick the class -> its date span
    s, u, name = mock_input(["2"], lambda: pd.choose_scope(recs))
    assert name == "Emerald Lilies" and s == datetime(2025, 9, 3)
    # custom range
    s, u, name = mock_input(["3", "2025-10-01", "2025-12-31"], lambda: pd.choose_scope(recs))
    assert s == datetime(2025, 10, 1) and u == datetime(2025, 12, 31, 23, 59, 59) and name is None


def test_first_name():
    assert sb.first_name({"name": "Patel, Maya"}) == "Maya"
    assert sb.first_name({"name": "Maya Patel"}) == "Maya"
    assert sb.first_name({"first_name": "Maya", "name": "Patel, Maya"}) == "Maya"


def test_layout_single_child():
    out = tempfile.mkdtemp(prefix="sb_single_")
    rec = photo_activity("k1", "2025-06-01", "p1")
    plant(sb.media_root(out), rec)
    sb.build_scrapbook([{"name": "Maya", "class_name": "Emerald Lilies",
                         "folder": "", "records": [rec]}], out)
    # tidy root: only the landing + Media/ + Scrapbook/
    assert set(os.listdir(out)) == {"Open Scrapbook.html", "Media", "Scrapbook"}
    land = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert "Maya&#x27;s Year in Emerald Lilies" in land
    mp = [f for f in os.listdir(os.path.join(out, "Scrapbook")) if f.endswith(").html")][0]
    mpath = os.path.join(out, "Scrapbook", mp)
    src = first_media_src(open(mpath, encoding="utf-8").read())
    assert src and src.startswith("../Media/") and link_resolves(mpath, src)


def test_layout_multi_child_isolated():
    out = tempfile.mkdtemp(prefix="sb_multi_")
    rM = photo_activity("k1", "2025-06-01", "m1", caption="Maya pic")
    rL = photo_activity("k2", "2025-07-01", "l1", caption="Leo pic")
    plant(sb.media_root(out, "Maya"), rM)
    plant(sb.media_root(out, "Leo"), rL)
    sb.build_scrapbook([{"name": "Maya", "class_name": "Emerald Lilies", "folder": "Maya", "records": [rM]},
                        {"name": "Leo", "class_name": "Daffodils", "folder": "Leo", "records": [rL]}],
                       out, school="Brunswick")
    assert set(os.listdir(out)) == {"Open Scrapbook.html", "Media", "Scrapbook"}
    master = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert "Choose a child" in master
    maya_mp = [f for f in os.listdir(os.path.join(out, "Scrapbook", "Maya")) if f.endswith(").html")][0]
    mpath = os.path.join(out, "Scrapbook", "Maya", maya_mp)
    mhtml = open(mpath, encoding="utf-8").read()
    assert "Emerald Lilies" in mhtml and "Leo pic" not in mhtml     # per-child isolation
    src = first_media_src(mhtml)
    assert src and "Media/Maya/" in src and link_resolves(mpath, src)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
