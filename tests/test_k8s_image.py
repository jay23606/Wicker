"""Tests for derived web-image delivery across orchestrators.

On Compose the web image is built locally and passed via `--canasta-image`.
On k8s a controller-local tag is unpullable by the cluster, so the web image's
context is staged as a `canasta create --build-from` source tree and Canasta
builds + pushes it to the in-cluster registry instead.
"""

import pytest

from conftest import make_deploy, make_upgrade, minimal_manifest
import wicker


def _deploy(app_dir, orchestrator, images, *, dry_run=True):
    return make_deploy(app_dir, minimal_manifest(images=images), id="app",
                       orchestrator=orchestrator, dry_run=dry_run)


class TestCanastaCreateImageFlags:
    def test_k8s_web_image_uses_build_from(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s", {"web": {"dockerfile": "./Dockerfile"}})
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--build-from" in out
        assert "--canasta-image" not in out

    def test_compose_web_image_uses_canasta_image(self, tmp_path, capsys):
        d = _deploy(tmp_path, "compose", {"web": {"dockerfile": "./Dockerfile"}})
        d.canasta_create(images={"web": "myimg:local"})
        out = capsys.readouterr().out
        assert "--canasta-image myimg:local" in out
        assert "--build-from" not in out

    def test_k8s_without_web_image_passes_neither(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s", {})
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--build-from" not in out
        assert "--canasta-image" not in out


class TestStageWebBuildFrom:
    def test_stages_dockerfile_under_canasta_subdir(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ghcr.io/canastawiki/canasta:latest\n")
        d = _deploy(tmp_path, "k8s", {"web": {"dockerfile": "./Dockerfile"}},
                    dry_run=False)
        root = d._stage_web_build_from({"dockerfile": "./Dockerfile"})
        staged = root / "Canasta" / "Dockerfile"
        assert staged.is_file()
        assert "canasta:latest" in staged.read_text()

    def test_explicit_context_is_copied(self, tmp_path):
        ctx = tmp_path / "images" / "web"
        ctx.mkdir(parents=True)
        (ctx / "Dockerfile").write_text("FROM scratch\n")
        (ctx / "extra.ini").write_text("x=1\n")
        d = _deploy(tmp_path, "k8s",
                    {"web": {"dockerfile": "./images/web/Dockerfile",
                             "context": "./images/web"}}, dry_run=False)
        root = d._stage_web_build_from(
            {"dockerfile": "./images/web/Dockerfile", "context": "./images/web"})
        assert (root / "Canasta" / "Dockerfile").is_file()
        assert (root / "Canasta" / "extra.ini").is_file()

    def test_staged_tree_registered_and_removed_by_cleanup(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        d = _deploy(tmp_path, "k8s", {"web": {"dockerfile": "./Dockerfile"}},
                    dry_run=False)
        root = d._stage_web_build_from({"dockerfile": "./Dockerfile"})
        assert root in d._staging_dirs
        assert (root / "Canasta" / "Dockerfile").is_file()
        d._cleanup_staging()
        assert not root.exists()
        assert d._staging_dirs == []


class TestDerivedTag:
    def test_semver_build_metadata_is_tag_safe(self, tmp_path):
        m = minimal_manifest(version="1.0.0+build.1")
        d = make_deploy(tmp_path, m, orchestrator="compose")
        # '+' is legal semver but illegal in a Docker tag.
        assert d._derived_tag("web") == "app-web:1.0.0-build.1"


class TestBuildImagesK8s:
    def test_k8s_skips_local_web_build(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s", {"web": {"dockerfile": "./Dockerfile"}})
        built = d.build_images()
        out = capsys.readouterr().out
        assert built == {}            # nothing built locally on k8s
        assert "docker build" not in out

    def test_k8s_delivers_elasticsearch_via_image_push(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s",
                    {"elasticsearch": {"dockerfile": "./Dockerfile"}})
        built = d.build_images()
        out = capsys.readouterr().out
        assert built == {
            "elasticsearch": "10.43.0.2:5000/app-elasticsearch:0.1.0"}
        assert "canasta image push" in out
        assert "--name app-elasticsearch:0.1.0" in out
        # The registry ref then rides the same config set as on Compose.
        d.install_elasticsearch_image(built)
        assert ("CANASTA_ELASTICSEARCH_IMAGE="
                "10.43.0.2:5000/app-elasticsearch:0.1.0"
                in capsys.readouterr().out)

    def test_k8s_skip_build_skips_the_push_but_keeps_the_ref(self, tmp_path,
                                                             capsys):
        d = make_deploy(
            tmp_path,
            minimal_manifest(
                images={"elasticsearch": {"dockerfile": "./Dockerfile"}}),
            id="app", orchestrator="k8s", skip_build=True)
        built = d.build_images()
        assert built == {
            "elasticsearch": "10.43.0.2:5000/app-elasticsearch:0.1.0"}
        assert "canasta image push" not in capsys.readouterr().out

    def test_k8s_es_offroot_dockerfile_is_staged(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s",
                    {"elasticsearch": {"dockerfile": "./docker/es.Dockerfile",
                                       "context": "./es"}})
        d.build_images()
        out = capsys.readouterr().out
        # The push targets a staged copy of the context with the manifest's
        # Dockerfile placed at its root, not the original context.
        assert "Staging elasticsearch image context for push" in out
        push = [ln for ln in out.splitlines()
                if ln.strip().startswith("$ canasta image push")][0]
        assert "/es " not in push
        assert "wicker-imagepush-" in push

    def test_k8s_es_base_image_forwards_build_arg(self, tmp_path, capsys):
        d = _deploy(tmp_path, "k8s",
                    {"elasticsearch": {"dockerfile": "./Dockerfile",
                                       "baseImage": "es:custom"}})
        d.build_images()
        assert "--build-arg BASE_IMAGE=es:custom" in capsys.readouterr().out

    def test_k8s_upgrade_applies_es_registry_ref(self, tmp_path, capsys):
        m = minimal_manifest(
            images={"elasticsearch": {"dockerfile": "./Dockerfile"}})
        u = make_upgrade(tmp_path, m, id="app", orchestrator="k8s")
        u.apply_images(
            {"elasticsearch": "10.43.0.2:5000/app-elasticsearch:0.1.0"})
        assert ("CANASTA_ELASTICSEARCH_IMAGE="
                "10.43.0.2:5000/app-elasticsearch:0.1.0"
                in capsys.readouterr().out)

    def test_k8s_upgrade_pushes_and_applies_web_overlay(self, tmp_path,
                                                        capsys):
        m = minimal_manifest(images={"web": {"dockerfile": "./Dockerfile"}})
        u = make_upgrade(tmp_path, m, id="app", orchestrator="k8s")
        u.apply_images({})
        out = capsys.readouterr().out
        assert "canasta image push" in out
        assert "--name app-web:0.1.0" in out
        # An overlay must layer on the full canasta image, so the resolved
        # base is always passed even when the manifest sets none.
        assert f"--build-arg BASE_IMAGE={wicker.DEFAULT_CANASTA_IMAGE}" in out
        assert "CANASTA_IMAGE=10.43.0.2:5000/app-web:0.1.0" in out

    def test_k8s_upgrade_without_web_overlay_sets_no_web_image(self, tmp_path,
                                                               capsys):
        u = make_upgrade(tmp_path, minimal_manifest(), id="app",
                         orchestrator="k8s")
        u.apply_images({})
        assert "CANASTA_IMAGE=" not in capsys.readouterr().out


class TestElasticsearchImage:
    """images.elasticsearch is delivered on Compose by pointing the
    instance's CANASTA_ELASTICSEARCH_IMAGE at the built tag; an explicit
    env entry for the key wins. Services outside Canasta's two image
    override points (web, elasticsearch) are rejected."""

    def test_unsupported_service_is_skipped_with_warning(self, tmp_path, capsys):
        d = _deploy(tmp_path, "compose", {"db": {"dockerfile": "./Dockerfile"}})
        built = d.build_images()
        err = capsys.readouterr().err
        assert built == {}
        assert "'web' and 'elasticsearch' only" in err

    def test_unsupported_service_fails_validation(self):
        m = minimal_manifest(images={"db": {"dockerfile": "./Dockerfile"}})
        with pytest.raises(wicker.BuildError, match="schema validation"):
            wicker.validate_manifest(m)

    def test_compose_delivers_built_tag(self, tmp_path, capsys):
        d = _deploy(tmp_path, "compose",
                    {"elasticsearch": {"dockerfile": "./Dockerfile"}})
        built = d.build_images()
        assert built == {"elasticsearch": "app-elasticsearch:0.1.0"}
        d.install_elasticsearch_image(built)
        out = capsys.readouterr().out
        assert ("config set -i app --no-restart "
                "CANASTA_ELASTICSEARCH_IMAGE=app-elasticsearch:0.1.0") in out

    def test_manifest_env_wins_over_built_tag(self, tmp_path, capsys):
        m = minimal_manifest(
            images={"elasticsearch": {"dockerfile": "./Dockerfile"}},
            env={"CANASTA_ELASTICSEARCH_IMAGE": "custom-es:1"})
        d = make_deploy(tmp_path, m, id="app")
        d.install_elasticsearch_image({"elasticsearch": "app-elasticsearch:0.1.0"})
        captured = capsys.readouterr()
        assert "overrides the built images.elasticsearch tag" in captured.err
        assert "config set" not in captured.out

    def test_no_es_image_is_a_noop(self, tmp_path, capsys):
        d = _deploy(tmp_path, "compose", {"web": {"dockerfile": "./Dockerfile"}})
        d.install_elasticsearch_image({"web": "app-web:0.1.0"})
        assert "CANASTA_ELASTICSEARCH_IMAGE" not in capsys.readouterr().out

    def test_upgrade_applies_es_image(self, tmp_path, capsys):
        m = minimal_manifest(
            images={"elasticsearch": {"dockerfile": "./Dockerfile"}})
        u = make_upgrade(tmp_path, m, id="app")
        u.apply_images({"elasticsearch": "app-elasticsearch:0.1.0"})
        assert ("CANASTA_ELASTICSEARCH_IMAGE=app-elasticsearch:0.1.0"
                in capsys.readouterr().out)


class TestBaseImageOverride:
    """--base-image / images.<svc>.baseImage forward a BASE_IMAGE build-arg so
    an app can be deployed against a custom/unreleased Canasta image."""

    def _web(self, tmp_path, orch, spec, base_image=None):
        return make_deploy(tmp_path, minimal_manifest(images={"web": spec}),
                           orchestrator=orch, base_image=base_image)

    def test_compose_cli_flag_forwards_build_arg(self, tmp_path, capsys):
        d = self._web(tmp_path, "compose", {"dockerfile": "./Dockerfile"},
                      base_image="canasta:local")
        d.build_images()
        assert "--build-arg BASE_IMAGE=canasta:local" in capsys.readouterr().out

    def test_k8s_cli_flag_forwards_build_arg(self, tmp_path, capsys):
        d = self._web(tmp_path, "k8s", {"dockerfile": "./Dockerfile"},
                      base_image="canasta:local")
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--build-from" in out
        assert "--build-arg BASE_IMAGE=canasta:local" in out

    def test_manifest_base_image_used_without_cli_flag(self, tmp_path, capsys):
        d = self._web(tmp_path, "compose",
                      {"dockerfile": "./Dockerfile", "baseImage": "canasta:manifest"})
        d.build_images()
        assert "--build-arg BASE_IMAGE=canasta:manifest" in capsys.readouterr().out

    def test_cli_flag_overrides_manifest_base_image(self, tmp_path, capsys):
        d = self._web(tmp_path, "compose",
                      {"dockerfile": "./Dockerfile", "baseImage": "canasta:manifest"},
                      base_image="canasta:cli")
        out = capsys.readouterr().out  # drain
        d.build_images()
        out = capsys.readouterr().out
        assert "--build-arg BASE_IMAGE=canasta:cli" in out
        assert "canasta:manifest" not in out

    def test_no_base_image_means_no_build_arg(self, tmp_path, capsys):
        # Compose builds the overlay locally, so an omitted base lets the
        # Dockerfile's own `ARG BASE_IMAGE` default apply — no build-arg needed.
        d = self._web(tmp_path, "compose", {"dockerfile": "./Dockerfile"})
        d.build_images()
        assert "BASE_IMAGE" not in capsys.readouterr().out

    def test_k8s_overlay_defaults_base_to_full_canasta_image(self, tmp_path, capsys):
        # On k8s the overlay is built via `canasta create --build-from`, whose
        # base defaults to canasta-base (no bundled extensions). With no base
        # given, Wicker must still pass an explicit BASE_IMAGE pointing at the
        # full canasta image, or the wiki fatals on the first bundled extension.
        d = self._web(tmp_path, "k8s", {"dockerfile": "./Dockerfile"})
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--build-from" in out
        assert f"--build-arg BASE_IMAGE={wicker.DEFAULT_CANASTA_IMAGE}" in out


class TestBaseImageNoOverlay:
    """Apps without a web overlay run the custom image directly, via
    canasta create --canasta-image."""

    def test_compose_no_overlay_passes_canasta_image(self, tmp_path, capsys):
        d = make_deploy(tmp_path, minimal_manifest(), orchestrator="compose",
                        base_image="canasta:local")
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--canasta-image canasta:local" in out
        assert "--build-arg" not in out

    def test_k8s_no_overlay_registry_ref_passes_canasta_image(self, tmp_path, capsys):
        d = make_deploy(tmp_path, minimal_manifest(), orchestrator="k8s",
                        base_image="10.43.0.2:5000/canasta:local")
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--canasta-image 10.43.0.2:5000/canasta:local" in out
        assert "--build-from" not in out

    def test_k8s_no_overlay_local_tag_is_rejected(self, tmp_path):
        import pytest
        import wicker
        d = make_deploy(tmp_path, minimal_manifest(), orchestrator="k8s",
                        base_image="canasta:local")
        with pytest.raises(wicker.BuildError, match="local-only tag"):
            d.canasta_create(images={})

    def test_no_base_image_no_overlay_passes_no_image_flag(self, tmp_path, capsys):
        d = make_deploy(tmp_path, minimal_manifest(), orchestrator="compose")
        d.canasta_create(images={})
        out = capsys.readouterr().out
        assert "--canasta-image" not in out


class TestIsRegistryQualified:
    def test_local_tag_is_not_qualified(self):
        import wicker
        assert not wicker.is_registry_qualified("canasta:local")

    def test_hostport_ref_is_qualified(self):
        import wicker
        assert wicker.is_registry_qualified("10.43.0.2:5000/canasta:local")

    def test_dotted_host_ref_is_qualified(self):
        import wicker
        assert wicker.is_registry_qualified("ghcr.io/canastawiki/canasta:latest")

    def test_localhost_ref_is_qualified(self):
        import wicker
        assert wicker.is_registry_qualified("localhost:5000/canasta:local")
