"""Tests for postInstall action dispatch and maintenance-script resolution.

Bundled extensions live under canasta-extensions/ in the web container,
custom (source/git) ones under extensions/; with no extension the script
is a core maintenance script.
"""

import pytest

from conftest import make_deploy, minimal_manifest
import wicker


def _deploy(tmp_path, **sections):
    return make_deploy(tmp_path, minimal_manifest(**sections))


class TestDispatch:
    def test_unsupported_kind_raises(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="unsupported action kind"):
            _deploy(tmp_path).run_actions(
                [{"kind": "bogus"}], phase="postInstall")

    def test_empty_actions_do_nothing(self, tmp_path, capsys):
        _deploy(tmp_path).run_actions([], phase="postInstall")
        assert capsys.readouterr().out == ""


class TestResolveScript:
    def test_core_script(self, tmp_path):
        d = _deploy(tmp_path)
        assert d._resolve_script(None, "update.php") == \
            f"{wicker.MW_ROOT}/maintenance/update.php"

    def test_bundled_extension_script(self, tmp_path):
        d = _deploy(tmp_path, extensions=[{"name": "Cargo"}])
        assert d._resolve_script("Cargo", "cargoRecreateData.php") == \
            f"{wicker.MW_ROOT}/canasta-extensions/Cargo/maintenance/cargoRecreateData.php"

    def test_custom_extension_script(self, tmp_path):
        d = _deploy(tmp_path,
                    extensions=[{"name": "MyExt", "source": "./MyExt"}])
        assert d._resolve_script("MyExt", "seed.php") == \
            f"{wicker.MW_ROOT}/extensions/MyExt/maintenance/seed.php"


class TestActions:
    def test_maintenance_action_command(self, tmp_path, capsys):
        d = _deploy(tmp_path, extensions=[{"name": "Cargo"}])
        d._action_maintenance({"kind": "maintenance", "extension": "Cargo",
                               "script": "cargoRecreateData.php",
                               "args": ["--table=Example"]})
        out = capsys.readouterr().out
        assert "canasta maintenance exec -i t" in out
        assert "cargoRecreateData.php --table=Example --wiki=main" in out

    def test_update_action_command(self, tmp_path, capsys):
        _deploy(tmp_path)._action_update({"kind": "update"})
        assert "update --quick --wiki=main" in capsys.readouterr().out

    def test_page_edit_from_file(self, tmp_path, capsys):
        (tmp_path / "page.mediawiki").write_text("hello")
        _deploy(tmp_path)._action_page_edit(
            {"kind": "page-edit", "page": "Main Page",
             "source": "./page.mediawiki"})
        out = capsys.readouterr().out
        assert "--stdin-file" in out
        assert "page.mediawiki" in out

    def test_page_edit_literal_content(self, tmp_path, capsys):
        _deploy(tmp_path)._action_page_edit(
            {"kind": "page-edit", "page": "Main Page",
             "source": "literal wikitext, not a file"})
        out = capsys.readouterr().out
        assert "--stdin-file" in out
        assert ".wikitext" in out

    def test_shell_unknown_target_raises(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="unknown shell container"):
            _deploy(tmp_path)._action_shell(
                {"kind": "shell", "command": "true", "container": "db"})


class TestInstallContent:
    def _run(self, tmp_path, capsys, entries, files=()):
        pages_dir = tmp_path / "content" / "pages"
        pages_dir.mkdir(parents=True)
        for rel, text in files:
            f = pages_dir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        d = _deploy(tmp_path, content={"pages": {"dir": "./content/pages",
                                                 "list": entries}})
        d.install_content()
        return capsys.readouterr()

    def test_string_and_dict_entries(self, tmp_path, capsys):
        cap = self._run(tmp_path, capsys,
                        ["Main Page", {"page": "Custom", "file": "c.wiki"}],
                        files=[("Main Page.mediawiki", "x"), ("c.wiki", "y")])
        assert "-> Main Page" in cap.out
        assert "-> Custom" in cap.out

    def test_missing_source_warns_and_continues(self, tmp_path, capsys):
        cap = self._run(tmp_path, capsys, ["Main Page", "There"],
                        files=[("There.mediawiki", "x")])
        assert "missing source for Main Page" in cap.err
        assert "-> There" in cap.out
