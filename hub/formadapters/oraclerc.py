"""ORACLE RECRUITING CLOUD (*.oraclecloud.com/hcmUI/CandidateExperience): Oracle JET components. Text is `oj-input-text`, selects are
`oj-select-single` / combo boxes (an input with role=combobox and a pop-up listbox), dates are `oj-input-date`, yes/no are radio sets, the
flow has a progress header and Next / Submit buttons. oraclecloud.com hosts other Oracle products, so the host only counts with the
CandidateExperience path."""
import re

from .engine import System

ORACLE = System(
    key="oraclerc", name="Oracle Recruiting", hosts=("oraclecloud.com",), path=re.compile(r"/hcmUI/CandidateExperience", re.I),
    marker=re.compile(r"CandidateExperience|<oj-(?:select-single|input-text|radioset)", re.I),
    login_marker=re.compile(r"sign-?in-?form|signin-page|oj-sign-?in|candidate-?sign-?in|create-?account", re.I),
    combo=re.compile(r"oj-select|oj-combobox|combobox|oj-listbox", re.I),
    date=re.compile(r"oj-input-date|oj-inputdate|oj-input-date-time|datepicker", re.I),
    note="JET listbox / combo boxes",
)
