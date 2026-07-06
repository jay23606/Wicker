"""Tests for custom-extension installation (source copy and git clone).

`ref` accepts a branch, tag, or commit: a branch/tag rides `git clone
--branch`; a commit SHA (which --branch rejects) falls back to a plain
clone plus checkout.
"""

import subprocess

import pytest

from conftest import make_deploy, minimal_manifest


@pytest.fixture
def git_repo(tmp_path):
    """A local two-commit repo (file:// clonable): returns (url, first_sha).
    HEAD has content v2; the first commit (tagged v1) has content v1."""
    repo = tmp_path / "ext-src"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                        "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    (repo / "extension.json").write_text("v1\n")
    git("add", ".")
    git("commit", "-qm", "one")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True,
                         text=True).stdout.strip()
    git("tag", "v1")
    (repo / "extension.json").write_text("v2\n")
    git("add", ".")
    git("commit", "-qm", "two")
    return f"file://{repo}", sha


def _install(tmp_path, extensions):
    d = make_deploy(tmp_path, minimal_manifest(extensions=extensions),
                    parent=tmp_path / "instances", dry_run=False)
    d.install_extensions()
    return d.instance_dir / "extensions"


class TestSourceCopy:
    def test_copies_local_extension(self, tmp_path):
        src = tmp_path / "MyExt"
        src.mkdir()
        (src / "extension.json").write_text("{}\n")
        dest = _install(tmp_path, [{"name": "MyExt", "source": "./MyExt"}])
        assert (dest / "MyExt" / "extension.json").is_file()

    def test_bundled_extension_installs_nothing(self, tmp_path):
        dest = _install(tmp_path, [{"name": "Cargo"}])
        assert not dest.exists()


class TestGitClone:
    def test_clone_default_branch_tip(self, tmp_path, git_repo):
        url, _ = git_repo
        dest = _install(tmp_path, [{"name": "E", "git": url}])
        assert (dest / "E" / "extension.json").read_text() == "v2\n"

    def test_clone_tag_ref(self, tmp_path, git_repo):
        url, _ = git_repo
        dest = _install(tmp_path, [{"name": "E", "git": url, "ref": "v1"}])
        assert (dest / "E" / "extension.json").read_text() == "v1\n"

    def test_clone_branch_ref(self, tmp_path, git_repo):
        url, _ = git_repo
        dest = _install(tmp_path, [{"name": "E", "git": url, "ref": "main"}])
        assert (dest / "E" / "extension.json").read_text() == "v2\n"

    def test_commit_sha_falls_back_to_checkout(self, tmp_path, git_repo):
        url, sha = git_repo
        dest = _install(tmp_path, [{"name": "E", "git": url, "ref": sha}])
        assert (dest / "E" / "extension.json").read_text() == "v1\n"

    def test_bad_ref_still_fails(self, tmp_path, git_repo):
        url, _ = git_repo
        import wicker
        with pytest.raises(wicker.BuildError):
            _install(tmp_path, [{"name": "E", "git": url, "ref": "no-such"}])


class TestResyncOverwrite:
    """`sync-code` re-runs install_extensions(overwrite=True) against an
    existing instance: source copies are refreshed (and pruned), git checkouts
    are re-cloned and re-pinned to the manifest ref."""

    def _deploy(self, tmp_path, extensions):
        d = make_deploy(tmp_path, minimal_manifest(extensions=extensions),
                        parent=tmp_path / "instances", dry_run=False)
        d.install_extensions()
        return d, d.instance_dir / "extensions"

    def test_source_resync_updates_and_prunes(self, tmp_path):
        src = tmp_path / "MyExt"
        src.mkdir()
        (src / "extension.json").write_text("v1\n")
        (src / "old.php").write_text("x\n")
        d, dest = self._deploy(tmp_path, [{"name": "MyExt", "source": "./MyExt"}])
        assert (dest / "MyExt" / "old.php").is_file()
        # Source changes: file updated, one removed, one added.
        (src / "extension.json").write_text("v2\n")
        (src / "old.php").unlink()
        (src / "new.php").write_text("y\n")
        d.install_extensions(overwrite=True)
        assert (dest / "MyExt" / "extension.json").read_text() == "v2\n"
        assert not (dest / "MyExt" / "old.php").exists()
        assert (dest / "MyExt" / "new.php").is_file()

    def test_source_reinstall_without_overwrite_raises(self, tmp_path):
        src = tmp_path / "MyExt"
        src.mkdir()
        (src / "extension.json").write_text("{}\n")
        d, _ = self._deploy(tmp_path, [{"name": "MyExt", "source": "./MyExt"}])
        with pytest.raises(FileExistsError):
            d.install_extensions()

    def test_git_resync_repins_to_new_ref(self, tmp_path, git_repo):
        url, _ = git_repo
        d, dest = self._deploy(tmp_path, [{"name": "E", "git": url, "ref": "v1"}])
        assert (dest / "E" / "extension.json").read_text() == "v1\n"
        d.m["extensions"][0]["ref"] = "main"
        d.install_extensions(overwrite=True)  # re-clone into existing dest
        assert (dest / "E" / "extension.json").read_text() == "v2\n"


class TestSyncCode:
    def test_missing_instance_raises(self, tmp_path):
        import wicker
        d = make_deploy(tmp_path, minimal_manifest(),
                        parent=tmp_path / "instances", dry_run=True)
        with pytest.raises(wicker.BuildError):
            d.sync_code()

    def test_dry_run_syncs_existing_instance(self, tmp_path):
        d = make_deploy(tmp_path, minimal_manifest(),
                        parent=tmp_path / "instances", dry_run=True)
        d.instance_dir.mkdir(parents=True)
        d.sync_code()  # dry-run: prints actions, changes nothing, no raise
