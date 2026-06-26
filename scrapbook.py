"""
Scrapbook generator for the Procare downloader.

Turns the full daily-activities feed (notes, learning, photos, videos, meals,
naps, sign in/out, bathroom, ...) into a browsable HTML "scrapbook":

  Open Scrapbook.html          <- landing page (open this)
  2025-06 (June 2025).html     <- one page per month
  assets/scrapbook.css         <- shared styling

Content entries (notes, learning, photos, videos) render as full cards with the
teacher's text and the media embedded inline. Routine entries (meals, naps, sign
in/out, bathroom) are collapsed into a compact "daily log" strip per day.

Media is referenced from the monthly folders the downloader created, so keep the
whole output folder together when sharing.
"""

import html
import os
import urllib.parse
from collections import OrderedDict
from datetime import datetime

import procare_download as pd


# activity_type -> (emoji, label). Anything not listed still renders (generic).
TYPE_META = {
    "note_activity": ("📝", "Note"),
    "learning_activity": ("🎓", "Learning"),
    "photo_activity": ("📷", "Photo"),
    "video_activity": ("🎥", "Video"),
    "meal_activity": ("🍽️", "Meal"),
    "nap_activity": ("🛏️", "Nap"),
    "sign_in_activity": ("⏰", "Sign in"),
    "sign_out_activity": ("👋", "Sign out"),
    "bathroom_activity": ("🚻", "Bathroom"),
    "incident_activity": ("⚠️", "Incident"),
    "observation_activity": ("🔍", "Observation"),
    "kudo_activity": ("⭐", "Kudos"),
}

# Types shown as a compact per-day summary rather than full cards.
ROUTINE_TYPES = {"meal_activity", "nap_activity", "sign_in_activity",
                 "sign_out_activity", "bathroom_activity"}

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def esc(text):
    return html.escape(str(text)) if text is not None else ""


def first_name(kid):
    """First name only, handling the API's 'Lastname, Firstname' name format."""
    fn = kid.get("first_name")
    if isinstance(fn, str) and fn.strip():
        return fn.strip()
    name = (kid.get("name") or "").strip()
    if not name:
        return ""
    if "," in name:                       # "Lastname, Firstname"
        after = name.split(",", 1)[1].strip()
        return after.split()[0] if after else ""
    return name.split()[0]                 # "Firstname Lastname"


def paragraphs(text):
    """Turn plain text (with newlines) into safe HTML paragraphs."""
    if not text:
        return ""
    # Strip stray "object replacement"/control chars Procare leaves in some text.
    cleaned = str(text).replace("￼", "").replace("�", "")
    blocks = [b.strip() for b in cleaned.replace("\r\n", "\n").split("\n\n")]
    out = []
    for b in blocks:
        if b:
            out.append("<p>" + esc(b).replace("\n", "<br>") + "</p>")
    return "\n".join(out)


def fmt_time(dt):
    if not dt:
        return ""
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def fmt_clock(s):
    """Format an ISO time string into a short clock, or '' on failure."""
    dt = pd.find_capture_dt({"t": s}) if s else None
    return fmt_time(dt)


def record_dt(record):
    return pd.find_capture_dt(record)


def day_key(record):
    d = record.get("activity_date")
    if isinstance(d, str) and len(d) >= 10:
        return d[:10]
    dt = record_dt(record)
    return dt.strftime("%Y-%m-%d") if dt else "unknown"


def href(name):
    return urllib.parse.quote(name)


# --------------------------------------------------------------------------- #
# Per-type text
# --------------------------------------------------------------------------- #
def routine_summary(record):
    """One short phrase describing a routine activity (for the daily-log strip)."""
    atype = record.get("activity_type")
    data = record.get("data") or {}
    act = record.get("activiable") or {}
    emoji = TYPE_META.get(atype, ("•", atype))[0]

    if atype == "meal_activity":
        bits = " ".join(p for p in (data.get("type"), data.get("desc")) if p)
        qty = f" ({data.get('quantity')})" if data.get("quantity") else ""
        return f"{emoji} {esc((data.get('type') or 'Meal'))}{esc(qty)}: {esc(data.get('desc') or '')}".strip().rstrip(":")
    if atype == "nap_activity":
        start = fmt_clock(data.get("start_time"))
        end = fmt_clock(data.get("end_time"))
        span = f"{start}–{end}" if start and end else (f"from {start}" if start else "")
        return f"{emoji} Nap {esc(span)}".strip()
    if atype == "sign_in_activity":
        who = act.get("signed_in_by")
        t = fmt_clock(act.get("sign_in_time")) or fmt_time(record_dt(record))
        return f"{emoji} In {esc(t)}" + (f" ({esc(who)})" if who else "")
    if atype == "sign_out_activity":
        who = act.get("signed_out_by")
        t = fmt_clock(act.get("sign_out_time")) or fmt_time(record_dt(record))
        return f"{emoji} Out {esc(t)}" + (f" ({esc(who)})" if who else "")
    if atype == "bathroom_activity":
        kind = " ".join(p for p in (data.get("type"), data.get("sub_type")) if p)
        return f"{emoji} {esc(kind or 'Bathroom')}"
    return f"{emoji} {esc(TYPE_META.get(atype, ('', atype))[1])}"


def content_text(record):
    """Main text body for a content card."""
    atype = record.get("activity_type")
    data = record.get("data") or {}
    if atype == "note_activity":
        return paragraphs(data.get("desc") or record.get("comment"))
    # learning / photo / video / unknown: caption/lesson text lives in comment
    return paragraphs(record.get("comment") or data.get("desc"))


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #
def media_html(record, out_dir):
    """Inline <img>/<video> for any media attached to this activity."""
    pieces = []
    for url, dt, ident, kind in pd.collect_media_entries(record):
        path = pd.find_local_media(out_dir, dt, kind, ident)
        if not path:
            pieces.append('<div class="missing">media file not found '
                          '(re-run the downloader to fetch it)</div>')
            continue
        rel = href(os.path.relpath(path, out_dir).replace(os.sep, "/"))
        if kind == "video":
            pieces.append(f'<video class="media" controls preload="none" '
                          f'src="{rel}"></video>')
        else:
            pieces.append(f'<img class="media" loading="lazy" src="{rel}" alt="photo">')
    return "\n".join(pieces)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_card(record, out_dir):
    atype = record.get("activity_type", "unknown")
    emoji, label = TYPE_META.get(atype, ("•", atype.replace("_", " ").title()))
    dt = record_dt(record)
    staff = record.get("staff_present_name") or ""
    body = content_text(record)
    media = media_html(record, out_dir)
    meta = " · ".join(p for p in (fmt_time(dt), esc(staff)) if p)
    return f"""<div class="card">
  <div class="card-head"><span class="badge">{emoji} {esc(label)}</span>
    <span class="meta">{meta}</span></div>
  {body}
  {media}
</div>"""


def render_day(dkey, records, out_dir):
    dt = pd.find_capture_dt({"t": dkey})
    heading = f"{dt.strftime('%A')}, {MONTH_NAMES[dt.month]} {dt.day}, {dt.year}" if dt else dkey

    routine = [r for r in records if r.get("activity_type") in ROUTINE_TYPES]
    content = [r for r in records if r.get("activity_type") not in ROUTINE_TYPES]
    content.sort(key=lambda r: record_dt(r) or datetime.min)

    parts = [f'<section class="day"><h3 class="day-head">{esc(heading)}</h3>']
    if routine:
        badges = " ".join(f'<span class="rb">{routine_summary(r)}</span>'
                          for r in sorted(routine, key=lambda r: record_dt(r) or datetime.min))
        parts.append(f'<div class="daily-log"><span class="dl-label">Daily log</span>{badges}</div>')
    for r in content:
        parts.append(render_card(r, out_dir))
    parts.append("</section>")
    return "\n".join(parts)


def page_shell(title, body, css_rel="assets/scrapbook.css"):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{css_rel}">
</head>
<body>
{body}
</body>
</html>"""


def month_label(mkey):
    y, m = mkey.split("-")
    return f"{MONTH_NAMES[int(m)]} {y}"


def month_filename(mkey, prefix=""):
    return f"{prefix}{mkey} ({month_label(mkey)}).html"


def safe_name(name):
    """Make a string safe to use in a filename."""
    out = "".join(c for c in str(name) if c.isalnum() or c in " -_'").strip()
    return out or "Child"


def detect_class_name(records):
    """Most common class/room name from the feed (activiable.section.name)."""
    from collections import Counter
    counts = Counter()
    for r in records:
        if not isinstance(r, dict):
            continue
        act = r.get("activiable")
        sec = act.get("section") if isinstance(act, dict) else None
        if isinstance(sec, dict) and isinstance(sec.get("name"), str) and sec["name"].strip():
            counts[sec["name"].strip()] += 1
    return counts.most_common(1)[0][0] if counts else None


def _build_section(records, out_dir, who, school, class_name, landing_name, prefix=""):
    """Write the month pages + a landing page for one set of records.
    Returns the number of month pages written."""
    title = f"{who}'s Year in {class_name}" if class_name else f"{who}'s Scrapbook"
    context = " · ".join(p for p in (school, class_name) if p)

    by_month = OrderedDict()
    for r in sorted(records, key=lambda r: record_dt(r) or datetime.min):
        dk = day_key(r)
        by_month.setdefault(dk[:7], OrderedDict()).setdefault(dk, []).append(r)
    months = list(by_month.keys())

    for i, mk in enumerate(months):
        days = by_month[mk]
        month_sub = " · ".join(p for p in (who, context) if p)
        body = [f'<header class="top"><a class="home" href="{href(landing_name)}">'
                f'&larr; All months</a><h1>{esc(month_label(mk))}</h1>'
                f'<div class="who">{esc(month_sub)}</div></header>']
        nav = []
        if i > 0:
            nav.append(f'<a href="{href(month_filename(months[i-1], prefix))}">&larr; '
                       f'{esc(month_label(months[i-1]))}</a>')
        if i < len(months) - 1:
            nav.append(f'<a href="{href(month_filename(months[i+1], prefix))}">'
                       f'{esc(month_label(months[i+1]))} &rarr;</a>')
        if nav:
            body.append(f'<div class="monthnav">{" ".join(nav)}</div>')
        for dk in days:
            body.append(render_day(dk, days[dk], out_dir))
        if nav:
            body.append(f'<div class="monthnav">{" ".join(nav)}</div>')
        page = page_shell(f"{month_label(mk)} — {title}", "\n".join(body))
        with open(os.path.join(out_dir, month_filename(mk, prefix)), "w", encoding="utf-8") as fh:
            fh.write(page)

    rows = []
    for mk in months:
        recs = [r for d in by_month[mk].values() for r in d]
        photos = sum(1 for r in recs if r.get("activity_type") == "photo_activity")
        videos = sum(1 for r in recs if r.get("activity_type") == "video_activity")
        notes = sum(1 for r in recs if r.get("activity_type") == "note_activity")
        summary = " · ".join(s for s in (
            f"{photos} photos" if photos else "",
            f"{videos} videos" if videos else "",
            f"{notes} notes" if notes else "") if s) or f"{len(recs)} entries"
        rows.append(f'<li><a href="{href(month_filename(mk, prefix))}">{esc(month_label(mk))}</a>'
                    f'<span class="sum">{esc(summary)}</span></li>')

    school_line = f'<div class="school">{esc(school)}</div>' if school else ""
    body = f"""<header class="top">
  {school_line}
  <h1>{esc(title)}</h1>
  <div class="who">A year of memories — {len(records):,} moments</div>
</header>
<ul class="months">
{chr(10).join(rows)}
</ul>
<footer class="foot">Keep this folder together — the pages link to the photos and
videos inside it. Generated {esc(datetime.now().strftime('%Y-%m-%d'))}.</footer>"""
    with open(os.path.join(out_dir, landing_name), "w", encoding="utf-8") as fh:
        fh.write(page_shell(title, body))
    return len(months)


def build_scrapbook(records, kids_meta, out_dir, school=None, class_name=None):
    """Write the scrapbook HTML pages. With multiple children, each gets its own
    set of pages plus a top-level index. Returns the number of month pages."""
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    with open(os.path.join(out_dir, "assets", "scrapbook.css"), "w", encoding="utf-8") as fh:
        fh.write(CSS)

    kids = kids_meta or []
    # Single child (or unknown): one scrapbook at "Open Scrapbook.html".
    if len(kids) <= 1:
        who = (first_name(kids[0]) if kids else None) or "My Child"
        cls = class_name or detect_class_name(records)
        return _build_section(records, out_dir, who, school, cls, "Open Scrapbook.html")

    # Multiple children: a per-child scrapbook each, plus a master index.
    total_pages, child_links = 0, []
    used = set()
    for kid in kids:
        kid_id = kid.get("id")
        who = first_name(kid) or "Child"
        kid_records = [r for r in records if kid_id in (r.get("kid_ids") or [])]
        cls = class_name or detect_class_name(kid_records)
        base = safe_name(who)
        prefix = base + " - "
        landing = base + " - Scrapbook.html"
        if landing in used:  # disambiguate duplicate first names
            landing = f"{base} ({kid_id[:6]}) - Scrapbook.html"
            prefix = f"{base} ({kid_id[:6]}) - "
        used.add(landing)
        total_pages += _build_section(kid_records, out_dir, who, school, cls, landing, prefix)
        child_links.append((who, cls, landing, len(kid_records)))

    items = "\n".join(
        f'<li><a href="{href(landing)}">{esc(who)}</a>'
        f'<span class="sum">{esc(cls or "")}{" · " if cls else ""}{n:,} moments</span></li>'
        for who, cls, landing, n in child_links)
    school_line = f'<div class="school">{esc(school)}</div>' if school else ""
    body = f"""<header class="top">
  {school_line}
  <h1>Procare Scrapbook</h1>
  <div class="who">Choose a child</div>
</header>
<ul class="months">
{items}
</ul>"""
    with open(os.path.join(out_dir, "Open Scrapbook.html"), "w", encoding="utf-8") as fh:
        fh.write(page_shell("Procare Scrapbook", body))
    return total_pages


CSS = """
:root{
  --bg:#faf7f2; --card:#ffffff; --ink:#2c2a28; --muted:#8a8378;
  --accent:#c9745b; --line:#ece5da; --chip:#f1ece3;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.55;}
.top{max-width:820px;margin:0 auto;padding:32px 20px 8px;}
.top h1{margin:.2em 0;font-size:2rem;}
.school{color:var(--accent);font-weight:700;letter-spacing:.04em;
  text-transform:uppercase;font-size:.85rem;}
.who{color:var(--muted);font-size:1.05rem;}
.home{color:var(--accent);text-decoration:none;font-size:.95rem;}
.monthnav{max-width:820px;margin:8px auto;padding:0 20px;display:flex;
  justify-content:space-between;gap:12px;}
.monthnav a{color:var(--accent);text-decoration:none;}
.months{max-width:820px;margin:16px auto;padding:0 20px;list-style:none;}
.months li{display:flex;justify-content:space-between;align-items:baseline;
  padding:14px 16px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;margin-bottom:10px;}
.months li a{font-size:1.2rem;color:var(--ink);text-decoration:none;font-weight:600;}
.months .sum{color:var(--muted);font-size:.9rem;}
.day{max-width:820px;margin:24px auto;padding:0 20px;}
.day-head{font-size:1.15rem;border-bottom:2px solid var(--line);
  padding-bottom:6px;margin:24px 0 14px;color:var(--accent);}
.daily-log{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
  background:var(--chip);border-radius:10px;padding:10px 12px;margin-bottom:14px;
  font-size:.88rem;color:#5c554b;}
.dl-label{font-weight:700;color:var(--muted);text-transform:uppercase;
  font-size:.72rem;letter-spacing:.04em;margin-right:4px;}
.rb{background:#fff;border:1px solid var(--line);border-radius:20px;padding:3px 10px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.03);}
.card-head{display:flex;justify-content:space-between;align-items:center;
  gap:10px;margin-bottom:8px;}
.badge{background:var(--chip);border-radius:20px;padding:3px 12px;font-size:.82rem;
  font-weight:600;}
.meta{color:var(--muted);font-size:.85rem;text-align:right;}
.card p{margin:.5em 0;}
.media{display:block;width:100%;max-width:640px;height:auto;border-radius:10px;
  margin:10px 0;background:#000;}
.missing{color:#b00;background:#fff3f3;border:1px solid #f3d0d0;border-radius:8px;
  padding:8px 10px;font-size:.85rem;}
.foot{max-width:820px;margin:40px auto;padding:16px 20px;color:var(--muted);
  font-size:.85rem;border-top:1px solid var(--line);}
@media(max-width:560px){.top h1{font-size:1.5rem}.meta{font-size:.78rem}}
"""
