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


# --- gallery routing helpers (mirror the structures collect_gallery produces) --- #
def _section(kid_id, records=None, folder="", since=None, until=None):
    return {"name": f"kid-{kid_id}", "class_name": None, "kid_id": kid_id,
            "folder": folder, "records": list(records or []), "since": since, "until": until}


def _gitem(kind, ident, dt, assoc=(), returned_for=()):
    url = f"https://cdn/{'photos' if kind == 'photo' else 'attachments'}/files/{ident}/main/{ident}"
    url += ".jpg" if kind == "photo" else ""
    return (kind, ident), {"url": url, "dt": dt, "assoc": set(assoc),
                           "returned_for": set(returned_for)}


def test_gallery_entry_roundtrip():
    rec = pd.gallery_entry_to_record("https://cdn/photos/files/g1/main/g1.jpg",
                                     datetime(2025, 6, 1, 10), "g1", "photo", "k1")
    assert rec["activity_type"] == "photo_activity" and rec["kid_ids"] == ["k1"]
    assert pd.collect_media_entries(rec) == [
        ("https://cdn/photos/files/g1/main/g1.jpg", datetime(2025, 6, 1, 10), "g1", "photo")]
    # Video: open-uri URL has no extension, so it must be detectable by key name.
    vrec = pd.gallery_entry_to_record("https://cdn/attachments/files/v9/original/open-uri-x",
                                      datetime(2025, 6, 1, 11), "v9", "video", "k1")
    got = pd.collect_media_entries(vrec)
    assert got == [("https://cdn/attachments/files/v9/original/open-uri-x",
                    datetime(2025, 6, 1, 11), "v9", "video")]


def test_gallery_item_kids_extraction():
    assert pd.gallery_item_kids({"kid_ids": ["a", "b"]}) == ["a", "b"]
    assert pd.gallery_item_kids({"kid_id": 7}) == ["7"]
    assert pd.gallery_item_kids({"participants": [{"id": "x"}, {"nope": 1}]}) == ["x"]
    assert pd.gallery_item_kids({"caption": "hi"}) == []       # the real, untagged case


def test_gallery_single_child_folds_in():
    s = _section("k1")
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10), returned_for={"k1"})])
    shared = pd.distribute_gallery(meta, [s], None, None)
    assert shared == [] and len(s["records"]) == 1
    assert pd.collect_media_entries(s["records"][0])[0][2] == "v1"


def test_gallery_no_kid_profiles_folds_in():
    s = _section(None)                                        # single account-wide section
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10))])
    shared = pd.distribute_gallery(meta, [s], None, None)
    assert shared == [] and len(s["records"]) == 1


def test_gallery_multichild_agnostic_goes_shared():
    # The real 2-child case: one global list returned identically for every kid,
    # no per-item child info -> a single Shared Gallery bucket, not dumped on kid1.
    s1, s2 = _section("k1"), _section("k2")
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10), returned_for={"k1", "k2"})])
    shared = pd.distribute_gallery(meta, [s1, s2], None, None)
    assert len(shared) == 1
    assert s1["records"] == [] and s2["records"] == []        # not assigned to either child
    assert shared[0]["kid_ids"] == []


def test_gallery_dedups_against_activity_feed():
    # A video already present via kid1's activity feed is NOT re-added from the gallery.
    act = video_activity("k1", "2025-06-01", "v1")
    s1, s2 = _section("k1", [act]), _section("k2")
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10), returned_for={"k1", "k2"})])
    shared = pd.distribute_gallery(meta, [s1, s2], None, None)
    assert shared == []                                       # already known via activities
    assert len(s1["records"]) == 1 and s2["records"] == []


def test_gallery_explicit_kids_attributed_to_each():
    s1, s2 = _section("k1"), _section("k2")
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10), assoc={"k1", "k2"})])
    shared = pd.distribute_gallery(meta, [s1, s2], None, None)
    assert shared == []
    assert len(s1["records"]) == 1 and len(s2["records"]) == 1


def test_gallery_per_child_subset_attributed():
    # If the endpoint returns an item for only one kid, attribute it to that kid.
    s1, s2 = _section("k1"), _section("k2")
    meta = dict([_gitem("video", "v1", datetime(2025, 6, 1, 10), returned_for={"k1"})])
    shared = pd.distribute_gallery(meta, [s1, s2], None, None)
    assert shared == [] and len(s1["records"]) == 1 and s2["records"] == []


def test_gallery_respects_date_range():
    s = _section("k1", since=datetime(2025, 6, 1))
    meta = dict([_gitem("video", "v1", datetime(2025, 1, 1, 10), returned_for={"k1"})])
    shared = pd.distribute_gallery(meta, [s], datetime(2025, 6, 1), None)
    assert shared == [] and s["records"] == []               # out of range, dropped


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


def test_scrub_signed_urls():
    rec = {"activiable": {"id": "p1",
           "main_url": "https://cdn/photos/files/p1/main/p1.jpg?Expires=99&Signature=SECRET&Key-Pair-Id=K"},
           "lower": "https://cdn/a/b.jpg?signature=secret&token=abc",
           "amz": "https://cdn/c/d.jpg?X-Amz-Signature=zzz",
           "amz_lower": "https://cdn/c/e.jpg?x-amz-signature=zzz",
           "unknown_param": "https://cdn/f/g.jpg?foo=bar",
           "fragment": "https://cdn/h/i.jpg#secretfrag",
           "plain": "https://cdn/x/y.jpg",
           "nested": [{"deep": "https://cdn/z/w.jpg?token=abc#frag"}]}
    out = pd.scrub_signed_urls(rec)
    assert out["activiable"]["main_url"] == "https://cdn/photos/files/p1/main/p1.jpg"
    assert out["lower"] == "https://cdn/a/b.jpg"                    # case-insensitive
    assert out["amz"] == "https://cdn/c/d.jpg"
    assert out["amz_lower"] == "https://cdn/c/e.jpg"
    assert out["unknown_param"] == "https://cdn/f/g.jpg"           # ANY query dropped
    assert out["fragment"] == "https://cdn/h/i.jpg"               # fragment dropped
    assert out["plain"] == "https://cdn/x/y.jpg"                   # already clean
    assert out["nested"][0]["deep"] == "https://cdn/z/w.jpg"      # nested dict in list
    blob = str(out)
    for leak in ("Signature", "signature", "token=", "X-Amz", "secretfrag", "foo=bar"):
        assert leak not in blob
    # local-file lookup still works after scrubbing (id_from_url ignores query)
    assert pd.id_from_url(out["activiable"]["main_url"]) == "p1"


def test_auth_host_allowlist():
    # Token goes ONLY to the exact Procare API hosts, over https.
    assert pd.is_procare_host("https://api-school.procareconnect.com/api/web/parent/photos/")
    assert pd.is_procare_host("https://api-school.kinderlime.com/x") is True
    # A signed CDN/S3 link must NOT be treated as a Procare host.
    assert pd.is_procare_host("https://d123.cloudfront.net/v/x.mp4?Signature=z") is False
    assert pd.is_procare_host("https://s3.amazonaws.com/bucket/x.jpg") is False
    # Deceptive look-alike host (suffix attack) is rejected.
    assert pd.is_procare_host("https://api-school.procareconnect.com.attacker.test/x") is False
    # http:// (non-TLS) is never a trusted host, even for the real domain.
    assert pd.is_procare_host("http://api-school.procareconnect.com/x") is False
    # A query string can't sneak the real host past the check either way.
    assert pd.is_procare_host("https://evil.test/?x=api-school.procareconnect.com") is False


def test_error_page_rejected():
    assert pd._looks_like_error_page("text/html", b"<!DOCTYPE html>") is True
    assert pd._looks_like_error_page("application/json; charset=utf-8", b'{"error"') is True
    assert pd._looks_like_error_page(None, b"  <html><body>nope") is True
    # Real media is accepted, including formats sniff_ext doesn't recognize.
    assert pd._looks_like_error_page("image/jpeg", b"\xff\xd8\xff\x00") is False
    assert pd._looks_like_error_page("video/x-matroska", b"\x1aE\xdf\xa3") is False  # .mkv
    assert pd._looks_like_error_page(None, b"\x00\x00\x00\x18ftypmp42") is False


def test_stable_media_ident_deterministic():
    u = "https://cdn/attachments/files/x/original/open-uri-random?Signature=changes"
    # Deterministic across calls, ignores the (changing) query, never "None"/hash().
    a = pd.stable_media_ident(u)
    b = pd.stable_media_ident("https://cdn/attachments/files/x/original/open-uri-random?Signature=other")
    assert a == b and a and a != "None" and len(a) == 20


def test_idless_records_stay_distinct():
    # Two activities with no API id must not collapse into one dedup key.
    base = {"activity_type": "note_activity", "activity_time": "2025-06-01T09:00:00-04:00",
            "kid_ids": ["k1"]}
    a = dict(base, comment="first")
    b = dict(base, comment="second")
    assert pd.record_dedup_key(a) != pd.record_dedup_key(b)
    # ...but the same content yields the same (stable) key, not a random one.
    assert pd.record_dedup_key(dict(base, comment="first")) == pd.record_dedup_key(a)


def test_idless_media_stay_distinct():
    # Two photos recognized as media (end in .jpg) but with a blank filename stem
    # must get distinct, stable idents — never both the literal "None".
    e1 = pd.collect_media_entries(
        {"activiable": {"id": None, "main_url": "https://cdn/one/.jpg"}})
    e2 = pd.collect_media_entries(
        {"activiable": {"id": None, "main_url": "https://cdn/two/.jpg"}})
    assert e1 and e2
    id1, id2 = e1[0][2], e2[0][2]
    assert id1 != id2 and "None" not in (id1, id2)


def test_lightbox_and_summary():
    out = tempfile.mkdtemp(prefix="sb_lb_")
    recs = [photo_activity("k1", "2025-06-01", "p1"), photo_activity("k1", "2025-06-02", "p2"),
            {"activity_type": "note_activity", "id": "n1", "activity_date": "2025-06-01",
             "activity_time": "2025-06-01T09:00:00-04:00", "kid_ids": ["k1"], "data": {"desc": "hi"}}]
    for r in recs:
        if r["activity_type"] == "photo_activity":
            plant(sb.media_root(out), r)
    sb.build_scrapbook([{"name": "Maya", "class_name": "Room", "folder": "", "records": recs}], out)
    land = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert 'class="stats"' in land and "2</b> photos" in land       # summary present
    assert 'id="lightbox"' in land                                  # lightbox on landing too
    mp = [f for f in os.listdir(os.path.join(out, "Scrapbook")) if f.endswith(").html")][0]
    month = open(os.path.join(out, "Scrapbook", mp), encoding="utf-8").read()
    assert "lightbox" in month and "classList.contains('media')" in month


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
