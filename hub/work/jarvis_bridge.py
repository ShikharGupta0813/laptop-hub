"""Loads the owner's Jarvis helper modules (agenda, mailer, gchat) by file path.

The folder is never added to sys.path, so nothing else in it (jarvis.py, overlay.py, a
module that shadows a library) can be imported by accident. Each helper is loaded under a
private module name. All problems become a one-sentence `reason` the UI can show.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HELPERS = ("agenda", "mailer", "gchat")
PRIVATE_PREFIX = "_laptop_hub_jarvis_"

SET_UP_HINT = ("Set jarvis_dir in hub.json to your jarvis-voice-assistant folder and run "
               "pip install -r requirements-work.txt")

# feature -> (helper module, "is it set up" message)
FEATURES = {
    "calendar": ("agenda", "Your calendar isn't connected yet. Run setup calendar in the Jarvis folder."),
    "email": ("mailer", "Email isn't set up yet. Run setup email in the Jarvis folder."),
    "chat": ("gchat", "No Google Chat spaces are set up yet. Run setup chat in the Jarvis folder."),
}


@dataclass
class FeatureState:
    configured: bool
    reason: str = ""
    module: Any = None


class Bridge:
    def __init__(self, jarvis_dir: str = "", preloaded: dict[str, Any] | None = None):
        self.jarvis_dir = (jarvis_dir or "").strip()
        self._modules: dict[str, Any] = dict(preloaded or {})
        self._lock = threading.Lock()

    def load(self, name: str):
        """The helper module, or raises RuntimeError(reason). Blocking (imports a file)."""
        with self._lock:
            if name in self._modules:
                return self._modules[name]
            if not self.jarvis_dir:
                raise RuntimeError(f"{SET_UP_HINT}.")
            folder = Path(self.jarvis_dir).expanduser()
            if not folder.is_dir():
                raise RuntimeError(f"The jarvis_dir folder {folder} doesn't exist. {SET_UP_HINT}.")
            path = folder / f"{name}.py"
            if not path.is_file():
                raise RuntimeError(f"{path.name} isn't in {folder}. {SET_UP_HINT}.")
            private = PRIVATE_PREFIX + name
            spec = importlib.util.spec_from_file_location(private, path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"{path} can't be loaded as a Python module.")
            module = importlib.util.module_from_spec(spec)
            sys.modules[private] = module
            try:
                spec.loader.exec_module(module)
            except ImportError as exc:
                sys.modules.pop(private, None)
                missing = getattr(exc, "name", None) or "a package"
                raise RuntimeError(f"The Python package '{missing}' is missing. "
                                   "Run pip install -r requirements-work.txt") from None
            except Exception as exc:  # the owner's file may fail in any way
                sys.modules.pop(private, None)
                raise RuntimeError(f"{path.name} failed to load: {exc}") from None
            self._modules[name] = module
            return module

    def state(self, feature: str) -> FeatureState:
        """Is this feature usable? Blocking (touches the Windows keyring)."""
        helper, not_set_up = FEATURES[feature]
        try:
            module = self.load(helper)
        except RuntimeError as exc:
            return FeatureState(False, str(exc))
        try:
            if feature == "chat":
                ok = bool(module.space_names())
            else:
                ok = bool(module.is_configured())
        except Exception as exc:  # e.g. no keyring backend
            return FeatureState(False, f"Couldn't read your saved settings: {exc}")
        return FeatureState(ok, "" if ok else not_set_up, module)
