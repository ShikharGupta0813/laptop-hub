"""ASHBY (jobs.ashbyhq.com): one long React page. Name / email / phone are plain inputs (`_systemfield_*`), selects are React combo boxes
(an input with role=combobox and a pop-up list), yes/no questions are two buttons, the resume is a file input. There is no login and no
Next step: the page ends in "Submit Application", which stays the owner's tap."""
import re

from .engine import System

ASHBY = System(
    key="ashby", name="Ashby", hosts=("ashbyhq.com",),
    marker=re.compile(r"ashby-application-form|_systemfield_(name|email|resume)", re.I),
    combo=re.compile(r"combobox|_select_|select__control|ashby-select|react-select", re.I),
    note="React selects and Yes/No buttons",
)
