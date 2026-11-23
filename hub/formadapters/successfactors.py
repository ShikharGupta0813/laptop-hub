"""SAP SUCCESSFACTORS (career*.successfactors.com, *.sapsf.com, jobs.sap.com ...): UI5 controls. Combo boxes are an input inside a
`sapMComboBox` (pop-up list of `li[role=option]`), dates are `sapMDatePicker` inputs, plain fields are `sapMInputBaseInner` inputs, and
the candidate flow ends in a Submit. The sign-in / create-account form is the owner's."""
import re

from .engine import System

SUCCESSFACTORS = System(
    key="successfactors", name="SuccessFactors", hosts=("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "jobs.sap.com"),
    marker=re.compile(r"sapMInputBaseInner|sapMComboBox|career_job_req_id", re.I),
    login_marker=re.compile(r"\bloginform\b|candidate-?login|fbclc_login|create-?account|j_username", re.I),
    combo=re.compile(r"sapmcombobox|sapmselect|ui5-combobox|ui5-select|combobox", re.I),
    date=re.compile(r"sapmdp|sapmdatepicker|ui5-date|datepicker", re.I),
    note="UI5 combo boxes and date pickers",
)
