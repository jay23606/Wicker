"""Tests for deploy-time state recording (.wicker-app/).

record_state writes state.json (app, version, manifest hash, per-page content
hashes, history), a verbatim copy of the applied manifest, and a snapshot of
every imported page file — the pristine-test and merge base a future
`wicker upgrade` reads.
"""

import hashlib
import json

import yaml

from conftest import make_deploy, minimal_manifest


def _write_app(tmp_path, manifest, pages=None):
    """Write wicker.yaml plus page files ({relative-path: text}) into the
    app dir."""
    (tmp_path / "wicker.yaml").write_text(yaml.safe_dump(manifest))
    for rel, text in (pages or {}).items():
        f = tmp_path / "content" / "pages" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


def _deploy(tmp_path, manifest, **kw):
    kw.setdefault("dry_run", False)
    return make_deploy(tmp_path, manifest, **kw)


PAGES = {
    "Template/Expense.mediawiki": "{{Expense|cost={{{cost|}}}}}",
    "Main Page.mediawiki": "Welcome",
    "custom-name.css": "body { color: red }",
}

MANIFEST = minimal_manifest(
    version="1.0.0",
    content={"pages": {"dir": "./content/pages", "list": [
        "Template:Expense",
        "Main Page",
        {"page": "Template:Expense/styles.css", "file": "custom-name.css"},
    ]}})


class TestRecordState:
    def test_state_json_contents(self, tmp_path):
        _write_app(tmp_path, MANIFEST, PAGES)
        _deploy(tmp_path, MANIFEST).record_state()
        state = json.loads((tmp_path / "t" / ".wicker-app" / "state.json")
                           .read_text())
        assert state["app"] == "app"
        assert state["version"] == "1.0.0"
        assert state["manifestSha256"] == hashlib.sha256(
            (tmp_path / "wicker.yaml").read_bytes()).hexdigest()
        assert state["pages"] == {
            "Template:Expense": hashlib.sha256(
                PAGES["Template/Expense.mediawiki"].encode()).hexdigest(),
            "Main Page": hashlib.sha256(
                PAGES["Main Page.mediawiki"].encode()).hexdigest(),
            "Template:Expense/styles.css": hashlib.sha256(
                PAGES["custom-name.css"].encode()).hexdigest(),
        }
        assert [h["op"] for h in state["history"]] == ["deploy"]
        assert state["history"][0]["version"] == "1.0.0"
        assert "at" in state["history"][0]

    def test_manifest_copied_verbatim(self, tmp_path):
        _write_app(tmp_path, MANIFEST, PAGES)
        _deploy(tmp_path, MANIFEST).record_state()
        copy = tmp_path / "t" / ".wicker-app" / "manifest.yaml"
        assert copy.read_bytes() == (tmp_path / "wicker.yaml").read_bytes()

    def test_snapshot_uses_derived_names(self, tmp_path):
        # A per-page `file` override still lands under the title-derived
        # name, so upgrade can look pages up by title alone.
        _write_app(tmp_path, MANIFEST, PAGES)
        _deploy(tmp_path, MANIFEST).record_state()
        content = tmp_path / "t" / ".wicker-app" / "content"
        assert (content / "Template/Expense.mediawiki").read_text() == \
            PAGES["Template/Expense.mediawiki"]
        assert (content / "Main Page.mediawiki").read_text() == \
            PAGES["Main Page.mediawiki"]
        assert (content / "Template/Expense/styles.css").read_text() == \
            PAGES["custom-name.css"]
        assert not (content / "custom-name.css").exists()

    def test_missing_page_file_skipped(self, tmp_path):
        pages = dict(PAGES)
        del pages["Main Page.mediawiki"]
        _write_app(tmp_path, MANIFEST, pages)
        _deploy(tmp_path, MANIFEST).record_state()
        state = json.loads((tmp_path / "t" / ".wicker-app" / "state.json")
                           .read_text())
        assert "Main Page" not in state["pages"]
        assert "Template:Expense" in state["pages"]

    def test_no_content_section(self, tmp_path):
        manifest = minimal_manifest(version="1.0.0")
        _write_app(tmp_path, manifest)
        _deploy(tmp_path, manifest).record_state()
        state = json.loads((tmp_path / "t" / ".wicker-app" / "state.json")
                           .read_text())
        assert state["pages"] == {}

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        _write_app(tmp_path, MANIFEST, PAGES)
        _deploy(tmp_path, MANIFEST, dry_run=True).record_state()
        assert not (tmp_path / "t" / ".wicker-app").exists()
        assert ".wicker-app" in capsys.readouterr().out

    def test_rerecord_appends_history_and_prunes_snapshot(self, tmp_path):
        _write_app(tmp_path, MANIFEST, PAGES)
        _deploy(tmp_path, MANIFEST).record_state()
        # Same app dir, next version, one page dropped from the manifest.
        m2 = minimal_manifest(
            version="1.1.0",
            content={"pages": {"dir": "./content/pages",
                               "list": ["Template:Expense"]}})
        (tmp_path / "wicker.yaml").write_text(yaml.safe_dump(m2))
        _deploy(tmp_path, m2).record_state()
        app_state = tmp_path / "t" / ".wicker-app"
        state = json.loads((app_state / "state.json").read_text())
        assert state["version"] == "1.1.0"
        assert [h["version"] for h in state["history"]] == ["1.0.0", "1.1.0"]
        assert list(state["pages"]) == ["Template:Expense"]
        # The snapshot reflects only the current manifest — no stale files.
        assert not (app_state / "content" / "Main Page.mediawiki").exists()


def test_record_state_rejects_snapshot_filename_collisions(tmp_path):
    import pytest
    import yaml as _yaml
    import wicker
    from conftest import make_deploy, minimal_manifest
    m = minimal_manifest(content={"pages": {
        "dir": "./content/pages",
        "list": ["Template:Foo", "Template/Foo"]}})
    app = tmp_path / "app"
    (app / "content" / "pages" / "Template").mkdir(parents=True)
    (app / "content" / "pages" / "Template" / "Foo.mediawiki").write_text("x")
    (app / "wicker.yaml").write_text(_yaml.safe_dump(m))
    d = make_deploy(app, m, parent=tmp_path, dry_run=False)
    with pytest.raises(wicker.BuildError, match="both map to the snapshot"):
        d.record_state()
