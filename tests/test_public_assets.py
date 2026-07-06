"""Tests for publicAssets — a directory copied verbatim into the instance's
public_assets/. On Compose it is bind-mounted into the web tier; on k8s
`canasta restart` mirrors the instance dir into the public-assets PVC."""

from conftest import make_deploy, minimal_manifest


def _deploy(tmp_path, dry_run=True, orchestrator="compose", **sections):
    return make_deploy(tmp_path, minimal_manifest(**sections),
                       dry_run=dry_run, orchestrator=orchestrator)


def _make_assets(tmp_path):
    src = tmp_path / "public_assets"
    (src / "main").mkdir(parents=True)
    (src / "robots.txt").write_text("User-agent: *\n")
    (src / "main" / "logo.png").write_text("PNG")
    return src


class TestPublicAssets:
    def test_absent_section_does_nothing(self, tmp_path, capsys):
        _deploy(tmp_path).install_public_assets()
        assert capsys.readouterr().out == ""

    def test_dry_run_prints_copy(self, tmp_path, capsys):
        _make_assets(tmp_path)
        _deploy(tmp_path, publicAssets="./public_assets").install_public_assets()
        assert "public_assets" in capsys.readouterr().out

    def test_real_copy_merges_tree(self, tmp_path):
        _make_assets(tmp_path)
        d = _deploy(tmp_path, dry_run=False, publicAssets="./public_assets")
        d.install_public_assets()
        dest = d.instance_dir / "public_assets"
        assert (dest / "robots.txt").read_text() == "User-agent: *\n"
        assert (dest / "main" / "logo.png").read_text() == "PNG"

    def test_real_copy_into_existing_dir(self, tmp_path):
        _make_assets(tmp_path)
        d = _deploy(tmp_path, dry_run=False, publicAssets="./public_assets")
        # canasta create would have made public_assets/ already
        (d.instance_dir / "public_assets").mkdir(parents=True)
        d.install_public_assets()
        assert (d.instance_dir / "public_assets" / "robots.txt").is_file()

    def test_k8s_copies_into_instance_dir(self, tmp_path):
        # On k8s the files are staged into the instance dir just like Compose;
        # `canasta restart` then mirrors them into the public-assets PVC.
        _make_assets(tmp_path)
        d = _deploy(tmp_path, dry_run=False, orchestrator="k8s",
                    publicAssets="./public_assets")
        d.install_public_assets()
        assert (d.instance_dir / "public_assets" / "robots.txt").is_file()

    def test_missing_dir_warns(self, tmp_path, capsys):
        _deploy(tmp_path, publicAssets="./nope").install_public_assets()
        assert "not a directory" in capsys.readouterr().err
