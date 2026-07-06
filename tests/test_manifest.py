"""Tests for manifest loading and validation, including the degraded
required-keys check used when jsonschema is not installed."""

import sys

import pytest

from conftest import minimal_manifest
import wicker


class TestLoadManifest:
    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="no wicker.yaml"):
            wicker.load_manifest(tmp_path)

    def test_non_mapping_manifest_raises(self, tmp_path):
        (tmp_path / "wicker.yaml").write_text("- just\n- a list\n")
        with pytest.raises(wicker.BuildError, match="not a mapping"):
            wicker.load_manifest(tmp_path)

    def test_invalid_yaml_raises(self, tmp_path):
        # e.g. an unsubstituted {{name}} scaffold placeholder
        (tmp_path / "wicker.yaml").write_text("name: {{name}}\n")
        with pytest.raises(wicker.BuildError, match="not valid YAML"):
            wicker.load_manifest(tmp_path)


class TestValidateManifest:
    def test_schema_violation_raises(self):
        with pytest.raises(wicker.BuildError, match="schema validation"):
            wicker.validate_manifest(minimal_manifest(name="Not Kebab"))

    def test_valid_manifest_passes(self):
        wicker.validate_manifest(minimal_manifest())


class TestWithoutJsonschema:
    """jsonschema is a hard requirement — validation refuses to degrade,
    failing with the install hint before anything runs."""

    def test_missing_jsonschema_is_fatal(self, monkeypatch):
        import importlib.util
        real = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda m, *a: None if m == "jsonschema" else real(m, *a))
        with pytest.raises(wicker.BuildError, match="jsonschema"):
            wicker.validate_manifest(minimal_manifest())
