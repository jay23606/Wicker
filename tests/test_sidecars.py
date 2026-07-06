"""Tests for sidecar declaration passthrough: `files` staging, `depends_on`,
and the warn-and-drop fields (sysctls, sidecar-level secrets).

A sidecar `files` source is declared manifest-relative but Canasta resolves
it against the instance dir, so Wicker stages each file into the instance
and rewrites `source`.
"""

from conftest import make_deploy, minimal_manifest
import wicker


def _manifest(sidecar):
    return minimal_manifest(sidecars=[sidecar])


class TestSidecarFiles:
    SC = {"name": "gen", "image": "x:1",
          "files": [{"source": "./sidecars/gen/app.toml",
                     "mountPath": "/etc/app.toml"},
                    {"source": "./other/app.toml",
                     "mountPath": "/etc/other.toml", "readOnly": False}]}

    def test_staging_copies_and_rewrites_source(self, tmp_path):
        for rel in ("sidecars/gen/app.toml", "other/app.toml"):
            src = tmp_path / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("k=1\n")
        d = make_deploy(tmp_path, _manifest(self.SC),
                        parent=tmp_path / "instances", dry_run=False)
        staged = d._stage_sidecar_files(self.SC)
        assert staged == [
            {"source": "sidecars/gen-files/0-app.toml",
             "mountPath": "/etc/app.toml"},
            {"source": "sidecars/gen-files/1-app.toml",
             "mountPath": "/etc/other.toml", "readOnly": False},
        ]
        for entry in staged:
            assert (d.instance_dir / entry["source"]).read_text() == "k=1\n"

    def test_declared_files_are_rewritten_in_sidecar_yaml(self, tmp_path, capsys):
        d = make_deploy(tmp_path, _manifest(self.SC))
        d.install_sidecars()
        out = capsys.readouterr().out
        assert "source: sidecars/gen-files/0-app.toml" in out
        assert "source: ./sidecars/gen/app.toml" not in out


class TestPassthroughAndDrops:
    def test_depends_on_passes_through(self, tmp_path, capsys):
        sc = {"name": "worker", "image": "x:1", "depends_on": ["gen"]}
        make_deploy(tmp_path, _manifest(sc)).install_sidecars()
        assert "depends_on" in capsys.readouterr().out

    def test_sysctls_and_secrets_warn_and_drop(self, tmp_path, capsys):
        sc = {"name": "es", "image": "x:1",
              "sysctls": {"vm.max_map_count": "262144"},
              "secrets": ["TOK"]}
        make_deploy(tmp_path, _manifest(sc)).install_sidecars()
        cap = capsys.readouterr()
        assert "sysctls is not supported" in cap.err
        assert "secrets is not supported" in cap.err
        assert "sysctls" not in cap.out
        assert "vm.max_map_count" not in cap.out


class TestStagingCleanup:
    def test_dry_run_deploy_leaves_no_temp_dirs(self, tmp_path, monkeypatch,
                                                capsys):
        tmp = tmp_path / "tmpdir"
        tmp.mkdir()
        monkeypatch.setattr(wicker.tempfile, "tempdir", str(tmp))
        monkeypatch.setattr(wicker, "have", lambda t: True)
        d = make_deploy(tmp_path, _manifest({"name": "gen", "image": "x:1"}),
                        parent=tmp_path / "instances")
        d.run_pipeline()
        assert "canasta sidecar add" in capsys.readouterr().out
        assert list(tmp.iterdir()) == []
