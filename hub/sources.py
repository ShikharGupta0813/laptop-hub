"""INBOUND SOURCES: one place that lists everything REDLINE listens to, each kind a LIST the owner can add to, edit, switch off
or delete (Settings > Alerts). See docs/api/sources.md.

Kinds (the {kind} in the routes):
  app       notification apps the phone capture listens to, by package + label (WhatsApp, Slack, Teams, Gmail, SMS...)
  task      task sources: Asana, Jira, Trello, Linear, GitHub, RSS/Atom, inbound webhooks     (hub/tasksources.py)
  webhook   the same list, only the inbound webhooks (adding one here always makes a webhook)
  mail      mail accounts: Gmail OAuth / app password and generic IMAP/SMTP                      (hub/gmailx.py)
  calendar  calendar feeds: Google and iCal URLs                                                 (hub/calendars.py)

Routes (login needed): GET /api/sources ; GET|POST /api/sources/{kind} ; PUT|DELETE /api/sources/{kind}/{id} ;
POST /api/sources/{kind}/{id}/test ; POST /api/sources/{task|webhook}/{id}/poll.

`alerts_sources` in the config stays: it is the list of match words hub.alerts.is_watched checks, and it is now KEPT IN STEP with
the enabled `app` entries (plus `email` while a mail account takes part in alerts and `asana` while the Asana poller is on),
so the older code path and the Settings screen always agree. The phone capture app reads its package list from
`GET /api/alerts/capture-config` (see `capture_config`).

The app list is stored in `sources.json` in the data folder. It starts from the ready-made list below; the first time it is
loaded, words already in `alerts_sources` that no ready-made entry covers become custom entries, so nothing is lost.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .common import Ctx, JsonFile

log = logging.getLogger("hub.sources")

MAX_APPS = 80
LABEL_MAX = 60
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+){1,8}$")

# id, label, Android package, match word (what hub.alerts.is_watched looks for)
DEFAULT_APPS: list[tuple[str, str, str, str]] = [
    ("whatsapp", "WhatsApp", "com.whatsapp", "whatsapp"),
    ("slack", "Slack", "com.Slack", "slack"),
    ("teams", "Microsoft Teams", "com.microsoft.teams", "teams"),
    ("gmail", "Gmail", "com.google.android.gm", "gmail"),
    ("gchat", "Google Chat", "com.google.android.apps.dynamite", "gchat"),
    ("asana", "Asana", "com.asana.app", "asana"),
    ("jira", "Jira", "com.atlassian.android.jira.core", "jira"),
    ("trello", "Trello", "com.trello", "trello"),
    ("telegram", "Telegram", "org.telegram.messenger", "telegram"),
    ("outlook", "Outlook", "com.microsoft.office.outlook", "outlook"),
    ("sms-google", "SMS (Google Messages)", "com.google.android.apps.messaging", "sms"),
    ("sms-android", "SMS (Android Messages)", "com.android.mms", "sms"),
    ("sms-samsung", "SMS (Samsung Messages)", "com.samsung.android.messaging", "sms"),
]
DEFAULT_MATCHES = {m for *_x, m in DEFAULT_APPS}


class SourceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _file(ctx: Ctx) -> JsonFile:
    return JsonFile(ctx.config.data_path() / "sources.json")


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


# ---- the app list ------------------------------------------------------------------------------------------

def _seed(ctx: Ctx) -> list[dict]:
    from .alerts import effective_sources
    watched = [str(x).strip().lower() for x in effective_sources(ctx.config) if str(x).strip()]
    from .alerts import DEFAULT_SOURCES
    customised = {w for w in watched} != {w.lower() for w in DEFAULT_SOURCES}      # untouched config: new ready-made apps start on
    apps = [{"id": i, "kind": "app", "label": label, "package": pkg, "match": match,
             "enabled": any(match in w or w in match for w in watched) or (not customised and match not in DEFAULT_SOURCES), "builtin": True}
            for i, label, pkg, match in DEFAULT_APPS]
    taken = {a["id"] for a in apps}
    for w in watched:
        if w in DEFAULT_MATCHES or w in ("email", "mail", "test") or any(w in m or m in w for m in DEFAULT_MATCHES):
            continue
        iid = re.sub(r"[^a-z0-9]+", "-", w).strip("-")[:30] or "app"
        while iid in taken:
            iid += "-x"
        taken.add(iid)
        apps.append({"id": iid, "kind": "app", "label": w[:LABEL_MAX], "package": w if PACKAGE_RE.match(w) else "", "match": w[:80],
                     "enabled": True, "builtin": False})
    return apps


def load_apps(ctx: Ctx) -> list[dict]:
    data = _file(ctx).load({})
    apps = data.get("apps") if isinstance(data, dict) else None
    if isinstance(apps, list) and apps:
        return [a for a in apps if isinstance(a, dict) and a.get("id")]
    return _seed(ctx)


def save_apps(ctx: Ctx, apps: list[dict]) -> None:
    _file(ctx).save({"apps": apps})


def watch_tokens(ctx: Ctx, apps: list[dict] | None = None) -> list[str]:
    apps = load_apps(ctx) if apps is None else apps
    tokens: list[str] = []
    for a in apps:
        if a.get("enabled") is not False and a.get("match"):
            t = str(a["match"]).strip()
            if t and t not in tokens:
                tokens.append(t)
    try:
        from . import gmailx
        if any(v.get("alerts") is not False for v in gmailx.list_accounts(ctx)) and "email" not in tokens:
            tokens.append("email")
    except Exception:  # noqa: BLE001 - mail is optional
        pass
    try:
        if getattr(ctx.config, "asana_enabled", False) is True and "asana" not in tokens:
            tokens.append("asana")
    except Exception:  # noqa: BLE001
        pass
    return tokens[:40]


def sync_watch(ctx: Ctx) -> list[str]:
    """Write the enabled app entries into `alerts_sources` (kept for hub.alerts.is_watched). Only once the owner has an app list."""
    if not _file(ctx).path.exists():
        return list(getattr(ctx.config, "alerts_sources", None) or [])
    from .alerts import persist_config
    tokens = watch_tokens(ctx)
    if list(getattr(ctx.config, "alerts_sources", None) or []) != tokens:
        persist_config(ctx, alerts_sources=tokens)
    return tokens


def capture_config(ctx: Ctx) -> dict:
    """What the phone capture app listens to: the enabled apps that have a package name."""
    pk, seen = [], set()
    for a in load_apps(ctx):
        pkg = str(a.get("package") or "").strip()
        if a.get("enabled") is not False and pkg and pkg not in seen:
            seen.add(pkg)
            pk.append({"package": pkg, "label": str(a.get("label") or pkg)})
    return {"version": 1, "packages": pk}


def app_view(a: dict) -> dict:
    return {"id": a["id"], "kind": "app", "label": a.get("label") or a["id"], "package": a.get("package") or "",
            "enabled": a.get("enabled") is not False, "builtin": a.get("builtin") is True, "match": a.get("match") or ""}


def app_list(ctx: Ctx) -> dict:
    return {"items": [app_view(a) for a in load_apps(ctx)]}


def app_add(ctx: Ctx, body: Any) -> dict:
    if not isinstance(body, dict):
        raise SourceError("Send the app as a JSON object.")
    label = _clean(body.get("label"), LABEL_MAX)
    package = str(body.get("package") or "").strip()
    if not label:
        raise SourceError("Give the app a name.")
    if package and not PACKAGE_RE.match(package):
        raise SourceError("A package name looks like com.example.app.")
    apps = load_apps(ctx)
    if len(apps) >= MAX_APPS:
        raise SourceError(f"At most {MAX_APPS} apps.")
    if package and any(a.get("package") == package for a in apps):
        raise SourceError("That package is already in the list.", 409)
    taken = {a["id"] for a in apps}
    iid = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30] or "app"
    n = 1
    base = iid
    while iid in taken:
        n += 1
        iid = f"{base}-{n}"
    item = {"id": iid, "kind": "app", "label": label, "package": package, "match": package or label.lower()[:80],
            "enabled": body.get("enabled") is not False, "builtin": False}
    apps.append(item)
    save_apps(ctx, apps)
    return app_view(item)


def app_update(ctx: Ctx, item_id: str, body: Any) -> dict:
    if not isinstance(body, dict):
        raise SourceError("Send the changes as a JSON object.")
    apps = load_apps(ctx)
    item = next((a for a in apps if a["id"] == item_id), None)
    if item is None:
        raise SourceError("There is no app with that id.", 404)
    if "label" in body:
        label = _clean(body["label"], LABEL_MAX)
        if not label:
            raise SourceError("Give the app a name.")
        item["label"] = label
    if "package" in body:
        package = str(body["package"] or "").strip()
        if package and not PACKAGE_RE.match(package):
            raise SourceError("A package name looks like com.example.app.")
        item["package"] = package
        if item.get("builtin") is not True:
            item["match"] = package or str(item.get("label") or "").lower()[:80]
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise SourceError("enabled is true or false.")
        item["enabled"] = body["enabled"]
    save_apps(ctx, apps)
    return app_view(item)


def app_delete(ctx: Ctx, item_id: str) -> None:
    apps = load_apps(ctx)
    if not any(a["id"] == item_id for a in apps):
        raise SourceError("There is no app with that id.", 404)
    save_apps(ctx, [a for a in apps if a["id"] != item_id])


def app_test(ctx: Ctx, item_id: str) -> dict:
    a = next((x for x in load_apps(ctx) if x["id"] == item_id), None)
    if a is None:
        raise SourceError("There is no app with that id.", 404)
    pk = capture_config(ctx)["packages"]
    on = any(p["package"] == a.get("package") for p in pk)
    return {"ok": True, "message": ("The phone app will listen to this app after it next refreshes its list. "
                                    if on else "This app is switched off, so the phone app ignores it. ")
            + "Open the phone app once to refresh."}


# ---- the other kinds, behind one small interface ----------------------------------------------------------------

def _tasks():
    from . import tasksources
    return tasksources


def _cals():
    from . import calendars
    return calendars


def _mail_service(ctx: Ctx):
    from . import gmailx
    return gmailx.get_service(ctx)


def mail_list(ctx: Ctx) -> dict:
    from . import mailaccounts
    svc = _mail_service(ctx)
    return {"items": [{**v, "kind_label": {"legacy": "Gmail (app password)", "oauth": "Gmail (Google sign-in)",
                                            "imap": "Mail server (IMAP/SMTP)"}.get(v["kind"], v["kind"])} for v in svc.list_accounts()],
            "presets": mailaccounts.PRESETS, "client_configured": svc.client_configured()}


def mail_add(ctx: Ctx, body: Any) -> dict:
    if not isinstance(body, dict):
        raise SourceError("Send the account as a JSON object.")
    if str(body.get("type") or "imap") != "imap":
        raise SourceError("Gmail sign-in is started from Settings > Mail accounts > Connect (POST /api/gmail/accounts/connect). "
                          "Here, add a mail-server account (type imap).")
    server = {k: body[k] for k in ("preset", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security",
                                   "username") if body.get(k) is not None}
    return _mail_service(ctx).add_imap(body.get("label"), body.get("address"), body.get("password"), tags=body.get("tags"),
                                       send_enabled=body.get("send_enabled") is True,
                                       cloud_ai_allowed=body.get("cloud_ai_allowed") if isinstance(body.get("cloud_ai_allowed"), bool) else None,
                                       **server)


def mail_update(ctx: Ctx, item_id: str, body: Any) -> dict:
    if not isinstance(body, dict):
        raise SourceError("Send the changes as a JSON object.")
    extra: dict = {}
    if "tags" in body:
        extra["tags"] = body["tags"]
    if "cloud_ai_allowed" in body:
        extra["cloud_ai_allowed"] = body["cloud_ai_allowed"]
    server = {k: body[k] for k in ("imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "username")
              if body.get(k) is not None}
    return _mail_service(ctx).update(item_id, body.get("label"), body.get("send_enabled") if isinstance(body.get("send_enabled"), bool) else None,
                                     imap=server or None, password=body.get("password") or None,
                                     alerts=body.get("alerts") if isinstance(body.get("alerts"), bool) else None, **extra)


def mail_delete(ctx: Ctx, item_id: str) -> None:
    _mail_service(ctx).disconnect(item_id)


def mail_test(ctx: Ctx, item_id: str) -> dict:
    svc = _mail_service(ctx)
    found = svc.search(item_id, "", 1)
    return {"ok": True, "message": "Connected." + (" The newest message was read." if found else " The inbox is empty.")}


def _only_webhooks(data: dict) -> dict:
    return {**data, "items": [i for i in data["items"] if i.get("kind") == "webhook"]}


def _task_add(ctx: Ctx, body: Any) -> dict:
    return _tasks().kind_add(ctx, body)


def _webhook_add(ctx: Ctx, body: Any) -> dict:
    if not isinstance(body, dict):
        raise SourceError("Send the webhook as a JSON object.")
    return _tasks().kind_add(ctx, {**body, "kind": "webhook"})


def kinds() -> dict[str, dict]:
    """kind -> {list, add, update, delete, test, errors}. Built lazily so an import problem in one module hides only that kind."""
    t, c = _tasks(), _cals()
    return {
        "app": {"list": app_list, "add": app_add, "update": app_update, "delete": app_delete, "test": app_test, "errors": (SourceError,)},
        "task": {"list": t.kind_list, "add": _task_add, "update": t.kind_update, "delete": t.kind_delete, "test": t.kind_test,
                 "errors": (t.SourceProblem,)},
        "webhook": {"list": lambda ctx: _only_webhooks(t.kind_list(ctx)), "add": _webhook_add, "update": t.kind_update,
                    "delete": t.kind_delete, "test": t.kind_test, "errors": (t.SourceProblem,)},
        "mail": {"list": mail_list, "add": mail_add, "update": mail_update, "delete": mail_delete, "test": mail_test,
                 "errors": (_gmailx_error(), SourceError)},
        "calendar": {"list": c.kind_list, "add": c.kind_add, "update": c.kind_update, "delete": c.kind_delete, "test": c.kind_test,
                     "errors": (c.CalendarProblem,)},
    }


def _gmailx_error():
    from . import gmailx
    return gmailx.GmailxError


def overview(ctx: Ctx) -> dict:
    out: dict[str, Any] = {}
    for name, k in kinds().items():
        if name == "webhook":
            continue                                      # shown inside "task"
        try:
            out[name] = k["list"](ctx)
        except k["errors"] as exc:
            out[name] = {"items": [], "error": str(exc)}
        except Exception:  # noqa: BLE001 - one list failing must not hide the others
            log.info("source list %s failed", name)
            out[name] = {"items": [], "error": "This list could not be read."}
    return out


def make_router(ctx: Ctx) -> APIRouter:
    router = APIRouter(prefix="/api/sources", dependencies=[Depends(ctx.need_auth)])

    def kind_of(kind: str) -> dict:
        k = kinds().get(kind)
        if k is None:
            raise HTTPException(404, "Kinds are: " + ", ".join(kinds()) + ".")
        return k

    def run(kind: str, op: str, *args):
        k = kind_of(kind)
        try:
            result = k[op](ctx, *args)
        except k["errors"] as exc:
            raise HTTPException(getattr(exc, "status", 400), str(exc)) from None
        except ValueError as exc:                    # e.g. an IMAP settings problem
            raise HTTPException(400, str(exc)) from None
        if op != "list" and op != "test":
            try:
                sync_watch(ctx)
            except Exception:  # noqa: BLE001
                log.info("sources: alerts_sources could not be updated")
        return result

    @router.get("")
    def everything():
        return overview(ctx)

    @router.get("/{kind}")
    def listing(kind: str):
        return run(kind, "list")

    @router.post("/{kind}")
    def add(kind: str, body: Any = Body(default=None)):
        return run(kind, "add", body)

    @router.put("/{kind}/{item_id}")
    def update(kind: str, item_id: str, body: Any = Body(default=None)):
        return run(kind, "update", item_id[:60], body)

    @router.delete("/{kind}/{item_id}")
    def delete(kind: str, item_id: str):
        run(kind, "delete", item_id[:60])
        return {"ok": True}

    @router.post("/{kind}/{item_id}/test")
    def test(kind: str, item_id: str):
        return run(kind, "test", item_id[:60])

    @router.post("/{kind}/{item_id}/poll")
    def poll(kind: str, item_id: str):
        if kind not in ("task", "webhook"):
            raise HTTPException(404, "Only task sources are polled.")
        t = _tasks()
        try:
            return t.get(ctx).poll_item(item_id[:60])
        except t.SourceProblem as exc:
            raise HTTPException(exc.status if exc.status in (400, 404, 409, 429) else 502, str(exc)) from None

    return router
