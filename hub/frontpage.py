"""The morning front page: a printable, self-contained one-pager built from the data /api/today already collects.

Pure rendering (`render(data, call)` -> HTML string). No external assets, inline CSS, white-paper variant of the
ARC-Glass colours. EVERY dynamic string goes through `html.escape`.
"""
from __future__ import annotations

from html import escape as _esc


def e(value) -> str:
    return _esc(str(value if value is not None else ""), quote=True)


def focus_line(data: dict) -> str:
    """One deterministic sentence: the single thing to look at first."""
    tasks = data.get("tasks") or {}
    due = (data.get("brain") or {}).get("due") or []
    nxt = (data.get("pending") or {}).get("next_meeting")
    if tasks.get("overdue"):
        return f"Clear the overdue task first: {tasks['overdue'][0].get('name') or 'untitled'}."
    if due:
        return f"Follow up on {due[0].get('title') or 'the open item'}."
    if tasks.get("due_today"):
        return f"Finish {tasks['due_today'][0].get('name') or 'untitled'} today."
    if nxt:
        return f"Prepare for {nxt.get('title') or 'your next meeting'} at {nxt.get('time') or ''}.".replace(" at .", ".")
    if (data.get("pending") or {}).get("autopilot"):
        return "Approve the waiting Autopilot drafts."
    return "A clear day. Pick one thing that moves the work forward."


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _section(title: str, rows: list[str], empty: str) -> str:
    body = "<ul>" + "".join(f"<li>{r}</li>" for r in rows) + "</ul>" if rows else f'<p class="none">{e(empty)}</p>'
    return f"<section><h2>{e(title)}</h2>{body}</section>"


def render(data: dict, call: str = "Chief") -> str:
    meetings = [m for m in data.get("meetings") or [] if not m.get("past")]
    tasks = data.get("tasks") or {}
    brain = data.get("brain") or {}
    pending = data.get("pending") or {}
    numbers = data.get("numbers") or {}
    m_rows = [f'<b>{e("All day" if m.get("all_day") else m.get("start_time"))}</b> {e(m.get("title"))}'
              + (f' <span class="sub">until {e(m.get("end_time"))}</span>' if m.get("end_time") and not m.get("all_day") else "")
              for m in meetings[:8]]
    t_rows = [f'<b class="late">Overdue</b> {e(t.get("name"))} <span class="sub">{e(t.get("project"))} since {e(t.get("due_on"))}</span>'
              for t in (tasks.get("overdue") or [])[:8]]
    t_rows += [f'<b>Today</b> {e(t.get("name"))} <span class="sub">{e(t.get("project"))}</span>'
               for t in (tasks.get("due_today") or [])[:8]]
    f_rows = [f'{e(f.get("title"))} <span class="sub">{e(("from " + f["who"] + " - ") if f.get("who") else "")}'
              f'{"due today" if f.get("date") == data.get("date") else "since " + e(f.get("date"))}</span>'
              for f in (brain.get("due") or [])[:8]]
    drafts = pending.get("cards") or []
    d_rows = [e(c.get("title")) for c in drafts[:8]]
    failed = data.get("sources_failed") or []
    note = f'<p class="note">Could not read: {e(", ".join(failed))}.</p>' if failed else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Front page - {e(data.get("weekday"))} {e(data.get("date"))}</title>
<style>
:root{{--ink:#14161f;--mute:#5b6072;--line:#d9dbe6;--accent:#4f46e5;--bad:#b42318}}
*{{box-sizing:border-box}}
body{{margin:0;background:#fff;color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:28px 20px 40px}}
header{{border-bottom:3px double var(--ink);padding-bottom:10px;margin-bottom:16px}}
.kicker{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin:0}}
h1{{font:700 30px/1.15 Georgia,"Times New Roman",serif;margin:4px 0 0}}
.focus{{border-left:4px solid var(--accent);background:#f3f2ff;padding:10px 14px;margin:0 0 18px;font-weight:600}}
section{{margin:0 0 16px;break-inside:avoid}}
h2{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);border-bottom:1px solid var(--line);padding-bottom:3px;margin:0 0 6px}}
ul{{margin:0;padding-left:18px}}li{{margin:2px 0}}
.sub{{color:var(--mute);font-size:13px}}.none{{color:var(--mute);margin:0}}.late{{color:var(--bad)}}
.note{{color:var(--bad);font-size:13px}}
.spend{{font-size:18px;font-weight:700}}
footer{{color:var(--mute);font-size:12px;border-top:1px solid var(--line);padding-top:8px;margin-top:18px}}
@media print{{main{{padding:0}}.focus{{background:#fff}}a{{color:inherit}}}}
</style></head><body><main>
<header><p class="kicker">REDLINE - Friday's morning front page</p>
<h1>{e(data.get("weekday"))}, {e(data.get("date"))}</h1></header>
<p class="focus">{e(call)}, {e(_lower_first(focus_line(data)))}</p>
{_section("Next meetings", m_rows, "No meetings left today.")}
{_section("Tasks due and overdue", t_rows, "Nothing due.")}
{_section("Follow-ups due", f_rows, "No follow-ups due.")}
{_section("Autopilot drafts waiting", d_rows, "Nothing to approve.")}
<section><h2>Spend so far</h2><p class="spend">{e(numbers.get("spent_text") or "0")}</p></section>
{note}
<footer>Generated {e(data.get("generated_at"))}. Print this page or keep it open.</footer>
</main></body></html>"""
