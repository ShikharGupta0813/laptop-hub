"""ORACLE TALEO (*.taleo.net, classic careersection): a multi-page wizard of plain inputs and selects (ids like `requisitionListInterface...`
and `...FormPage...`), a sign-in page in front (the owner logs in), "Next" / "Continue" / "Submit" buttons that are `input type=button`,
and a voluntary "EEO / Self-Identification" page that is never filled."""
import re

from .engine import System

TALEO = System(
    key="taleo", name="Taleo", hosts=("taleo.net",),
    marker=re.compile(r"requisitionListInterface|jobapply\.ftl|/careersection/[^\"']+/(jobapply|login)", re.I),
    login_marker=re.compile(r"\bloginform\b|\blogin\.ftl\b|signin(form|button)|UserName|\bunauthenticated", re.I),
    note="classic multi-page wizard",
)
