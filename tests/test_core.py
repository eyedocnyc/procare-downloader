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
import html
import io
import json
import os
import re
import sys
import tempfile
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hashlib as _hashlib  # noqa: E402

import procare_download as pd  # noqa: E402
import scrapbook as sb  # noqa: E402
import updater as up  # noqa: E402


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


def plant(media_dir, rec, ext=".jpg", gallery=False):
    for _url, dt, ident, kind in pd.collect_media_entries(rec):
        md = pd.media_month_dir(media_dir, dt, gallery)
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


def test_detect_class_name_single_class():
    recs = [attend("k1", "2024-09-01", "Daffodils"), attend("k1", "2025-01-01", "Daffodils")]
    assert sb.detect_class_name(recs) == "Daffodils"


def test_detect_class_name_lists_every_class_with_its_span():
    # 8 months in the old room, 2 in the new one -- both should appear with their
    # own date range, not just whichever has the most attendance records (or is
    # most recent) outvoting/overwriting the other.
    recs = [attend("k1", f"2024-{m:02d}-01", "Toddler Room") for m in range(1, 9)]
    recs += [attend("k1", f"2024-{m:02d}-01", "Preschool Room") for m in (10, 11)]
    name = sb.detect_class_name(recs)
    assert name == ("Toddler Room (January 2024 – August 2024), "
                    "Preschool Room (October 2024 – November 2024)")


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
        out, total, ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {"kid_id": "k1"})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert len(out) == 1                       # only the first page's item is kept
    assert calls["n"] == 2                     # page 1, then page 2 detected as a repeat -> stop
    assert total is None and ok is True        # no total to fall short of


def test_paginate_gallery_repeated_page_short_of_total_is_incomplete():
    # The server reports 500 rows but repeats page 1. Stopping is right; calling
    # the window complete is not -- the caller would never record the shortfall.
    page = {"total": 500, "photos": [_photo(1)]}
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json, pd.time.sleep = (lambda *a, **k: page), (lambda *a, **k: None)
    try:
        out, total, ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert (len(out), total, ok) == (1, 500, False)


def test_paginate_gallery_respects_max_pages():
    # Distinct non-empty page every time (page-aware but "infinite") -> the cap stops it.
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: {"photos": [
        {"id": f"p{a[2]['page']}", "main_url": f"https://cdn/photos/files/x{a[2]['page']}/main/x.jpg"}]}
    pd.time.sleep = lambda *a, **k: None
    try:
        out, _total, _ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert len(out) == pd.GALLERY_MAX_PAGES     # bounded, never infinite


def _photo(i):
    return {"id": f"p{i}", "main_url": f"https://cdn/photos/files/p{i}/main/p{i}.jpg"}


def test_paginate_gallery_waits_out_a_silent_throttle():
    # Procare answers 200-with-empty-list when it rate-limits. With rows still
    # outstanding (total=3) that must NOT be read as "end of data": the walk waits
    # and retries the same page, and ends up with everything.
    pages = [{"total": 3, "photos": [_photo(1), _photo(2)]},   # page 1
             {"total": 3, "photos": []},                        # page 2 - throttled
             {"total": 3, "photos": [_photo(3)]},               # page 2 - retried
             {"total": 3, "photos": []}]                        # page 3 - genuinely done
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: pages.pop(0) if pages else {"total": 3, "photos": []}
    pd.time.sleep = lambda *a, **k: None
    try:
        out, total, ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert total == 3
    assert len(out) == 3        # the throttled page was retried, not skipped
    assert ok is True


def test_paginate_gallery_reports_incomplete_when_throttle_never_lifts():
    # Server says there are 500 rows but only ever hands back one page. The walk
    # must give up eventually AND report complete=False, so the run can warn the
    # user instead of claiming a full archive.
    state = {"n": 0}

    def fake(*a, **k):
        state["n"] += 1
        return {"total": 500, "photos": [_photo(1)]} if state["n"] == 1 else {"total": 500, "photos": []}

    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json, pd.time.sleep = fake, lambda *a, **k: None
    try:
        out, total, ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert total == 500
    assert len(out) == 1
    assert ok is False          # the caller must be told this window is short


def test_paginate_gallery_empty_window_is_complete_not_throttled():
    # total == 0 is a genuinely empty month; it must return immediately.
    orig_fj, orig_sleep = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: {"total": 0, "photos": []}
    pd.time.sleep = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not wait"))
    try:
        out, total, ok = pd._paginate_gallery(
            None, "https://api-school.procareconnect.com/api/web/",
            pd.GALLERY_PHOTO_PATH, "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig_fj, orig_sleep
    assert (out, total, ok) == ([], 0, True)


def test_polite_sleep_is_jittered_and_never_negative():
    seen = []
    orig = pd.time.sleep
    pd.time.sleep = seen.append
    try:
        for _ in range(40):
            pd.polite_sleep(1.0)
    finally:
        pd.time.sleep = orig
    assert all(v >= 0 for v in seen)
    assert len(set(seen)) > 1                 # actually varies, not a fixed clock
    assert max(seen) <= 1.0 * (1 + pd.PACING_JITTER) + 1e-9


def test_gallery_canary_distinguishes_empty_from_throttled():
    # The canary asks over the WHOLE range. A positive total proves the account has
    # gallery media, which is what lets a later `total: 0` window be read as
    # "rate-limited" instead of "empty" -- Procare zeroes `total` when it throttles,
    # so a single window can never tell the two apart on its own.
    from datetime import date as _date
    orig = pd.fetch_json
    pd.fetch_json = lambda *a, **k: {"total": 1234, "photos": []}
    try:
        assert pd._gallery_canary(None, "https://api-school.procareconnect.com/api/web/",
                                  "k1", _date(2022, 1, 1), _date(2022, 12, 31)) == 1234
    finally:
        pd.fetch_json = orig
    # Throttled (or truly empty): photos AND videos both report zero.
    pd.fetch_json = lambda *a, **k: {"total": 0, "photos": [], "videos": []}
    try:
        assert not pd._gallery_canary(None, "https://api-school.procareconnect.com/api/web/",
                                      "k1", _date(2022, 1, 1), _date(2022, 12, 31))
    finally:
        pd.fetch_json = orig


def test_gallery_canary_falls_back_to_videos():
    # A gallery with no photos but some videos must still read as "has media",
    # otherwise every window on that account would look throttled and the walk
    # would wait forever.
    from datetime import date as _date
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"total": 0, "photos": []} if calls["n"] == 1 else {"total": 7, "videos": []}

    orig = pd.fetch_json
    pd.fetch_json = fake
    try:
        assert pd._gallery_canary(None, "https://api-school.procareconnect.com/api/web/",
                                  "k1", _date(2022, 1, 1), _date(2022, 12, 31)) == 7
    finally:
        pd.fetch_json = orig


def test_gentle_constants_are_slower_than_default():
    assert pd.GENTLE_DELAY > pd.POLITE_DELAY
    assert pd.GENTLE_JITTER >= pd.PACING_JITTER


def test_gentle_mode_slows_the_default_polite_sleep():
    """--gentle rebinds the pacing globals at run time, so the default sleep must
    read them when it is CALLED, not when the function was defined -- otherwise the
    gallery walk keeps its 0.25s pace while the user asked for a human one."""
    seen, orig = [], (pd.time.sleep, pd.POLITE_DELAY, pd.PACING_JITTER)
    pd.time.sleep = seen.append
    try:
        pd.enable_gentle_pacing()
        for _ in range(20):
            pd.polite_sleep()
    finally:
        pd.time.sleep, pd.POLITE_DELAY, pd.PACING_JITTER = orig
    floor = pd.GENTLE_DELAY * (1 - pd.GENTLE_JITTER)
    assert min(seen) >= floor - 1e-9, f"slept {min(seen)}s, gentle floor is {floor}s"


def test_gallery_step_count_and_progress():
    from datetime import date as _date
    # 1 kid, 2 endpoints, Aug+Sep (2 months): 2 * (1 unfiltered + 2 windows) = 6.
    assert pd.gallery_step_count(["k1"], _date(2024, 8, 1), _date(2024, 9, 30)) == 6
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
    orig = pd.fetch_json, pd.time.sleep
    pd.fetch_json = lambda *a, **k: seen.append(dict(a[2])) or None  # a[2] == params; end walk
    pd.time.sleep = lambda *a, **k: None
    try:
        pd.fetch_gallery_media(None, "https://api-school.procareconnect.com/api/web/",
                               "k1", _date(2024, 8, 1), _date(2024, 9, 30))
    finally:
        pd.fetch_json, pd.time.sleep = orig
    def has_filter(p, r):
        return f"filters[{r}][datetime_from]" in p

    # unfiltered pass: a query with the kid but NO datetime filter
    assert any(p.get("kid_id") == "k1" and not has_filter(p, "photo") and not has_filter(p, "video")
               for p in seen)
    # date-windowed pass: datetime filters present for both resources
    assert any(has_filter(p, "photo") for p in seen)
    assert any(has_filter(p, "video") for p in seen)


def test_unfiltered_gallery_shortfall_is_reported():
    # Monthly windows cannot verify undated items from the all-history pass.
    from datetime import date as _date
    saved = pd._paginate_gallery, pd._read_window, pd.polite_sleep
    old_shortfalls = pd.gallery_shortfalls[:]

    class Guard:
        def first_canary(self, kid_id):
            return 5

    def paginate(_session, _base, _path, kind, params, *args, **kwargs):
        if "page" not in params and len(params) == 1:
            return (["one"], 5, False) if kind == "photo" else ([], 0, True)
        return [], 0, True

    pd._paginate_gallery = paginate
    pd._read_window = lambda *a, **k: ([], 0, pd.WINDOW_EMPTY)
    pd.polite_sleep = lambda *a, **k: None
    pd.gallery_shortfalls.clear()
    try:
        pd.fetch_gallery_media(None, GALLERY_BASE, "k1", _date(2025, 1, 1),
                               _date(2025, 1, 31), guard=Guard())
        assert ("all dates", "photo", 1, 5) in pd.gallery_shortfalls
    finally:
        pd._paginate_gallery, pd._read_window, pd.polite_sleep = saved
        pd.gallery_shortfalls[:] = old_shortfalls


GALLERY_BASE = "https://api-school.procareconnect.com/api/web/"


class _FakeGallery:
    """A scripted Procare gallery for walk tests: no network, no real sleeping.

    `photos` is [(YYYY-MM-DD, id)]. The limiter behaves the way Procare's does:
    while throttled, every list is empty AND `total` is 0. It switches on after
    `throttle_after` requests (or at once with `throttled=True`); each long sleep
    (a backoff, not the sub-second pacing) lets `lift_per_sleep` requests through."""

    PAGE = 50

    def __init__(self, photos=(), throttled=False, throttle_after=None, lift_per_sleep=0):
        self.photos = sorted(photos)
        self.throttled, self.throttle_after = throttled, throttle_after
        self.lift_per_sleep, self.lift = lift_per_sleep, 0
        self.requests, self.slept = [], []

    def fetch(self, _session, url, params, *_a, **_k):
        self.requests.append((url, dict(params)))
        if self.throttle_after is not None and len(self.requests) > self.throttle_after:
            self.throttled, self.throttle_after = True, None
        kind = "video" if url.endswith("videos/") else "photo"
        if self.throttled:
            if self.lift <= 0:
                return {"total": 0, kind + "s": []}
            self.lift -= 1
        frm = params.get(f"filters[{kind}][datetime_from]", "0000")[:10]
        to = params.get(f"filters[{kind}][datetime_to]", "9999")[:10]
        rows = [] if kind == "video" else [
            {"id": i, "created_at": f"{d}T10:00:00",
             "main_url": f"https://cdn/photos/files/{i}/main/{i}.jpg"}
            for d, i in self.photos if frm <= d <= to]
        page = params.get("page", 1)
        return {"total": len(rows), kind + "s": rows[(page - 1) * self.PAGE:page * self.PAGE]}

    def sleep(self, seconds):
        self.slept.append(seconds)
        if seconds >= pd.THROTTLE_BACKOFF[0]:
            self.lift = self.lift_per_sleep

    def waited(self):
        return sum(s for s in self.slept if s >= pd.THROTTLE_BACKOFF[0])


def _walk(fake, start, end, kid_ids=None):
    """Run the gallery walk against `fake` (one child unless `kid_ids` is given);
    returns (entry idents, shortfalls)."""
    orig = pd.fetch_json, pd.time.sleep, list(pd.gallery_shortfalls)
    pd.fetch_json, pd.time.sleep = fake.fetch, fake.sleep
    del pd.gallery_shortfalls[:]
    try:
        with redirect_stdout(io.StringIO()):
            if kid_ids is None:
                idents = {e[2] for e in pd.fetch_gallery_media(None, GALLERY_BASE, "k1",
                                                               start, end)}
            else:
                idents = {k[1] for k in pd.collect_gallery(None, GALLERY_BASE, kid_ids,
                                                           start, end)}
        return idents, list(pd.gallery_shortfalls)
    finally:
        pd.fetch_json, pd.time.sleep = orig[0], orig[1]
        pd.gallery_shortfalls[:] = orig[2]


def test_gallery_canary_asks_videos_when_photos_fail():
    """Some accounts answer the photos endpoint with a 400. That must not switch
    throttle detection off for good: ask videos, and say None only if both fail."""
    from datetime import date as _date

    def fake(_s, url, *_a, **_k):
        return None if url.endswith(pd.GALLERY_PHOTO_PATH) else {"total": 7, "videos": []}

    orig = pd.fetch_json
    try:
        pd.fetch_json = fake
        assert pd._gallery_canary(None, GALLERY_BASE, "k1", _date(2022, 1, 1),
                                  _date(2022, 12, 31)) == 7
        pd.fetch_json = lambda *a, **k: None
        assert pd._gallery_canary(None, GALLERY_BASE, "k1", _date(2022, 1, 1),
                                  _date(2022, 12, 31)) is None
    finally:
        pd.fetch_json = orig


def test_rethrottle_after_recovery_is_never_an_empty_month():
    """The limiter lifts long enough for the canary, then bites again on the
    re-read. That `total: 0` answer must not pass for an empty month."""
    from datetime import date as _date

    fake = _FakeGallery(photos=[("2025-01-10", "p1"), ("2025-01-11", "p2")],
                        throttle_after=1, lift_per_sleep=1)
    got, short = _walk(fake, _date(2025, 1, 1), _date(2025, 1, 31))
    if not {"p1", "p2"} <= got:        # not recovered: then it must be reported
        assert ("2025-01", "photo", 0, None) in short, short


def test_a_throttled_read_followed_by_a_lifted_canary_is_read_again():
    """The limiter can lift between a throttled read and the canary after it. One
    positive canary therefore can't prove the read was real: read once more."""
    from datetime import date as _date

    window_reads = []
    p1 = {"id": "p1", "created_at": "2025-01-10T10:00:00",
          "main_url": "https://cdn/photos/files/p1/main/p1.jpg"}

    def fake(_s, url, params, *_a, **_k):
        frm = params.get("filters[photo][datetime_from]", "")[:10]
        to = params.get("filters[photo][datetime_to]", "")[:10]
        if (frm, to) == ("2025-01-01", "2025-02-28"):        # the whole-range canary
            return {"total": 1, "photos": [p1]}
        if (frm, to) == ("2025-01-01", "2025-01-31"):        # the January window
            window_reads.append(1)
            if len(window_reads) > 1:                        # first read: throttled
                return {"total": 1, "photos": [p1]}
        return {"total": 0, "photos": [], "videos": []}      # everything else is empty

    orig = pd.fetch_json, pd.time.sleep, list(pd.gallery_shortfalls)
    pd.fetch_json, pd.time.sleep = fake, (lambda *a, **k: None)
    try:
        with redirect_stdout(io.StringIO()):
            got = pd.fetch_gallery_media(None, GALLERY_BASE, "k1", _date(2025, 1, 1),
                                         _date(2025, 2, 28))
    finally:
        pd.fetch_json, pd.time.sleep = orig[0], orig[1]
        pd.gallery_shortfalls[:] = orig[2]
    assert {e[2] for e in got} == {"p1"}, "the window was taken as empty after one read"


def test_empty_windows_without_a_positive_canary_are_not_reported():
    """With the whole-range count reading zero, an empty month could be the
    limiter or a genuinely empty gallery. Don't cry INCOMPLETE on an account
    that may simply have no gallery media."""
    from datetime import date as _date

    fake = _FakeGallery(photos=[("2025-01-10", "p1")], throttled=True)
    _got, short = _walk(fake, _date(2025, 1, 1), _date(2025, 2, 28))
    assert not short, short


def test_initial_zero_canary_is_rechecked_before_it_is_trusted():
    """A canary taken while throttled reads 0; trusting it would make every
    throttled month look empty. One short wait and a second look fixes that."""
    from datetime import date as _date

    fake = _FakeGallery(photos=[("2025-01-10", "p1"), ("2025-01-11", "p2")],
                        throttled=True, lift_per_sleep=20)
    got, short = _walk(fake, _date(2025, 1, 1), _date(2025, 1, 31))
    assert {"p1", "p2"} <= got and not short, (got, short)


def test_a_stuck_limiter_costs_one_wait_not_one_per_month():
    """Once a wait-out has failed, waiting again for every later month just burns
    hours (about 37 per child over two years). Later months go straight to short,
    and say their total is unknown rather than "0 of 0"."""
    from datetime import date as _date

    photos = [(f"{2024 + m // 12}-{m % 12 + 1:02d}-10", f"p{m}") for m in range(24)]
    fake = _FakeGallery(photos=photos, throttle_after=2)
    _got, short = _walk(fake, _date(2024, 1, 1), _date(2025, 12, 31))
    assert fake.waited() <= 2 * pd.THROTTLE_MAX_WAIT, f"waited {fake.waited()}s"
    assert len([s for s in short if s[1] == "photo"]) == 24
    assert all(total is None for _m, _k, _got, total in short), \
        "a throttled window's total is unknown, not 0"


def test_a_stuck_limiter_is_not_waited_out_again_for_the_next_child():
    """The breaker spans the whole run: a second child must not repeat the
    hour-long wait, and its throttled months are still reported."""
    from datetime import date as _date

    photos = [(f"2025-{m:02d}-10", f"p{m}") for m in range(1, 7)]
    fake = _FakeGallery(photos=photos, throttle_after=2)
    _got, short = _walk(fake, _date(2025, 1, 1), _date(2025, 6, 30), kid_ids=["k1", "k2"])
    assert fake.waited() <= 2 * pd.THROTTLE_MAX_WAIT, f"waited {fake.waited()}s"
    assert len([s for s in short if s[1] == "photo"]) == 12, short


def test_paginate_gallery_in_page_wait_never_exceeds_the_cap():
    fake = _FakeGallery(photos=[("2025-01-10", f"p{i}") for i in range(60)],
                        throttle_after=1)
    orig = pd.fetch_json, pd.time.sleep
    pd.fetch_json, pd.time.sleep = fake.fetch, fake.sleep
    try:
        _out, total, ok = pd._paginate_gallery(None, GALLERY_BASE, pd.GALLERY_PHOTO_PATH,
                                               "photo", {})
    finally:
        pd.fetch_json, pd.time.sleep = orig
    assert (total, ok) == (60, False)
    assert fake.waited() <= pd.THROTTLE_MAX_WAIT, f"waited {fake.waited()}s"


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


def test_meal_summary():
    # Type, quantity and description all surface; the emoji comes from TYPE_META.
    assert sb.routine_summary({"activity_type": "meal_activity",
                               "data": {"type": "Lunch", "desc": "pasta",
                                        "quantity": "all"}}) == "🍽️ Lunch (all): pasta"
    # A missing description must not leave a dangling colon.
    assert sb.routine_summary({"activity_type": "meal_activity",
                               "data": {"type": "Snack"}}) == "🍽️ Snack"
    # A missing type falls back to the generic label.
    assert sb.routine_summary({"activity_type": "meal_activity",
                               "data": {"desc": "milk"}}) == "🍽️ Meal: milk"


def test_layout_single_child():
    out = tempfile.mkdtemp(prefix="sb_single_")
    rec = photo_activity("k1", "2025-06-01", "p1")
    plant(sb.media_root(out), rec)
    sb.build_scrapbook([{"name": "Maya", "class_name": "Emerald Lilies",
                         "folder": "", "records": [rec]}], out)
    # tidy root: only the landing + Media/ + Scrapbook/
    assert set(os.listdir(out)) == {"Open Scrapbook.html", "Media", "Scrapbook"}
    land = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert "<h1>Maya&#x27;s Scrapbook</h1>" in land
    assert "Emerald Lilies" in land
    assert "A collection of memories" in land
    mp = [f for f in os.listdir(os.path.join(out, "Scrapbook")) if f.endswith(").html")][0]
    mpath = os.path.join(out, "Scrapbook", mp)
    src = first_media_src(open(mpath, encoding="utf-8").read())
    assert src and src.startswith("../Media/") and link_resolves(mpath, src)


def test_layout_multi_class_name_renders_intact():
    # class_name can be the multi-class "Room (span), Room (span)" string from
    # detect_class_name -- it should render whole on its own line, not get mashed
    # into a "Year in ..." title.
    out = tempfile.mkdtemp(prefix="sb_multiclass_")
    rec = photo_activity("k1", "2025-06-01", "p1")
    plant(sb.media_root(out), rec)
    multi_class = "Toddler Room (January 2024 – August 2024), Preschool Room (October 2024 – November 2024)"
    sb.build_scrapbook([{"name": "Maya", "class_name": multi_class,
                         "folder": "", "records": [rec]}], out)
    land = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert "<h1>Maya&#x27;s Scrapbook</h1>" in land
    assert html.escape(multi_class) in land
    assert "Year in" not in land

def test_scrapbook_groups_same_caption_batch():
    # A multi-photo post is one photo_activity record per photo, all sharing an
    # exact activity_time + caption. They should render as ONE card (caption shown
    # once, all photos in a grid); a same-time post with a DIFFERENT caption stays
    # its own card -- proving the caption is part of the batch key.
    out = tempfile.mkdtemp(prefix="sb_group_")
    batch = [photo_activity("k1", "2025-06-01", pid, caption="Hand Print Murals!")
             for pid in ("p1", "p2", "p3")]          # same kid+date -> same activity_time
    other = photo_activity("k1", "2025-06-01", "p9", caption="Snack time")  # same time, other text
    recs = batch + [other]
    for r in recs:
        plant(sb.media_root(out), r)
    sb.build_scrapbook(
        [{"name": "Maya", "class_name": "Room", "folder": "", "records": recs}], out)
    mp = [f for f in os.listdir(os.path.join(out, "Scrapbook")) if f.endswith(").html")][0]
    html = open(os.path.join(out, "Scrapbook", mp), encoding="utf-8").read()
    assert html.count("Hand Print Murals!") == 1        # caption deduped, shown once
    assert html.count("Snack time") == 1
    assert html.count('<div class="card">') == 2        # batch card + standalone card
    assert html.count('class="media-grid"') == 1        # only the 3-photo batch gets a grid
    assert html.count('<img class="media"') == 4        # all 3 batch photos + the standalone


def test_scrapbook_never_groups_records_without_a_time():
    """Grouping keys on the exact activity_time. A record that only carries a
    DATE must stay its own card: keying on the day would fold every same-type
    record of that day into one, and with an empty caption -- the common case --
    that is all of them."""
    recs = [{"id": f"u{n}", "activity_type": "photo_activity",
             "activity_date": "2025-06-01", "comment": "",
             "activiable": {"id": f"u{n}", "main_url": f"https://x/u{n}.jpg"}}
            for n in range(4)]
    groups = sb.group_records(recs)
    assert len(groups) == 4, "un-timed records must not collapse into one card"
    assert all(len(g) == 1 for g in groups)
    # A precise time still batches, so the fix does not disable grouping.
    timed = [{"id": f"t{n}", "activity_type": "photo_activity",
              "activity_time": "2025-06-01T10:00:00", "comment": "Murals!",
              "activiable": {"id": f"t{n}", "main_url": f"https://x/t{n}.jpg"}}
             for n in range(3)]
    assert len(sb.group_records(timed)) == 1


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


def test_download_file_leaves_no_partial_when_the_stream_breaks():
    """A connection that drops mid-body on the last attempt must not strand a
    `.part` file next to the media: nothing would ever clean it up."""
    class Broken:
        status_code, headers = 200, {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b"\xff\xd8\xff\x00"
            raise pd.requests.exceptions.ChunkedEncodingError("connection reset")

    class Session:
        def get(self, url, **kw):
            return Broken()

    out = tempfile.mkdtemp(prefix="pd_part_")
    dest = os.path.join(out, "photo.part")
    orig_sleep, pd.time.sleep = pd.time.sleep, lambda *_a: None
    try:
        ok, _head = pd.download_file(Session(), Session(), "https://cdn/x.jpg", dest)
    finally:
        pd.time.sleep = orig_sleep
    assert ok is False
    assert os.listdir(out) == [], f"partial download left behind: {os.listdir(out)}"


def test_stable_media_ident_deterministic():
    u = "https://cdn/attachments/files/x/original/open-uri-random?Signature=changes"
    # Deterministic across calls, ignores the (changing) query, never "None"/hash().
    a = pd.stable_media_ident(u)
    b = pd.stable_media_ident("https://cdn/attachments/files/x/original/open-uri-random?Signature=other")
    assert a == b and a and a != "None" and len(a) == 20


def test_find_local_media_handles_an_undated_item():
    """An item with no date is saved under the run's own timestamp (the
    downloader falls back to now()), so a later lookup can't rebuild its stem.
    It must still be found by kind+ident -- and must never crash the caller,
    which would abort the run before the feed and scrapbook are written."""
    out = tempfile.mkdtemp(prefix="pd_nodate_")
    assert pd.find_local_media(out, None, "photo", "p1") is None
    saved = datetime(2026, 1, 2, 3, 4, 5)
    md = os.path.join(out, saved.strftime("%Y-%m"))
    os.makedirs(md)
    path = os.path.join(md, pd.media_stem(saved, "photo", "p1") + ".jpg")
    open(path, "wb").write(b"\xff\xd8\xff\x00")
    assert pd.find_local_media(out, None, "photo", "p1") == path


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


class _FakeUsage:
    def __init__(self, free):
        self.free = free


def test_warn_if_low_disk_space_prints_when_free_space_low():
    orig = pd.shutil.disk_usage
    pd.shutil.disk_usage = lambda path: _FakeUsage(pd.LOW_DISK_SPACE_BYTES - 1)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            pd._warn_if_low_disk_space("/some/dir")
        assert "Heads up" in buf.getvalue()
    finally:
        pd.shutil.disk_usage = orig


def test_warn_if_low_disk_space_silent_when_plenty():
    orig = pd.shutil.disk_usage
    pd.shutil.disk_usage = lambda path: _FakeUsage(pd.LOW_DISK_SPACE_BYTES * 10)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            pd._warn_if_low_disk_space("/some/dir")
        assert buf.getvalue() == ""
    finally:
        pd.shutil.disk_usage = orig


def test_download_summary_flags_nothing_found():
    stats = {"downloaded": 0, "skipped_exist": 0, "skipped_old": 0, "failed": 0}
    buf = io.StringIO()
    with redirect_stdout(buf):
        pd._print_download_summary(stats, "/out", ranged=False)
    assert "Nothing matched" in buf.getvalue()


def test_download_summary_silent_when_something_found():
    stats = {"downloaded": 3, "skipped_exist": 0, "skipped_old": 0, "failed": 0}
    buf = io.StringIO()
    with redirect_stdout(buf):
        pd._print_download_summary(stats, "/out", ranged=False)
    assert "Nothing matched" not in buf.getvalue()

# --------------------------------------------------------------------------- #
# activity vs. gallery media filing, and metadata embedding
# --------------------------------------------------------------------------- #
def test_is_gallery_record():
    assert pd.is_gallery_record(pd.gallery_entry_to_record(
        "https://cdn/photos/files/g1/main/g1.jpg", datetime(2025, 6, 1), "g1", "photo", "k1"))
    assert not pd.is_gallery_record(photo_activity("k1", "2025-06-01", "p1"))


def test_media_metadata_activity_vs_gallery():
    # Activity: tagged with the child's name + an "activity" keyword; caption + staff kept.
    act = photo_activity("k1", "2025-06-01", "p1", caption="first steps")
    act["staff_present_name"] = "Ms. A"
    m = pd.media_metadata(act, {"k1": "Maya"})
    assert m["gallery"] is False and m["caption"] == "first steps" and m["creator"] == "Ms. A"
    assert m["keywords"] == ["Maya", "activity"]
    # Gallery: no person tag, no caption -> flagged untagged for review.
    g = pd.gallery_entry_to_record("https://cdn/photos/files/g1/main/g1.jpg",
                                   datetime(2025, 6, 1), "g1", "photo", "k1")
    mg = pd.media_metadata(g, {"k1": "Maya"})
    assert mg["gallery"] is True and mg["caption"] is None
    assert mg["keywords"] == ["gallery", "untagged"]


def test_exiftool_args_cover_exif_iptc_xmp():
    args = pd._exiftool_args({"caption": "hi", "keywords": ["Maya", "activity"],
                              "creator": "Ms. A"})
    assert "-overwrite_original" in args
    assert f"-XMP-xmp:CreatorTool={pd.META_TOOL_TAG}" in args
    # Caption lands in all three homes.
    assert "-EXIF:ImageDescription=hi" in args
    assert "-IPTC:Caption-Abstract=hi" in args
    assert "-XMP-dc:Description=hi" in args
    # Every keyword is appended to both IPTC and XMP.
    for kw in ("Maya", "activity"):
        assert f"-IPTC:Keywords+={kw}" in args and f"-XMP-dc:Subject+={kw}" in args
    assert "-EXIF:Artist=Ms. A" in args and "-XMP-dc:Creator=Ms. A" in args


def test_exiftool_keywords_are_written_idempotently():
    """`+=` alone appends even when the keyword is already there, so every
    re-tag (a lost marker, --overwrite) duplicated the person tag. exiftool's
    documented `-=` then `+=` pair removes the value before adding it back."""
    args = pd._exiftool_args({"keywords": ["Maya", "activity"]})
    for tag in ("-IPTC:Keywords", "-XMP-dc:Subject"):
        for kw in ("Maya", "activity"):
            add = args.index(f"{tag}+={kw}")
            assert args[add - 1] == f"{tag}-={kw}", f"{tag}+={kw} needs a -= before it"


def test_find_local_media_looks_in_gallery_subtree():
    out = tempfile.mkdtemp(prefix="pd_gal_")
    dt = datetime(2025, 6, 1, 10)
    gdir = os.path.join(out, pd.GALLERY_SUBDIR, "2025-06")
    os.makedirs(gdir)
    open(os.path.join(gdir, pd.media_stem(dt, "photo", "g1") + ".jpg"), "wb").write(b"\xff\xd8\xff\x00")
    found = pd.find_local_media(out, dt, "photo", "g1")
    assert found and pd.GALLERY_SUBDIR in found


def test_media_month_dir_routes_gallery():
    dt = datetime(2025, 6, 1, 10)
    assert pd.media_month_dir("/out", dt) == os.path.join("/out", "2025-06")
    assert pd.media_month_dir("/out", dt, gallery=True) == os.path.join(
        "/out", pd.GALLERY_SUBDIR, "2025-06")


def test_download_records_files_gallery_directly():
    # download_records tells save_media gallery=True for gallery records and False
    # for activity records, so untagged media lands in Gallery/ at download time
    # (no later move needed -- no empty month dir left behind).
    dt = datetime(2025, 6, 1, 10)
    act = photo_activity("k1", "2025-06-01", "p1")
    gal = pd.gallery_entry_to_record(
        "https://cdn/photos/files/g1/main/g1.jpg", dt, "g1", "photo", "k1")
    flags = {}
    orig = pd.save_media
    pd.save_media = lambda *a, **k: flags.__setitem__(a[5], k.get("gallery"))
    try:
        stats = {"downloaded": 0, "skipped_exist": 0, "skipped_old": 0, "failed": 0}
        pd.download_records(None, None, [act, gal], "/out", None, None, stats)
    finally:
        pd.save_media = orig
    assert flags == {"p1": False, "g1": True}
def test_enrich_media_tags_all_and_is_idempotent():
    out = tempfile.mkdtemp(prefix="pd_enrich_")
    dt = datetime(2025, 6, 1, 10)
    act = photo_activity("k1", "2025-06-01", "p1", caption="pic")
    gal = pd.gallery_entry_to_record("https://cdn/photos/files/g1/main/g1.jpg", dt, "g1", "photo", "k1")
    plant(out, act)
    plant(out, gal, gallery=True)
    # Record metadata calls instead of writing bytes (no exiftool/valid-image dependence).
    calls = []
    orig = pd.write_media_metadata

    def fake_write(path, meta, dt=None):
        calls.append(meta)
        return True

    pd.write_media_metadata = fake_write
    try:
        done = set()
        pd.enrich_media([act, gal], out, {"k1": "Maya"}, done)
        assert done == {pd.enriched_key(out, "photo", "p1"),
                        pd.enriched_key(out, "photo", "g1")}
        assert len(calls) == 2
        # The gallery file is found in Gallery/ and tagged without a person keyword.
        assert [c["keywords"] for c in calls if c["gallery"]] == [["gallery", "untagged"]]
        # Re-running with the same `done` set is a no-op (nothing re-tagged).
        pd.enrich_media([act, gal], out, {"k1": "Maya"}, done)
        assert len(calls) == 2
    finally:
        pd.write_media_metadata = orig


def test_enrich_media_tags_every_child_copy_of_the_same_photo():
    """A photo tagged with two children is downloaded into BOTH children's
    folders, so both copies need their own tags. Keyed on kind+ident alone, only
    whichever copy the run reached first would ever be written -- and the marker
    is persisted, so the other one would be skipped forever."""
    out = tempfile.mkdtemp(prefix="pd_enrich_multi_")
    act = photo_activity("k1", "2025-06-01", "p1", caption="pic")
    maya, leo = os.path.join(out, "Maya"), os.path.join(out, "Leo")
    plant(maya, act)
    plant(leo, act)
    written, orig = [], pd.write_media_metadata

    def fake_write(path, meta, dt=None):
        written.append(path)
        return True

    pd.write_media_metadata = fake_write
    try:
        done = set()
        pd.enrich_media([act], maya, {"k1": "Maya"}, done)
        pd.enrich_media([act], leo, {"k1": "Maya"}, done)
    finally:
        pd.write_media_metadata = orig
    assert len(written) == 2, f"only one copy was tagged: {written}"
    assert len({os.path.dirname(p) for p in written}) == 2


def test_enrich_media_retries_a_write_that_failed():
    """The `done` marker outlives the run, so writing one for a failed write
    excludes that file from every later retry. Only a real write earns it."""
    out = tempfile.mkdtemp(prefix="pd_enrich_fail_")
    act = photo_activity("k1", "2025-06-01", "p1", caption="pic")
    plant(out, act)
    attempts, orig, results = [], pd.write_media_metadata, [False, True]

    def fake_write(path, meta, dt=None):
        attempts.append(path)
        return results.pop(0)

    pd.write_media_metadata = fake_write
    try:
        done = set()
        pd.enrich_media([act], out, {"k1": "Maya"}, done)
        assert not done, "a failed write must not be marked done"
        pd.enrich_media([act], out, {"k1": "Maya"}, done)   # the retry
        assert done == {pd.enriched_key(out, "photo", "p1")}
    finally:
        pd.write_media_metadata = orig
    assert len(attempts) == 2, "the failed file was never retried"


def test_write_media_metadata_reports_whether_it_wrote():
    """It has to answer honestly: nothing to write is not a successful write."""
    out = tempfile.mkdtemp(prefix="pd_meta_ret_")
    path = os.path.join(out, "x.jpg")
    open(path, "wb").write(b"\xff\xd8\xff\x00")
    empty = {"caption": "", "keywords": [], "creator": "", "gallery": False}
    assert pd.write_media_metadata(path, empty) is False


def test_enriched_state_roundtrip():
    p = os.path.join(tempfile.mkdtemp(prefix="pd_state_"), ".procare_enriched.json")
    assert pd.load_enriched(p) == set()          # missing file -> empty
    pd.save_enriched(p, {"photo:a", "video:b"})
    assert pd.load_enriched(p) == {"photo:a", "video:b"}


def test_read_password_from_stdin():
    import io

    class _Args:
        password_stdin = True

    orig = sys.stdin
    try:
        sys.stdin = io.StringIO("hunter2\n")
        assert pd.read_password(_Args()) == "hunter2"      # one line, newline stripped
        sys.stdin = io.StringIO("hunter2\r\n")
        assert pd.read_password(_Args()) == "hunter2"      # Windows PowerShell pipe
        for blank in ("\n", "\r\n"):
            sys.stdin = io.StringIO(blank)                  # blank pipe -> refuse
            try:
                pd.read_password(_Args())
            except SystemExit:
                pass
            else:
                raise AssertionError("expected SystemExit for blank stdin")
        sys.stdin = io.StringIO("")                        # nothing piped -> refuse
        try:
            pd.read_password(_Args())
        except SystemExit:
            pass
        else:
            raise AssertionError("expected SystemExit on empty stdin")
    finally:
        sys.stdin = orig


# --------------------------------------------------------------------------- #
# messages / chat (experimental, defensive parsing)
# --------------------------------------------------------------------------- #
def test_list_from_handles_unknown_shapes():
    assert pd._list_from([1, 2]) == [1, 2]
    assert pd._list_from({"messages": [{"id": 1}]}, "messages") == [{"id": 1}]
    assert pd._list_from({"data": {"conversations": [{"id": 9}]}}, "conversations") == [{"id": 9}]
    assert pd._list_from({"nope": 1}, "messages") == []
    assert pd._list_from(None) == []


def test_message_fields_matches_real_shape():
    # Procare's real message: nested `sender`, `message` body, `posted_at`, `subject`,
    # and `message_type` -> the chat channel (Office/Classroom).
    f = pd.message_fields({"id": "m1", "sender": {"name": "Ms. A"}, "message": "<p>Hi!</p>",
                           "posted_at": "2026-07-05T09:00:00Z", "message_type": "general",
                           "subject": "Field trip"})
    assert f["sender"] == "Ms. A" and f["body"] == "<p>Hi!</p>"   # raw body kept
    assert f["category"] == "Classroom Chat" and f["subject"] == "Field trip" and f["dt"].year == 2026
    # Tolerates other field names/shapes.
    f2 = pd.message_fields({"sender_name": "Dad", "text": "ok", "sent_at": "2025-06-02",
                            "message_type": "parent_admin_com"})
    assert f2["sender"] == "Dad" and f2["body"] == "ok" and f2["category"] == "Office Chat"
    assert f2["dt"].year == 2025
    # Unknown/absent type falls back gracefully.
    assert pd.message_category({"message_type": "weird_new_type"}) == "Weird New Type"
    assert pd.message_category({}) == "Messages"


def test_message_body_html_links_and_escaping():
    # <a> becomes a real clickable anchor; script/text is escaped, tags dropped.
    out = pd._message_body_html('Sign up <a href="https://forms.gle/x">here</a> <b>now</b>')
    assert '<a href="https://forms.gle/x" target="_blank" rel="noopener">here</a>' in out
    assert "<b>" not in out and "now" in out
    # Bare URLs get linkified; special chars are HTML-escaped; unknown tags dropped.
    out2 = pd._message_body_html("see https://zoom.us/j/1 & more <tag>")
    assert '<a href="https://zoom.us/j/1' in out2 and "&amp;" in out2 and "<tag>" not in out2
    # A javascript: link is neutralized (kept as text, not an anchor).
    assert "<a" not in pd._message_body_html('<a href="javascript:alert(1)">x</a>')


def test_message_body_html_bare_url_cannot_swallow_a_link():
    """A bare URL right before an <a> must not absorb that link's placeholder.

    It used to: the link was spliced into the bare URL's href attribute, so a
    crafted message could break out of the attribute and add an event handler,
    and ordinary rich text rendered as broken nested anchors."""
    from html.parser import HTMLParser

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, attrs))

    cases = [  # (body, anchors expected, visible link text)
        ('https://zoom.us/j/1<strong><a href="https://ok.com/ onmouseover=alert(1) x=">'
         'join</a>', 2, "join"),
        ('https://forms.gle/a<span><a href="https://ok.com/b">form</a></span> and <b>bold</b>',
         2, "form"),
        ('Hi \x000\x00 <a href="https://ok.com/c">c</a>', 1, "c"),  # a forged placeholder
    ]
    for body, anchors, text in cases:
        out = pd._message_body_html(body)
        parser = Tags()
        parser.feed(out)
        assert [t for t, _ in parser.tags if t != "br"] == ["a"] * anchors, out
        for _tag, attrs in parser.tags:
            names = {name for name, _value in attrs}
            assert names <= {"href", "target", "rel"}, f"unexpected attribute in {out}"
            assert not any(name.startswith("on") for name in names), out
            assert not any("<" in (value or "") for _name, value in attrs), \
                f"markup nested inside an attribute: {out}"
        assert f">{html.escape(text, quote=False)}</a>" in out, out


def test_is_from_us_matches_family_sender():
    fam_id, fam_names = {"u1"}, {"Jamie Lee"}
    assert pd._is_from_us({"sender": {"id": "u1", "name": "Ray"}}, fam_id, fam_names)   # by id
    assert pd._is_from_us({"sender": {"name": "Jamie Lee"}}, fam_id, fam_names)        # by name
    assert not pd._is_from_us({"sender": {"id": "s9", "name": "Ms. A"}}, fam_id, fam_names)


def test_message_attachment_urls_reads_attachments_only():
    m = {"message": "see this",
         "attachments": [{"url": "https://cdn/msgs/files/a1/main/a1.jpg?sig=1"}],
         # A profile pic OUTSIDE attachments must be ignored (we only read attachments),
         "sender": {"name": "Ms. A", "profile_pic_url": "https://cdn/photos/x/main/av.jpg"}}
    assert pd.message_attachment_urls(m) == ["https://cdn/msgs/files/a1/main/a1.jpg?sig=1"]
    # And a profile pic INSIDE attachments is still skipped by path fragment.
    m2 = {"attachments": [{"url": "https://cdn/profile_pics/files/t/main/t.jpg"}]}
    assert pd.message_attachment_urls(m2) == []


def test_message_attachment_urls_keeps_documents():
    """A school attaches PDFs far more often than videos -- newsletters,
    permission slips, menus. An image/video allowlist dropped every one of them
    without a word, which is the opposite of archiving the inbox."""
    m = {"attachments": [{"url": "https://cdn/msgs/files/n1/newsletter.pdf"},
                         {"url": "https://cdn/msgs/files/n2/menu.docx"},
                         {"url": "https://cdn/msgs/files/n3/photo.jpg"},
                         {"url": "https://cdn/msgs/files/n4/nosuffix"}]}
    assert pd.message_attachment_urls(m) == [
        "https://cdn/msgs/files/n1/newsletter.pdf",
        "https://cdn/msgs/files/n2/menu.docx",
        "https://cdn/msgs/files/n3/photo.jpg",
        "https://cdn/msgs/files/n4/nosuffix",
    ]
    # A document is named for what it really is, from its own bytes.
    assert pd.sniff_ext(b"%PDF-1.7\n%\xe2\xe3") == ".pdf"
    # And where the bytes say nothing, the URL still gives a sane name.
    assert pd.ext_from_url("https://cdn/msgs/files/n2/menu.docx", ".bin") == ".docx"
    assert pd.ext_from_url("https://cdn/msgs/files/n4/nosuffix", ".bin") == ".bin"


def test_scrapbook_keeps_undated_records_in_their_own_bucket():
    """A record with no usable date must neither crash the build nor vanish.

    `day_key` returns "unknown" for it, which used to reach `month_label` as a
    month key and abort the whole scrapbook."""
    out = tempfile.mkdtemp(prefix="pd_undated_")
    act = photo_activity("k1", "2025-01-08", "p1")
    plant(out, act)
    undated = {"activity_type": "note_activity", "id": "n1", "kid_ids": ["k1"],
               "comment": "A note with no date"}
    undated_photo = {"activity_type": "photo_activity", "id": "n2", "kid_ids": ["k1"],
                     "activiable": {"id": "p9",
                                    "main_url": "https://cdn/photos/files/p9/main/p9.jpg"}}
    # The downloader files an undated item under the day it was fetched.
    fetched = os.path.join(out, sb.MEDIA_DIR, "2026-10")
    os.makedirs(fetched)
    open(os.path.join(fetched, "2026-10-04_120000_photo_p9.jpg"), "w").close()
    pages = sb.build_scrapbook([{"name": "Maya", "class_name": "", "folder": "",
                                 "records": [undated, undated_photo, act]}], out)
    assert pages == 2, "the undated records get a page of their own"
    pages_dir = os.path.join(out, sb.PAGES_DIR)
    undated_page = open(os.path.join(pages_dir, sb.month_filename(sb.UNDATED)),
                        encoding="utf-8").read()
    assert "A note with no date" in undated_page
    assert "2026-10-04_120000_photo_p9.jpg" in undated_page, "undated media is still linked"
    january = open(os.path.join(pages_dir, "2025-01 (January 2025).html"), encoding="utf-8").read()
    assert "A note with no date" not in january
    landing = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert landing.index("January 2025") < landing.index(sb.UNDATED), "undated sorts last"


def test_render_messages_html_channels_family_style_and_order():
    msgs = [{"message_type": "general", "sender": {"name": "Ms. A"}, "subject": "Older",
             "message": '<p>Visit <a href="https://x.test/a">link</a></p>',
             "posted_at": "2025-06-01T10:00:00Z"},
            {"message_type": "general", "sender": {"id": "u1", "name": "Jamie Lee"}, "subject": "Newer",
             "message": "Thanks!", "posted_at": "2025-06-02T10:00:00Z"},
            {"message_type": "parent_admin_com", "sender": {"name": "Director"}, "subject": "Office note",
             "message": "FYI", "posted_at": "2025-06-01T09:00:00Z"},
            {"message_type": "general", "weird_field": "no parseable body",
             "posted_at": "2025-06-03T10:00:00Z"}]
    html = pd.render_messages_html([], msgs, our_ids={"u1"}, our_names={"Jamie Lee"})
    assert "Classroom Chat</h2>" in html and "Office Chat</h2>" in html   # channel sections
    # Channel colours come from CSS classes now, not inline styles, so the
    # transcript restyles with the rest of the scrapbook.
    assert "chan-classroom" in html and "chan-office" in html
    assert "style='background:" not in html, "channel colours belong in the stylesheet"
    assert "<style>" not in html, "the page must use the shared stylesheet"
    assert "scrapbook.css" in html
    assert 'href="https://x.test/a"' in html                             # clickable anchor
    assert "msg chan-classroom us" in html and "you / family" in html    # family styling
    assert "weird_field" in html                                         # bodyless -> raw JSON
    # Reverse chronological within a channel: newest ("Newer") before oldest ("Older").
    assert html.index("Newer") < html.index("Older")


def test_archive_messages_merges_instead_of_replacing():
    """A --since/--until run fetches a slice; it must not shrink messages.json.

    The transcript says messages.json holds the complete raw data, and the
    landing page counts from it, so a dated run has to merge into the archive
    (fresh copy wins) rather than overwrite it with the slice."""
    def msg(mid, day, body):
        return {"id": mid, "message_type": "general", "sender": {"name": "Ms. A"},
                "subject": f"s{mid}", "message": body, "posted_at": f"2025-06-{day}T10:00:00Z"}

    inbox = [msg(1, "01", "first"), msg(2, "10", "second"), msg(3, "20", "third")]
    orig_paginate, orig_carers = pd._paginate, pd.fetch_carers

    def fake_paginate(session, base, path, reauth, *keys, params=None):
        return list(inbox) if path == pd.MESSAGES_PATH else []

    pd._paginate, pd.fetch_carers = fake_paginate, lambda *a, **kw: []
    out = tempfile.mkdtemp(prefix="pd_msgmerge_")
    try:
        with redirect_stdout(io.StringIO()):
            pd.archive_messages(None, None, "https://x/", out)
            inbox[1] = msg(2, "10", "second, edited")
            pd.archive_messages(None, None, "https://x/", out,
                                since_dt=datetime(2025, 6, 5), until_dt=datetime(2025, 6, 15))
    finally:
        pd._paginate, pd.fetch_carers = orig_paginate, orig_carers

    with open(os.path.join(out, "Messages", "messages.json"), encoding="utf-8") as fh:
        saved = json.load(fh)["messages"]
    assert sorted(m["id"] for m in saved) == [1, 2, 3], "a dated run dropped messages"
    assert next(m for m in saved if m["id"] == 2)["message"] == "second, edited"
    transcript = open(os.path.join(out, "Messages", "messages.html"), encoding="utf-8").read()
    assert "3 message(s)" in transcript and "first" in transcript and "third" in transcript


def test_select_data_independent_flags():
    import types

    def A(media=False, messages=False, all_data=False):
        return types.SimpleNamespace(media=media, messages=messages, all_data=all_data)

    assert pd.select_data(A()) == {"media": True, "messages": False}              # default -> media
    assert pd.select_data(A(media=True)) == {"media": True, "messages": False}
    assert pd.select_data(A(messages=True)) == {"media": False, "messages": True}  # independent
    assert pd.select_data(A(all_data=True)) == {"media": True, "messages": True}
    assert pd.select_data(A(media=True, messages=True)) == {"media": True, "messages": True}


def test_category_class_is_stable_and_has_a_fallback():
    assert pd.category_class("Office Chat") == "chan-office"
    assert pd.category_class("Classroom Chat") == "chan-classroom"
    # An unknown channel must still get a class, or it would render unstyled.
    assert pd.category_class("Bus Route") == "chan-other"
    assert pd.category_class("") == "chan-other"
    assert pd.category_class(None) == "chan-other"


def test_gallery_media_groups_into_one_card_per_day():
    """Gallery uploads have no caption to batch on, so without this a day of them
    renders as hundreds of single-photo cards -- which is how every month before
    the activity feed began used to look."""
    out = tempfile.mkdtemp(prefix="pd_gal_")
    recs = []
    for i in range(5):
        dt = datetime(2025, 1, 8, 9 + i, 30)
        rec = pd.gallery_entry_to_record(
            f"https://cdn/photos/files/g{i}/main/g{i}.jpg", dt, f"g{i}", "photo", "k1")
        plant(out, rec)
        recs.append(rec)
    html = sb.render_day("2025-01-08", recs, out, out)
    assert html.count('class="card"') == 1, "a day of gallery media is ONE card"
    assert html.count("media-grid") == 1
    assert html.count("<img") == 5, "every photo still shown"
    assert sb.GALLERY_BADGE in html
    assert "5 photos" in html


def test_gallery_and_activity_media_stay_separate_cards():
    """Gallery items carry no caption, staff or child tag; folding them in with
    tagged activity photos would imply an attribution Procare never made."""
    out = tempfile.mkdtemp(prefix="pd_mix_")
    dt = datetime(2025, 1, 8, 10)
    act = photo_activity("k1", "2025-01-08", "p1", caption="Painting today")
    gal = pd.gallery_entry_to_record(
        "https://cdn/photos/files/g1/main/g1.jpg", dt, "g1", "photo", "k1")
    plant(out, act)
    plant(out, gal)
    html = sb.render_day("2025-01-08", [act, gal], out, out)
    assert html.count('class="card"') == 2
    assert "Painting today" in html
    assert sb.GALLERY_BADGE in html
    # A single gallery photo still uses the same grid markup as a batch, so the
    # two never look like different kinds of page furniture.
    assert "media-grid" in html


def test_single_photo_activity_is_not_forced_into_a_grid():
    """The existing look for an ordinary one-photo post must not change."""
    out = tempfile.mkdtemp(prefix="pd_one_")
    act = photo_activity("k1", "2025-01-08", "p1", caption="Just one")
    plant(out, act)
    html = sb.render_day("2025-01-08", [act], out, out)
    assert "media-grid" not in html


def test_year_rows_groups_months_under_year_headings():
    months = ["2024-11", "2024-12", "2025-01"]
    counts = {"2024-11": (10, 1), "2024-12": (5, 0), "2025-01": (7, 2)}
    html = sb.year_rows(months, lambda mk: f"<li>{mk}</li>", lambda mk: counts[mk])
    assert html.count('class="year"') == 2, "two years -> two groups"
    assert html.index("2024") < html.index("2025"), "order follows the months given"
    # The year heading totals its months: 10+5 photos, 1+0 videos.
    assert "15 photos" in html and "1 video" in html
    assert "7 photos" in html and "2 videos" in html
    # Every month still gets its row.
    for mk in months:
        assert f"<li>{mk}</li>" in html


def test_year_rows_handles_empty_input():
    assert sb.year_rows([], lambda mk: "", lambda mk: (0, 0)) == ""


def test_count_label_reads_naturally():
    assert sb.count_label(["photo"]) == "1 photo"
    assert sb.count_label(["photo", "photo", "video"]) == "2 photos · 1 video"
    assert sb.count_label([]) == ""


def test_messages_summary_reads_the_raw_archive():
    """The scrapbook links the transcript by reading messages.json, so a media run
    picks up an earlier --messages run without them having to talk to each other."""
    out = tempfile.mkdtemp(prefix="pd_msum_")
    assert sb.messages_summary(out) is None            # no archive -> nothing shown
    msg_dir = os.path.join(out, sb.MESSAGES_DIR)
    os.makedirs(msg_dir)
    with open(os.path.join(msg_dir, "messages.json"), "w", encoding="utf-8") as fh:
        json.dump({"conversations": [], "messages": [
            {"message_type": "general", "sender": {"name": "Ms. A"}, "subject": "Hi",
             "message": "x", "posted_at": "2025-06-01T10:00:00Z"},
            {"message_type": "parent_admin_com", "sender": {"name": "Office"},
             "subject": "Bill", "message": "y", "posted_at": "2025-07-02T10:00:00Z"},
        ]}, fh)
    summary = sb.messages_summary(out)
    assert summary["count"] == 2
    assert summary["channels"] == {"Classroom Chat": 1, "Office Chat": 1}
    assert "June 1, 2025" in summary["span"] and "July 2, 2025" in summary["span"]
    assert summary["page"] is None, "no transcript written yet"

    html = sb.messages_html(summary, out)
    assert "2</b> messages" in html and "Office Chat" in html
    assert "--messages" in html, "should say how to get the transcript"

    open(os.path.join(msg_dir, "messages.html"), "w").close()
    summary = sb.messages_summary(out)
    assert summary["page"]
    html = sb.messages_html(summary, out)
    assert "Messages/messages.html" in html and "Read the messages" in html


def test_messages_html_is_empty_without_an_archive():
    assert sb.messages_html(None, "/out") == ""


def test_every_emitted_class_has_a_rule_in_the_shared_stylesheet():
    """Styling lives in one stylesheet; a class with no rule renders unstyled.

    This is what keeps the pages consistent as they grow: forget a rule and the
    page silently looks wrong, which is exactly the kind of thing nobody notices
    until much later."""
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", sb.CSS))
    for layout, paths in _archive_every_layout().items():
        used = set()
        for path in paths:
            page_html = open(path, encoding="utf-8").read()
            for attr in re.findall(r"""class=['"]([^'"]+)['"]""", page_html):
                used.update(attr.split())
        assert used, f"{layout}: the pages should emit some classes"
        missing = sorted(used - defined)
        assert not missing, f"{layout}: classes with no CSS rule: {missing}"


def _archive_every_layout():
    """Build an archive in each page layout and return {layout: [html paths]}.

    One child, several children plus the shared gallery, and the messages
    transcript (with the landing-page panel that links it). Styling rules have
    to hold on every one of them, not just the layout a test happened to use."""
    dt = datetime(2025, 1, 8, 10)
    act = photo_activity("k1", "2025-01-08", "p1", caption="Painting")
    gal = pd.gallery_entry_to_record(
        "https://cdn/photos/files/g1/main/g1.jpg", dt, "g1", "photo", "k1")
    sibling = photo_activity("k2", "2025-02-03", "p2", caption="Blocks")
    layouts: dict[str, list[dict]] = {
        "single child": [{"name": "Maya", "class_name": "Daffodils", "folder": "",
                          "records": [act, gal]}],
        "several children": [
            {"name": "Maya", "class_name": "Daffodils", "folder": "Maya", "records": [act]},
            {"name": "Leo", "class_name": "", "folder": "Leo", "records": [sibling]},
            {"name": "Shared Gallery", "class_name": "", "folder": "Shared Gallery",
             "records": [gal], "shared": True}],
    }
    inbox = [{"id": 1, "message_type": "general", "sender": {"name": "Ms. A"},
              "subject": "Hi", "message": 'See <a href="https://x.test/a">this</a>',
              "posted_at": "2025-06-01T10:00:00Z"},
             {"id": 2, "message_type": "parent_admin_com", "sender": {"id": "u1", "name": "Dad"},
              "subject": "Re", "message": "Thanks", "posted_at": "2025-06-02T10:00:00Z"},
             {"id": 3, "message_type": "new_channel", "sender": {"name": "Bus"},
              "subject": "Route", "message": "On time", "posted_at": "2025-06-03T10:00:00Z"}]
    orig_paginate, orig_carers = pd._paginate, pd.fetch_carers
    pd._paginate = lambda s, b, path, r, *k, **kw: list(inbox) if path == pd.MESSAGES_PATH else []
    carers: list = [{"id": "u1", "name": "Dad"}]
    pd.fetch_carers = lambda *a, **kw: carers
    pages = {}
    try:
        for layout, sections in layouts.items():
            out = tempfile.mkdtemp(prefix="pd_layout_")
            for s in sections:
                for rec in s["records"]:
                    plant(sb.media_root(out, s["folder"]), rec)
            with redirect_stdout(io.StringIO()):
                pd.archive_messages(None, None, "https://x/", out, scrapbook_pending=True)
            sb.build_scrapbook(sections, out)
            pages[layout] = [os.path.join(root, f) for root, _dirs, files in os.walk(out)
                             for f in files if f.endswith(".html")]
    finally:
        pd._paginate, pd.fetch_carers = orig_paginate, orig_carers
    return pages


def test_pages_link_the_stylesheet_by_relative_path_and_never_inline_styles():
    """The archive is opened straight off disk, with no web server, so every
    stylesheet reference has to be a relative path that resolves as a file --
    and all styling has to come from that stylesheet, on every layout."""
    for layout, paths in _archive_every_layout().items():
        names = {os.path.basename(p) for p in paths}
        assert {"messages.html", "Open Scrapbook.html"} <= names, (layout, names)
        for page in paths:
            html = open(page, encoding="utf-8").read()
            where = f"{layout}: {page}"
            assert "<style" not in html.lower(), f"{where} inlines a <style> block"
            assert not re.search(r"<[^>]*\sstyle\s*=", html, re.IGNORECASE), \
                f"{where} has a style= attribute"
            links = re.findall(r"""<link rel=["']stylesheet["'] href=["']([^"']+)["']""", html)
            assert links, f"{where} links no stylesheet"
            for href in links:
                assert not href.startswith(("/", "http")), f"{where}: not relative: {href}"
                target = os.path.join(os.path.dirname(page), urllib.parse.unquote(href))
                assert os.path.exists(target), f"{where}: stylesheet does not resolve: {href}"


def test_transcript_links_home_when_the_scrapbook_is_built_later_in_the_run():
    """--all-data writes the transcript before the landing page exists; the
    back-link must still be there, and must not dangle on a --messages-only run."""
    inbox = [{"id": 1, "message_type": "general", "sender": {"name": "Ms. A"},
              "subject": "Hi", "message": "x", "posted_at": "2025-06-01T10:00:00Z"}]
    orig_paginate, orig_carers = pd._paginate, pd.fetch_carers
    pd._paginate = lambda s, b, path, r, *k, **kw: list(inbox) if path == pd.MESSAGES_PATH else []
    pd.fetch_carers = lambda *a, **kw: []
    try:
        for pending, linked in ((True, True), (False, False)):
            out = tempfile.mkdtemp(prefix="pd_msghome_")
            with redirect_stdout(io.StringIO()):
                pd.archive_messages(None, None, "https://x/", out, scrapbook_pending=pending)
            transcript = open(os.path.join(out, "Messages", "messages.html"),
                              encoding="utf-8").read()
            assert ("Open%20Scrapbook.html" in transcript) is linked, (pending, transcript)
    finally:
        pd._paginate, pd.fetch_carers = orig_paginate, orig_carers


def test_empty_scrapbook_still_shows_the_messages_panel():
    """Messages belong to the account, not to a child's media, so a landing page
    with no activities must still link the message archive."""
    out = tempfile.mkdtemp(prefix="pd_emptymsg_")
    msg_dir = os.path.join(out, sb.MESSAGES_DIR)
    os.makedirs(msg_dir)
    with open(os.path.join(msg_dir, "messages.json"), "w", encoding="utf-8") as fh:
        json.dump({"messages": [{"message_type": "general", "sender": {"name": "Ms. A"},
                                 "subject": "Hi", "message": "x",
                                 "posted_at": "2025-06-01T10:00:00Z"}]}, fh)
    open(os.path.join(msg_dir, "messages.html"), "w").close()
    assert sb.build_scrapbook([], out) == 0
    landing = open(os.path.join(out, "Open Scrapbook.html"), encoding="utf-8").read()
    assert "No activities found" in landing
    assert "Messages/messages.html" in landing, "the messages panel went missing"


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
