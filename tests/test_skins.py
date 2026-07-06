"""Tests for custom-skin installation.

Skins share the install path with extensions (`_install_components`): a
name-only entry is bundled and skipped; `source` is copied and `git` is
cloned into `<instance>/skins/`. These tests cover the skin-specific wiring
(section name, destination dir) and the sync overwrite path; the git ref
matrix is exercised in test_extensions.py against the same code.
"""

import subprocess

import pytest

from conftest import make_deploy, minimal_manifest


@pytest.fixture
def git_repo(tmp_path):
    """A local one-commit skin repo (file:// clonable); returns its URL."""
    repo = tmp_path / "skin-src"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                        "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    (repo / "skin.json").write_text("v1\n")
    git("add", ".")
    git("commit", "-qm", "one")
    return f"file://{repo}"


def _install(tmp_path, skins):
    d = make_deploy(tmp_path, minimal_manifest(skins=skins),
                    parent=tmp_path / "instances", dry_run=False)
    d.install_skins()
    return d, d.instance_dir / "skins"


class TestSkinInstall:
    def test_copies_local_skin(self, tmp_path):
        src = tmp_path / "MySkin"
        src.mkdir()
        (src / "skin.json").write_text("{}\n")
        _, dest = _install(tmp_path, [{"name": "MySkin", "source": "./MySkin"}])
        assert (dest / "MySkin" / "skin.json").is_file()

    def test_clones_git_skin(self, tmp_path, git_repo):
        _, dest = _install(tmp_path, [{"name": "S", "git": git_repo}])
        assert (dest / "S" / "skin.json").read_text() == "v1\n"

    def test_bundled_skin_installs_nothing(self, tmp_path):
        _, dest = _install(tmp_path, [{"name": "Vector"}])
        assert not dest.exists()


class TestSkinResync:
    def test_resync_updates_and_prunes(self, tmp_path):
        src = tmp_path / "MySkin"
        src.mkdir()
        (src / "skin.json").write_text("v1\n")
        (src / "old.css").write_text("x\n")
        d, dest = _install(tmp_path, [{"name": "MySkin", "source": "./MySkin"}])
        assert (dest / "MySkin" / "old.css").is_file()
        (src / "skin.json").write_text("v2\n")
        (src / "old.css").unlink()
        (src / "new.css").write_text("y\n")
        d.install_skins(overwrite=True)
        assert (dest / "MySkin" / "skin.json").read_text() == "v2\n"
        assert not (dest / "MySkin" / "old.css").exists()
        assert (dest / "MySkin" / "new.css").is_file()


class TestSyncCodeCoversSkins:
    def test_sync_code_installs_skins(self, tmp_path):
        src = tmp_path / "MySkin"
        src.mkdir()
        (src / "skin.json").write_text("{}\n")
        d = make_deploy(
            tmp_path, minimal_manifest(skins=[{"name": "MySkin",
                                               "source": "./MySkin"}]),
            parent=tmp_path / "instances", dry_run=True)
        d.instance_dir.mkdir(parents=True)
        d.sync_code()  # dry-run over an existing instance: no raise
