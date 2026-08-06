# CLAUDE.md — project notes for the Procare Downloader

Guidance for working in this repo. Read this before changing behavior; it captures
hard-won details about Procare's private API and the design decisions that fix real bugs.

## What this is

A local tool that logs into a parent's Procare (Procare Connect) account and:
1. Downloads **all** photos and videos (full resolution) into monthly folders.
2. Builds a browsable **HTML scrapbook** of the whole activity feed (teacher notes,
   learning activities, photos, videos; routine logs collapsed).

Everything runs locally; the password is only sent to Procare, never stored.
Public repo: https://github.com/eyedocnyc/procare-downloader

## Files

- `procare_download.py` — the engine: auth, feed fetch, media download, CLI + guided menu.
- `scrapbook.py` — HTML scrapbook generator (imported by the engine).
- `updater.py` — startup self-update: checks GitHub Releases, verifies the SHA-256, swaps the binary.
- `package_app.py` — assembles the shareable zip from a PyInstaller build (Win + Mac).
- `build_exe.bat` / `build_mac.command` — local one-click builds.
- `START HERE (Windows).bat` / `START HERE (Mac).command` — launchers for source users.
- `.github/workflows/ci.yml` — PR/`main` CI: byte-compile + regression tests on Python 3.9 and 3.12.
- `.github/workflows/build.yml` — release CI: builds Win + Mac apps, publishes a Release on tags only.
- `tests/test_core.py` — self-contained regression tests (`python tests/test_core.py`, no pytest).
- `docs/preview.png` + `docs/sample/` — README screenshot and its anonymized source.

## Procare API (reverse-engineered; no official public API)

- Base: `https://api-school.procareconnect.com/api/web/` (legacy fallback `api-school.kinderlime.com`,
  now a **dead domain** — DNS no longer resolves; it survives only as a last-ditch fallback).
- Auth (`authenticate`): two paths, tried in order.
  1. **Primary — the flow the web app itself uses:** `POST https://online-auth.procareconnect.com/sessions/`
     with `{email, password, role: "carer", platform: "web", preserve_sites: true}` → response carries
     `auth_token` and `sites: [{base_url, is_default, ...}]`. We take the default site's `base_url`,
     append `/api/web/`, and that's the account's home API host (`session_token_and_base`). Verified
     against a real parent account: `base_url` is the plain `api-school.procareconnect.com`.
  2. **Fallback — legacy `POST /api/web/auth/`** with `{email, password}` → `user.auth_token`. This
     endpoint now **returns HTTP 500 for ordinary parent ("carer") accounts**, which is why the primary
     path exists; keep the fallback for older backends.
- Token is sent as `Authorization: Bearer <token>`. **Rejected credentials come back as HTTP 422**
  (`{"errors":[...]}`), NOT 401/403 — `AUTH_REJECTED_CODES` covers all three, and a rejection **exits
  immediately** (`_fail_login`): parent accounts lock after repeated failed logins and only the daycare
  can unlock them, so never loop on a bad password.
- Multi-tenant schools can live on `api-school.<school>.procareconnect.com`; `allow_auth_host` adds the
  resolved host to `PROCARE_AUTH_HOSTS` (suffix-checked) so `download_file` may authenticate to it.
  Email+password only — **2FA / SSO / MFA accounts won't work** (`_online_auth` detects a session that
  isn't cleared for `regular_requests` and says so).
- Kids: `GET parent/kids/` → each has `id` (UUID), `name` (usually "Lastname, Firstname"), sometimes `first_name`.
- Feed: `GET parent/daily_activities/?kid_id=<id>&filters[daily_activity][date_from]=YYYY-MM-DD&filters[daily_activity][date_to]=...&page=N`.
  The feed **caps the window per query**, so we walk **month-by-month** (`month_windows`).
- The bare `parent/photos/` endpoint returns HTTP 400 on *some* accounts — where it works, it (and
  `parent/videos/`) is how "gallery-only" daycares expose media that never went through an activity.
  We query both endpoints unconditionally per child (`fetch_gallery_media`); a 400 there is treated as
  "nothing here", not an error (`fetch_json(..., quiet=True)`).
- **The gallery endpoints are date-capped, so they need an explicit date filter to reach old media.**
  Procare now hides media older than ~1 year from the default view (issue #1). The dashboard reaches
  the rest with `?filters[photo][datetime_from]=YYYY-MM-DD 00:00&filters[photo][datetime_to]=... 23:59`
  (videos use `filters[video][...]`) — verified against a live account. So `fetch_gallery_media` walks
  the timeline **month-by-month** with these filters (`gallery_query_params`, reusing `month_windows`),
  exactly like the activity feed — a bare unfiltered query only returns the recent window.
  The month-by-month walk over years of history is long and was silent, so it looked frozen: it now
  prints a `\r` progress line (`_gallery_progress`, `gallery_step_count`) behind an upfront time
  warning. `_paginate_gallery` is **bounded** (`GALLERY_MAX_PAGES` + a repeat-page check that stops if
  an endpoint ignores `page`) so it can never loop forever, and gallery calls pass a small
  `fetch_json(retries=GALLERY_RETRIES)` so a flaky month costs ~seconds, not the full 5xx backoff.
- Each activity: `activity_type`, `activity_time`/`activity_date`, `comment`, `data` (type-specific),
  `kid_ids` (which children it belongs to), `staff_present_name`, and `activiable` (the media/detail object).

## Critical gotchas (each fixes a real bug — don't regress)

- **Media only comes from `activiable`.** The item-level `photo_url` on non-photo activities (e.g.
  `learning_activity`) is a **teacher profile picture** (`/profile_pics/...`), not content. We ignore
  item-level URLs and skip `/profile_pics/` paths. See `SKIP_URL_PATH_FRAGMENTS`, `collect_media_entries`.
- **Video URLs are unstable.** Procare serves videos from randomized `open-uri...` URLs that change on
  every request, so the URL is NOT a stable id. Identify videos by the **resource UUID**
  (`activiable.id`). Photos are fine — their URL path carries a stable file UUID (`id_from_url`).
- **Posters vs videos.** A `video_activity.activiable` also has `main_url`/`thumb_url` (a poster image).
  `collect_media_urls` suppresses images when a video URL is present so we don't save the poster as content.
- **Full resolution.** Photo objects expose `main_url` (full), `medium_url` (== main here), `thumb_url`.
  `_photo_score` prefers `main`/`original`/`large` and avoids `thumb`/`small`/`medium`.
- **File type by magic bytes.** `sniff_ext` reads the first bytes so a file's extension always matches
  its real content (this is how we caught PNG posters saved as `.mp4`).
- **Don't read ids as dates.** `_parse_dt` rejects numbers outside a plausible epoch range; `find_capture_dt`
  only falls back to string values — otherwise an `id` like `50` becomes "1970".
- **The bearer token is host-allowlisted; media uses a separate unauthenticated session.** Downloads
  send `Authorization` only to exact Procare API hosts (`is_procare_host`, `PROCARE_AUTH_HOSTS`) — signed
  CDN/S3 links authorize themselves, so sending the token there would leak it off-domain. `download_file`
  picks the authed `session` iff `is_procare_host(url)`, else the unauthenticated `media_session`; it also
  refuses non-`https` media URLs. This is belt-and-suspenders with `requests` dropping auth on cross-host
  redirects (which still covers a Procare URL that 302s to S3). Do NOT collapse the two sessions back
  together, and do NOT send auth to the CDN "to be safe" — that's the leak this prevents.
- **No browser/hosted version is feasible.** A hosted web app can't call Procare's API (CORS: the API only
  allows Procare's own origin). Only in-page code (extension/userscript/bookmarklet reusing the logged-in
  session) could work. The desktop app is the supported path.
- **Self-updater (`updater.py`) is best-effort and never blocks the app.** `self_update` swallows all
  errors, no-ops when not frozen (source runs) or on unsupported platforms, and only prompts when stdin
  is a TTY. It downloads the release zip and **verifies it against the published `*.zip.sha256` before
  swapping** — never install unverified code. GitHub/CDN are NOT Procare hosts, so the account token is
  never involved (it makes its own plain requests). A programmatic download carries no macOS
  `com.apple.quarantine` / Windows Mark-of-the-Web, so the swapped binary launches without re-triggering
  Gatekeeper/SmartScreen — do NOT "fix" this by shelling out to curl/browser (that would re-add the mark).
  Windows can't overwrite a running `.exe`, so the swap is done by a temp `.bat` (uniquely named via
  `mkstemp`, with a **bounded** wait-retry — never an infinite loop) that waits for exit, replaces
  (old kept as `.bak`), and relaunches; macOS uses `_swap_file` (backup → same-dir atomic `os.replace`
  → `chmod`) then `os.execv`. The relaunch preserves `sys.argv`. `_download` follows redirects
  manually, **rejecting any non-`https` hop** and capping the body size (`MAX_DOWNLOAD_BYTES`, checked
  via `Content-Length` and again while streaming). Any failure falls back to opening `RELEASES_PAGE`.
  The checksum proves **integrity only** (it's fetched from the same release as the zip) — cryptographic
  authenticity via **signed update manifests is the documented next step** if the threat model warrants
  it. `_swap_file` / `_windows_script` are pure so they're unit-tested; the `os.execv`/detached-`.bat`
  relaunch is process-bound and stays behind the fail-safe fallback.
- **Activities and gallery are two independent, overlapping sources.** Some daycares post everything as
  activities, some skip activities and upload straight to the gallery, and some do both for the same
  photo/video. We always fetch both: `fetch_all_records` (activity feed, correctly tagged per child via
  `kid_ids`) and the gallery. **The gallery is account-wide and child-agnostic** — verified against a real
  2-child account: `parent/videos/` returns one global list identical for every kid (it ignores `kid_id`)
  and gallery items carry NO child-association field. `parent/photos/` is empty on that account, so we
  treat gallery photos and videos symmetrically. `collect_gallery` queries once per kid and records, per
  `(kind, ident)`, which kids' requests returned it; `distribute_gallery` then: dedups against every
  child's activity media (same `(kind, ident)` key — video ident = resource id, photo = `id_from_url`),
  honors any explicit `kid_ids` if present (`gallery_item_kids`, absent today but future-proof), attributes
  an item to the single kid whose query returned it if the endpoint discriminates, and otherwise treats it
  as account-wide → the sole child (single-child) or a dedicated **"Shared Gallery"** section (multi-child,
  `shared: True`). Do NOT reintroduce the old global `gallery_seen`/`merge_gallery_media` that dumped every
  untagged gallery item on child 1. `gallery_entry_to_record` puts a video's URL under `video_file_url`
  (open-uri URLs have no extension, so `collect_media_urls` can only detect them by key name) and a photo's
  under `main_url`.

## Output layout

```
<out>/Open Scrapbook.html   # landing (only page at the top level)
<out>/Scrapbook/            # month pages + assets/ + feed.json
<out>/Media/                # photos & videos under YYYY-MM/
```
Multiple children: each gets `Scrapbook/<Child>/` and `Media/<Child>/`; the root `Open Scrapbook.html`
is a "choose a child" index. The engine builds one **section per child**
(`{name, class_name, folder, records}`); `scrapbook.media_root`/`pages_root` define the folders and are
shared by the download path and the renderer so filenames always agree
(`media_stem` / `find_local_media`). Landing pages show a summary (`stats_html`); every page includes a
photo lightbox (`LIGHTBOX` injected by `page_shell`). Cross-folder links use real relative paths
(`rel_href`).
- **The landing page `<h1>` never assumes a duration.** `_build_section` used to render
  `"{who}'s Year in {class}"` and "A year of memories" — both wrong whenever the actual span is
  shorter than a year, or (since `detect_class_name`'s multi-class fix) `class_name` is really a list
  of several classes with their own spans. The `<h1>` is always just `"{who}'s Scrapbook"`; `class_name`
  (single or multi-class) renders on its own line underneath; "A collection of memories" replaces the
  duration claim, since the actual date range is already shown accurately by `stats_html`'s `statsub`
  right below. The browser tab `<title>` (`title_full`) still includes the class name for context —
  only the big visible heading was the problem.

**Multi-photo posts render as one card, not one per photo.** Procare represents a batch upload as one
`photo_activity` record *per photo*, but stamps every record in the batch with the same `activity_time`,
`comment` and `activity_type`. `scrapbook.group_records` collapses a day's content records by that exact
key (`_group_key`), so a 12-photo post shows the caption once with all 12 photos in a `.media-grid` (CSS
masonry). Verified against a real account: within any one `activity_time` the caption is always
byte-identical, so the exact-match key never merges photos that don't belong together, and a caption
reused on another day stays separate (different time). `render_card` therefore takes a **list** of
records (the batch); the header/caption come from the first, the media from all. This is **render-only** —
`feed.json` stays raw (one record per Procare activity), so the grouping can change without
re-downloading and `--scrapbook-only` re-applies it. Don't push grouping into `feed.json`; the raw feed
is the faithful archive.

## Security / privacy

- `feed.json` (and the `--debug` `debug_activities.json`) are passed through `scrub_signed_urls`
  before writing — it strips the **entire query string and fragment** from every http(s) URL,
  unconditionally (not a denylist of known signing params), so no signed/expiring/token'd link can
  leak into a shared archive regardless of param name or casing. The local-file lookup still works
  because `id_from_url` ignores the query. Both files are written via `write_private_json`, which
  also `chmod 0600`s them on POSIX (they hold a child's activity history).
- `fetch_json` re-authenticates once on 401/403 via the `reauth` closure (guards long feed walks
  if the token expires). Media downloads use signed URLs and aren't affected.
- Releases include per-zip **SHA-256** files: `package_app.py` writes `*.zip.sha256`; the workflow
  attaches them. These prove a download matches the file published beside it (integrity), NOT who
  authored it (authenticity) — don't describe them as proof of publisher. Apps are unsigned (paid
  certs not worth it), hence the one-time SmartScreen/Gatekeeper prompts.

## Behavior notes

- **Guided mode** (no CLI args / double-clicked exe) always asks a scope menu after login:
  Everything / a specific class (with date span) / a custom date range. Per child when there are siblings.
- Class name is auto-detected from the feed (`section.name`, only on attendance records,
  `scrapbook.detect_class_name` via `procare_download.class_spans`). A single class -> just its name
  (unchanged title format). **Multiple classes** (a mid-year room move) -> every class listed with its
  own date span, oldest first — not just whichever has the most attendance records or is most recent,
  since either one alone would erase part of the child's actual history from the title. School name is
  NOT auto-detected (no reliable field) — only shown if `--school` is passed.
- Re-runs are idempotent (skip existing files, matched by stem across months).
- `--scrapbook-only` rebuilds from `Scrapbook/feed.json` with no login (falls back to legacy root `feed.json`).
- **The guided-mode window stays open on failure too, not just success.** `main()` catches `SystemExit`
  (raised by `_fail_login`, bad `--since`/`--until`, etc.) and bare `Exception`, prints the message, and
  still runs the "Press Enter to close this window" pause — otherwise a double-clicked `.exe` flashes
  shut before a parent can read why (e.g. a bad password). Don't let a new top-level error path bypass
  this by exiting the process directly (`os._exit`, unguarded `sys.exit` outside `run()`, etc.).
- `_warn_if_low_disk_space` prints a heads-up (not a hard stop) when free space on `out_dir`'s drive is
  under `LOW_DISK_SPACE_BYTES` (2 GB), since years of full-res media can be many GB. `_print_download_summary`
  also flags when a selection matched nothing (all-zero stats) and suggests widening the scope — the raw
  "Downloaded: 0" line alone gave no guidance (issue #1 was reported as "No Activity Found").

## A GUI was tried and reverted — Smart App Control

v2.0 briefly shipped a Tkinter GUI replacing the terminal prompts on double-click launch. Real-world
testing on Windows 11 hit **Smart App Control (SAC)**: a stricter, separate mechanism from classic
SmartScreen. Unlike SmartScreen's "More info → Run anyway," SAC's block dialog has **no override** at
all — it just refuses to run unsigned/unrecognized apps.

**SAC is subsystem-agnostic**, which is the key fact if this comes up again: it evaluates code
signing/publisher reputation, not whether a binary is a console or windowed PE subsystem. Reverting the
GUI back to `--console` does **not** fix a Smart App Control block — the plain console build is equally
unsigned and would likely be blocked the same way. The GUI was reverted anyway because it wasn't
solving anything for the SAC-affected user and hadn't been verified on a real Mac, not because console
mode is somehow more SAC-friendly. The two real fixes are code-signing (EV cert, real recurring cost,
uncertain payoff against SAC specifically for a low-volume free tool) or Microsoft Store distribution
(the one option that actually solves it — Store apps are inherently trusted by SAC — but a real chunk
of new packaging/certification work). Neither is done. The documented workaround for affected users is
running from source (see README Troubleshooting) — not disabling SAC, which is a one-way switch on
most Windows 11 builds and not something to casually recommend.

## Build & release

- CI builds on `macos-latest` (Apple Silicon) + `windows-latest` via PyInstaller `--onefile --console`
  with `--hidden-import scrapbook --hidden-import updater --hidden-import piexif`.
- **Versioning: `APP_VERSION` in `procare_download.py` is the source of truth** and the self-updater
  compares against it. It MUST equal the release tag — `build.yml` fails the release build if
  `APP_VERSION != ${GITHUB_REF_NAME#v}`. So the release flow is: **bump `APP_VERSION` to `X.Y` in code →
  merge → `git tag vX.Y && git push origin vX.Y`**. CI runs tests, builds both apps, and publishes them
  to a GitHub Release (with auto-generated "What's Changed" notes via `generate_release_notes`) + the
  zips and `.sha256`. Manual runs (Actions tab) produce artifacts only and skip the drift guard.
  `gh` CLI lives at `C:\Program Files\GitHub CLI\gh.exe`; call it via bash if PowerShell misbehaves.
- Apps are unsigned → one-time SmartScreen ("Run anyway") / Gatekeeper ("right-click → Open") on a
  *browser* download; a download the app performs for its own self-update avoids that prompt (see the
  `updater.py` gotcha above).

## Testing

`python tests/test_core.py` (also run in CI). Add a test when you touch media identity, poster/avatar
suppression, full-res selection, date parsing, scope/class logic, the scrapbook folder layout, or the
updater's version/asset/verify logic. (The actual binary swap is platform+process bound and isn't
unit-tested — keep it behind the fail-safe fallback.)
