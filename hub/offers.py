"""OFFERS AND NEGOTIATION: record job offers, compare up to four side by side in an India-aware way, draft a counter-offer from the
owner's own numbers, check the contract clauses, and check a posting's pay against the owner's target. See docs/api/offers.md.

Everything is typed by the owner. The hub never knows or guesses market pay and never invents a number: an in-hand figure is an
ESTIMATE from the owner's offer and a short list of ASSUMPTIONS (PF rate, basic share, professional tax, tax regime) that the owner can
change and that are printed next to the result. Tax slabs are the ones built in below (`TAX_RULES_AS_OF`); surcharge, HRA, 80D and other
exemptions are not modelled unless the owner types them as "other deductions". Only INR offers get an in-hand estimate; other currencies
are compared on gross figures. Counter-offer text is a DRAFT: nothing is sent, a number the owner did not give shows as [FILL: ...] and
blocks Copy. Pay check reads ranges only from postings already in the hub (no Glassdoor, no Levels.fyi, no scraping).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import threading
from pathlib import Path
from statistics import median
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException

from . import oppsnorm as N
from .assistant import ExtraTool, mark_tools
from .common import Ctx, InProcessClient, JsonFile
from .factbase import clean

log = logging.getLogger("hub.offers")

from . import assistant as _assistant  # noqa: E402
_assistant.TOOL_GROUPS["offers"] = (
    {"offer_compare"},
    re.compile(r"\bcompare (?:my |the )?(?:job )?offers?\b|\bwhich offer\b|\bin[- ]hand (?:salary|pay)\b|\bctc (?:vs|versus|to)\b|\boffers?\b.{0,40}\b(?:comparison|in[- ]hand)\b", re.I))

MAX_OFFERS = 30
MAX_COMPARE = 4
_LOCK = threading.RLock()
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAX_RULES_AS_OF = "FY 2025-26 slabs, built into the hub. Check them each year."

# The assumptions the owner can change (hub shows them next to every estimate). None of them is a fact about any employer.
DEFAULTS = {"regime": "new",                 # "new" or "old" tax regime
            "basic_pct": 50,                 # basic pay as a percent of the fixed pay (PF is worked out on basic)
            "pf_rate_pct": 12,               # employee and employer PF rate on basic
            "pf_capped": False,              # True: PF only on the first pf_wage_cap_monthly of basic
            "pf_wage_cap_monthly": 15000,
            "ctc_includes_employer_pf": True,  # True: the fixed pay in the offer already includes the employer's PF
            "professional_tax_annual": 2400,  # state professional tax per year (often 200 a month)
            "variable_payout_pct": 80,       # the share of variable pay expected to be paid out (variable is shown as an expected value)
            "esop_counted_pct": 0,           # how much of the ESOP paper value per year to count in the cash total (0 = none, shown apart)
            "other_deductions_old": 0,       # 80C extras, 80D, HRA and so on you can claim, old regime only (typed by you)
            "current_monthly_gross": None}   # your current monthly gross pay, to price a notice buy-out
ASSUMPTION_LABELS = {"regime": "Tax regime (new or old)", "basic_pct": "Basic pay, percent of fixed", "pf_rate_pct": "PF rate, percent of basic",
                     "pf_capped": "PF only on the wage cap", "pf_wage_cap_monthly": "PF wage cap per month", "ctc_includes_employer_pf": "Fixed pay includes the employer's PF",
                     "professional_tax_annual": "Professional tax per year", "variable_payout_pct": "Variable pay expected to be paid, percent",
                     "esop_counted_pct": "ESOP paper value counted in the cash total, percent", "other_deductions_old": "Other deductions (old regime), per year",
                     "current_monthly_gross": "Your current monthly gross pay (for a notice buy-out)"}

NEW_SLABS = ((400000, 0.0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15), (2000000, 0.20), (2400000, 0.25), (float("inf"), 0.30))
OLD_SLABS = ((250000, 0.0), (500000, 0.05), (1000000, 0.20), (float("inf"), 0.30))
STD_DEDUCTION = {"new": 75000, "old": 50000}
REBATE_LIMIT = {"new": 1200000, "old": 500000}      # section 87A: no tax up to this taxable income
CESS = 0.04

# The contract clauses worth reading before signing. `ask` is the question to put to the recruiter.
CLAUSES = (
    ("non_compete", "Non-compete", "You may be barred from working for similar companies after you leave.", "Is there a non-compete or non-solicit clause, and for how long?"),
    ("bond", "Service bond or minimum stay", "You may owe money if you leave before a set period.", "Is there a minimum service period, and what is the amount if I leave earlier?"),
    ("clawback", "Clawback of joining bonus or relocation", "The joining bonus or relocation money may have to be paid back.", "Is any bonus or relocation money repayable, and for how many months?"),
    ("ip", "IP assignment", "Work you do outside office hours or on your own time may be claimed.", "Does the IP clause cover only work done for the company?"),
    ("notice", "Notice period and buy-out", "A long notice or no buy-out option can delay your next move.", "What is the notice period, and can it be bought out?"),
    ("probation", "Probation", "Probation can have a shorter notice or no benefits.", "How long is the probation, and what changes during it?"),
    ("variable", "Variable pay terms", "Variable pay may be at the company's discretion.", "How is variable pay decided, and what was the payout over the last two years?"),
    ("esop", "ESOP terms", "Vesting cliff, exercise window and buy-back decide what the grant is worth.", "What are the vesting schedule, the cliff, the exercise window and the strike price?"),
)
CLAUSE_IDS = tuple(c[0] for c in CLAUSES)


class OfferError(ValueError):
    """A plain sentence for the owner."""


def enabled(config) -> bool:
    return getattr(config, "offers_enabled", True) is not False


# ---- the India-aware estimate (pure) -------------------------------------------------------------------------------------

def slab_tax(taxable: float, regime: str) -> float:
    """Income tax on a taxable income, with the 87A rebate and 4 percent cess. No surcharge."""
    slabs = NEW_SLABS if regime == "new" else OLD_SLABS
    tax, prev = 0.0, 0.0
    for limit, rate in slabs:
        if taxable > prev:
            tax += (min(taxable, limit) - prev) * rate
        prev = limit
    limit = REBATE_LIMIT[regime]
    if taxable <= limit:
        tax = 0.0
    elif regime == "new":
        tax = min(tax, taxable - limit)          # marginal relief just above the rebate limit
    return round(tax * (1 + CESS), 2)


def estimate(offer: dict, a: dict | None = None) -> dict | None:
    """The in-hand estimate for an INR offer. `None` for another currency or when no fixed pay is given. Every number it uses is in
    the result (`assumptions`) so the owner can see what it rests on."""
    a = {**DEFAULTS, **(a or {})}
    if (offer.get("currency") or "INR") != "INR" or not offer.get("fixed"):
        return None
    regime = a["regime"] if a["regime"] in ("new", "old") else "new"
    fixed = float(offer["fixed"])
    var_target = float(offer.get("variable") or 0)
    payout = float(offer["variable_payout_pct"] if offer.get("variable_payout_pct") is not None else a["variable_payout_pct"])
    var_exp = var_target * payout / 100
    bonus = float(offer.get("bonus") or 0)
    joining = float(offer.get("joining_bonus") or 0)
    basic = fixed * float(a["basic_pct"]) / 100
    pf_base = min(basic, float(a["pf_wage_cap_monthly"]) * 12) if a["pf_capped"] else basic
    emp_pf = pf_base * float(a["pf_rate_pct"]) / 100
    er_pf = emp_pf if a["ctc_includes_employer_pf"] else 0.0
    pt = float(a["professional_tax_annual"])
    gross_taxable = fixed - er_pf + var_exp + bonus
    deductions = STD_DEDUCTION[regime]
    if regime == "old":
        deductions += min(150000.0, emp_pf) + float(a["other_deductions_old"] or 0)
    taxable = max(0.0, gross_taxable - deductions - (pt if regime == "old" else 0))
    tax = slab_tax(taxable, regime)
    cash_fixed = fixed - er_pf - emp_pf - pt
    share = (fixed - er_pf) / gross_taxable if gross_taxable else 1.0
    monthly = (cash_fixed - tax * share) / 12
    annual_take_home = (fixed - er_pf + var_exp + bonus) - emp_pf - pt - tax
    joining_after = joining
    if joining:
        tax2 = slab_tax(max(0.0, taxable + joining), regime)
        joining_after = joining - (tax2 - tax)
    return {"currency": "INR", "regime": regime, "fixed": round(fixed), "variable_expected": round(var_exp), "variable_target": round(var_target),
            "variable_payout_pct": payout, "bonus": round(bonus), "employer_pf": round(er_pf), "employee_pf": round(emp_pf), "professional_tax": round(pt),
            "taxable_income": round(taxable), "income_tax": round(tax), "monthly_in_hand": round(monthly),
            "annual_take_home": round(annual_take_home), "avg_monthly_all_in": round(annual_take_home / 12), "joining_bonus_after_tax": round(joining_after),
            "assumptions": {k: a[k] for k in DEFAULTS if k != "current_monthly_gross"}, "rules": TAX_RULES_AS_OF,
            "label": "ESTIMATE only. Your payslip will differ (gratuity, insurance, allowances, TDS timing, surcharge and exemptions are not modelled)."}


def esop_per_year(esop: dict) -> float | None:
    """The ESOP paper value per year from what the owner typed (grant value over vesting years); None when not given."""
    v, yrs = esop.get("grant_value"), esop.get("vesting_years")
    return round(float(v) / float(yrs)) if v and yrs else None


def buyout_cost(offer: dict, a: dict) -> float | None:
    days, monthly = offer.get("notice_buyout_days"), a.get("current_monthly_gross")
    if not days or offer.get("buyout_covered"):
        return 0.0 if days and offer.get("buyout_covered") else None
    if not monthly:
        return None
    return round(float(monthly) / 30 * float(days))


def totals(offer: dict, a: dict) -> dict:
    """Year-one and recurring cash from the owner's own figures. A part that was not typed counts as zero and is listed in `missing`."""
    est = estimate(offer, a)
    fixed = float(offer.get("fixed") or 0)
    var_exp = est["variable_expected"] if est else round(float(offer.get("variable") or 0) * float(
        offer["variable_payout_pct"] if offer.get("variable_payout_pct") is not None else a.get("variable_payout_pct", DEFAULTS["variable_payout_pct"])) / 100)
    bonus = float(offer.get("bonus") or 0)
    esop_year = esop_per_year(offer.get("esop") or {})
    counted = round((esop_year or 0) * float(a.get("esop_counted_pct", 0)) / 100)
    benefits = float(offer.get("benefits_value") or 0)
    recurring = round(fixed + var_exp + bonus + benefits + counted)
    cost_buy = buyout_cost(offer, a)
    reloc = float(offer.get("relocation_cost") or 0) - float(offer.get("relocation_support") or 0)
    joining = float(offer.get("joining_bonus") or 0)
    first_year = round(recurring + joining - (cost_buy or 0) - reloc)
    missing = [lab for k, lab in (("fixed", "fixed pay"), ("variable", "variable pay")) if not offer.get(k)]
    return {"annual_recurring": recurring, "first_year": first_year, "variable_expected": round(var_exp), "esop_per_year": esop_year, "esop_counted": counted,
            "buyout_cost": cost_buy, "relocation_net_cost": round(reloc), "missing": missing}


def clause_review(offer: dict) -> list[dict]:
    state = offer.get("clauses") or {}
    out = []
    for cid, label, risk, ask in CLAUSES:
        s = state.get(cid) if state.get(cid) in ("yes", "no") else "unknown"
        out.append({"id": cid, "label": label, "state": s, "risk": risk, "ask": ask,
                    "action": {"yes": "Present: read it and consider asking to change it.", "no": "Not present.", "unknown": "Not checked yet: ask the recruiter or read the contract."}[s]})
    return out


def compare(offers: list[dict], a: dict) -> dict:
    """Rows side by side for up to four offers. `best` marks the highest number in a row of the same currency (a hint, not advice)."""
    if not 2 <= len(offers) <= MAX_COMPARE:
        raise OfferError(f"Pick from 2 to {MAX_COMPARE} offers to compare.")
    cur = {o.get("currency") or "INR" for o in offers}
    est = [estimate(o, a) for o in offers]
    tot = [totals(o, a) for o in offers]
    rows = []

    def num(key, label, values, note="", best="max"):
        have = [v for v in values if isinstance(v, (int, float))]
        pick = None
        if len(cur) == 1 and len(have) >= 2:
            target = max(have) if best == "max" else min(have)
            pick = [i for i, v in enumerate(values) if v == target]
        rows.append({"key": key, "label": label, "values": values, "best": pick or [], "note": note, "numeric": True})

    def text(key, label, values):
        rows.append({"key": key, "label": label, "values": values, "best": [], "numeric": False})

    num("fixed", "Fixed pay per year", [o.get("fixed") for o in offers])
    num("variable_expected", "Variable per year, expected value", [t["variable_expected"] if o.get("variable") else None for o, t in zip(offers, tot)],
        "Variable target times the expected payout percent.")
    num("bonus", "Annual bonus", [o.get("bonus") for o in offers])
    num("joining_bonus", "Joining bonus (once)", [o.get("joining_bonus") for o in offers])
    num("esop", "ESOP paper value per year (not counted unless you set it)", [t["esop_per_year"] for t in tot], "Grant value over vesting years. It may be worth nothing.")
    num("recurring", "Cash value per year", [t["annual_recurring"] for t in tot], "Fixed + expected variable + bonus + benefits value.")
    num("monthly_in_hand", "In-hand per month, estimate", [e["monthly_in_hand"] if e else None for e in est], "From the fixed pay only. INR offers only. ESTIMATE.")
    num("avg_monthly_all_in", "In-hand per month, with variable, estimate", [e["avg_monthly_all_in"] if e else None for e in est], "ESTIMATE.")
    num("first_year", "First year total (joining bonus, relocation, notice buy-out included)", [t["first_year"] for t in tot])
    num("buyout", "Notice buy-out cost to you", [t["buyout_cost"] for t in tot], "Lower is better.", best="min")
    text("notice", "Notice period", [f"{o['notice_days']} days" if o.get("notice_days") else "" for o in offers])
    text("location", "Location and mode", [" ".join(x for x in (o.get("location", ""), o.get("work_mode", "")) if x) for o in offers])
    text("start", "Start date", [o.get("start_date", "") for o in offers])
    text("probation", "Probation", [f"{o['probation_months']} months" if o.get("probation_months") is not None else "" for o in offers])
    text("benefits", "Benefits", [o.get("benefits", "") for o in offers])
    text("clauses", "Clauses to review", [", ".join(c["label"] for c in clause_review(o) if c["state"] == "yes") or "none marked" for o in offers])
    notes = ["In-hand and tax figures are ESTIMATES from your assumptions. Bold marks are only the highest number in a row: look at the whole package."]
    if len(cur) > 1:
        notes.append("The offers use different currencies, so no row is ranked.")
    return {"offers": [{"id": o["id"], "company": o.get("company", ""), "role": o.get("role", ""), "currency": o.get("currency") or "INR"} for o in offers],
            "rows": rows, "notes": notes, "assumptions": {k: a.get(k) for k in DEFAULTS}, "rules": TAX_RULES_AS_OF}


# ---- counter-offer drafts (pure) -----------------------------------------------------------------------------------------

def _money_txt(cur: str, n: Any) -> str:
    return f"{cur} {float(n):,.0f}".strip()


def counter_draft(offer: dict, ask: dict, competing: list[dict], reasons: list[str], *, name_competitor: bool = False, recruiter: str = "",
                  channel: str = "email", owner_name: str = "") -> dict:
    """An email or a call script. Every number comes from the offer, the owner's ask or a competing offer the owner ticked. A missing ask is [FILL: ...]."""
    cur = offer.get("currency") or "INR"
    who = recruiter or "[FILL: recruiter's name]"
    role = offer.get("role") or "the role"
    company = offer.get("company") or "the company"
    pts = []
    for key, label in (("fixed", "fixed pay per year"), ("variable", "variable pay per year"), ("joining_bonus", "joining bonus")):
        if key not in ask and key != "fixed":
            continue
        want = _money_txt(cur, ask[key]) if ask.get(key) else f"[FILL: the {label} you want]"
        have = offer.get(key)
        pts.append(f"{label}: the offer is {_money_txt(cur, have)}; I was hoping for {want}" if have else f"{label}: I was hoping for {want}")
    if ask.get("notice_days"):
        pts.append(f"notice period: I can join after {int(ask['notice_days'])} days")
    if ask.get("start_date"):
        pts.append(f"start date: {ask['start_date']}")
    if ask.get("other"):
        pts.append(str(ask["other"])[:200])
    comp_lines = []
    for c in competing:
        ccur = c.get("currency") or cur
        who_c = f" from {c['company']}" if name_competitor and c.get("company") else ""
        parts = [f"fixed pay of {_money_txt(ccur, c['fixed'])}"] if c.get("fixed") else []
        if c.get("variable"):
            parts.append(f"variable pay of {_money_txt(ccur, c['variable'])}")
        if parts:
            comp_lines.append(f"I also hold another offer{who_c} with " + " and ".join(parts) + ".")
    reasons = [r for r in (clean(x, 240) for x in reasons) if r]
    sign = owner_name or "[FILL: your name]"
    if channel == "call":
        lines = ["Opening: \"Thank you for the offer. I am keen to join, and I would like to talk through a few points.\""]
        lines += [f"Say about {p}." for p in pts]
        lines += [f"If they ask why: \"{r}\"" for r in reasons]
        lines += [f"If they ask about other offers: \"{x}\"" for x in comp_lines]
        lines += ["If they cannot move on pay: \"Could we look at the joining bonus, a review in six months or the start date?\"",
                  "Close: \"Thank you. Could you send the revised offer in writing?\"", "Then stop talking. A pause after your number is normal."]
        text = "\n".join(lines)
        subject = ""
    else:
        subject = f"Offer for {role}: a few points to discuss"
        body = [f"Hi {who},", "", f"Thank you for the offer for {role} at {company}. I am keen to join. After reading the whole package I would like to discuss a few points:", ""]
        body += [f"- {p}" for p in pts]
        if reasons:
            body += ["", " ".join(reasons)]
        if comp_lines:
            body += ["", " ".join(comp_lines)]
        body += ["", "Is there some flexibility? I can confirm quickly once I see a revised offer in writing.", "", "Regards,", sign]
        text = "\n".join(body)
    fills = sorted(set(re.findall(r"\[FILL: [^\]]+\]", text)))
    return {"channel": channel, "subject": subject, "text": text, "blocked": bool(fills), "fills": fills,
            "numbers_from": "your offer, the numbers you typed and the competing offers you ticked: nothing else",
            "note": "Draft only. Nothing is sent. Copy it and send it yourself. Never claim an offer you do not hold.", "sends": False}


# ---- pay check (pure) ----------------------------------------------------------------------------------------------------

_STOP = frozenset("the and for with senior junior lead staff principal remote hybrid onsite full time part contract job role engineer developer".split())
_PER_YEAR = {"year": 1, "month": 12, "week": 52}


def annual_range(pay: dict | None) -> tuple[float, float] | None:
    """(min, max) per year from a parsed pay range, or None when the unit cannot be turned into a year (hourly, daily, per project)."""
    if not pay or not pay.get("max"):
        return None
    mult = _PER_YEAR.get(pay.get("unit") or "year", 0) if pay.get("unit") else 1
    if not mult:
        return None
    return (float(pay.get("min") or pay["max"]) * mult, float(pay["max"]) * mult)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]{3,}", (title or "").lower()) if w not in _STOP}


def pay_check(posting: dict, target: dict, items: list[dict]) -> dict:
    """The posting's advertised range, the owner's target from the pay sheet, ranges in similar postings already in the hub, and a verdict."""
    pay = posting.get("pay") or N.parse_pay(posting.get("pay_text") or "") or N.parse_pay(f"{posting.get('title', '')}\n{posting.get('description', '')[:3000]}")
    adv = annual_range(pay)
    t_cur = (target.get("currency") or "").upper()
    cur = (pay or {}).get("currency") or (t_cur if not adv else "")      # with no advertised range, similar postings are read in the currency of the owner's target
    want = _words(posting.get("title", ""))
    sims, skip = [], posting.get("item_id") or ""
    for i in items:
        if i.get("id") == skip or not i.get("pay"):
            continue
        shared = want & _words(i.get("title", ""))
        if want and len(shared) >= min(2, len(want)) and (i["pay"].get("currency") or "") == cur:
            rng = annual_range(i["pay"])
            if rng:
                sims.append({"title": i.get("title", ""), "company": i.get("company", ""), "min": rng[0], "max": rng[1], "currency": cur})
    similar = None
    if sims:
        similar = {"count": len(sims), "typical_min": round(median(s["min"] for s in sims)), "typical_max": round(median(s["max"] for s in sims)),
                   "lowest": round(min(s["min"] for s in sims)), "highest": round(max(s["max"] for s in sims)), "currency": cur, "examples": sims[:4]}
    t = {k: target.get(k) for k in ("low", "target", "high")}
    verdict, basis, why = "unknown", "", ""
    rng = adv or ((similar["typical_min"], similar["typical_max"]) if similar else None)
    if rng:
        basis = "advertised range" if adv else "similar postings in the hub"
    if t["target"] is None:
        why = "Set your target on the Pay tab of the interview coach to get a verdict."
    elif not t_cur or not cur:
        why = "A currency is missing on your pay sheet or in the posting, so the numbers cannot be compared."
    elif t_cur != cur:
        why = f"The posting is in {cur} but your target is in {t_cur}: not compared."
    elif not rng:
        why = "The posting shows no range that can be read as a yearly figure, and no similar posting in the hub has one."
    else:
        lo, hi = rng
        if hi < t["target"]:
            verdict = "below"
            why = "The top of the range is below your target" + (" and below your lowest." if t["low"] is not None and hi < t["low"] else ".")
        elif lo >= t["target"]:
            verdict = "above"
            why = "The bottom of the range is at or above your target."
        else:
            verdict = "in"
            why = "Your target sits inside the range."
    return {"advertised": ({"min": adv[0], "max": adv[1], "per": "year", "currency": cur, "label": (pay or {}).get("text", "")} if adv else None),
            "advertised_raw": pay, "target": {**t, "currency": t_cur}, "similar": similar, "verdict": verdict, "verdict_basis": basis, "why": why,
            "note": "Local data only: the range in the posting, your own target and postings the hub already holds. Nothing was looked up on the internet."}


# ---- validation ----------------------------------------------------------------------------------------------------------

def _money(v: Any, label: str) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise OfferError(f"{label} must be a number.")
    try:
        f = float(str(v).replace(",", ""))
    except ValueError:
        raise OfferError(f"{label} must be a number.") from None
    if not 0 <= f <= 1e10 or f != f:
        raise OfferError(f"{label} must be from 0 to 10,000,000,000.")
    return f


def _int(v: Any, label: str, hi: int) -> int | None:
    f = _money(v, label)
    if f is None:
        return None
    if f != int(f) or f > hi:
        raise OfferError(f"{label} must be a whole number up to {hi}.")
    return int(f)


def _day(v: Any, label: str) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    if not _DATE.match(s):
        raise OfferError(f"{label} must be a date like 2026-12-01.")
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        raise OfferError(f"{label} is not a real date.") from None
    return s


def check_assumptions(body: dict, base: dict | None = None) -> dict:
    a = {**DEFAULTS, **(base or {})}
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        if k == "regime":
            if v not in ("new", "old"):
                raise OfferError("Tax regime must be new or old.")
            a[k] = v
        elif k in ("pf_capped", "ctc_includes_employer_pf"):
            if not isinstance(v, bool):
                raise OfferError(f"{ASSUMPTION_LABELS[k]} must be true or false.")
            a[k] = v
        elif k == "current_monthly_gross":
            a[k] = _money(v, ASSUMPTION_LABELS[k])
        else:
            n = _money(v, ASSUMPTION_LABELS[k])
            if n is None:
                n = DEFAULTS[k]
            if k.endswith("_pct") and n > 100:
                raise OfferError(f"{ASSUMPTION_LABELS[k]} must be from 0 to 100.")
            a[k] = n
    return a


# ---- the service ---------------------------------------------------------------------------------------------------------

class Offers:
    def __init__(self, ctx: Ctx, *, clock: Callable[[], dt.datetime] | None = None, applyq_fn=None):
        self.ctx = ctx
        self.clock = clock or dt.datetime.now
        self.applyq_fn = applyq_fn
        base = Path(ctx.config.data_path())
        self.file = JsonFile(base / "offers.json")
        self.items_file = JsonFile(base / "oppsources_items.json")
        self.interviews_file = JsonFile(base / "interviews.json")

    def _data(self) -> dict:
        d = self.file.load({})
        d = d if isinstance(d, dict) else {}
        d["offers"] = [o for o in d.get("offers", []) if isinstance(o, dict) and o.get("id")]
        d["assumptions"] = check_assumptions({}, d.get("assumptions") if isinstance(d.get("assumptions"), dict) else {})
        return d

    def _save(self, d: dict) -> None:
        self.file.save(d)

    def assumptions(self) -> dict:
        return self._data()["assumptions"]

    def set_assumptions(self, body: dict) -> dict:
        with _LOCK:
            d = self._data()
            d["assumptions"] = check_assumptions(body, d["assumptions"])
            self._save(d)
            return d["assumptions"]

    # -- offers --
    def _fields(self, body: dict, base: dict | None = None) -> dict:
        o = dict(base or {})
        for k, n in (("company", 120), ("role", 160), ("location", 120), ("work_mode", 30), ("benefits", 500), ("notes", 1000), ("recruiter", 80)):
            if k in body:
                o[k] = clean(str(body.get(k) or ""), n)
        if "currency" in body:
            c = str(body.get("currency") or "").strip().upper()
            if c and not re.match(r"^[A-Z]{3}$", c):
                raise OfferError("Currency is a three-letter code such as INR or USD.")
            o["currency"] = c or "INR"
        for k, label in (("fixed", "Fixed pay"), ("variable", "Variable pay"), ("bonus", "Bonus"), ("joining_bonus", "Joining bonus"), ("benefits_value", "Benefits value"),
                         ("relocation_cost", "Relocation cost"), ("relocation_support", "Relocation support")):
            if k in body:
                o[k] = _money(body.get(k), label)
        if "variable_payout_pct" in body:
            v = _money(body.get("variable_payout_pct"), "Variable payout percent")
            if v is not None and v > 100:
                raise OfferError("Variable payout percent must be from 0 to 100.")
            o["variable_payout_pct"] = v
        for k, label, hi in (("notice_days", "Notice period in days", 365), ("notice_buyout_days", "Notice days to buy out", 365), ("probation_months", "Probation in months", 36)):
            if k in body:
                o[k] = _int(body.get(k), label, hi)
        for k, label in (("start_date", "Start date"), ("reply_by", "Reply-by date")):
            if k in body:
                o[k] = _day(body.get(k), label)
        if "buyout_covered" in body:
            o["buyout_covered"] = body.get("buyout_covered") is True
        if isinstance(body.get("esop"), dict):
            e = body["esop"]
            o["esop"] = {"grant_value": _money(e.get("grant_value"), "ESOP grant value"), "vesting_years": _int(e.get("vesting_years"), "Vesting years", 10),
                         "cliff_months": _int(e.get("cliff_months"), "Cliff months", 60), "note": clean(str(e.get("note") or ""), 200)}
        if isinstance(body.get("clauses"), dict):
            o["clauses"] = {k: v for k, v in body["clauses"].items() if k in CLAUSE_IDS and v in ("yes", "no", "unknown")}
        if "status" in body:
            if body.get("status") not in ("open", "negotiating", "accepted", "declined"):
                raise OfferError("Status must be open, negotiating, accepted or declined.")
            o["status"] = body["status"]
        if not o.get("company") or not o.get("role"):
            raise OfferError("Give the company and the role.")
        o.setdefault("currency", "INR")
        o.setdefault("status", "open")
        o.setdefault("esop", {})
        o.setdefault("clauses", {})
        return o

    def list(self) -> list[dict]:
        return sorted(self._data()["offers"], key=lambda o: o.get("created", ""), reverse=True)

    def get(self, oid: str) -> dict:
        for o in self._data()["offers"]:
            if o["id"] == oid:
                return o
        raise LookupError("No such offer.")

    def add(self, body: dict) -> dict:
        with _LOCK:
            d = self._data()
            if len(d["offers"]) >= MAX_OFFERS:
                raise OfferError("You have recorded the most offers allowed; delete an old one first.")
            o = self._fields(body)
            o.update(id="of-" + secrets.token_hex(4), created=self.clock().isoformat(timespec="seconds"))
            d["offers"].append(o)
            self._save(d)
            return o

    def update(self, oid: str, body: dict) -> dict:
        with _LOCK:
            d = self._data()
            for i, o in enumerate(d["offers"]):
                if o["id"] == oid:
                    d["offers"][i] = {**self._fields(body, o), "id": oid, "created": o.get("created", "")}
                    self._save(d)
                    return d["offers"][i]
        raise LookupError("No such offer.")

    def delete(self, oid: str) -> None:
        with _LOCK:
            d = self._data()
            if not any(o["id"] == oid for o in d["offers"]):
                raise LookupError("No such offer.")
            d["offers"] = [o for o in d["offers"] if o["id"] != oid]
            self._save(d)

    def view(self, o: dict, a: dict) -> dict:
        return {**o, "estimate": estimate(o, a), "totals": totals(o, a), "clause_review": clause_review(o),
                "esop_per_year": esop_per_year(o.get("esop") or {})}

    def overview(self) -> dict:
        d = self._data()
        a = d["assumptions"]
        iv = self.interviews_file.load({})
        neg = iv.get("negotiation", {}) if isinstance(iv, dict) else {}
        return {"offers": [self.view(o, a) for o in sorted(d["offers"], key=lambda o: o.get("created", ""), reverse=True)], "assumptions": a, "defaults": DEFAULTS,
                "labels": ASSUMPTION_LABELS, "clauses": [{"id": c[0], "label": c[1], "risk": c[2], "ask": c[3]} for c in CLAUSES], "pay_target": neg,
                "settings": {"enabled": enabled(self.ctx.config)}, "rules": TAX_RULES_AS_OF, "max_compare": MAX_COMPARE, "sends": False}

    def compare(self, ids: list[str]) -> dict:
        offers = []
        for i in ids:
            offers.append(self.get(str(i)))
        return compare(offers, self.assumptions())

    def counter(self, oid: str, body: dict) -> dict:
        o = self.get(oid)
        ask = {}
        for k, label in (("fixed", "Fixed pay you want"), ("variable", "Variable pay you want"), ("joining_bonus", "Joining bonus you want")):
            if k in (body.get("ask") or {}):
                v = _money(body["ask"][k], label)
                if v is not None:
                    ask[k] = v
        a2 = body.get("ask") or {}
        if a2.get("notice_days") not in (None, ""):
            ask["notice_days"] = _int(a2["notice_days"], "Notice days", 365)
        if a2.get("start_date"):
            ask["start_date"] = _day(a2["start_date"], "Start date")
        if a2.get("other"):
            ask["other"] = clean(str(a2["other"]), 200)
        comp = []
        for cid in (body.get("use_competing") or [])[:3]:
            if cid != oid:
                comp.append(self.get(str(cid)))
        reasons = [str(r) for r in (body.get("reasons") or [])[:4] if isinstance(r, str)]
        channel = "call" if body.get("channel") == "call" else "email"
        return {"offer_id": oid, **counter_draft(o, ask, comp, reasons, name_competitor=body.get("name_competitor") is True,
                                                 recruiter=clean(str(body.get("recruiter") or o.get("recruiter") or ""), 80), channel=channel,
                                                 owner_name=clean(str(body.get("owner_name") or ""), 80))}

    # -- pay check --
    def _items(self) -> list[dict]:
        d = self.items_file.load({})
        return [i for i in (d.values() if isinstance(d, dict) else []) if isinstance(i, dict)]

    def posting_from(self, body: dict) -> dict:
        if body.get("item_id"):
            it = next((i for i in self._items() if i.get("id") == body["item_id"]), None)
            if not it:
                raise LookupError("No such posting in the hub.")
            return {"title": it.get("title", ""), "company": it.get("company", ""), "description": it.get("description", ""), "pay": it.get("pay"), "item_id": it["id"]}
        if body.get("application_id"):
            from . import applyq
            it = (self.applyq_fn() if self.applyq_fn else applyq.Applyq(self.ctx)).get(str(body["application_id"]))
            return {"title": it.get("title", ""), "company": it.get("company", ""), "description": it.get("description", "")}
        if not any(isinstance(body.get(k), str) and body[k].strip() for k in ("title", "description", "pay_text")):
            raise OfferError("Pick a posting, or type the title and paste the description or the pay line.")
        return {k: clean(str(body.get(k) or ""), 20000 if k == "description" else 300) for k in ("title", "company", "description", "pay_text")}

    def pay_check(self, body: dict) -> dict:
        posting = self.posting_from(body)
        d = self.interviews_file.load({})
        neg = d.get("negotiation", {}) if isinstance(d, dict) else {}
        res = pay_check(posting, neg if isinstance(neg, dict) else {}, self._items())
        res["posting"] = {"title": posting.get("title", ""), "company": posting.get("company", "")}
        return res


# ---- routes ----------------------------------------------------------------------------------------------------------------

def make_router(ctx: Ctx, *, clock=None, applyq_fn=None, **_: Any) -> APIRouter:
    router = APIRouter(prefix="/api/offers", dependencies=[Depends(ctx.need_auth)])
    svc = Offers(ctx, clock=clock, applyq_fn=applyq_fn)
    router.service = svc                                      # type: ignore[attr-defined]

    def on() -> None:
        if not enabled(ctx.config):
            raise HTTPException(404, "Offers are switched off (offers_enabled is false).")

    def guard(fn):
        try:
            return fn()
        except OfferError as exc:
            raise HTTPException(400, str(exc)) from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @router.get("")
    async def overview():
        on()
        return await asyncio.to_thread(lambda: guard(svc.overview))

    @router.post("")
    async def add(body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.view(svc.add(body), svc.assumptions()))

    @router.put("/assumptions")
    async def set_assumptions(body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.set_assumptions(body))

    @router.post("/compare")
    async def cmp(body: dict = Body(default_factory=dict)):
        on()
        ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        return guard(lambda: svc.compare(ids))

    @router.post("/pay-check")
    async def pay(body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.pay_check(body))

    @router.put("/{oid}")
    async def put(oid: str, body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.view(svc.update(oid, body), svc.assumptions()))

    @router.delete("/{oid}")
    async def drop(oid: str):
        on()
        guard(lambda: svc.delete(oid))
        return {"ok": True}

    @router.post("/{oid}/counter")
    async def counter(oid: str, body: dict = Body(default_factory=dict)):
        on()
        return guard(lambda: svc.counter(oid, body))

    return router


# ---- assistant tool --------------------------------------------------------------------------------------------------------

def build(get_app: Callable[[], Any], config) -> list[ExtraTool]:
    hub = InProcessClient(get_app, config)

    async def offer_compare(a):
        ids = [i for i in (a.get("ids") or []) if isinstance(i, str)][:MAX_COMPARE]
        if not ids:
            ok, d = await hub.call("GET", "/api/offers")
            if not ok:
                return False, d
            ids = [o["id"] for o in d["offers"][:MAX_COMPARE]]
        if len(ids) < 2:
            return False, "Record at least two offers (Apps > Offers) to compare them."
        ok, d = await hub.call("POST", "/api/offers/compare", body={"ids": ids})
        if not ok:
            return False, d
        head = " | ".join(f"{o['company']} ({o['role']})" for o in d["offers"])
        lines = [f"Offers compared: {head}"]
        for r in d["rows"]:
            vals = [("-" if v in (None, "") else (f"{v:,}" if isinstance(v, (int, float)) else str(v))) for v in r["values"]]
            if any(v != "-" for v in vals):
                lines.append(f"- {r['label']}: " + " | ".join(vals))
        lines += d["notes"]
        lines.append("Tax rules: " + d["rules"] + " Estimates from the owner's assumptions. Nothing was sent.")
        return True, "\n".join(lines)

    spec = {"name": "offer_compare",
            "description": "Compare the job offers the owner recorded (2 to 4, default the newest): fixed, expected variable, joining bonus, ESOP, in-hand per month "
                           "ESTIMATE for India (stated assumptions), notice, location and clauses to review. Read-only: it never sends or drafts to anyone.",
            "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}, "required": [], "additionalProperties": False}}
    return mark_tools([ExtraTool(spec, offer_compare)])
