"""ICIMS (*.icims.com): the application form is inside an iframe (`icims_content_iframe`) on the same or an iCIMS sub host. The browser
driver (formfill.PlaywrightDriver) reads and fills inside that frame when `frame_wanted` says so; every other call is the same as for
a page. A "Returning Candidate" sign-in / create-profile wall is the owner's to pass."""
import re

from .. import formfill as ff
from .engine import System

FRAME_HOSTS = ("icims.com",)
FRAME_MARKER = re.compile(r"icims_content_iframe|iCIMS_(?:Main|Content|Header|Form|Table)", re.I)

ICIMS = System(
    key="icims", name="iCIMS", hosts=FRAME_HOSTS,
    marker=re.compile(r"icims_content_iframe|iCIMS_", re.I),
    login_marker=re.compile(r"loginName|icims_?login|candidate-?login|returning-?candidate|create-?profile", re.I),
    note="the form is inside an iframe",
)


def frame_wanted(url: str, html: str = "") -> bool:
    """Should the driver look for the form inside a child frame? Only for iCIMS addresses or pages that carry the iCIMS frame marker."""
    host = ff.host_of(url or "")
    return any(host == s or host.endswith("." + s) for s in FRAME_HOSTS) or bool(html and FRAME_MARKER.search(html[:ff.MAX_HTML]))
