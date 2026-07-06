"""Tests for content.uploads — media imported into the wikis via
importImages.php. Loose files go to the first wiki; a <wiki-id>/ subdirectory
targets that wiki; a subdirectory matching no wiki is skipped. Compose-only.
"""

from conftest import make_deploy, minimal_manifest


def _deploy(tmp_path, dry_run=True, orchestrator="compose",
            domain_name="localhost", **sections):
    return make_deploy(tmp_path, minimal_manifest(**sections),
                       dry_run=dry_run, orchestrator=orchestrator,
                       domain_name=domain_name)


def _make_uploads(tmp_path, tree):
    """tree: {relative-path: content}. Returns the uploads dir."""
    root = tmp_path / "content" / "uploads"
    for rel, text in tree.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


class TestUploads:
    def test_absent_section_does_nothing(self, tmp_path, capsys):
        _deploy(tmp_path).install_uploads()
        assert capsys.readouterr().out == ""

    def test_loose_files_go_to_primary_wiki(self, tmp_path, capsys):
        _make_uploads(tmp_path, {"logo.png": "x", "diagram.png": "y"})
        _deploy(tmp_path,
                content={"uploads": {"dir": "./content/uploads"}}).install_uploads()
        out = capsys.readouterr().out
        assert "2 file(s) to wiki 'main'" in out
        assert "importImages" in out
        assert "--wiki=main" in out
        assert "/mediawiki/images/.wicker-uploads/main" in out

    def test_subdirectory_targets_its_wiki(self, tmp_path, capsys):
        _make_uploads(tmp_path, {"top.png": "x", "docs/shot.png": "y"})
        d = _deploy(tmp_path,
                    wikis=[{"id": "main"}, {"id": "docs"}],
                    domain_name="localhost,localhost/docs",
                    content={"uploads": {"dir": "./content/uploads"}})
        d.install_uploads()
        out = capsys.readouterr().out
        assert "--wiki=main" in out          # loose top.png
        assert "--wiki=docs" in out          # docs/ subdir
        assert "/mediawiki/images/.wicker-uploads/docs" in out

    def test_unknown_subdirectory_is_skipped_with_warning(self, tmp_path, capsys):
        _make_uploads(tmp_path, {"ghost/x.png": "z"})
        _deploy(tmp_path,
                content={"uploads": {"dir": "./content/uploads"}}).install_uploads()
        cap = capsys.readouterr()
        assert "matches no wiki" in cap.err
        assert "importImages" not in cap.out

    def test_default_dir(self, tmp_path, capsys):
        _make_uploads(tmp_path, {"a.png": "x"})
        # uploads present with no dir -> ./content/uploads
        _deploy(tmp_path, content={"uploads": {}}).install_uploads()
        assert "--wiki=main" in capsys.readouterr().out

    def test_real_staging_copies_and_cleans_up(self, tmp_path, monkeypatch):
        _make_uploads(tmp_path, {"a.png": "x", "docs/b.png": "y"})
        d = _deploy(tmp_path, dry_run=False,
                    content={"uploads": {"dir": "./content/uploads"}})
        # Capture what would be imported without invoking canasta, and assert
        # the files were staged into the bind-mounted images/ dir at call time.
        seen = []

        def fake_exec(command, **kw):
            staged = d.instance_dir / "images" / ".wicker-uploads" / "main"
            seen.append(sorted(p.name for p in staged.iterdir()))

        monkeypatch.setattr(d, "_canasta_exec", fake_exec)
        d.install_uploads()
        assert seen == [["a.png"]]           # only the loose file for 'main'
        # staging dir is removed afterward
        assert not (d.instance_dir / "images" / ".wicker-uploads").exists()

    def test_k8s_is_skipped(self, tmp_path, capsys):
        _make_uploads(tmp_path, {"a.png": "x"})
        _deploy(tmp_path, orchestrator="k8s",
                content={"uploads": {"dir": "./content/uploads"}}).install_uploads()
        assert "Compose-only" in capsys.readouterr().err

    def test_missing_dir_warns(self, tmp_path, capsys):
        _deploy(tmp_path,
                content={"uploads": {"dir": "./nope"}}).install_uploads()
        assert "not found" in capsys.readouterr().err
