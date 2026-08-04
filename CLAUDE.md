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

- `procare_download.py` — the engine: auth, feed fetch, media download, CLI + guided menu + GUI entry point.
- `gui.py` — Tkinter GUI (login, mode menu, scope/class picker); imported lazily only when GUI mode
  is actually selected, so it's never touched by CLI/scripted runs.
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

## GUI (Tkinter)

Launched double-click apps now open a real window instead of a terminal — Tkinter (stdlib) was chosen
over `pywebview`/Electron because this app's whole value prop for non-technical parents is "double-click
and it just works," and a webview backend needs a platform runtime (WebView2 on Windows, PyObjC/WKWebView
on Mac) that isn't guaranteed present — a missing one means a blank window with no fix, worse than a
plain-looking Tkinter form. This first pass covers **only the interactive parts** (login, top-level menu,
scope/class picker) — live progress bars are a deliberate follow-up; see `Deliberately NOT doing` notes
in the PR that introduced this.

- **`gui.py` is imported lazily**, only inside `main()`'s `if mode == "gui":` branch — never at module
  level in `procare_download.py` — so a CLI/scripted run (or a test) never needs `tkinter` installed.
  `tests/test_core.py` never calls `main()` either, so it doesn't need `tkinter` at all (confirmed: this
  dev container doesn't have it installed, and the test suite still passes).
- **`main()`'s launch decision** (`_decide_launch_mode`, pure/tested): `"cli"` for any real flag
  (`--email`, `--since`, `--scrapbook-only`, ...) — byte-for-byte the existing scripted behavior,
  untouched. Otherwise `"gui"` if a Tk window can actually be constructed (`_gui_available()` — probes
  `tkinter.Tk()`/destroy in a `try/except`; also respects `PROCARE_FORCE_NO_GUI` for CI/tests), else
  `"guided"` (today's text menu) as the automatic fallback for headless/stripped environments. The
  explicit escape hatch is `--no-gui`. Both `--no-gui` and `--no-update-check` alone still count as
  "no real arguments" (`_GUIDED_COMPATIBLE_FLAGS`) so they don't force a `--email`-style required-args
  error.
- **`run()` is untouched** — no progress-callback plumbing was added through the feed scan, downloads,
  or gallery walk for this pass. Instead, `gui.py` populates `args` with already-decided values
  (`args.email`, `args.password` — a GUI-only in-memory attribute, never a CLI flag, so it can't leak
  into shell history — `args.scrapbook`/`args.scrapbook_only`) before calling `run(args)`, so `run()`'s
  existing `args.X or input(...)` guards are simply never reached.
- **The one exception: `choose_scope()` always prompted unconditionally.** It's split into a pure
  `resolve_scope(items, choice, custom_start, custom_finish)` (no I/O) plus the unchanged
  `input()`-based wrapper. `run()` calls `args._scope_resolver(kid_records)` instead of `choose_scope()`
  when that optional attribute is set. The GUI's resolver is a closure
  (`gui.GuiIO.ask_scope`) that puts a request on a queue and **blocks the worker thread only** (not the
  Tk mainloop) until the GUI thread shows the picker screen and the user answers — so `run()` still
  fetches the activity feed exactly once, same as the CLI today, and the picker just appears mid-run
  instead of before it. Don't reintroduce a duplicate "enumeration fetch" to show the picker earlier;
  that was considered and rejected as a needless second network pass.
- **Threading**: `run(args)` executes in a background thread (`gui.App._start_run`'s `worker()`); the
  Tk mainloop stays responsive by polling a queue (`root.after(100, ...)`). `run()`'s existing `print()`
  calls are captured for free by swapping `sys.stdout` to a queue-backed adapter (`gui.GuiIO`) for the
  duration of the worker thread's call — **no changes to any `print()` call site were needed**. The
  same queue also carries the `"ask_scope"` hand-off and the terminal `"done"` message (success or the
  caught exception). `WM_DELETE_WINDOW` confirms before closing mid-download.
- **Screen order deliberately mirrors the CLI's, not this plan's earlier draft**: Mode menu comes
  *first*, then Login only if needed — `run()`'s `--scrapbook-only` fast path (rebuild from an existing
  `feed.json`, no login at all) happens before the CLI ever prompts for credentials, so `gui.py` checks
  the same condition (`_scrapbook_only_feed_exists`) after the mode choice and skips straight to the
  run when it applies, instead of forcing a pointless login screen first.
- **Update-prompt under GUI launch**: `updater.self_update()` takes an optional `ask: Callable[[str],
  bool]` (default `None` = today's TTY-gated `_prompt_yes()`), so `updater.py` itself stays UI-agnostic
  (doesn't import `tkinter`). GUI mode passes `procare_download._gui_ask_yes_no` — a throwaway hidden
  `Tk()` root just for the `messagebox`, torn down before the real GUI's root is created. This also
  fixes a real gap: without it, a windowed build with no console would have silently swallowed both the
  update prompt *and* its non-interactive fallback message, with nothing visible anywhere.
- **Packaging switched to `--windowed`** (was `--console`) with a new `--hidden-import gui`. A Tk init
  failure under `--windowed` would otherwise exit with **no console and no window** — worse than today's
  visible traceback — so `gui.launch_gui()` wraps its own startup and falls back to a dependency-free
  native error box (`ctypes`/`MessageBoxW` on Windows, `osascript` on Mac) if constructing the very
  first `Tk()` root throws. `build.yml` also smoke-tests each built binary (launch, confirm it doesn't
  exit immediately, kill it) specifically to catch "Tk itself won't bundle" packaging breakage — macOS
  Tcl/Tk + PyInstaller bundling has known historical quirks, so don't skip that step when touching the
  build config.
- The two `START HERE (*)` launcher scripts pre-select a mode via their own batch/shell menu, then call
  `procare_download.py` with an explicit flag for two of the three choices — but the "download only"
  choice used to call it with **zero** args, which (both before and after this GUI work) re-triggers
  `main()`'s guided-menu decision a second, redundant time. Fixed by passing `--out procare_media`
  (its own default value — a no-op flag whose only purpose is making `sys.argv` non-empty) for that
  branch too. If you add a fourth choice to those scripts, give it a real/no-op flag too, not zero args.

## This branch: `gui-experiment` — not merged to `main`

This is a deliberately unmerged branch reviving the Tkinter GUI that `main` reverted (Smart App
Control blocked it — see below). It exists to let the GUI be tried again without exposing `main`'s
stable 1.x console users to an unproven build.

- **Versioning stays on a `2.0-alphaN` track** (`APP_VERSION` in `procare_download.py`), never a bare
  `1.x` or `2.0` — that keeps it from ever numerically colliding with a real release tag. Bump the
  suffix (`2.0-alpha2`, `2.0-alpha3`, ...) each time you cut a new preview, same tag-must-equal-
  `APP_VERSION` rule as any other release (`git tag v2.0-alphaN && git push origin v2.0-alphaN`).
- **`build.yml`'s release step sets `prerelease: true`.** This is the actual safety mechanism, not the
  version string: `updater.py`'s self-update check hits GitHub's `/releases/latest` API, which by
  contract excludes prereleases and drafts — so stable 1.x users are never auto-upgraded into this
  branch's builds. Testers install manually from the Releases page. **Do not flip this to `false` on
  this branch** — only a real graduation release cut from `main` should ever be non-prerelease.
- **Known unresolved risk, carried over from the first attempt (do not re-declare victory without
  retesting this):** real-world testing on Windows 11 hit **Smart App Control (SAC)**, a stricter,
  separate mechanism from classic SmartScreen. Unlike SmartScreen's "More info → Run anyway," SAC's
  block dialog has **no override at all** — it just refuses to run unsigned/unrecognized apps. SAC is
  subsystem-agnostic (evaluates code-signing/publisher reputation, not console vs. windowed PE
  subsystem), so `--windowed` packaging does not fix this on its own, and neither would reverting to
  `--console`. The two real fixes are code-signing (EV cert, recurring cost, uncertain payoff against
  SAC specifically) or Microsoft Store distribution (the one option that actually satisfies SAC, at
  the cost of real packaging/certification work). Neither is done. Verify on a real SAC-enabled Windows
  11 machine before considering this ready to merge — the first attempt shipped to `main` and reached a
  real user before this was discovered, which is exactly what the prerelease flag above now prevents.
- **Graduation path**: once the GUI is proven (including the SAC question above) and someone decides to
  ship it for real, merge this branch into `main` and cut a normal, non-prerelease `vX.Y` tag through
  the usual flow below — don't just drop the `prerelease: true` flag on an alpha tag in place.

## Build & release

- CI builds on `macos-latest` (Apple Silicon) + `windows-latest` via PyInstaller `--onefile --windowed`
  with `--hidden-import scrapbook --hidden-import updater --hidden-import gui --hidden-import piexif`.
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
