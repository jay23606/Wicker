"""Tests for `wicker init` — scaffolding a new application directory.

init copies the skeleton tree into a target directory, substituting the
app name/description into the templated files, and the result must be a
schema-valid manifest.
"""

import argparse
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wicker  # noqa: E402


def _init(path, name=None, description=None):
    wicker.init_app(argparse.Namespace(
        path=str(path), name=name, description=description))


class TestSlug:
    def test_kebabs_arbitrary_text(self):
        assert wicker.slug("My Cool App") == "my-cool-app"
        assert wicker.slug("Foo_Bar.Baz") == "foo-bar-baz"
        assert wicker.slug("--Already-Kebab--") == "already-kebab"


class TestScaffold:
    def test_creates_tree_with_readmes(self, tmp_path):
        target = tmp_path / "myapp"
        _init(target)
        for rel in ("wicker.yaml", "README.md", "extensions/README.md",
                    "images/README.md", "settings/README.md",
                    "sidecars/README.md", "content/README.md",
                    "content/pages/README.md"):
            assert (target / rel).is_file(), rel

    def test_name_derived_from_directory(self, tmp_path):
        target = tmp_path / "My Cool App"
        _init(target)
        manifest, _ = wicker.load_manifest(target)
        assert manifest["name"] == "my-cool-app"

    def test_name_and_description_override(self, tmp_path):
        target = tmp_path / "whatever"
        _init(target, name="myapp", description="A test app")
        manifest, _ = wicker.load_manifest(target)
        assert manifest["name"] == "myapp"
        assert manifest["description"] == "A test app"

    def test_generated_manifest_is_schema_valid(self, tmp_path):
        target = tmp_path / "myapp"
        _init(target)
        manifest, _ = wicker.load_manifest(target)
        wicker.validate_manifest(manifest)  # raises BuildError on failure

    def test_no_unreplaced_placeholders(self, tmp_path):
        target = tmp_path / "myapp"
        _init(target)
        for rel in ("wicker.yaml", "README.md"):
            assert "{{" not in (target / rel).read_text(), rel


class TestInitGuards:
    def test_rejects_non_empty_directory(self, tmp_path):
        target = tmp_path / "myapp"
        target.mkdir()
        (target / "existing").write_text("x")
        with pytest.raises(wicker.BuildError, match="not empty"):
            _init(target)

    def test_empty_directory_is_allowed(self, tmp_path):
        target = tmp_path / "myapp"
        target.mkdir()
        _init(target)
        assert (target / "wicker.yaml").is_file()

    def test_rejects_invalid_explicit_name(self, tmp_path):
        with pytest.raises(wicker.BuildError, match="invalid app name"):
            _init(tmp_path / "myapp", name="Bad Name")

    def test_rejects_path_that_is_a_file(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x")
        with pytest.raises(wicker.BuildError, match="not a directory"):
            _init(target)


class TestSkeletonTracksSchema:
    """The skeleton wicker.yaml is hand-written, not generated from the
    schema, so it can drift. Guard the realistic failure mode: a top-level
    manifest section added to the schema but forgotten in the skeleton."""

    # Schema-declared but deliberately absent from the skeleton, because
    # `deploy` does not implement them yet — scaffolding them would advertise
    # sections that do nothing. Adding support means dropping the key here and
    # documenting it in the skeleton.
    NOT_IN_SKELETON = set()

    def test_every_schema_section_is_in_the_skeleton(self):
        schema = json.loads(wicker.SCHEMA_PATH.read_text())
        text = (wicker.SKELETON_PATH / "wicker.yaml").read_text()
        for prop in schema["properties"]:
            if prop in self.NOT_IN_SKELETON:
                continue
            # Matches the key whether live (`name:`) or commented (`# env:`).
            assert re.search(rf"^#?\s*{re.escape(prop)}:", text, re.MULTILINE), \
                f"schema property {prop!r} is not mentioned in skeleton/wicker.yaml"

    def test_omissions_are_real_schema_properties(self):
        # Keep NOT_IN_SKELETON honest: a name left here after the property is
        # removed/renamed in the schema is stale and should be cleaned up.
        schema = json.loads(wicker.SCHEMA_PATH.read_text())
        assert self.NOT_IN_SKELETON <= set(schema["properties"])
