"""The sidecar `shell` postInstall action routes through `canasta maintenance
exec --service` (orchestrator-agnostic + remote-capable), not raw `docker exec`
by the Compose container name (which is local-Compose-only)."""

from conftest import make_deploy


def _deploy(orchestrator="compose"):
    return make_deploy(".", id="rs", parent="/tmp/x", orchestrator=orchestrator)


class TestSidecarShellAction:
    def _run(self, capsys, orchestrator):
        _deploy(orchestrator)._action_shell(
            {"kind": "shell", "container": "sidecar:gen",
             "command": "echo hi && true"})
        return capsys.readouterr().out

    def test_routes_through_canasta_not_docker(self, capsys):
        out = self._run(capsys, "compose")
        assert "docker exec" not in out
        assert "canasta maintenance exec -i rs --service gen" in out
        assert "sh -c" in out  # shell semantics preserved (&& must work)

    def test_identical_command_on_k8s(self, capsys):
        # Wicker emits the same canasta call on both orchestrators; canasta
        # resolves the sidecar service per orchestrator.
        assert "--service gen" in self._run(capsys, "kubernetes")
