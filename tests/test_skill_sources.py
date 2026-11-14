"""Skill SOURCES list (hub/skills.py): github / git on approved hosts / local folder / direct SKILL.md link.
Fake git runner, fake HTTP, temp folders: no network, nothing is ever executed."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import skills
from hub.common import Ctx
from tests.helpers import make_config
from tests.test_skills import FakeGit, FakeHttp, GOOD


def need_auth(request: Request):
    if request.cookies.get("hub_session") != "yes":
        raise HTTPException(401, "Log in first.")


class FakeText:
    """Stands in for the SKILL.md link fetch: (status, content-type, body)."""

    def __init__(self, status=200, ctype="text/plain", body=GOOD.encode()):
        self.status, self.ctype, self.body, self.calls = status, ctype, body, []

    def __call__(self, url):
        self.calls.append(url)
        return self.status, self.ctype, self.body


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skillsrc-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git = FakeGit({"skills/arxiv/SKILL.md": GOOD, "SKILL.md": GOOD})
        self.text = FakeText()
        self.audits = []
        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth,
                  audit=lambda e, **f: self.audits.append((e, f)))
        app = FastAPI()
        app.include_router(skills.make_router(ctx, runner=self.git, http=FakeHttp(), text_http=self.text))
        self.client = TestClient(app, cookies={"hub_session": "yes"})
        self.anon = TestClient(app)

    def add(self, kind, settings, **extra):
        r = self.client.post("/api/skills/sources", json={"kind": kind, "settings": settings, **extra})
        return r

    def approve(self, host="git.example.com"):
        return self.client.post("/api/skills/hosts", json={"host": host, "confirm": True})


class SourceList(Base):
    def test_today_catalogs_are_just_the_first_entries(self):
        entries = self.client.get("/api/skills/sources").json()["entries"]
        self.assertEqual([e["settings"]["value"] for e in entries], skills.DEFAULT_SOURCES)
        self.assertEqual(self.client.get("/api/skills").json()["sources"], skills.DEFAULT_SOURCES)

    def test_toggle_edit_delete_and_browse_follows_the_list(self):
        first = self.client.get("/api/skills/sources").json()["entries"][0]["id"]
        self.assertEqual(self.client.put(f"/api/skills/sources/{first}", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.client.get("/api/skills").json()["sources"], ["openclaw/openclaw/skills"])
        r = self.add("github", {"value": "https://github.com/acme/skills/tree/main/pack"})
        self.assertEqual(r.json()["entry"]["settings"]["value"], "acme/skills/pack")
        self.assertEqual(self.add("github", {"value": "https://gitlab.com/x/y"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/skills/sources/{first}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/skills/sources/{first}").status_code, 404)

    def test_everything_needs_login(self):
        for method, path in [("get", "/api/skills/sources"), ("post", "/api/skills/sources"), ("put", "/api/skills/sources/x"),
                             ("delete", "/api/skills/sources/x"), ("post", "/api/skills/sources/x/install"),
                             ("post", "/api/skills/sources/x/preview"), ("get", "/api/skills/sources/x/catalog"),
                             ("post", "/api/skills/hosts"), ("delete", "/api/skills/hosts/a.example.com")]:
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)


class GitHosts(Base):
    def test_unapproved_host_is_refused_and_approval_needs_confirm(self):
        r = self.add("git", {"url": "https://git.example.com/team/skills.git"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("approved Git hosts", r.json()["detail"])
        self.assertEqual(self.client.post("/api/skills/hosts", json={"host": "git.example.com"}).status_code, 400)
        self.assertEqual(self.approve().json()["hosts"], ["git.example.com"])
        self.assertEqual(self.add("git", {"url": "https://git.example.com/team/skills.git"}).status_code, 200)

    def test_bad_hosts_and_urls(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "git.example.com:8443", "a b", "x.local", "nodots"):
            self.assertEqual(self.approve(host).status_code, 400, host)
        self.approve()
        for url in ("http://git.example.com/a/b", "https://u:p@git.example.com/a/b", "https://git.example.com:8443/a/b",
                    "https://git.example.com/a", "https://git.example.com/a/../b", "https://git.example.com/a/b?x=1",
                    "ssh://git.example.com/a/b", "file:///etc/passwd", "ext::sh -c id"):
            self.assertEqual(self.add("git", {"url": url}).status_code, 400, url)
        self.assertEqual(self.add("git", {"url": "https://git.example.com/a/b", "path": "../x"}).status_code, 400)
        self.assertEqual(self.add("git", {"url": "https://git.example.com/a/b", "branch": "--upload-pack=x"}).status_code, 400)

    def test_install_clones_with_argv_and_never_runs_anything(self):
        self.approve()
        ident = self.add("git", {"url": "https://git.example.com/team/skills.git", "path": "skills/arxiv"}).json()["entry"]["id"]
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/install", json={}).status_code, 400)   # needs confirm
        preview = self.client.post(f"/api/skills/sources/{ident}/preview", json={}).json()
        self.assertEqual((preview["name"], preview["installed"], preview["untrusted"]), ("arxiv", False, True))
        r = self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        argv = self.git.calls[-1]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[argv.index("--") + 1], "https://git.example.com/team/skills.git")
        self.assertIn("protocol.allow=never", argv)
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertEqual(r.json()["skill"]["source"], "git.example.com/team/skills.git/skills/arxiv")
        self.assertIn(("skill_install", {"name": "arxiv", "source": "git.example.com/team/skills.git/skills/arxiv", "size": mock.ANY}), self.audits)

    def test_removing_the_host_blocks_existing_entries(self):
        self.approve()
        ident = self.add("git", {"url": "https://git.example.com/team/skills.git"}).json()["entry"]["id"]
        self.client.delete("/api/skills/hosts/git.example.com")
        r = self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.git.calls, [])

    def test_symlink_in_a_cloned_repo_is_refused(self):
        self.git.tree = {"SKILL.md": GOOD, "evil": ("link", "/etc")}
        self.approve()
        ident = self.add("git", {"url": "https://git.example.com/team/skills.git"}).json()["entry"]["id"]
        r = self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("symbolic link", r.json()["detail"])
        self.assertEqual(self.client.get("/api/skills").json()["count"], 0)

    def test_github_host_needs_no_approval_for_a_git_entry(self):
        self.assertEqual(self.add("git", {"url": "https://github.com/acme/skills"}).status_code, 200)


class LocalFolder(Base):
    def make(self, files, name="mine"):
        folder = self.tmp / name
        for rel, content in files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(content, encoding="utf-8")
        return folder

    def test_copy_install_lists_scripts_and_runs_nothing(self):
        marker = self.tmp / "ran"
        folder = self.make({"SKILL.md": GOOD, "scripts/run.py": f"open({str(marker)!r}, 'w').write('x')", "notes.md": "n"})
        ident = self.add("folder", {"path": str(folder)}).json()["entry"]["id"]
        preview = self.client.post(f"/api/skills/sources/{ident}/preview", json={}).json()
        self.assertEqual(preview["scripts"], ["scripts/run.py"])
        r = self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(marker.exists())
        installed = self.config.data_path() / "skills" / "arxiv"
        self.assertTrue((installed / "SKILL.md").is_file() and (installed / "scripts" / "run.py").is_file())
        self.assertEqual(r.json()["skill"]["scripts"], ["scripts/run.py"])

    def test_folder_of_skills_lists_children_and_installs_one(self):
        base = self.make({"arxiv/SKILL.md": GOOD, "other/SKILL.md": GOOD.replace("arxiv", "other"), "nothing/readme.md": "x"}, "pack")
        ident = self.add("folder", {"path": str(base)}).json()["entry"]["id"]
        names = [s["folder"] for s in self.client.get(f"/api/skills/sources/{ident}/catalog").json()["skills"]]
        self.assertEqual(names, ["arxiv", "other"])
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/preview", json={}).status_code, 400)   # which one?
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True, "name": "other"}).json()["skill"]["name"], "other")

    def test_traversal_in_the_chosen_child_name_is_refused(self):
        base = self.make({"arxiv/SKILL.md": GOOD}, "pack")
        ident = self.add("folder", {"path": str(base)}).json()["entry"]["id"]
        for name in ("../data", "..", "a/b", "/etc"):
            self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/preview", json={"name": name}).status_code, 400, name)

    def test_symlinks_and_size_caps(self):
        folder = self.make({"SKILL.md": GOOD})
        os.symlink("/etc/hostname", folder / "link.txt")
        ident = self.add("folder", {"path": str(folder)}).json()["entry"]["id"]
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/preview", json={}).status_code, 400)
        os.remove(folder / "link.txt")
        (folder / "big.bin").write_bytes(b"0" * (skills.MAX_TOTAL_BYTES + 1))
        r = self.client.post(f"/api/skills/sources/{ident}/preview", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("too big", r.json()["detail"])
        os.remove(folder / "big.bin")
        (folder / "SKILL.md").write_text("x" * (skills.MAX_SKILL_MD + 1))
        self.assertIn("too big", self.client.post(f"/api/skills/sources/{ident}/preview", json={}).json()["detail"])

    def test_missing_folder_and_symlinked_root(self):
        ident = self.add("folder", {"path": str(self.tmp / "nope")}).json()["entry"]["id"]
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident}/preview", json={}).status_code, 400)
        real = self.make({"SKILL.md": GOOD})
        os.symlink(real, self.tmp / "alias")
        ident2 = self.add("folder", {"path": str(self.tmp / "alias")}).json()["entry"]["id"]
        self.assertEqual(self.client.post(f"/api/skills/sources/{ident2}/preview", json={}).status_code, 400)


class SkillLink(Base):
    def install(self, url="https://example.com/skills/my-skill/SKILL.md"):
        ident = self.add("url", {"url": url}).json()["entry"]["id"]
        return self.client.post(f"/api/skills/sources/{ident}/install", json={"confirm": True})

    def test_installs_only_the_markdown(self):
        r = self.install()
        self.assertEqual(r.status_code, 200, r.text)
        folder = self.config.data_path() / "skills" / "arxiv"
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [".hub-skill.json", "SKILL.md"])
        self.assertEqual(self.text.calls, ["https://example.com/skills/my-skill/SKILL.md"])

    def test_name_comes_from_the_link_when_there_is_no_frontmatter(self):
        self.text.body = b"# Just text\nno frontmatter"
        self.assertEqual(self.install("https://example.com/skills/my-skill/SKILL.md").json()["skill"]["name"], "my-skill")

    def test_refusals(self):
        self.text.body = b"x" * (skills.MAX_SKILL_MD + 1)
        self.assertIn("too big", self.install().json()["detail"])
        self.text.body = GOOD.encode()
        self.text.ctype = "text/html"
        self.assertIn("not a markdown", self.install().json()["detail"])
        self.text.ctype, self.text.status = "text/plain", 404
        self.assertEqual(self.install().status_code, 502)
        self.text.status, self.text.body = 200, b"abc\x00def"
        self.assertIn("not text", self.install().json()["detail"])
        self.text.body = b"---\nname: ../evil\n---\nbody"
        self.assertEqual(self.install().status_code, 400)
        self.assertEqual(self.client.get("/api/skills").json()["count"], 0)

    def test_bad_links_cannot_be_added(self):
        for url in ("ftp://example.com/SKILL.md", "https://u:p@example.com/SKILL.md", "not a link", "javascript:alert(1)"):
            self.assertEqual(self.add("url", {"url": url}).status_code, 400, url)


class RealFetch(unittest.TestCase):
    """The default link fetcher: public addresses only, redirects checked by hand, size capped."""

    def setUp(self):
        patcher = mock.patch("hub.research.default_resolver", lambda host: ["127.0.0.1"] if host == "internal.example.com" else ["93.184.216.34"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_redirect_to_a_private_host_is_blocked(self):
        def handler(request):
            if request.url.host == "internal.example.com":
                raise AssertionError("must not be contacted")
            return httpx.Response(302, headers={"location": "http://internal.example.com/SKILL.md"})
        with self.assertRaises(skills.SkillError) as e:
            skills.fetch_markdown("https://example.com/a/SKILL.md", transport=httpx.MockTransport(handler))
        self.assertIn("not allowed", str(e.exception))
        for bad in ("http://127.0.0.1/SKILL.md", "http://localhost/x", "https://u:p@example.com/x", "file:///etc/passwd"):
            with self.assertRaises(skills.SkillError, msg=bad):
                skills.fetch_markdown(bad, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x")))

    def test_body_is_cut_at_the_cap(self):
        big = b"a" * (skills.MAX_SKILL_MD * 3)
        status, ctype, body = skills.fetch_markdown("https://example.com/SKILL.md", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=big, headers={"content-type": "text/markdown; charset=utf-8"})))
        self.assertEqual((status, ctype, len(body)), (200, "text/markdown", skills.MAX_SKILL_MD + 1))

    def test_too_many_redirects(self):
        handler = lambda r: httpx.Response(302, headers={"location": "https://example.com/again"})  # noqa: E731
        with self.assertRaises(skills.SkillError):
            skills.fetch_markdown("https://example.com/a", transport=httpx.MockTransport(handler))


class AssistantPathUnchanged(Base):
    def test_assistant_style_install_stays_github_only(self):
        store = skills.store_for(self.config, self.git, FakeHttp(), self.text)
        for raw in ("https://git.example.com/a/b", "/etc/passwd", "../x", "file:///x", "https://example.com/SKILL.md"):
            with self.assertRaises(skills.SkillError, msg=raw):
                store.install(raw)
        self.assertEqual(self.git.calls, [])


if __name__ == "__main__":
    unittest.main()
