"""CLI-level tests: argv parsing via main(), secret redaction, and the
canasta command contract — every flag wicker passes to `canasta` must
exist in Canasta-CLI's command definitions (the bug class where a test
asserts a wrong command string and ships it)."""

import os
import shlex
from pathlib import Path

import pytest
import yaml

import wicker
from conftest import make_deploy, make_upgrade, minimal_manifest


def _app(tmp_path, **sections):
    m = minimal_manifest(**sections)
    d = tmp_path / "app"
    d.mkdir(exist_ok=True)
    (d / "wicker.yaml").write_text(yaml.safe_dump(m))
    return d


class TestMainArgv:

    def test_validate_ok(self, tmp_path, capsys):
        assert wicker.main(["validate", str(_app(tmp_path))]) == 0
        assert "OK:" in capsys.readouterr().out

    def test_deploy_requires_domain(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            wicker.main(["deploy", str(_app(tmp_path)), "-i", "t", "--dry-run"])
        assert e.value.code == 2

    def test_deploy_dry_run_creates(self, tmp_path, capsys):
        rc = wicker.main(["deploy", str(_app(tmp_path)), "-i", "t",
                          "-n", "localhost", "-p", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert "$ canasta create" in capsys.readouterr().out

    def test_upgrade_parses_without_n_o_p(self, tmp_path, capsys):
        rc = wicker.main(["upgrade", str(_app(tmp_path)), "-i", "t", "--dry-run"])
        assert rc == 1
        assert "error:" in capsys.readouterr().err

    def test_sync_code_defaults_shim(self, tmp_path, capsys):
        rc = wicker.main(["sync-code", str(_app(tmp_path)), "-i", "t"])
        assert rc == 1
        assert "instance directory not found" in capsys.readouterr().err

    def test_bad_orchestrator_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            wicker.main(["deploy", str(_app(tmp_path)), "-i", "t",
                         "-n", "x", "-o", "bogus", "--dry-run"])
        assert e.value.code == 2


class TestRunFailure:

    def test_nonzero_exit_raises_with_returncode(self):
        with pytest.raises(wicker.BuildError, match=r"command failed \(1\)"):
            wicker.run(["false"], dry_run=False)


class TestDeployPreflight:

    def test_missing_tool_is_listed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wicker, "have", lambda t: t != "docker")
        d = make_deploy(_app(tmp_path))
        with pytest.raises(wicker.BuildError, match="not in PATH: docker"):
            d.preflight()

    def test_existing_instance_dir_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wicker, "have", lambda t: True)

        class NoContainer:
            returncode = 1
        monkeypatch.setattr(wicker.subprocess, "run",
                            lambda *a, **k: NoContainer())
        d = make_deploy(_app(tmp_path), parent=tmp_path, dry_run=False)
        (tmp_path / "t").mkdir()
        with pytest.raises(wicker.BuildError, match="already exists"):
            d.preflight()


class TestWaitHealthy:

    def _deploy(self, tmp_path, monkeypatch, statuses):
        it = iter(statuses)

        class Probe:
            returncode = 0

            def __init__(self):
                self.stdout = next(it)
        monkeypatch.setattr(wicker.subprocess, "run",
                            lambda *a, **k: Probe())
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)
        return make_deploy(_app(tmp_path), parent=tmp_path, dry_run=False)

    def test_returns_once_healthy(self, tmp_path, monkeypatch):
        d = self._deploy(tmp_path, monkeypatch,
                         ["starting\n", "healthy\n"])
        d.wait_healthy(attempts=2)  # a third probe would raise StopIteration

    def test_timeout_warns_and_continues(self, tmp_path, monkeypatch, capsys):
        d = self._deploy(tmp_path, monkeypatch, ["starting\n"] * 2)
        d.wait_healthy(attempts=2)
        assert "did not report healthy within 6s" in capsys.readouterr().err


class TestSecretRedaction:

    def test_secret_value_never_echoed(self, tmp_path, capsys):
        app = _app(tmp_path, secrets=[{"name": "API_KEY", "target": "web"}])
        rc = wicker.main(["deploy", str(app), "-i", "t", "-n", "localhost",
                          "-p", str(tmp_path), "--dry-run",
                          "--secret", "API_KEY=hunter2secret"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "hunter2secret" not in out
        assert "***" in out


DEFS = Path(os.environ.get(
    "CANASTA_CLI_REPO",
    Path(__file__).resolve().parents[2] / "Canasta-CLI"
)) / "meta" / "command_definitions.yml"


@pytest.mark.skipif(not DEFS.is_file(),
                    reason="Canasta-CLI checkout not available")
class TestCanastaCommandContract:

    def _spec(self):
        cmds = yaml.safe_load(DEFS.read_text())["commands"]
        return {c["name"]: c for c in cmds}

    def _harvest(self, out):
        return [shlex.split(s.strip()[2:]) for s in out.splitlines()
                if s.strip().startswith("$ canasta ")]

    def _check(self, argvs):
        spec = self._spec()
        assert argvs, "no canasta commands harvested"
        for argv in argvs:
            words = argv[1:]
            # Tokens after a bare `--` belong to the remote command.
            if "--" in words:
                words = words[:words.index("--")]
            plain = []
            for w in words:
                if w.startswith("-"):
                    break
                plain.append(w)
            name = n_sub = None
            for k in range(min(3, len(plain)), 0, -1):
                cand = "_".join(plain[:k])
                if cand in spec:
                    name, n_sub = cand, k
                    break
            assert name, f"no command definition for: {' '.join(words[:3])}"
            params = spec[name].get("parameters", [])
            shorts = {p.get("short") for p in params if p.get("short")}
            # canasta derives long flags from the parameter name with
            # underscores as dashes (e.g. no_restart -> --no-restart).
            longs = {(p.get("long") or p["name"]).replace("_", "-")
                     for p in params}
            for tok in words[n_sub:]:
                if tok.startswith("--"):
                    flag = tok[2:].split("=", 1)[0]
                    assert flag in longs, \
                        f"canasta {name}: flag --{flag} not in the CLI spec"
                elif len(tok) == 2 and tok.startswith("-"):
                    assert tok[1] in shorts, \
                        f"canasta {name}: flag {tok} not in the CLI spec"

    def test_deploy_commands_match_spec(self, tmp_path, capsys):
        m = minimal_manifest(
            wikis=[{"id": "main"}, {"id": "docs"}],
            env={"HTTP_PORT": "8080"},
            sidecars=[{"name": "svc", "image": "img:1"}])
        app = tmp_path / "app"
        app.mkdir()
        d = make_deploy(app, m, parent=tmp_path,
                        domain_name="localhost,localhost/docs")
        d.canasta_create({})
        d.install_env()
        d.install_sidecars()
        d.canasta_restart()
        self._check(self._harvest(capsys.readouterr().out))

    def test_upgrade_commands_match_spec(self, tmp_path, capsys):
        old = minimal_manifest(
            version="1.0.0",
            wikis=[{"id": "main"}],
            sidecars=[{"name": "gone", "image": "x"}])
        new = minimal_manifest(
            version="2.0.0",
            wikis=[{"id": "main"}, {"id": "docs"}],
            sidecars=[{"name": "svc", "image": "img:1"}])
        app = tmp_path / "app"
        app.mkdir()
        up = make_upgrade(app, new, parent=tmp_path,
                          domain_name="localhost,localhost/docs")
        up.old_manifest = old
        up.old_version = "1.0.0"
        up.old_app = "app"
        up.backup()
        up.remove_old_sidecars()
        up.install_sidecars()
        up.canasta_restart()
        up.add_new_wikis()
        self._check(self._harvest(capsys.readouterr().out))

    def test_sidecar_remove_uses_name_flag(self, tmp_path, capsys):
        # Regression guard for the shipped positional-name bug.
        old = minimal_manifest(version="1.0.0",
                               sidecars=[{"name": "dropme", "image": "x"}])
        new = minimal_manifest(version="2.0.0")
        app = tmp_path / "app"
        app.mkdir()
        up = make_upgrade(app, new, parent=tmp_path)
        up.old_manifest = old
        up.old_version = "1.0.0"
        up.old_app = "app"
        up.remove_old_sidecars()
        out = capsys.readouterr().out
        assert "canasta sidecar remove -i t --name dropme" in out
        self._check(self._harvest(out))
