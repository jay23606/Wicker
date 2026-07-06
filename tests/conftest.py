"""Shared test helpers: a minimal manifest and a Deploy factory.

Every deploy-flag added to the CLI lands in one place (make_deploy) instead
of a hand-rolled argparse.Namespace per test file.
"""

import argparse
import os
import sys

import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import wicker  # noqa: E402


def minimal_manifest(**sections):
    """A schema-valid minimal manifest, with extra sections merged in. Includes
    the required primary wiki (override by passing `wikis`). Domains are not in
    the manifest — they are supplied at deploy time (see make_deploy's
    `domain_name`)."""
    m = {"apiVersion": "wicker-app/v1", "name": "app", "version": "0.1.0",
         "description": "test app",
         "wikis": [{"id": "main"}]}
    m.update(sections)
    return m


def make_deploy(app_dir, manifest=None, *, id="t", orchestrator="compose",
                dry_run=True, secret=None, var=None, skip_build=False,
                parent=None, base_image=None, domain_name="localhost"):
    """Build a Deploy the way main() would. `parent` (default: app_dir) is
    the -p parent directory, so instance_dir = parent/<id>. `domain_name` is
    the -n value (comma-separated, one per wiki in the manifest)."""
    args = argparse.Namespace(
        id=id, path=str(parent or app_dir), domain_name=domain_name,
        dry_run=dry_run, skip_build=skip_build, var=var, secret=secret,
        orchestrator=orchestrator, storage_class=None, access_mode=None,
        ingress_class=None, skip_tls=False, base_image=base_image)
    return wicker.Deploy(manifest or minimal_manifest(), Path(app_dir), args)


_UNSET = object()


def make_upgrade(app_dir, manifest=None, *, id="t", orchestrator="compose",
                 dry_run=True, secret=None, var=None, skip_build=False,
                 parent=None, base_image=None, baseline=None,
                 skip_backup=False, report_only=False, domain_name="localhost",
                 path=_UNSET):
    """Build an Upgrade the way main() would (see make_deploy). `path` is
    the raw -p value; default derives it from `parent`/`app_dir`, pass
    None to simulate omitting -p."""
    args = argparse.Namespace(
        id=id,
        path=(str(parent or app_dir) if path is _UNSET else path),
        domain_name=domain_name,
        dry_run=dry_run, skip_build=skip_build, var=var, secret=secret,
        orchestrator=orchestrator, storage_class=None, access_mode=None,
        ingress_class=None, skip_tls=False, base_image=base_image,
        baseline=baseline, skip_backup=skip_backup, report_only=report_only)
    return wicker.Upgrade(manifest or minimal_manifest(), Path(app_dir), args)


@pytest.fixture(autouse=True)
def _isolated_canasta_registry(tmp_path_factory, monkeypatch):
    """Point $CANASTA_CONFIG_DIR at an empty per-test dir so nothing ever
    reads the host's real canasta registry."""
    monkeypatch.setenv(
        "CANASTA_CONFIG_DIR", str(tmp_path_factory.mktemp("canasta-config")))
