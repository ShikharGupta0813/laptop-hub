"""Scam and quality check for job / freelance / task finds. Pure functions: no network, no clock unless passed in.

`vet(...)` returns {"flags": [{"code", "label", "severity", "detail"}], "severity": "none"|"low"|"medium"|"high"}.
A `high` find is never sent to the apply queue; the Opportunities screen lists it under Blocked with the reasons.
The checks are plain patterns, so they miss clever scams and can flag odd-but-honest posts: the owner always has the
last word (the Blocked list can be read), and nothing here ever contacts the poster.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

SEVERITIES = ("none", "low", "medium", "high")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.in",
             "ymail.com", "aol.com", "proton.me", "protonmail.com", "icloud.com", "mail.com", "gmx.com", "zoho.com",
             "rediffmail.com", "yandex.com"}
BIG_BRANDS = {"google", "amazon", "microsoft", "meta", "apple", "netflix", "tesla", "deloitte", "accenture", "tcs",
              "infosys", "wipro", "ibm", "oracle", "adobe", "salesforce", "nvidia", "openai", "anthropic", "uber",
              "paypal", "stripe", "walmart", "cognizant", "capgemini", "flipkart", "swiggy", "zomato"}
CHEAP_TLDS = {"xyz", "top", "click", "work", "buzz", "icu", "tk", "ml", "ga", "cf", "gq", "cyou", "rest", "monster",
              "loan", "men", "vip"}
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "cutt.ly", "rb.gy", "shorturl.at", "ow.ly"}
JOB_PLATFORMS = {"linkedin.com", "indeed.com", "glassdoor.com", "upwork.com", "freelancer.com", "toptal.com",
                 "fiverr.com", "weworkremotely.com", "remotive.com", "remoteok.com", "himalayas.app",
                 "workingnomads.com", "news.ycombinator.com", "greenhouse.io", "lever.co", "ashbyhq.com",
                 "workable.com", "smartrecruiters.com", "wellfound.com", "contra.com", "peopleperhour.com",
                 "mercor.com", "turing.com", "outlier.ai", "dataannotation.tech", "alignerr.com", "naukri.com"}

_NEG = re.compile(r"(?:\bno|\bnot|\bnever|\bwithout|\bzero|\bfree of|\bdon'?t|\bdoesn'?t|\bwon'?t|\bafter|\bonce you)"
                  r"(?:\s+[\w'-]+){0,8}\s*$", re.I)


def _flag(code: str, label: str, severity: str, detail: str = "") -> dict:
    return {"code": code, "label": label, "severity": severity, "detail": detail[:160]}


def _search(patterns: list[re.Pattern], text: str, negatable: bool = True) -> str:
    """The first matching phrase that is not negated ('no fees', 'we never ask you to pay'), or ''."""
    for pattern in patterns:
        for m in pattern.finditer(text):
            before = text[max(0, m.start() - 40):m.start()]
            if negatable and _NEG.search(before):
                continue
            return " ".join(m.group(0).split())
    return ""


def _c(*raw: str) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in raw]


UPFRONT_FEE = _c(
    r"\b(?:registration|application|processing|training|onboarding|admin|activation|starter|background[- ]check|verification) (?:fee|charge|cost)\b",
    r"\bpay (?:a|an|the|your)? ?(?:small |one[- ]time |refundable )?(?:fee|deposit)\b",
    r"\b(?:upfront|up-front|advance) (?:fee|payment|deposit|cost)s?\b",
    r"\b(?:refundable|security) deposit\b",
    r"\b(?:buy|purchase|order) (?:your own |the |our |a )?(?:equipment|laptop|software|starter kit|kit|license|licence|gear|tools?)\b",
    r"\bsend (?:us )?(?:money|funds|payment)\b",
    r"\bwe will (?:send|ship) you a (?:cheque|check)\b",
)
CRYPTO_WIRE = _c(
    r"\bwire transfer\b", r"\bwestern union\b", r"\bmoneygram\b", r"\bgift ?cards?\b", r"\bzelle\b",
    r"\b(?:cash|deposit) (?:the|a|this) (?:cheque|check)\b", r"\bsend (?:us )?(?:bitcoin|btc|usdt|crypto)\b",
    r"\bbuy (?:bitcoin|btc|usdt|crypto)\b",
)
CRYPTO_PAY = _c(r"\bpaid (?:only )?in (?:crypto(?:currency)?|bitcoin|btc|usdt)\b",
                r"\bpayment (?:is |will be )?(?:made )?(?:only )?in (?:crypto(?:currency)?|bitcoin|btc|usdt)\b")
OFF_PLATFORM = _c(
    r"\b(?:contact|message|text|reach|ping|dm|add|chat with) (?:us|me|hr|our (?:hr|recruiter|manager)|the (?:hr|recruiter|manager))?[^.\n]{0,30}\b(?:on|via|through) (?:whatsapp|telegram|signal)\b",
    r"\b(?:whatsapp|telegram) only\b", r"\bonly (?:on |via |through )?(?:whatsapp|telegram)\b",
    r"\bwa\.me/", r"\bt\.me/", r"\bwhatsapp (?:number|no\.?)\b", r"\btelegram (?:id|handle|channel)\b",
)
GUARANTEED = _c(
    r"\bguaranteed (?:income|salary|earnings?|pay|profit|returns?|job|placement)\b",
    r"\b100% guaranteed\b", r"\bget rich\b", r"\bpassive income\b", r"\bmake money fast\b", r"\beasy money\b",
    r"\bearn (?:\$|usd |rs\.? ?|inr |₹)?\d[\d,]*(?:\.\d+)?k?\+? ?(?:per|a|/|every) ?(?:day|week)\b[^.\n]{0,40}\bno experience\b",
    r"\bno experience (?:needed|required|necessary)[^.\n]{0,60}\bearn\b",
)
URGENCY = _c(r"\bact now\b", r"\bapply (?:immediately|today only|now or)\b", r"\blimited (?:spots|slots|positions)\b",
             r"\burgent(?:ly)? (?:hiring|need|required)\b", r"\bonly \d+ (?:spots?|slots?|positions?) left\b",
             r"\b(?:respond|reply) within (?:24|12|2|1) ?(?:hours?|hrs?)\b", r"\boffer expires\b", r"\bdon'?t miss (?:out|this)\b",
             r"\bhurry\b", r"\blast chance\b")
EARLY_ID = _c(
    r"\b(?:send|share|provide|submit|email|upload) (?:us |me )?(?:a |your |a copy of |copies of |scans? of )*(?:your )?(?:passport|driver'?s? licen[sc]e|national id|aadhaar|aadhar|pan card|ssn|social security(?: number)?|bank (?:account|details|statement|login)|credit card|debit card|card (?:number|details)|id (?:card|proof|copy))\b",
    r"\bbank (?:account )?(?:details|number|login|password)\b[^.\n]{0,30}\b(?:to (?:apply|start|register)|before|upfront|now)\b",
    r"\b(?:ssn|social security number) (?:is )?required\b",
)
NEW_COMPANY = _c(r"\b(?:recently|newly) (?:founded|established|launched|started|incorporated|formed)\b",
                 r"\bstealth (?:startup|mode|company)\b", r"\bjust (?:launched|started|founded)\b")
NO_EXPERIENCE = re.compile(r"\bno (?:prior |previous )?(?:experience|skills?|qualifications?|degree) (?:needed|required|necessary)\b|\banyone can\b", re.I)
PAY = re.compile(r"(?:\$|usd ?|€|£|₹|rs\.? ?|inr ?)\s?(\d[\d,]*(?:\.\d+)?)\s?(k)?\s?(?:usd)?\s?(?:/|per|an?|each|every|\bp\.?)\s?(hour|hr|day|week|wk|month|mo)\b", re.I)
# Per-period amounts that are very unusual for honest postings; rupee amounts are scaled down by 80.
LIMITS = {"hour": 250.0, "day": 1500.0, "week": 7500.0, "month": 40000.0}


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def email_domains(text: str) -> list[str]:
    return [d.lower() for d in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text or "")]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def pay_amounts(text: str) -> list[tuple[float, str]]:
    """[(amount in dollars per period, period)] for each 'X per hour/day/week/month' found."""
    out = []
    for m in PAY.finditer(text or ""):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if m.group(2):
            value *= 1000
        symbol = m.group(0).strip().lower()
        if symbol.startswith(("₹", "rs", "inr")):
            value /= 80.0
        period = {"hr": "hour", "wk": "week", "mo": "month"}.get(m.group(3).lower(), m.group(3).lower())
        out.append((value, period))
    return out


def vet(title: str = "", company: str = "", url: str = "", description: str = "", contact: str = "",
        sender: str = "", now: dt.date | None = None) -> dict:
    """Check one find. `contact` / `sender` are an email address or free text holding one (the poster's, never the owner's)."""
    now = now or dt.date.today()
    text = " ".join(x for x in (title, company, description) if x)
    flags: list[dict] = []

    hit = _search(UPFRONT_FEE, text)
    if hit:
        flags.append(_flag("upfront_fee", "Asks you to pay or buy something first", "high", hit))
    hit = _search(CRYPTO_WIRE, text)
    if hit:
        flags.append(_flag("wire_or_gift", "Wire transfer, gift cards or cheque cashing", "high", hit))
    hit = _search(CRYPTO_PAY, text)
    if hit:
        flags.append(_flag("crypto_pay", "Paid only in crypto", "medium", hit))
    hit = _search(OFF_PLATFORM, text, negatable=False)
    if hit:
        flags.append(_flag("off_platform", "Moves you to WhatsApp or Telegram", "high", hit))
    hit = _search(GUARANTEED, text)
    if hit:
        flags.append(_flag("guaranteed_income", "Promises guaranteed income", "high", hit))
    hit = _search(EARLY_ID, text)
    if hit:
        flags.append(_flag("early_id", "Wants ID or bank details early", "high", hit))

    # Free-mail contact pretending to be a company.
    domains = email_domains(" ".join((contact, sender, description)))
    free = [d for d in domains if d in FREE_MAIL]
    if free:
        brandish = BIG_BRANDS & set(re.findall(r"[a-z]+", text.lower()))
        recruiter = bool(re.search(r"\b(?:recruit\w*|hiring manager|hr)\b", text, re.I))
        if brandish and (recruiter or company):
            flags.append(_flag("free_mail_brand", "Big-company name but a free email address", "high", f"{sorted(brandish)[0]} / {free[0]}"))
        elif company or recruiter:
            flags.append(_flag("free_mail", "Recruiter uses a free email address", "medium", free[0]))

    # Mismatched domain: the contact or apply link does not belong to the named company.
    host = host_of(url)
    company_slug = _slug((company or "").split()[0]) if company else ""
    for d in [d for d in domains if d not in FREE_MAIL]:
        if company_slug and len(company_slug) >= 4 and company_slug not in _slug(d) and d not in JOB_PLATFORMS:
            flags.append(_flag("domain_mismatch", "Email domain does not match the company", "medium", f"{company} / {d}"))
            break
    if host and company_slug and len(company_slug) >= 4 and host.split(".")[-1] in CHEAP_TLDS and company_slug not in _slug(host):
        flags.append(_flag("domain_mismatch", "Apply link is on an unrelated cheap domain", "medium", host))
    elif host and host.split(".")[-1] in CHEAP_TLDS:
        flags.append(_flag("cheap_tld", "Apply link is on a cheap, often abused domain", "low", host))
    if host in SHORTENERS:
        flags.append(_flag("short_link", "Shortened link hides the real site", "low", host))

    # Pay that is too good to be true.
    over = [(v, p) for v, p in pay_amounts(text) if v > LIMITS.get(p, 1e12)]
    if over:
        value, period = over[0]
        severity = "high" if NO_EXPERIENCE.search(text) else "medium"
        flags.append(_flag("unrealistic_pay", "Pay looks unrealistic", severity, f"${value:,.0f} per {period}"))

    hits = [h for p in URGENCY if (h := _search([p], text, negatable=False))]
    if hits:
        flags.append(_flag("urgency", "Pressure to act fast", "medium" if len(hits) >= 2 else "low", "; ".join(hits[:3])))

    hit = _search(NEW_COMPANY, text, negatable=False)
    founded = re.search(r"\b(?:founded|established|incorporated) (?:in )?(20\d\d)\b", text, re.I)
    if founded and int(founded.group(1)) >= now.year:
        hit = hit or f"founded {founded.group(1)}"
    if hit:
        flags.append(_flag("new_company", "Very new, little-known company", "low", hit))

    worst = "none"
    for f in flags:
        if _RANK[f["severity"]] > _RANK[worst]:
            worst = f["severity"]
    return {"flags": flags, "severity": worst}


def blocked(result: dict) -> bool:
    return result.get("severity") == "high"


def reason_text(result: dict) -> str:
    return "; ".join(f["label"] for f in result.get("flags", []) if f["severity"] == "high") or \
           "; ".join(f["label"] for f in result.get("flags", []))
