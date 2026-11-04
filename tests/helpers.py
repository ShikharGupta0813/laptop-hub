"""Shared test setup: a Config pointing at temporary folders, with a known login token."""
import secrets
import tempfile
from pathlib import Path

from hub import config as cfg
from hub.auth import hash_token

TOKEN = "test-token-123"


def make_config(**overrides):
    root = Path(tempfile.mkdtemp(prefix="hub-test-"))
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("# Hello\n", encoding="utf-8")
    base = dict(
        token_hash=hash_token(TOKEN), session_secret=secrets.token_bytes(32),
        roots={"docs": str(root / "docs"), "notes": str(root / "notes"), "inbox": str(root / "inbox")},
        notes_dir=str(root / "notes"), inbox_dir=str(root / "inbox"),
        scripts={"hello": {"label": "Say hello", "argv": ["echo", "hello from a button"]}},
        shells={"sh": ["/bin/sh"]})
    base.update(overrides)
    return cfg.Config(**base), root
