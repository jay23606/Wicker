"""Tests for settings-fragment installation and {{KEY}} templating.

Manifest `vars` are defaults; a deploy-time --var overrides them.
"""

import pytest

from conftest import make_deploy, minimal_manifest
import wicker


def _install(tmp_path, settings, var=None, source_text="x"):
    src = tmp_path / "Settings.php"
    src.write_text(source_text)
    d = make_deploy(tmp_path, minimal_manifest(settings=settings),
                    parent=tmp_path / "instances", var=var, dry_run=False)
    d.install_settings()
    return d.instance_dir


ENTRY = {"path": "./Settings.php",
         "install": "config/settings/global/Settings.php"}


class TestInstallSettings:
    def test_verbatim_copy_without_vars(self, tmp_path):
        inst = _install(tmp_path, [ENTRY], source_text="{{K}} untouched")
        installed = inst / "config" / "settings" / "global" / "Settings.php"
        assert installed.read_text() == "{{K}} untouched"

    def test_substitutes_both_placeholder_forms(self, tmp_path):
        inst = _install(tmp_path, [{**ENTRY, "vars": {"K": "v"}}],
                        source_text="a={{K}} b={{ K }}")
        installed = inst / "config" / "settings" / "global" / "Settings.php"
        assert installed.read_text() == "a=v b=v"

    def test_cli_var_overrides_manifest_default(self, tmp_path):
        inst = _install(tmp_path, [{**ENTRY, "vars": {"K": "default"}}],
                        var=["K=cli"], source_text="{{K}}")
        installed = inst / "config" / "settings" / "global" / "Settings.php"
        assert installed.read_text() == "cli"

    def test_cli_var_usable_without_manifest_entry(self, tmp_path):
        # vars: {} marks the file as templated; the key comes from --var.
        inst = _install(tmp_path, [{**ENTRY, "vars": {}}],
                        var=["K=cli"], source_text="{{K}}")
        installed = inst / "config" / "settings" / "global" / "Settings.php"
        assert installed.read_text() == "cli"


class TestVarParsing:
    def test_malformed_var_is_a_build_error(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="KEY=VALUE"):
            make_deploy(tmp_path, var=["NOEQUALS"])
