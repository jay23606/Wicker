"""Tests for declared secrets: target routing, validation, and delivery.

A secret is declared in the manifest (name + target); its value is supplied at
deploy time and routed by target — to a sidecar (referenced + envPrivate), to
the web tier's getenv(), or to the instance .env only. Values are never stored
in the manifest, and are redacted in logs.
"""

import pytest

from conftest import make_deploy as _make_deploy, minimal_manifest
import wicker


def make_deploy(tmp_path, manifest, secret=None, orchestrator="compose"):
    return _make_deploy(tmp_path, manifest, secret=secret,
                        orchestrator=orchestrator)


def manifest(secrets, sidecars=None):
    m = minimal_manifest(secrets=secrets)
    if sidecars is not None:
        m["sidecars"] = sidecars
    return m


class TestSecretHelpers:
    def test_targets_scalar_list_and_missing(self):
        assert wicker.secret_targets({"target": "web"}) == ["web"]
        assert wicker.secret_targets({"target": ["web", "env"]}) == ["web", "env"]
        assert wicker.secret_targets({}) == []

    def test_secrets_for_sidecar(self):
        decls = [{"name": "A", "target": ["sidecar:gen"]},
                 {"name": "B", "target": "web"},
                 {"name": "C", "target": ["sidecar:gen", "web"]}]
        assert wicker.secrets_for_sidecar(decls, "gen") == ["A", "C"]
        assert wicker.secrets_for_sidecar(decls, "other") == []

    def test_web_secret_names(self):
        decls = [{"name": "A", "target": ["sidecar:gen"]},
                 {"name": "B", "target": ["web"]},
                 {"name": "C", "target": ["sidecar:gen", "web"]}]
        assert wicker.web_secret_names(decls) == ["B", "C"]


class TestValidateSecretDecls:
    def test_ok(self):
        decls = [{"name": "A", "target": ["sidecar:gen", "web"]},
                 {"name": "B", "target": "env"}]
        assert wicker.validate_secret_decls(decls, {"gen"}) is None

    def test_no_target(self):
        err = wicker.validate_secret_decls([{"name": "A", "target": []}], set())
        assert "at least one target" in err

    def test_unknown_sidecar(self):
        err = wicker.validate_secret_decls(
            [{"name": "A", "target": ["sidecar:nope"]}], {"gen"})
        assert "unknown sidecar" in err

    def test_invalid_target(self):
        err = wicker.validate_secret_decls(
            [{"name": "A", "target": ["bogus"]}], set())
        assert "invalid target" in err


class TestInjectSidecarSecret:
    def test_adds_reference_and_private(self):
        d = {"name": "gen", "image": "x:1"}
        wicker.inject_sidecar_secret(d, "TOK")
        assert d["env"]["TOK"] == "${TOK}"
        assert d["envPrivate"] == ["TOK"]

    def test_preserves_author_env_and_is_idempotent(self):
        d = {"name": "gen", "image": "x:1",
             "env": {"TOK": "${TOK}", "OTHER": "v"}, "envPrivate": ["TOK"]}
        wicker.inject_sidecar_secret(d, "TOK")
        assert d["env"] == {"TOK": "${TOK}", "OTHER": "v"}  # not duplicated
        assert d["envPrivate"] == ["TOK"]


class TestMarkEnvSecret:
    def test_adds_and_is_idempotent(self):
        d = {"name": "gen", "image": "x:1"}
        wicker.mark_envsecret(d, "TOK")
        wicker.mark_envsecret(d, "TOK")
        assert d["envSecret"] == ["TOK"]

    def test_appends_to_existing(self):
        d = {"name": "gen", "image": "x:1", "envSecret": ["A"]}
        wicker.mark_envsecret(d, "B")
        assert d["envSecret"] == ["A", "B"]


class TestSecretValue:
    def test_override_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOK", "fromenv")
        d = make_deploy(tmp_path, manifest([{"name": "TOK", "target": "env"}]),
                        secret=["TOK=explicit"])
        assert d._secret_value("TOK") == "explicit"

    def test_bare_secret_reads_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOK", "fromenv")
        d = make_deploy(tmp_path, manifest([{"name": "TOK", "target": "env"}]),
                        secret=["TOK"])
        assert d._secret_value("TOK") == "fromenv"

    def test_unset_is_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOK", raising=False)
        d = make_deploy(tmp_path, manifest([{"name": "TOK", "target": "env"}]))
        assert d._secret_value("TOK") is None


class TestValidateSecrets:
    def test_missing_required_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOK", raising=False)
        d = make_deploy(tmp_path, manifest([{"name": "TOK", "target": "env"}]))
        with pytest.raises(wicker.BuildError) as e:
            d.validate_secrets()
        assert "TOK" in str(e.value)

    def test_optional_missing_ok(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOK", raising=False)
        d = make_deploy(tmp_path, manifest(
            [{"name": "TOK", "required": False, "target": "env"}]))
        d.validate_secrets()  # no raise

    def test_provided_ok(self, tmp_path):
        d = make_deploy(tmp_path, manifest([{"name": "TOK", "target": "env"}]),
                        secret=["TOK=v"])
        d.validate_secrets()  # no raise

    def test_unknown_sidecar_target_raises(self, tmp_path):
        d = make_deploy(tmp_path, manifest(
            [{"name": "TOK", "target": ["sidecar:nope"]}]), secret=["TOK=v"])
        with pytest.raises(wicker.BuildError) as e:
            d.validate_secrets()
        assert "unknown sidecar" in str(e.value)


class TestWebBridgeIncludesSecrets:
    def test_web_secret_reaches_bridge(self, tmp_path):
        m = manifest([{"name": "WTOK", "target": ["web"]},
                      {"name": "STOK", "target": ["sidecar:gen"]}],
                     sidecars=[{"name": "gen", "image": "x:1"}])
        d = make_deploy(tmp_path, m, secret=["WTOK=wv", "STOK=sv"])
        names = wicker.web_secret_names(d.secret_decls)
        # Only the web-targeted secret is bridged; the sidecar-only one is not.
        assert names == ["WTOK"]


class TestWebBridgeOrchestratorRouting:
    M = manifest([{"name": "WTOK", "target": ["web"]},
                  {"name": "ETOK", "target": ["env"]}])

    def test_compose_bridges_web_secret_value(self, tmp_path):
        d = make_deploy(tmp_path, self.M, secret=["WTOK=wv", "ETOK=ev"],
                        orchestrator="compose")
        bridged = d._web_bridge_env()
        assert bridged.get("WTOK") == "wv"   # web secret rides the bridge
        assert "ETOK" not in bridged          # env-only never bridged

    def test_k8s_excludes_web_secret_from_bridge(self, tmp_path):
        d = make_deploy(tmp_path, self.M, secret=["WTOK=wv", "ETOK=ev"],
                        orchestrator="kubernetes")
        bridged = d._web_bridge_env()
        # On k8s the web secret reaches the pod via secretKeyRef (--web), so it
        # must NOT appear in the cleartext settings fragment.
        assert "WTOK" not in bridged
        assert "ETOK" not in bridged

    def test_manifest_env_always_bridged(self, tmp_path):
        m = {"apiVersion": "wicker-app/v1", "name": "a", "version": "0.1.0",
             "description": "t", "wikis": [{"id": "main", "url": "localhost"}],
             "env": {"PLAIN": "v"}, "secrets": []}
        d = make_deploy(tmp_path, m, orchestrator="kubernetes")
        assert d._web_bridge_env() == {"PLAIN": "v"}


class TestSidecarSecretGating:
    """An optional sidecar-targeted secret left unset must be wired nowhere —
    a secretKeyRef to a never-written Secret key would fail the pod on k8s."""

    def _render(self, capsys, tmp_path, secret):
        m = manifest(
            [{"name": "TOK", "required": False, "target": ["sidecar:gen"]}],
            sidecars=[{"name": "gen", "image": "x:1"}])
        make_deploy(tmp_path, m, secret=secret,
                    orchestrator="kubernetes").install_sidecars()
        return capsys.readouterr().out

    def test_unset_optional_not_wired(self, capsys, tmp_path):
        out = self._render(capsys, tmp_path, secret=None)
        assert "TOK" not in out
        assert "envSecret" not in out

    def test_set_secret_is_wired(self, capsys, tmp_path):
        out = self._render(capsys, tmp_path, secret=["TOK=v"])
        assert "envSecret" in out
        assert "TOK" in out
