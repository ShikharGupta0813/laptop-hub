"""Form adapters for hub/formfill.py: SAP SuccessFactors, Oracle Taleo, Oracle Recruiting Cloud, iCIMS and Ashby.

formfill.py asks `for_page(url, html)` once per page read; None means "not one of these", and then the generic mapper (or the Workday
reader) runs exactly as before. See docs/api/formfill.md, one section per system.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .ashby import ASHBY
from .engine import System
from .icims import ICIMS, FRAME_HOSTS, frame_wanted
from .oraclerc import ORACLE
from .successfactors import SUCCESSFACTORS
from .taleo import TALEO

SYSTEMS: tuple[System, ...] = (ASHBY, TALEO, ORACLE, SUCCESSFACTORS, ICIMS)

__all__ = ["SYSTEMS", "for_page", "frame_wanted", "FRAME_HOSTS"]


def for_page(url: str, html: str = "") -> System | None:
    # the address decides first (a marker only matters on a custom domain), so one system's words can never steal another's host
    for s in SYSTEMS:
        if s.applies(url, ""):
            return s
    for s in SYSTEMS:
        if s.marker is not None and s.applies(url, html):
            return s
    return None
