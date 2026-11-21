"""JOB INSIGHTS: the learning loop, one level deeper. See docs/api/jobinsights.md.

All of it is plain code on this laptop, over the job tracker's own records (`jobtrack.json`). No model reads anything, nothing is
sent, nothing is changed except this module's own small file (`jobinsights.json`: the weekly experiment and its result).

- FUNNEL: found -> triaged "apply" -> applied -> replied -> interview -> offer, per source, role family, resume version and the
  hour and weekday you applied. "found" and "triaged" come from the opportunity finder when it has them (guarded import);
  without them those two columns are empty and the rest still works.
- REJECTION PATTERNS: at which stage rejections came, how many days they took, and what rejected applications have in common.
- DOES THE SCORE PREDICT REPLIES: reply rate by score band, using the score stored on the record. Says "not enough data" honestly.
- SKILL GAPS: skills asked for most often across your saved and applied postings that are NOT in your own fact base (counts only).
  Report only: nothing is ever added to your facts or your resume.
- ONE EXPERIMENT A WEEK: a suggestion you accept or skip, and a plain result check a week later.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import threading
from collections import Counter
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.jobinsights")

REPLY = ("replied", "interview", "offer", "rejected")
INTERVIEW = ("interview", "offer")
MIN_BAND = 9              # scored applications needed before the score check says anything
MIN_RESULT = 5            # applications in an experiment before its result is more than a hint
EXPERIMENT_TARGET = 10
SKILL_TOP = 15
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HOUR_BUCKETS = (("before 11:00", 0, 11), ("11:00 to 14:00", 11, 14), ("14:00 to 18:00", 14, 18), ("after 18:00", 18, 24))

FAMILIES = (("data", r"\b(?:data|analyst|analytics|bi|machine learning|ml|scientist)\b"),
            ("devops and cloud", r"\b(?:devops|sre|cloud|infrastructure|platform|aws)\b"),
            ("mobile", r"\b(?:android|ios|mobile|flutter|react native)\b"),
            ("frontend", r"\b(?:frontend|front-end|front end|ui|react|angular|vue)\b"),
            ("backend", r"\b(?:backend|back-end|back end|python|java|php|node|golang|django|laravel|codeigniter|api|server)\b"),
            ("fullstack", r"\b(?:full[- ]?stack)\b"),
            ("qa and testing", r"\b(?:qa|quality|tester|testing|sdet)\b"),
            ("product and design", r"\b(?:product|designer|design|ux)\b"),
            ("support", r"\b(?:support|customer|success|helpdesk)\b"))
_FAMILY_RX = [(n, re.compile(r, re.I)) for n, r in FAMILIES]

# Skills worth counting (plain words people write in postings). Pattern -> display name. Extend freely; these are not claims.
SKILLS: dict[str, str] = {
    r"python": "Python", r"java(?!script)": "Java", r"javascript": "JavaScript", r"typescript": "TypeScript", r"php": "PHP", r"golang|go(?= (?:developer|engineer|programming|language|services?|microservices?))": "Go",
    r"c\+\+": "C++", r"c#": "C#", r"ruby": "Ruby", r"kotlin": "Kotlin", r"swift": "Swift", r"rust": "Rust", r"scala": "Scala", r"sql": "SQL",
    r"mysql": "MySQL", r"postgres(?:ql)?": "PostgreSQL", r"mongodb": "MongoDB", r"redis": "Redis", r"elasticsearch": "Elasticsearch",
    r"kafka": "Kafka", r"rabbitmq": "RabbitMQ", r"aws": "AWS", r"azure": "Azure", r"gcp|google cloud": "GCP", r"docker": "Docker",
    r"kubernetes|k8s": "Kubernetes", r"terraform": "Terraform", r"jenkins": "Jenkins", r"ci/cd|cicd": "CI/CD", r"git": "Git", r"linux": "Linux",
    r"react(?:\.js|js)?": "React", r"angular": "Angular", r"vue(?:\.js)?": "Vue", r"node(?:\.js|js)?": "Node.js", r"django": "Django",
    r"flask": "Flask", r"fastapi": "FastAPI", r"spring(?: boot)?": "Spring", r"laravel": "Laravel", r"codeigniter": "CodeIgniter",
    r"rest(?:ful)?(?: apis?)?": "REST APIs", r"graphql": "GraphQL", r"microservices?": "Microservices", r"pandas": "pandas", r"numpy": "NumPy",
    r"spark": "Spark", r"airflow": "Airflow", r"nifi": "NiFi", r"tableau": "Tableau", r"power ?bi": "Power BI", r"excel": "Excel",
    r"machine learning": "Machine learning", r"tensorflow": "TensorFlow", r"pytorch": "PyTorch", r"nlp": "NLP", r"llm": "LLMs",
    r"grafana": "Grafana", r"prometheus": "Prometheus", r"selenium": "Selenium", r"pytest": "pytest", r"junit": "JUnit", r"agile": "Agile",
    r"scrum": "Scrum", r"jira": "Jira", r"figma": "Figma", r"html": "HTML", r"css": "CSS", r"android": "Android", r"flutter": "Flutter",
    r"system design": "System design", r"data structures": "Data structures", r"etl": "ETL", r"snowflake": "Snowflake", r"dbt": "dbt",
    r"salesforce": "Salesforce", r"sap": "SAP", r"fintech|payments?": "Payments", r"api design": "API design", r"unit testing": "Unit testing",
}
_SKILL_RX = [(re.compile(r"(?<![a-z0-9+#.])(?:" + p + r")(?![a-z0-9+#])", re.I), name) for p, name in SKILLS.items()]
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def enabled(config) -> bool:
    return getattr(config, "jobinsights_enabled", True) is not False


def family_of(role: str) -> str:
    for name, rx in _FAMILY_RX:
        if rx.search(role or ""):
            return name
    return "other"


def skills_in(text: str) -> set[str]:
    return {name for rx, name in _SKILL_RX if rx.search(text or "")}


def hour_bucket(hour: int | None) -> str:
    if hour is None:
        return "(time not recorded)"
    for label, lo, hi in HOUR_BUCKETS:
        if lo <= hour < hi:
            return label
    return "(time not recorded)"


def weekday_of(day: str) -> str:
    try:
        return WEEKDAYS[dt.date.fromisoformat(day[:10]).weekday()]
    except ValueError:
        return "(unknown)"


def got(rec: dict, statuses) -> bool:
    return any(h.get("status") in statuses for h in rec.get("history", []))


def row(key: str, recs: list[dict]) -> dict:
    n = len(recs)
    rep = sum(1 for r in recs if got(r, REPLY))
    return {"key": key, "applied": n, "replies": rep, "interviews": sum(1 for r in recs if got(r, INTERVIEW)),
            "offers": sum(1 for r in recs if got(r, ("offer",))), "reply_rate": round(100 * rep / n) if n else 0}


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def iso_week(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


class Insights:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, jobtrack: Any = None, found_fn: Callable[[], list[dict]] | None = None,
                 postings_fn: Callable[[], list[dict]] | None = None, facts_fn: Callable[[], list[str]] | None = None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self._jt, self.found_fn, self.postings_fn, self.facts_fn = jobtrack, found_fn, postings_fn, facts_fn
        self.file = JsonFile(ctx.config.data_path() / "jobinsights.json")
        self.lock = threading.RLock()

    # sources of data (all local, all guarded) -----------------------------------------------------------------------
    @property
    def jt(self):
        if self._jt is not None:
            return self._jt
        from . import jobtrack
        return jobtrack.engine_for(self.ctx)

    def records(self) -> list[dict]:
        return [r for r in self.jt._data()["records"] if r.get("status") != "withdrawn"]

    def found_items(self) -> list[dict]:
        """Items the opportunity finder has seen (for the 'found' and 'triaged' columns). [] when unavailable."""
        try:
            if self.found_fn is not None:
                return list(self.found_fn())
            from . import oppsources
            eng = oppsources.engine_for(self.ctx)
            rows: list[dict] = []
            for st in ("new", "saved", "applied", "dismissed"):
                rows += eng.items(status=st, limit=500)
            return rows
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _triage_apply(item: dict) -> bool | None:
        """True / False when the finder triaged this item, None when it has no triage at all."""
        t = item.get("triage")
        if t is None:
            t = item.get("verdict") if item.get("verdict") in ("apply", "skip", "maybe", "later") else None
        if isinstance(t, dict):
            t = t.get("verdict") or t.get("decision") or t.get("action")
        if t is None:
            return None
        return str(t).lower() in ("apply", "yes", "good", "recommended")

    # funnel -----------------------------------------------------------------------------------------------------------
    def funnel(self, recs: list[dict], found: list[dict]) -> dict:
        def group(keyfn) -> list[dict]:
            g: dict[str, list[dict]] = {}
            for r in recs:
                g.setdefault(keyfn(r), []).append(r)
            return sorted((row(k, v) for k, v in g.items()), key=lambda x: (-x["applied"], x["key"]))

        by_source = group(lambda r: r.get("source") or "manual")
        triaged_any = any(self._triage_apply(i) is not None for i in found)
        fcount: Counter = Counter()
        tcount: Counter = Counter()
        for i in found:
            sid = str(i.get("source_id") or i.get("source") or "")
            fcount[sid] += 1
            if self._triage_apply(i):
                tcount[sid] += 1
        for r in by_source:
            r["found"] = fcount.get(r["key"]) if fcount else None
            r["triaged_apply"] = tcount.get(r["key"], 0) if triaged_any else None
        total = row("all", recs)
        total["found"] = len(found) if found else None
        total["triaged_apply"] = sum(1 for i in found if self._triage_apply(i)) if triaged_any else None
        return {"total": total, "by_source": by_source, "by_role_family": group(lambda r: family_of(r.get("role", ""))),
                "by_resume": group(lambda r: r.get("resume_version") or "(not recorded)"),
                "by_hour": [r for r in group(lambda r: hour_bucket(r.get("applied_hour"))) if r["key"] != "(time not recorded)" or r["applied"]],
                "by_weekday": sorted(group(lambda r: weekday_of(r.get("applied_at", ""))),
                                     key=lambda x: WEEKDAYS.index(x["key"]) if x["key"] in WEEKDAYS else 9)}

    # rejection patterns -------------------------------------------------------------------------------------------------
    def rejections(self, recs: list[dict]) -> dict:
        rej = [r for r in recs if got(r, ("rejected",))]
        if not rej:
            return {"n": 0, "text": "No rejections recorded yet.", "stages": [], "days": None, "traits": []}
        stages: Counter = Counter()
        days: list[int] = []
        for r in rej:
            hist = r.get("history", [])
            idx = next((i for i, h in enumerate(hist) if h.get("status") == "rejected"), None)
            before = [h["status"] for h in hist[:idx]] if idx is not None else []
            if "offer" in before:
                stage = "after an offer"
            elif "interview" in before:
                stage = "after an interview"
            elif "replied" in before:
                stage = "after a first reply"
            else:
                stage = "straight after applying (screening)"
            stages[stage] += 1
            if idx is not None:
                try:
                    days.append(max((dt.date.fromisoformat(hist[idx]["at"][:10]) - dt.date.fromisoformat(r["applied_at"])).days, 0))
                except (ValueError, KeyError):
                    pass
        traits = []
        if len(rej) >= 3:
            def share(keyfn, label, skip=("(not recorded)", "manual", "other")):
                mine, allc = Counter(keyfn(r) for r in rej), Counter(keyfn(r) for r in recs)
                for k, n in mine.most_common(2):
                    if k in skip or n < 2:
                        continue
                    pct, base = round(100 * n / len(rej)), round(100 * allc[k] / len(recs))
                    if pct >= base + 15:
                        traits.append({"trait": f"{label}: {k}", "rejected_share": pct, "all_share": base})
            share(lambda r: r.get("source") or "manual", "source")
            share(lambda r: family_of(r.get("role", "")), "role family")
            share(lambda r: r.get("resume_version") or "(not recorded)", "resume")
            stale = sum(1 for r in rej if r.get("stale_posting"))
            if stale >= 2 and stale / len(rej) >= 0.4:
                traits.append({"trait": "old posting (over 30 days when you applied)", "rejected_share": round(100 * stale / len(rej)),
                               "all_share": round(100 * sum(1 for r in recs if r.get("stale_posting")) / len(recs))})
        med = sorted(days)[len(days) // 2] if days else None
        top = stages.most_common(1)[0]
        text = f"{plural(len(rej), 'rejection')}. Most came {top[0]} ({top[1]})." + (f" Typically {med} days after applying." if med is not None else "")
        return {"n": len(rej), "text": text, "stages": [{"stage": k, "n": v} for k, v in stages.most_common()],
                "days": {"median": med, "n": len(days)} if days else None, "traits": traits[:4]}

    # does the score predict replies ------------------------------------------------------------------------------------------
    def score_check(self, recs: list[dict]) -> dict:
        scored = sorted((r for r in recs if isinstance(r.get("score"), (int, float)) and r["score"] > 0), key=lambda r: r["score"])
        if len(scored) < MIN_BAND:
            return {"verdict": "not_enough", "n": len(scored), "bands": [],
                    "text": f"Not enough scored applications yet ({len(scored)} of the {MIN_BAND} needed). Applications keep the match score they had when you applied."}
        third = len(scored) // 3
        parts = [("low", scored[:third]), ("middle", scored[third:len(scored) - third]), ("high", scored[len(scored) - third:])]
        bands = []
        for name, rs in parts:
            b = row(name, rs)
            b["score_from"], b["score_to"] = round(rs[0]["score"], 1), round(rs[-1]["score"], 1)
            bands.append(b)
        diff = bands[2]["reply_rate"] - bands[0]["reply_rate"]
        if diff >= 10:
            verdict, text = "yes", f"Yes, so far: high scores got a {bands[2]['reply_rate']}% reply rate, low scores {bands[0]['reply_rate']}%. Trust the score a little more."
        elif diff <= -10:
            verdict, text = "no", f"No: low scores got MORE replies ({bands[0]['reply_rate']}% against {bands[2]['reply_rate']}%). The score may be pointing at the wrong things."
        else:
            verdict, text = "unclear", f"No clear link yet: {bands[0]['reply_rate']}% (low), {bands[1]['reply_rate']}% (middle), {bands[2]['reply_rate']}% (high)."
        return {"verdict": verdict, "n": len(scored), "bands": bands, "text": text + " Small numbers move a lot; read this as a hint."}

    # skill gaps (report only) ---------------------------------------------------------------------------------------------------
    def posting_texts(self, recs: list[dict]) -> list[str]:
        texts: dict[str, str] = {}
        for r in recs:
            t = f"{r.get('role', '')}\n{r.get('posting', '')}"
            texts[r.get("url") or f"rec:{r['id']}"] = t
        try:
            extra = self.postings_fn() if self.postings_fn is not None else self._saved_postings()
        except Exception:  # noqa: BLE001
            extra = []
        for p in extra:
            key = p.get("url") or f"{p.get('company', '')}|{p.get('title', '')}"
            texts.setdefault(key, f"{p.get('title', '')}\n{p.get('description', '')}")
        return list(texts.values())

    def _saved_postings(self) -> list[dict]:
        out: list[dict] = []
        try:
            from . import applyq
            store = applyq.store_for(self.ctx.config)
            out += store.list(("ready", "sent", "applied", "replied"), 500)
        except Exception:  # noqa: BLE001
            pass
        out += [i for i in self.found_items() if i.get("status") in ("saved", "applied")]
        return out

    def confirmed_text(self) -> str:
        try:
            if self.facts_fn is not None:
                return " ".join(self.facts_fn()).lower()
            from . import applyq, factbase
            fb = factbase.FactBase.build(applyq.load_profile(self.ctx) or {}, factbase.load(self.ctx))
            return " ".join(str(f.get("text") or "") for f in fb.facts).lower()
        except Exception:  # noqa: BLE001
            return ""

    def skill_gaps(self, recs: list[dict]) -> dict:
        texts = self.posting_texts(recs)
        asked: Counter = Counter()
        for t in texts:
            asked.update(skills_in(t))
        mine = skills_in(self.confirmed_text())
        gaps = [{"skill": s, "count": n} for s, n in asked.most_common() if s not in mine][:SKILL_TOP]
        have = [{"skill": s, "count": n} for s, n in asked.most_common() if s in mine][:SKILL_TOP]
        return {"postings": len(texts), "gaps": gaps, "have": have,
                "note": "Counts only. Nothing here is added to your facts or resume: add a skill only if it is true, in Facts."}

    # the weekly experiment --------------------------------------------------------------------------------------------------------
    def _data(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        d["experiments"] = [e for e in d.get("experiments", []) if isinstance(e, dict) and e.get("id")]
        return d

    def _propose(self, recs: list[dict], used: set[str]) -> dict:
        """One concrete experiment from the data. Plain rules, first that fits."""
        by_resume = [r for r in self.funnel(recs, [])["by_resume"] if r["key"] != "(not recorded)" and r["applied"] >= 4]
        if len(by_resume) >= 2:
            best = max(by_resume, key=lambda r: (r["reply_rate"], r["applied"]))
            most = by_resume[0]
            if best["key"] != most["key"] and best["reply_rate"] > most["reply_rate"] and f"resume:{best['key']}" not in used:
                return {"kind": "resume", "target": best["key"], "text": f"Try the \"{best['key']}\" resume for your next {EXPERIMENT_TARGET} applications.",
                        "why": f"It got a {best['reply_rate']}% reply rate from {best['applied']} applications, against {most['reply_rate']}% for \"{most['key']}\" ({most['applied']})."}
        hours = {r["key"]: r for r in self.funnel(recs, [])["by_hour"] if r["applied"] >= 4 and r["key"] != "(time not recorded)"}
        morning = hours.get("before 11:00")
        later = [r for k, r in hours.items() if k != "before 11:00"]
        if "hour:before11" not in used:
            if morning is None and later:
                return {"kind": "hour", "target": "before11", "text": f"Apply before 11:00 for your next {EXPERIMENT_TARGET} applications.",
                        "why": "You have not tried early applications yet, and early ones are often read first."}
            if morning and later and morning["reply_rate"] > max(r["reply_rate"] for r in later):
                return {"kind": "hour", "target": "before11", "text": f"Apply before 11:00 for your next {EXPERIMENT_TARGET} applications.",
                        "why": f"Before 11:00 got {morning['reply_rate']}% replies from {morning['applied']} applications, better than your other times."}
        sc = self.score_check(recs)
        if sc["verdict"] in ("yes", "unclear") and "score:high" not in used and sc["bands"]:
            return {"kind": "score", "target": "high", "text": f"Only apply to the best-scored third of postings for your next {EXPERIMENT_TARGET} applications.",
                    "why": "It tests whether the score is worth trusting."}
        return {"kind": "hour", "target": "before11", "text": f"Apply before 11:00 for your next {EXPERIMENT_TARGET} applications.",
                "why": "A simple first experiment: early applications are often read first."}

    def _matches(self, e: dict, r: dict, bounds: dict) -> bool:
        if e["kind"] == "resume":
            return r.get("resume_version") == e["target"]
        if e["kind"] == "hour":
            return isinstance(r.get("applied_hour"), int) and r["applied_hour"] < 11
        if e["kind"] == "score":
            return isinstance(r.get("score"), (int, float)) and bounds.get("high") is not None and r["score"] >= bounds["high"]
        return False

    def _bounds(self, recs: list[dict]) -> dict:
        s = sorted(r["score"] for r in recs if isinstance(r.get("score"), (int, float)) and r["score"] > 0)
        return {"high": s[len(s) - len(s) // 3] if len(s) >= MIN_BAND else None}

    def result(self, e: dict, recs: list[dict]) -> dict:
        """The result check for a started experiment: applications since it began that match it, against those before."""
        started = e.get("started") or ""
        bounds = self._bounds(recs)
        inside = [r for r in recs if started and r["applied_at"] >= started and self._matches(e, r, bounds)]
        before = [r for r in recs if started and r["applied_at"] < started]
        a, b = row("during", inside), row("before", before)
        if a["applied"] < MIN_RESULT:
            verdict, text = "too_early", f"{plural(a['applied'], 'matching application')} so far; it needs {MIN_RESULT} before there is anything to compare."
        else:
            diff = a["reply_rate"] - b["reply_rate"]
            verdict = "better" if diff >= 10 else "worse" if diff <= -10 else "no_change"
            text = (f"{plural(a['applied'], 'application')} in the experiment got {a['reply_rate']}% replies; the ones before it got {b['reply_rate']}% "
                    f"({b['applied']}). " + {"better": "It looks better. Keep it for another week.", "worse": "It looks worse. Go back to what you did before.",
                                             "no_change": "No clear change."}[verdict] + " Small numbers; a hint, not proof.")
        return {"verdict": verdict, "text": text, "during": a, "before": b}

    def experiment_state(self, recs: list[dict]) -> dict:
        today = self.clock().date()
        week = iso_week(today)
        with self.lock:
            d = self._data()
            exps = d["experiments"]
            cur = next((e for e in exps if e["week"] == week), None)
            if cur is None:
                used = {f"{e['kind']}:{e['target']}" for e in exps[-4:]}
                prop = self._propose(recs, used)
                cur = {"id": secrets.token_hex(6), "week": week, "state": "suggested", "created": today.isoformat(), "started": "", **prop}
                exps.append(cur)
                d["experiments"] = exps[-20:]
                self.file.save(d)
        last = None
        for e in reversed(d["experiments"]):
            if e["week"] != week and e["state"] == "active":
                last = e
                break
        out = {"current": dict(cur), "last": None}
        if cur["state"] == "active":
            out["current"]["result"] = self.result(cur, recs)
        if last is not None:
            out["last"] = {**last, "result": self.result(last, recs)}
        return out

    def _pick(self, eid: str) -> tuple[dict, dict]:
        from .jobtrack import NotFound
        d = self._data()
        e = next((e for e in d["experiments"] if e["id"] == eid), None) if _ID_RE.match(eid or "") else None
        if e is None:
            raise NotFound("No such experiment.")
        return d, e

    def start_experiment(self, eid: str) -> dict:
        from .jobtrack import TrackError
        with self.lock:
            d, e = self._pick(eid)
            if e["state"] != "suggested":
                raise TrackError(f"This experiment is already {e['state']}.")
            for other in d["experiments"]:
                if other["state"] == "active":
                    other["state"] = "done"
            e["state"], e["started"] = "active", self.clock().date().isoformat()
            self.file.save(d)
        return e

    def skip_experiment(self, eid: str) -> dict:
        from .jobtrack import TrackError
        with self.lock:
            d, e = self._pick(eid)
            if e["state"] != "suggested":
                raise TrackError(f"This experiment is already {e['state']}.")
            e["state"] = "skipped"
            self.file.save(d)
        return e

    # the whole page --------------------------------------------------------------------------------------------------------------------
    def overview(self) -> dict:
        recs = self.records()
        found = self.found_items()
        return {"generated": self.clock().isoformat(timespec="seconds"), "applications": len(recs), "funnel": self.funnel(recs, found),
                "rejections": self.rejections(recs), "score_check": self.score_check(recs), "skill_gaps": self.skill_gaps(recs),
                "experiment": self.experiment_state(recs),
                "note": "Worked out on this laptop from your own tracker. No AI reads your jobs, and nothing is changed by this page."}


def engine_for(ctx: Ctx, **kw) -> Insights:
    eng = getattr(ctx, "jobinsights", None)
    if eng is None or kw:
        eng = Insights(ctx, **kw)
        try:
            ctx.jobinsights = eng
        except AttributeError:  # pragma: no cover
            pass
    return eng


def make_router(ctx: Ctx, **kw) -> APIRouter:
    from .jobtrack import NotFound, TrackError
    eng = engine_for(ctx, **kw)
    router = APIRouter(prefix="/api/jobinsights", dependencies=[Depends(ctx.need_auth)])
    router.service = eng      # type: ignore[attr-defined]

    def guard(fn):
        if not enabled(ctx.config):
            raise HTTPException(404, "Job insights are switched off (jobinsights_enabled is false).")
        try:
            return fn()
        except TrackError as exc:
            raise HTTPException(400, str(exc)) from None
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("")
    async def overview():
        return await asyncio.to_thread(lambda: guard(eng.overview))

    @router.post("/experiment/{eid}/start")
    async def start_exp(eid: str):
        return guard(lambda: eng.start_experiment(eid))

    @router.post("/experiment/{eid}/skip")
    async def skip_exp(eid: str):
        return guard(lambda: eng.skip_experiment(eid))

    return router
