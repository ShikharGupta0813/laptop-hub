"""Sample data for HUB_WORK_DEMO=1: UI development and tests. Nothing here touches Asana,
Gmail, Google Chat or any calendar; "writes" only change this in-memory copy."""
from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Callable

from .asana import AsanaError, check_gid

DEMO_ADDRESS = "you@example.com"


def _notes(text: str) -> str:
    return text.strip()


class DemoAsana:
    def __init__(self, today: Callable[[], dt.date] = dt.date.today):
        self._today = today
        t = today()

        def due(offset: int | None) -> str | None:
            return None if offset is None else (t + dt.timedelta(days=offset)).isoformat()

        def task(gid, name, offset, project, notes, subtasks=(), comments=()):
            return {"gid": gid, "name": name, "due_on": due(offset), "project": project,
                    "completed": False, "permalink_url": f"https://app.asana.com/0/0/{gid}",
                    "notes": _notes(notes),
                    "subtasks": [{"gid": f"{gid}{i}", "name": n, "completed": done}
                                 for i, (n, done) in enumerate(subtasks, 1)],
                    "comments": [{"author": a, "text": x, "created_at": c} for a, x, c in comments]}

        stamp = f"{t.isoformat()}T08:30:00.000Z"
        self.tasks = [
            task("1001", "Send the Q3 vendor invoice reminder", -3, "Finance ops",
                 "Remind the three vendors whose invoices are still open.\nUse the standard template.",
                 [("Draft the reminder", True), ("Send to vendors", False)],
                 [("Priya Nair", "Two of three have replied already.", stamp)]),
            task("1002", "Fix the login redirect bug", -1, "Web app",
                 "Users land on a blank page after the SSO callback on Safari.",
                 [("Reproduce on Safari", True), ("Patch the callback", False), ("Add a regression test", False)],
                 [("Rahul Sharma", "I can reproduce it on iOS 17 only.", stamp),
                  ("You", "Looking at the cookie flags now.", stamp)]),
            task("1003", "Prepare the demo for Thursday", 0, "Launch",
                 "Walk through signup, the dashboard and the export. Keep it under 15 minutes.",
                 [("Write the script", False), ("Rehearse once", False)]),
            task("1004", "Review Priya's pull request", 0, "Web app",
                 "PR adds rate limiting to the public API. Check the limits and the tests."),
            task("1005", "Plan next sprint", 2, "Team",
                 "Pick the top items from the backlog and agree the sprint goal.",
                 [("Collect requests", False)],
                 [("Rahul Sharma", "Please add the export bug to the list.", stamp)]),
            task("1006", "Renew the domain certificate", 7, "Infrastructure",
                 "Certificate expires next week. Renew it and check the staging site afterwards."),
        ]

    def me(self) -> dict:
        return {"gid": "1", "name": "Demo User", "workspace_gid": "9"}

    def _find(self, gid: str) -> dict:
        check_gid(gid)
        for t in self.tasks:
            if t["gid"] == gid:
                return t
        raise AsanaError("Asana can't find that task. It may be deleted or not shared with you.", 404)

    def open_tasks(self) -> list[dict]:
        keys = ("gid", "name", "due_on", "project", "completed", "permalink_url")
        return [{k: t[k] for k in keys} for t in self.tasks if not t["completed"]]

    def task_detail(self, gid: str) -> dict:
        return copy.deepcopy(self._find(gid))

    def comment(self, gid: str, text: str) -> None:
        task = self._find(gid)
        task["comments"].append({"author": "You", "text": text,
                                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})

    def complete(self, gid: str) -> None:
        self._find(gid)["completed"] = True


class _Meeting:
    def __init__(self, title, start, end, all_day=False, link=""):
        self.title, self.start, self.end, self.all_day, self.link = title, start, end, all_day, link

    def platform(self) -> str:
        if not self.link:
            return ""
        return "zoom" if "zoom.us" in self.link else "meet" if "meet.google" in self.link else "teams"


class DemoAgenda:
    def is_configured(self) -> bool:
        return True

    def meetings(self, day: dt.date | None = None, days: int = 1):
        tz = dt.datetime.now().astimezone().tzinfo
        day = day or dt.date.today()
        out = []
        for n in range(days):
            d = day + dt.timedelta(days=n)

            def at(h, m=0):
                return dt.datetime.combine(d, dt.time(h, m), tzinfo=tz)

            out += [_Meeting("Team standup", at(9, 30), at(9, 45), link="https://meet.google.com/abc-defg-hij"),
                    _Meeting("Design review", at(11), at(12), link=""),
                    _Meeting("Customer call: Acme", at(14), at(15), link="https://zoom.us/j/123456789?pwd=demo"),
                    _Meeting("1:1 with Priya", at(16, 30), at(17))]
        return out


CONTACTS = {"me": DEMO_ADDRESS, "rahul": "rahul.sharma@example.com", "priya": "priya.nair@example.com"}
_EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)


class DemoMailer:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []   # kept so tests can prove nothing real was sent

    def is_configured(self) -> bool:
        return True

    def account(self):
        return DEMO_ADDRESS, "demo"

    def load_contacts(self) -> dict[str, str]:
        return dict(CONTACTS)

    def resolve_recipient(self, spoken: str) -> str:
        s = spoken.strip().lower().rstrip(".")
        if s in ("me", "myself"):
            return DEMO_ADDRESS
        if s in CONTACTS:
            return CONTACTS[s]
        if _EMAIL.match(s):
            return s
        raise ValueError(f"I don't have an email address for {spoken}. Add them to contacts.json")

    def send(self, to: str, subject: str, body: str, attachment=None) -> None:
        self.sent.append((to, subject, body))   # pretend


class DemoChat:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def space_names(self) -> list[str]:
        return ["engineering", "standup"]

    def send(self, space: str, text: str) -> None:
        self.sent.append((space, text))   # pretend
