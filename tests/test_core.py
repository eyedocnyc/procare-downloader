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
import updater as up                   # noqa: E402
import hashlib as _hashlib             # noqa: E402


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


class _AuthResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _AuthSession:
    """Replays a canned response per base URL, in order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_auth_error_message_extraction():
    # Procare's real shape: {"errors": [...]}.
    assert pd.auth_error_message(
        _AuthResp(422, {"errors": ["Email and password did not match."]})
    ) == "Email and password did not match."
    # Singular string form, and a non-JSON body.
    assert pd.auth_error_message(_AuthResp(422, {"error": "nope"})) == "nope"
    assert pd.auth_error_message(_AuthResp(500, None, "<html>")) is None


def test_auth_422_is_a_credential_failure_not_a_host_failure():
    """A 422 must stop immediately, not fall through to another endpoint.

    Regression: Procare answers bad credentials with 422, so the old code
    treated it as "wrong host", tried the dead legacy host, and reported that
    host's DNS error -- hiding the real reason from the user.
    """
    # First call is online-auth; a 422 there is a definitive rejection.
    s = _AuthSession([_AuthResp(422, {"errors": ["Email and password did not match."]})])
    try:
        pd.authenticate(s, "a@b.c", "wrong")
    except SystemExit as e:
        assert "did not match" in str(e)
    else:
        raise AssertionError("expected SystemExit on a 422")
    # Stopped after the first endpoint; never touched the legacy fallback.
    assert s.calls == 1


def test_auth_reports_every_endpoint_it_tried():
    """All endpoints unreachable -> the message names them, not just the last."""
    import requests as _rq
    # online-auth + each legacy host all fail to connect.
    s = _AuthSession([_rq.RequestException("boom-online")]
                     + [_rq.RequestException("boom") for _ in pd.BASE_URLS])
    try:
        pd.authenticate(s, "a@b.c", "pw")
    except SystemExit as e:
        msg = str(e)
        assert pd.ONLINE_AUTH_URL in msg
        for base in pd.BASE_URLS:
            assert base in msg, f"{base} missing from the error message"
    else:
        raise AssertionError("expected SystemExit when nothing works")


def test_session_token_and_base_prefers_default_site():
    payload = {"auth_token": "tok",
               "sites": [{"base_url": "https://api-school.other.procareconnect.com", "is_default": False},
                         {"base_url": "https://api-school.procareconnect.com", "is_default": True}]}
    token, base = pd.session_token_and_base(payload)
    assert token == "tok"
    # The default site wins, and the /api/web/ suffix the tool expects is added.
    assert base == "https://api-school.procareconnect.com/api/web/"
    # No token or no sites -> nothing usable.
    assert pd.session_token_and_base({"sites": []}) == (None, None)
    assert pd.session_token_and_base({"auth_token": "t"}) == (None, None)


def test_auth_online_success_sets_bearer_and_resolves_host():
    """The happy path: online-auth returns a token + the account's home host."""
    s = _AuthSession([_AuthResp(200, {
        "auth_token": "TKN", "role": "carer", "access_to": "regular_requests",
        "sites": [{"base_url": "https://api-school.procareconnect.com", "is_default": True}]})])
    base, _payload = pd.authenticate(s, "a@b.c", "pw")
    assert base == "https://api-school.procareconnect.com/api/web/"
    assert s.headers["Authorization"] == "Bearer TKN"
    assert s.calls == 1                                   # legacy path not touched


def test_auth_falls_back_to_legacy_when_online_500s():
    """A server error on online-auth must fall through to /api/web/auth/."""
    s = _AuthSession([
        _AuthResp(500, None, "oops"),                    # online-auth is unhappy
        _AuthResp(200, {"user": {"auth_token": "L"}}),   # first legacy host works
    ])
    base, user = pd.authenticate(s, "a@b.c", "pw")
    assert base == pd.BASE_URLS[0] and user["auth_token"] == "L"
    assert s.headers["Authorization"] == "Bearer L"
    assert s.calls == 2


def test_auth_mfa_account_is_rejected_clearly():
    """An MFA/SSO session (token withheld) gets a specific, honest message."""
    s = _AuthSession([_AuthResp(200, {"access_to": "mfa_required", "mfa_methods": ["sms"]})])
    try:
        pd.authenticate(s, "a@b.c", "pw")
    except SystemExit as e:
        assert "two-factor" in str(e) or "single sign-on" in str(e)
    else:
        raise AssertionError("expected SystemExit for an MFA account")


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


def test_gallery_query_params_date_filter():
    # Matches the live dashboard request the endpoint requires to reach old media:
    #   parent/photos/?filters[photo][datetime_from]=2024-08-01 00:00
    #                 &filters[photo][datetime_to]=2024-08-31 23:59
    p = pd.gallery_query_params("photo", "2024-08-01", "2024-08-31", kid_id="k1", page=2)
    assert p["filters[photo][datetime_from]"] == "2024-08-01 00:00"
    assert p["filters[photo][datetime_to]"] == "2024-08-31 23:59"
    assert p["page"] == 2 and p["kid_id"] == "k1"
    v = pd.gallery_query_params("video", "2024-08-01", "2024-08-31")
    assert "filters[video][datetime_from]" in v and "kid_id" not in v  # no kid -> omitted


def test_paginate_gallery_stops_on_repeated_page():
    # A backend that ignores `page` returns the SAME non-empty page forever.
    # _paginate_gallery must detect the repeat and stop instead of looping.
    calls = {"n": 0}
    same_page = {"photos": [{"id": "p1", "main_url": "https://cdn/photos/files/p1/main/p1.jpg"}]}
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), same_page)[1]
    pd.time.sleep = lambda *a, **k: None       # don't actually wait between pages
    try:
        out = pd._paginate_gallery(None, "https://api-school.procareconnect.com/api/web/",
                                   pd.GALLERY_PHOTO_PATH, "photo", {"kid_id": "k1"})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert len(out) == 1                       # only the first page's item is kept
    assert calls["n"] == 2                     # page 1, then page 2 detected as a repeat -> stop


def test_paginate_gallery_respects_max_pages():
    # Distinct non-empty page every time (page-aware but "infinite") -> the cap stops it.
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: {"photos": [
        {"id": f"p{a[2]['page']}", "main_url": f"https://cdn/photos/files/x{a[2]['page']}/main/x.jpg"}]}
    pd.time.sleep = lambda *a, **k: None
    try:
        out = pd._paginate_gallery(None, "https://api-school.procareconnect.com/api/web/",
                                   pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert len(out) == pd.GALLERY_MAX_PAGES     # bounded, never infinite


def test_gallery_step_count_and_progress():
    from datetime import date as _date
    # 1 kid, 2 endpoints, Aug+Sep (2 months): 2 * (1 unfiltered + 2 windows) = 6.
    assert pd.gallery_step_count(["k1"], _date(2024, 8, 1), _date(2024, 9, 30)) == 6
    seen = []
    cb = pd._gallery_progress(4)
    for lbl in (None, "2024-08", "2024-09", "2024-10"):
        cb(lbl)                                 # must not raise; drives the \r line
    assert True                                 # smoke: 4 steps over total 4 = up to 100%


def test_fetch_gallery_media_runs_unfiltered_and_windowed_passes():
    # Both an unfiltered pass (backends that return the whole gallery) AND a
    # date-windowed pass (reaches media the ~1yr-capped backends hide) must fire,
    # for photos and videos, so neither kind of account regresses.
    from datetime import date as _date
    seen = []
    orig = pd.fetch_json
    pd.fetch_json = lambda *a, **k: seen.append(dict(a[2])) or None  # a[2] == params; end walk
    try:
        pd.fetch_gallery_media(None, "https://api-school.procareconnect.com/api/web/",
                               "k1", _date(2024, 8, 1), _date(2024, 9, 30))
    finally:
        pd.fetch_json = orig
    has_filter = lambda p, r: f"filters[{r}][datetime_from]" in p
    # unfiltered pass: a query with the kid but NO datetime filter
    assert any(p.get("kid_id") == "k1" and not has_filter(p, "photo") and not has_filter(p, "video")
               for p in seen)
    # date-windowed pass: datetime filters present for both resources
    assert any(has_filter(p, "photo") for p in seen)
    assert any(has_filter(p, "video") for p in seen)


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


# --------------------------------------------------------------------------- #
# self-updater
# --------------------------------------------------------------------------- #
def test_version_parsing_and_compare():
    assert up.parse_version("v1.9") == (1, 9)
    assert up.parse_version("1.10.2") == (1, 10, 2)
    assert up.parse_version("v2.0-beta") == (2,)            # stops at non-numeric segment
    assert up.parse_version("junk") == () and up.parse_version(None) == ()
    assert up.is_newer("v1.10", "1.9") is True              # 1.10 > 1.9 numerically
    assert up.is_newer("v1.9", "1.9") is False              # equal
    assert up.is_newer("1.8", "1.9") is False               # older
    assert up.is_newer("", "1.9") is False                  # unparseable never newer


def test_app_version_matches_engine():
    # The engine constant is what the updater compares against; keep them coupled.
    assert isinstance(pd.APP_VERSION, str) and up.parse_version(pd.APP_VERSION)


def test_platform_asset_selection():
    import platform
    orig = platform.system
    try:
        platform.system = lambda: "Windows"
        assert up.platform_asset() == ("ProcareDownloader-Windows.zip",
                                       "ProcareDownloader-Windows/ProcareDownloader.exe")
        platform.system = lambda: "Darwin"
        assert up.platform_asset() == ("ProcareDownloader-Mac.zip",
                                       "ProcareDownloader-Mac/ProcareDownloader")
        platform.system = lambda: "Linux"
        assert up.platform_asset() is None                  # no binary published
    finally:
        platform.system = orig


def test_sha256_file_parse_and_verify():
    blob = b"pretend-zip-bytes"
    digest = _hashlib.sha256(blob).hexdigest()
    assert up.parse_sha256_file(f"{digest}  ProcareDownloader-Mac.zip\n") == digest
    assert up.parse_sha256_file("not a hash") is None
    tmp = tempfile.mkdtemp(prefix="up_")
    p = os.path.join(tmp, "z.zip")
    open(p, "wb").write(blob)
    assert up.sha256_of(p) == digest                        # matches
    open(p, "wb").write(b"tampered")
    assert up.sha256_of(p) != digest                        # mismatch detected


def test_find_asset():
    rel = {"tag_name": "v2.0", "assets": [
        {"name": "ProcareDownloader-Mac.zip", "browser_download_url": "https://x/mac.zip"},
        {"name": "ProcareDownloader-Mac.zip.sha256", "browser_download_url": "https://x/mac.zip.sha256"}]}
    assert up.find_asset(rel, "ProcareDownloader-Mac.zip") == "https://x/mac.zip"
    assert up.find_asset(rel, "ProcareDownloader-Mac.zip.sha256") == "https://x/mac.zip.sha256"
    assert up.find_asset(rel, "missing") is None
    # a non-https url is rejected (defense against a tampered release listing)
    assert up.find_asset({"assets": [{"name": "z", "browser_download_url": "http://x/z"}]}, "z") is None


def test_self_update_noop_from_source():
    # Not frozen (running from source) -> never attempts a swap, never raises,
    # even when a newer release exists.
    orig_fetch, orig_apply = up.fetch_latest, up.apply_update
    calls = {"apply": 0}
    up.fetch_latest = lambda *a, **k: {"tag_name": "v999.0", "assets": []}
    up.apply_update = lambda *a, **k: calls.__setitem__("apply", calls["apply"] + 1) or True
    try:
        up.self_update("1.9")                # sys.frozen is False under the test runner
    finally:
        up.fetch_latest, up.apply_update = orig_fetch, orig_apply
    assert calls["apply"] == 0


def test_self_update_silent_when_offline():
    orig = up.fetch_latest
    up.fetch_latest = lambda *a, **k: None   # simulate offline / rate-limited
    try:
        up.self_update("1.9")                # must not raise
    finally:
        up.fetch_latest = orig


def test_swap_file_replaces_and_backs_up():
    d = tempfile.mkdtemp(prefix="up_swap_")
    target = os.path.join(d, "app")
    new = os.path.join(d, "downloaded", "app.new")
    os.makedirs(os.path.dirname(new))
    open(target, "wb").write(b"OLD-BINARY")
    os.chmod(target, 0o644)                   # start non-executable to prove chmod happens
    open(new, "wb").write(b"NEW-BINARY")
    backup = up._swap_file(new, target)
    assert open(target, "rb").read() == b"NEW-BINARY"          # swapped in
    assert backup and open(backup, "rb").read() == b"OLD-BINARY"  # old kept as .bak
    if os.name == "posix":                                     # Windows has no exec bit
        assert os.stat(target).st_mode & 0o111                 # executable bit set
    assert not os.path.exists(target + ".new")                 # staging cleaned up by replace


def test_windows_script_is_bounded_and_carries_args():
    s = up._windows_script(r"C:\app\ProcareDownloader.exe", r"C:\tmp\app.new",
                           r"C:\app\ProcareDownloader.exe.bak", '"--scrapbook"')
    # bounded: has a try counter + limit, and no unconditional 'goto retry'
    assert "set /a tries" in s and "GEQ 30" in s
    assert "goto retry" in s and "if %tries% GEQ 30 goto done" in s   # exit path exists
    # carries the paths and the preserved relaunch args
    assert r"C:\app\ProcareDownloader.exe" in s and r"C:\tmp\app.new" in s
    assert '"--scrapbook"' in s
    assert "del /f /q" in s                                    # self-deletes


# --- hardened _download (https-per-redirect + size cap), no real network -------- #
class _FakeResp:
    def __init__(self, status=200, headers=None, chunks=(b"data",)):
        self.status_code, self.headers, self._chunks = status, headers or {}, chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            yield c


class _FakeSession:
    """Returns queued responses in order (one per .get call)."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self._responses.pop(0)


def test_download_accepts_plain_https_200():
    d = tempfile.mkdtemp(prefix="up_dl_")
    dest = os.path.join(d, "z.zip")
    s = _FakeSession([_FakeResp(200, {"Content-Length": "4"}, (b"data",))])
    assert up._download(s, "https://cdn/z.zip", dest) is True
    assert open(dest, "rb").read() == b"data"


def test_download_rejects_http_redirect_hop():
    d = tempfile.mkdtemp(prefix="up_dl_")
    dest = os.path.join(d, "z.zip")
    # https -> 302 to http:// must be refused (no downgrade), and never written.
    s = _FakeSession([_FakeResp(302, {"Location": "http://evil/z.zip"})])
    assert up._download(s, "https://cdn/z.zip", dest) is False
    assert not os.path.exists(dest)


def test_download_rejects_oversized_content_length():
    d = tempfile.mkdtemp(prefix="up_dl_")
    dest = os.path.join(d, "z.zip")
    huge = str(up.MAX_DOWNLOAD_BYTES + 1)
    s = _FakeSession([_FakeResp(200, {"Content-Length": huge}, (b"x",))])
    assert up._download(s, "https://cdn/z.zip", dest, max_bytes=up.MAX_DOWNLOAD_BYTES) is False


def test_download_caps_streamed_bytes_without_content_length():
    d = tempfile.mkdtemp(prefix="up_dl_")
    dest = os.path.join(d, "z.zip")
    # No Content-Length; body streams past the cap -> abort + delete partial.
    s = _FakeSession([_FakeResp(200, {}, (b"a" * 6, b"b" * 6))])
    assert up._download(s, "https://cdn/z.zip", dest, max_bytes=10) is False
    assert not os.path.exists(dest)


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
