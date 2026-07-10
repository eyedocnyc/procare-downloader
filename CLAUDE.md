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
- `package_app.py` — assembles the shareable zip from a PyInstaller build (Win + Mac).
- `build_exe.bat` / `build_mac.command` — local one-click builds.
- `START HERE (Windows).bat` / `START HERE (Mac).command` — launchers for source users.
- `.github/workflows/ci.yml` — PR/`main` CI: byte-compile + regression tests on Python 3.9 and 3.12.
- `.github/workflows/build.yml` — release CI: builds Win + Mac apps, publishes a Release on tags only.
- `tests/test_core.py` — self-contained regression tests (`python tests/test_core.py`, no pytest).
- `docs/preview.png` + `docs/sample/` — README screenshot and its anonymized source.

## Procare API (reverse-engineered; no official public API)

- Base: `https://api-school.procareconnect.com/api/web/` (legacy fallback `api-school.kinderlime.com`).
- Auth: `POST auth/` with `{email, password}` → `user.auth_token`; send as `Authorization: Bearer <token>`.
  Email+password only — **2FA / SSO-only accounts won't work**.
- Kids: `GET parent/kids/` → each has `id` (UUID), `name` (usually "Lastname, Firstname"), sometimes `first_name`.
- Feed: `GET parent/daily_activities/?kid_id=<id>&filters[daily_activity][date_from]=YYYY-MM-DD&filters[daily_activity][date_to]=...&page=N`.
  The feed **caps the window per query**, so we walk **month-by-month** (`month_windows`).
- The bare `parent/photos/` endpoint returns HTTP 400 on *some* accounts — where it works, it (and
  `parent/videos/`) is how "gallery-only" daycares expose media that never went through an activity.
  We query both endpoints unconditionally per child (`fetch_gallery_media`); a 400 there is treated as
  "nothing here", not an error (`fetch_json(..., quiet=True)`).
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
- Class name is auto-detected from the feed (`section.name`, only on attendance records). School name is
  NOT auto-detected (no reliable field) — only shown if `--school` is passed.
- Re-runs are idempotent (skip existing files, matched by stem across months).
- `--scrapbook-only` rebuilds from `Scrapbook/feed.json` with no login (falls back to legacy root `feed.json`).

## Build & release

- CI builds on `macos-latest` (Apple Silicon) + `windows-latest` via PyInstaller `--onefile --console`
  with `--hidden-import scrapbook --hidden-import piexif`.
- **To ship a new version:** `git tag vX.Y && git push origin vX.Y`. CI runs tests, builds both apps, and
  publishes them to a GitHub Release (public download links). Manual runs (Actions tab) produce artifacts
  only. `gh` CLI lives at `C:\Program Files\GitHub CLI\gh.exe`; call it via bash if PowerShell misbehaves.
- Apps are unsigned → one-time SmartScreen ("Run anyway") / Gatekeeper ("right-click → Open").

## Testing

`python tests/test_core.py` (also run in CI). Add a test when you touch media identity, poster/avatar
suppression, full-res selection, date parsing, scope/class logic, or the scrapbook folder layout.
