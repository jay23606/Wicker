#!/usr/bin/env python3
"""Wicker — the Canasta wiki application builder.

Reads a wicker.yaml manifest and stands the application up on a fresh
Canasta instance by driving the existing `canasta` CLI (create, restart,
maintenance exec) plus `docker` for image builds.

This is the declarative replacement for an app's hand-rolled install
script: deploy creates the instance greenfield, upgrade moves a deployed
instance to the manifest's version, and sync-code refreshes extension /
skin code in place — all on both Compose and Kubernetes.

Usage:
    wicker init <PATH> [--name <name>] [--description <text>]
    wicker deploy <APP-PATH> -i <id> -n <domain[,domain...]> [-p <parent-path>]
                 [-o compose|kubernetes]
                 [--var KEY=VALUE ...] [--secret KEY[=VALUE] ...]
                 [--skip-build] [--dry-run]
    wicker upgrade <APP-PATH> -i <id> [-n <domain[,domain...]>]
                 [--baseline <old-app-dir>] [--skip-backup] [--report-only]
    wicker sync-code <APP-PATH> -i <id>
    wicker validate <APP-PATH>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required — install it for this python, e.g. "
             f"{sys.executable} -m pip install -r "
             f"{Path(__file__).resolve().parent / 'requirements.txt'} "
             "(or sudo apt install python3-yaml)")

MANIFEST_NAME = "wicker.yaml"
# Per-instance app state (version, manifest copy, imported-page snapshot),
# written at the end of deploy. `wicker upgrade` reads it as the pristine/
# merge base for shipped pages.
STATE_DIR_NAME = ".wicker-app"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "wicker-app.v1.schema.json"
SKELETON_PATH = Path(__file__).resolve().parent / "skeleton"

# App-name pattern (kebab-case), kept in sync with the schema's `name`.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Where MediaWiki and its maintenance runner live inside the web container.
MW_ROOT = "/var/www/mediawiki/w"
MW_RUN = f"php {MW_ROOT}/maintenance/run.php"

# The full Canasta image (MediaWiki + all bundled extensions/skins) a web
# overlay layers on top of. Used as the default BASE_IMAGE when a k8s overlay
# is built via `canasta create --build-from` and no base is otherwise given —
# without it the CLI falls back to canasta-base (no bundled extensions), which
# yields a wiki that fatals on the first wfLoadExtension of a bundled ext.
# Fallback only: default_canasta_image() pins the Canasta version the installed
# CLI targets, and uses this :latest tag when that version cannot be read.
DEFAULT_CANASTA_IMAGE = "ghcr.io/canastawiki/canasta:latest"

# Canasta exposes image overrides for exactly two services: web
# (CANASTA_IMAGE / --canasta-image) and elasticsearch
# (CANASTA_ELASTICSEARCH_IMAGE). Every other core container runs stock, and
# additional containers belong in `sidecars:`.
SUPPORTED_IMAGE_SERVICES = ("web", "elasticsearch")
ELASTICSEARCH_IMAGE_KEY = "CANASTA_ELASTICSEARCH_IMAGE"

# Pull-side address of Canasta's in-cluster image registry (a fixed
# ClusterIP, part of the registry's contract): pods pull what
# `canasta image push` pushed under this prefix.
K8S_REGISTRY_PULL = "10.43.0.2:5000"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

class BuildError(Exception):
    """A deploy step failed; message is operator-facing."""


def info(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  WARN {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, dry_run: bool,
        capture: bool = False,
        show: str | None = None,
        check: bool = True) -> subprocess.CompletedProcess | None:
    """Run a command, echoing it. Honors --dry-run. Raises on non-zero unless
    `check=False` (the caller inspects returncode itself).
    Pass `show` to echo a redacted form instead of the real command (secrets)."""
    shown = show if show is not None else " ".join(_quote(c) for c in cmd)
    print(f"  $ {shown}", flush=True)
    if dry_run:
        return None
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True if capture else False,
    )
    if not check:
        return result
    if result.returncode != 0:
        detail = ""
        if capture and result.stderr:
            detail = f"\n{result.stderr.strip()}"
        raise BuildError(f"command failed ({result.returncode}): {shown}{detail}")
    return result


def _quote(s: str) -> str:
    return s if all(c.isalnum() or c in "-_=:/.@," for c in s) else f'"{s}"'


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


import contextlib


@contextlib.contextmanager
def tmp_text_file(text: str, suffix: str):
    """A temporary file holding `text`, removed when the block exits."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(name)
    try:
        path.write_text(text)
        yield path
    finally:
        path.unlink(missing_ok=True)


def is_registry_qualified(ref: str) -> bool:
    """True if an image ref names a registry host (host[:port]/…), so a
    remote k8s cluster can pull it. A bare name[:tag] like 'canasta:local'
    is local-only and unpullable by the cluster."""
    if "/" not in ref:
        return False
    host = ref.split("/", 1)[0]
    return "." in host or ":" in host or host == "localhost"


# --------------------------------------------------------------------------- #
# Instance env plumbing
#
# Sidecars themselves are rendered and deployed by Canasta (the
# `canasta sidecar` primitive); Wicker's part is the env plumbing around
# them: resolving ${VAR} references from the deploy environment and
# re-exporting instance env to the web tier's getenv().
# --------------------------------------------------------------------------- #

# ${VAR}, ${VAR:-default}, ${VAR-default} — resolved from the deploy
# environment at render time. Compose interpolates these itself, but k8s
# passes env values verbatim, so resolving here keeps the SAME value on both
# orchestrators (e.g. a web<->sidecar shared secret).
_ENV_REF = re.compile(r"\$\{([A-Za-z_]\w*)(:-|-)?([^}]*)\}")


def resolve_env_value(v):
    if not isinstance(v, str):
        return v

    def repl(m):
        name, op, default = m.group(1), m.group(2), m.group(3)
        val = os.environ.get(name)
        if op == ":-":          # unset OR empty -> default
            return val if val else default
        if op == "-":           # unset -> default (empty kept)
            return val if val is not None else default
        return val if val is not None else ""

    return _ENV_REF.sub(repl, v)


def render_env_bridge_php(resolved: dict) -> str:
    """Render a global settings fragment that re-exports the instance env to
    the web tier's getenv().

    The web/jobrunner pod environment is a curated allowlist on Compose AND
    Kubernetes (Canasta #789), so app keys written to .env don't reach the web
    container's getenv(). This fragment putenv()s them back; named with a 00-
    prefix it loads first (Canasta sorts config/settings/global/*.php) so later
    app fragments can read the values."""
    lines = [
        "<?php",
        "// Generated by Wicker — do not edit.",
        "// Re-export the instance env so the web tier's getenv() sees it;",
        "// the web pod env is a curated allowlist on both orchestrators",
        "// (Canasta #789). Loaded first via the 00- prefix.",
        "",
    ]
    for k, v in resolved.items():
        ev = str(v).replace("\\", "\\\\").replace("'", "\\'")
        lines += [f"putenv('{k}={ev}');",
                  f"$_ENV['{k}'] = '{ev}';",
                  f"$_SERVER['{k}'] = '{ev}';"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Secrets
#
# A secret is declared in the manifest (name + target) and its VALUE is
# supplied at deploy time (`--secret KEY=VALUE`, or the environment), written
# to the instance .env (gitignored), and routed by target:
#   web            -> re-exported to the MediaWiki getenv() (the web env bridge)
#   env            -> the instance .env only (maintenance container, ${VAR})
#   sidecar:<name> -> referenced as ${NAME} in that sidecar's env and marked
#                     envPrivate, so Canasta delivers it to the sidecar but
#                     withholds it from the web tier.
# Values are never stored in the manifest, and are redacted in logs.
# --------------------------------------------------------------------------- #

def secret_targets(decl: dict) -> list[str]:
    """A secret's declared targets as a list (accepts a scalar or a list)."""
    tgt = decl.get("target")
    if tgt is None:
        return []
    return [tgt] if isinstance(tgt, str) else list(tgt)


def secrets_for_sidecar(decls: list[dict], name: str) -> list[str]:
    """Names of secrets routed to the named sidecar."""
    return [d["name"] for d in decls if f"sidecar:{name}" in secret_targets(d)]


def web_secret_names(decls: list[dict]) -> list[str]:
    """Names of secrets that should reach the web tier's getenv()."""
    return [d["name"] for d in decls if "web" in secret_targets(d)]


def validate_secret_decls(decls: list[dict], sidecar_names: set[str]) -> str | None:
    """Return an error string if a secret declares no target, an unknown
    target kind, or a `sidecar:<name>` that the manifest does not define;
    else None."""
    for d in decls:
        targets = secret_targets(d)
        if not targets:
            return f"secret '{d['name']}' must declare at least one target"
        for tgt in targets:
            if tgt in ("web", "env"):
                continue
            if tgt.startswith("sidecar:"):
                sc = tgt.split(":", 1)[1]
                if sc not in sidecar_names:
                    return (f"secret '{d['name']}' targets unknown sidecar "
                            f"'{sc}'")
                continue
            return f"secret '{d['name']}' has invalid target '{tgt}'"
    return None


def inject_sidecar_secret(sidecar_def: dict, name: str) -> None:
    """Reference a secret as ${NAME} in a sidecar's env (unless the author
    already references it) and mark it envPrivate — delivered to the sidecar,
    withheld from the web bridge. Idempotent."""
    env = dict(sidecar_def.get("env") or {})
    env.setdefault(name, "${%s}" % name)
    sidecar_def["env"] = env
    private = list(sidecar_def.get("envPrivate") or [])
    if name not in private:
        private.append(name)
    sidecar_def["envPrivate"] = private


def mark_envsecret(sidecar_def: dict, name: str) -> None:
    """Mark a sidecar env key as sourced from the per-instance k8s app Secret
    (Canasta renders it as a secretKeyRef instead of a cleartext value).
    Idempotent. k8s only — the Compose renderer ignores envSecret."""
    keys = list(sidecar_def.get("envSecret") or [])
    if name not in keys:
        keys.append(name)
    sidecar_def["envSecret"] = keys


# --------------------------------------------------------------------------- #
# Manifest loading & validation
# --------------------------------------------------------------------------- #

def load_manifest(app_path: Path) -> tuple[dict, Path]:
    """Return (manifest dict, manifest directory)."""
    if app_path.is_dir():
        manifest_file = app_path / MANIFEST_NAME
    else:
        manifest_file = app_path
    if not manifest_file.is_file():
        raise BuildError(f"no {MANIFEST_NAME} found at {manifest_file}")
    with open(manifest_file) as fh:
        try:
            manifest = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise BuildError(f"manifest is not valid YAML: {e}")
    if not isinstance(manifest, dict):
        raise BuildError(f"manifest is not a mapping: {manifest_file}")
    return manifest, manifest_file.resolve().parent


def validate_manifest(manifest: dict) -> None:
    # Full schema validation is mandatory: the degraded top-level-keys
    # fallback let structurally malformed manifests reach the pipeline as
    # raw KeyErrors.
    require_modules("jsonschema")
    import jsonschema
    with open(SCHEMA_PATH) as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
    if errors:
        lines = ["manifest failed schema validation:"]
        for e in errors:
            loc = "/".join(str(p) for p in e.path) or "(root)"
            lines.append(f"  - {loc}: {e.message}")
        raise BuildError("\n".join(lines))


# --------------------------------------------------------------------------- #
# Deploy context
# --------------------------------------------------------------------------- #

class Deploy:
    def __init__(self, manifest: dict, app_dir: Path, args: argparse.Namespace):
        self.m = manifest
        self.app_dir = app_dir
        self.id = args.id
        self._locate_instance(args)
        # The instance's wikis. Ids (and optional siteName) come from the
        # manifest `wikis:` list (required, at least one entry); the first is
        # the primary, created with the instance, the rest added via
        # `canasta add`. Domains are NOT in the manifest — they are supplied at
        # deploy/upgrade time via -n/--domain-name, one per wiki in order.
        # sync-code is code-only and passes no domains, so urls stay unset.
        self.wikis = self._resolve_wikis(getattr(args, "domain_name", None))
        self.wiki = self.wikis[0]["id"]
        self.domain = self.wikis[0].get("url")
        self.dry_run = args.dry_run
        self.skip_build = args.skip_build
        # Deploy against a custom Canasta image. With a web overlay it is the
        # overlay's base (a BASE_IMAGE build-arg; also settable per-service via
        # images.<svc>.baseImage). Without an overlay it is the instance image
        # itself, passed to `canasta create --canasta-image`. See canasta_create.
        self.base_image = getattr(args, "base_image", None)
        self.vars = {}
        for kv in (args.var or []):
            if "=" not in kv:
                raise BuildError(f"--var expects KEY=VALUE, got '{kv}'")
            key, val = kv.split("=", 1)
            self.vars[key] = val
        # Declared secrets (name + target); values come from --secret/env.
        self.secret_decls = manifest.get("secrets", []) or []
        # --secret KEY=VALUE sets a value; bare --secret KEY reads it from the
        # environment. A declared secret not named here falls back to the
        # environment too (see _secret_value).
        self.secret_overrides: dict = {}
        for item in (getattr(args, "secret", None) or []):
            if "=" in item:
                key, val = item.split("=", 1)
            else:
                key, val = item, os.environ.get(item)
            self.secret_overrides[key] = val
        self.container = f"{self.id}-web-1"
        self.orchestrator = getattr(args, "orchestrator", "compose") or "compose"
        self.is_k8s = self.orchestrator in ("kubernetes", "k8s")
        self.k8s_namespace = f"canasta-{self.id}"
        # k8s create flags passed through to `canasta create`.
        self.k8s_flags = []
        for flag, val in (("--storage-class", getattr(args, "storage_class", None)),
                          ("--access-mode", getattr(args, "access_mode", None)),
                          ("--ingress-class", getattr(args, "ingress_class", None))):
            if val:
                self.k8s_flags += [flag, val]
        if getattr(args, "skip_tls", False):
            self.k8s_flags.append("--skip-tls")

    def rel(self, path: str) -> Path:
        """Resolve a manifest-relative path."""
        return (self.app_dir / path).resolve()

    def _locate_instance(self, args: argparse.Namespace) -> None:
        """Set parent_path and instance_dir as <parent>/<id>, with the
        parent from -p (default: the current directory, matching
        `canasta create`). Existing-instance commands override this to
        consult the canasta registry instead."""
        self.parent_path = Path(
            getattr(args, "path", None) or ".").expanduser().resolve()
        self.instance_dir = self.parent_path / self.id

    def _locate_instance_from_registry(self, args: argparse.Namespace) -> None:
        """Existing-instance commands (upgrade, sync-code): the registry
        records where the instance lives, so -p is only a fallback for
        instances this host's registry does not know. An explicit -p that
        disagrees with the registry is an error rather than a silent
        re-target. The record is cached for other lookups (orchestrator)."""
        record = _canasta_registry_instance(self.id) or {}
        self._registry_record = record
        host = record.get("host")
        if host and host not in ("localhost", "127.0.0.1"):
            raise BuildError(
                f"instance '{self.id}' is recorded on remote host '{host}' "
                f"in the canasta registry; wicker does not manage "
                f"remote-host instances yet")
        recorded = record.get("path")
        if not recorded:
            Deploy._locate_instance(self, args)
            return
        recorded_dir = Path(recorded).expanduser().resolve()
        supplied = getattr(args, "path", None)
        if supplied is not None:
            supplied_dir = Path(supplied).expanduser().resolve() / self.id
            if supplied_dir != recorded_dir:
                raise BuildError(
                    f"instance '{self.id}' is at {recorded_dir} per the "
                    f"canasta registry, but -p puts it at {supplied_dir} — "
                    f"drop -p; the recorded path wins")
        self.instance_dir = recorded_dir
        self.parent_path = recorded_dir.parent

    def _resolve_wikis(self, domains: str | None) -> list[dict]:
        """The instance's wikis, primary first. The manifest declares each
        wiki's id (and optional siteName); their domains come from
        -n/--domain-name — a comma-separated list, one per wiki, in the same
        order and count as the manifest. `domains` is None for sync-code
        (code-only), which never uses a wiki url, so urls are left unset."""
        declared = self.m.get("wikis")
        if not declared:
            raise BuildError(
                "manifest must declare at least one wiki under `wikis:` "
                "(the first is the primary — its id names the instance)")
        if domains is None:
            return [dict(w) for w in declared]
        supplied = [d.strip() for d in domains.split(",")]
        if len(supplied) != len(declared):
            raise BuildError(
                f"manifest declares {len(declared)} wiki(s); -n/--domain-name "
                f"needs {len(declared)} comma-separated domain(s) in the same "
                f"order, got {len(supplied)} ({domains!r}). The primary wiki "
                f"takes a bare hostname (e.g. localhost); each farm wiki takes "
                f"its own hostname or a domain/path "
                f"(e.g. localhost/{declared[-1]['id']}).")
        if any(not d for d in supplied):
            raise BuildError(
                f"-n/--domain-name has an empty domain: {domains!r}")
        # Build new dicts — never mutate the caller's manifest.
        return [{**w, "url": d} for w, d in zip(declared, supplied)]

    def _secret_value(self, name: str) -> str | None:
        """A declared secret's value: an explicit --secret override, else the
        deploy environment."""
        if name in self.secret_overrides:
            return self.secret_overrides[name]
        return os.environ.get(name)

    def validate_secrets(self) -> None:
        """Fail fast on a malformed target, a target naming a missing sidecar,
        or a required secret with no value provided."""
        decls = self.secret_decls
        if not decls:
            return
        sidecar_names = {s["name"] for s in (self.m.get("sidecars") or [])}
        err = validate_secret_decls(decls, sidecar_names)
        if err:
            raise BuildError(err)
        missing = [d["name"] for d in decls
                   if d.get("required", True) and not self._secret_value(d["name"])]
        if missing:
            raise BuildError(
                "missing required secret(s): " + ", ".join(missing)
                + "\n       pass --secret KEY=VALUE (or set the environment "
                "variable) for each")

    # ---- pipeline ----------------------------------------------------- #

    def run_pipeline(self) -> None:
        try:
            self.preflight()
            images = self.build_images()
            self.canasta_create(images)
            self.install_elasticsearch_image(images)
            self.install_env()
            self.install_secrets()
            # Declare sidecars via the Canasta primitive; `canasta restart` then
            # renders, builds, and deploys them identically on Compose and k8s.
            self.install_sidecars()
            self.install_web_env_bridge()
            self.install_extensions()
            self.install_skins()
            self.install_settings()
            # Stage public assets before the restart so the k8s PVC sync (run by
            # canasta restart) picks them up; on Compose the bind mount makes the
            # copy live regardless of order.
            self.install_public_assets()
            self.canasta_restart()
            if not self.is_k8s:
                self.wait_healthy()
            self.add_wikis()
            self.install_content()
            self.install_uploads()
            self.run_actions(
                (self.m.get("content", {}) or {}).get("postInstall", []),
                phase="postInstall",
            )
            self.record_state()
            self.summary()
        finally:
            self._cleanup_staging()

    def _staging_tmpdir(self, prefix: str) -> Path:
        """A temp dir registered for removal by _cleanup_staging. Staged
        trees must outlive the canasta command that consumes them, so the
        pipeline entry points clean up at exit, not the staging site."""
        d = Path(tempfile.mkdtemp(prefix=prefix))
        if not hasattr(self, "_staging_dirs"):
            self._staging_dirs = []
        self._staging_dirs.append(d)
        return d

    def _cleanup_staging(self) -> None:
        for d in getattr(self, "_staging_dirs", []):
            shutil.rmtree(d, ignore_errors=True)
        self._staging_dirs = []

    def sync_code(self) -> None:
        """Re-sync extension and skin code into an existing instance, then
        restart. Code only: settings, env, secrets, seeded content, and the
        database are left untouched. `canasta restart` reconciles the code on
        either orchestrator (Compose bind mount; k8s PVC sync + pod roll)."""
        if not self.instance_dir.exists():
            raise BuildError(
                f"instance directory not found: {self.instance_dir}\n"
                f"sync-code updates an existing instance; deploy it first.")
        info(f"Syncing extension and skin code into {self.instance_dir}")
        self.install_extensions(overwrite=True)
        self.install_skins(overwrite=True)
        self.canasta_restart()
        info("Extension and skin code synced. Settings, env, secrets, and "
             "content were not touched.")

    # ---- steps -------------------------------------------------------- #

    def preflight(self) -> None:
        info("Preflight")
        missing = [t for t in ("docker", "canasta", "git") if not have(t)]
        if missing:
            raise BuildError(f"required tool(s) not in PATH: {', '.join(missing)}")
        constraint = (self.m.get("requires") or {}).get("canasta")
        if constraint:
            check_canasta_requirement(constraint)
        self.validate_secrets()
        if self.dry_run:
            return
        if self.instance_dir.exists():
            raise BuildError(
                f"instance directory already exists: {self.instance_dir}\n"
                f"       remove it or pick a different --id")
        if not self.is_k8s:
            probe = subprocess.run(
                ["docker", "inspect", self.container],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                raise BuildError(f"a container named '{self.container}' already exists")

    def _base_image_for(self, service: str, spec: dict) -> str | None:
        """Resolved BASE_IMAGE build-arg for a service's image, or None.

        The CLI --base-image overrides the web service's base (the Canasta
        image the app overlays); the manifest's images.<svc>.baseImage sets
        it per service. The CLI flag wins for `web`."""
        if service == "web" and self.base_image:
            return self.base_image
        return spec.get("baseImage")

    def build_images(self) -> dict:
        """Build each per-service derived image locally. Returns {service: tag}.

        Only the services Canasta can point at a custom image are accepted
        (SUPPORTED_IMAGE_SERVICES); anything else is skipped with a warning.
        On k8s the `web` image is built and delivered by Canasta via
        `canasta create --build-from` (which pushes it to the in-cluster
        registry), so it is not built here; the elasticsearch image is
        delivered via `canasta image push` and its entry carries the
        cluster-pullable ref."""
        built: dict = {}
        for service, spec in (self.m.get("images", {}) or {}).items():
            if service not in SUPPORTED_IMAGE_SERVICES:
                warn(f"image '{service}': Canasta has image overrides for "
                     f"'web' and 'elasticsearch' only; run additional "
                     f"containers as sidecars — skipped")
                continue
            if self.is_k8s:
                if service == "elasticsearch":
                    built[service] = self._push_image_k8s(service, spec)
                # web is delivered by `canasta create --build-from` at
                # deploy; upgrade pushes it via _push_image_k8s instead.
                continue
            tag = spec.get("tag") or self._derived_tag(service)
            if self.skip_build:
                info(f"Skipping image build for '{service}' (using existing {tag})")
            else:
                dockerfile = self.rel(spec["dockerfile"])
                context = self.rel(spec["context"]) if spec.get("context") \
                    else dockerfile.parent
                info(f"Building image for service '{service}': {tag}")
                build_cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile)]
                base = self._base_image_for(service, spec)
                if base:
                    build_cmd += ["--build-arg", f"BASE_IMAGE={base}"]
                build_cmd.append(str(context))
                run(build_cmd, dry_run=self.dry_run)
            built[service] = tag
        return built

    def _push_image_k8s(self, service: str, spec: dict) -> str:
        """Deliver a derived image to k8s: `canasta image push` (canasta
        >= 4.9.0) builds the context and pushes it to the in-cluster
        registry; the returned ref is what the pods pull. The push runs on
        the controller, which must be the cluster's control-plane node —
        the same assumption as create's --build-from delivery. A resolved
        base is forwarded as the BASE_IMAGE build arg; for web it is always
        passed (an overlay must layer on the full canasta image, and the
        Dockerfile's ARG default may not match this deploy)."""
        dockerfile = self.rel(spec["dockerfile"])
        context = self.rel(spec["context"]) if spec.get("context") \
            else dockerfile.parent
        if dockerfile.parent != context or dockerfile.name != "Dockerfile":
            # `canasta image push` builds <context>/Dockerfile, so stage a
            # copy of the context with the manifest's Dockerfile in place —
            # the same normalization deploy's --build-from staging does.
            staged = self._staging_tmpdir("wicker-imagepush-") / "ctx"
            info(f"Staging {service} image context for push: "
                 f"{context} -> {staged}")
            if not self.dry_run:
                shutil.copytree(context, staged)
                shutil.copy2(dockerfile, staged / "Dockerfile")
            context = staged
        tag = spec.get("tag") or self._derived_tag(service)
        ref = f"{K8S_REGISTRY_PULL}/{tag}"
        if self.skip_build:
            info(f"Skipping {service} image push (assuming {ref} is "
                 f"already in the in-cluster registry)")
            return ref
        cmd = ["canasta", "image", "push", str(context), "--name", tag]
        base = self._base_image_for(service, spec)
        if service == "web":
            base = base or default_canasta_image()
        if base:
            cmd += ["--build-arg", f"BASE_IMAGE={base}"]
        info(f"Delivering the derived {service} image to k8s via "
             f"canasta image push ({tag})")
        run(cmd, dry_run=self.dry_run)
        return ref

    def canasta_create(self, images: dict) -> None:
        info(f"Creating Canasta instance: id={self.id} path={self.parent_path} "
             f"domain={self.domain} wiki={self.wiki} orchestrator={self.orchestrator}")
        cmd = ["canasta", "create", "-i", self.id, "-p", str(self.parent_path),
               "-n", self.domain, "-w", self.wiki, "-o", self.orchestrator]
        # Primary wiki display name ($wgSitename); defaults to the wiki id.
        site_name = self.wikis[0].get("siteName")
        if site_name:
            cmd += ["-t", site_name]
        if self.is_k8s:
            cmd += self.k8s_flags
            # The web pod must pull from a registry the cluster can reach, so a
            # locally built --canasta-image tag is useless here. Hand the web
            # image's build context to `canasta create --build-from`, which
            # builds it and pushes it to the in-cluster registry; the pod then
            # pulls the registry-qualified ref. Other per-service images have no
            # such delivery path (see build_images).
            web = (self.m.get("images", {}) or {}).get("web")
            if web:
                staged = self._stage_web_build_from(web)
                info("Delivering the derived web image to k8s via "
                     "canasta create --build-from")
                cmd += ["--build-from", str(staged)]
                # An overlay must layer on the FULL canasta image. Under
                # --build-from the CLI otherwise defaults BASE_IMAGE to
                # canasta-base (no bundled extensions), so always pass an
                # explicit base: the resolved one (CLI --base-image or manifest
                # baseImage), else the Canasta image for the targeted version.
                base = self._base_image_for("web", web) or default_canasta_image()
                cmd += ["--build-arg", f"BASE_IMAGE={base}"]
            elif self.base_image:
                # No web overlay: run the custom image directly. The cluster
                # must be able to pull it, so a local-only tag is rejected
                # (it would only produce an ImagePullBackOff on the pod).
                if not is_registry_qualified(self.base_image):
                    raise BuildError(
                        f"--base-image '{self.base_image}' is a local-only tag; "
                        "a Kubernetes cluster can't pull it. Push it to a "
                        "registry the cluster can reach and pass the "
                        "registry-qualified ref, or add a web image overlay "
                        "(built and pushed to the in-cluster registry via "
                        "--build-from).")
                cmd += ["--canasta-image", self.base_image]
        elif images.get("web"):
            cmd += ["--canasta-image", images["web"]]
        elif self.base_image:
            # No web overlay on Compose: run the custom image directly. A local
            # tag is fine here — Compose uses the local Docker daemon.
            cmd += ["--canasta-image", self.base_image]
        run(cmd, dry_run=self.dry_run)

    def install_elasticsearch_image(self, images: dict) -> None:
        """Point the instance's elasticsearch service at the derived image
        via a config set the pipeline's restart picks up — the local tag on
        Compose, the in-cluster registry ref on k8s. An explicit env entry
        for the key wins over the built tag."""
        tag = images.get("elasticsearch")
        if not tag:
            return
        if ELASTICSEARCH_IMAGE_KEY in (self.m.get("env", {}) or {}):
            warn(f"env {ELASTICSEARCH_IMAGE_KEY} is set in the manifest; it "
                 f"overrides the built images.elasticsearch tag ({tag})")
            return
        info(f"Pointing elasticsearch at derived image {tag}")
        run(["canasta", "config", "set", "-i", self.id, "--no-restart",
             f"{ELASTICSEARCH_IMAGE_KEY}={tag}"], dry_run=self.dry_run)

    def add_wikis(self) -> None:
        """Add the non-primary wikis to the farm. The primary was created with
        the instance (canasta_create); each remaining wiki is registered with
        `canasta add` now that the instance is up."""
        extra = self.wikis[1:]
        if not extra:
            return
        info(f"Adding {len(extra)} wiki(s) to the farm")
        for w in extra:
            cmd = ["canasta", "add", "-i", self.id, "-w", w["id"], "-u", w["url"]]
            if w.get("siteName"):
                cmd += ["-t", w["siteName"]]
            run(cmd, dry_run=self.dry_run)

    def _build_from_root(self) -> Path:
        """Durable home for the web overlay's `--build-from` source tree.

        Canasta records the --build-from path in its registry and rebuilds
        from it on every later `canasta upgrade`, so this tree must outlive
        the deploy that stages it — it is not staging scratch, and must not
        be routed through _staging_tmpdir. It cannot live in the instance
        dir either: `canasta create` refuses a non-empty target."""
        state_home = os.environ.get("XDG_STATE_HOME") \
            or str(Path.home() / ".local" / "state")
        return Path(state_home) / "wicker" / "build-from" / self.id

    def _stage_web_build_from(self, spec: dict) -> Path:
        """Stage the web image's build context as a `canasta create
        --build-from` source tree and return its path. `--build-from` builds
        `<path>/Canasta/Dockerfile` with that directory as the context, so the
        manifest's context is copied under a `Canasta/` subdir and the
        manifest's Dockerfile is placed there as `Dockerfile`."""
        dockerfile = self.rel(spec["dockerfile"])
        context = self.rel(spec["context"]) if spec.get("context") \
            else dockerfile.parent
        root = self._build_from_root()
        staged = root / "Canasta"
        info(f"Staging web build context for --build-from: {context} -> {staged}")
        if self.dry_run:
            return root
        if staged.exists():
            shutil.rmtree(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(context, staged)
        shutil.copy2(dockerfile, staged / "Dockerfile")
        return root

    def install_sidecars(self) -> None:
        """Declare the app's sidecars via the Canasta sidecar primitive.

        Writes the manifest sidecars (which already use the agnostic schema:
        name/image/build/command/ports/env/volumes/files/depends_on/
        healthcheck/resources) to a file and runs `canasta sidecar add
        --file`. `canasta restart` then
        renders, builds, and deploys them identically on Compose and k8s —
        Canasta builds a `build:` sidecar (natively on Compose, via the
        in-cluster registry on k8s) and bridges sidecar-referenced env to the
        web tier. A build context is staged into the instance dir, since
        Canasta resolves `build:` relative to the instance."""
        sidecars = self.m.get("sidecars", []) or []
        if not sidecars:
            return
        defs = []
        for sc in sidecars:
            # restart is Canasta's to set; sysctls/secrets aren't in the v1
            # primitive; build/image/files are handled explicitly below.
            d = {k: v for k, v in sc.items()
                 if k not in ("restart", "build", "image", "dockerfile",
                              "sysctls", "secrets", "files")}
            for unsupported in ("sysctls", "secrets"):
                if sc.get(unsupported):
                    warn(f"sidecar '{sc['name']}': {unsupported} is not "
                         f"supported by the canasta sidecar primitive — dropped")
            if sc.get("build"):
                ctx_rel = f"sidecars/{sc['name']}"
                src = self.rel(sc["build"])
                dest = self.instance_dir / ctx_rel
                info(f"Staging sidecar build context: {src} -> {dest}")
                if not self.dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                if sc.get("dockerfile"):
                    warn(f"sidecar '{sc['name']}': custom dockerfile ignored; "
                         f"canasta builds <context>/Dockerfile")
                d["build"] = ctx_rel
            else:
                d["image"] = sc["image"]
            if sc.get("files"):
                d["files"] = self._stage_sidecar_files(sc)
            # Route declared secrets targeting this sidecar: reference each as
            # ${NAME} and mark it envPrivate so Canasta delivers it here but
            # keeps it out of the web tier. On k8s also mark it envSecret, so
            # Canasta sources it from the per-instance Secret via secretKeyRef
            # (the value never touches .env or the gitops repo); on Compose the
            # ${NAME} reference resolves from the gitignored .env.
            for sname in secrets_for_sidecar(self.secret_decls, sc["name"]):
                # An optional secret left unset is wired nowhere — otherwise the
                # sidecar would reference a value that was never written (on k8s,
                # a secretKeyRef to a missing Secret key fails the pod). Required
                # secrets always have a value (preflight gate).
                if self._secret_value(sname) is None:
                    continue
                inject_sidecar_secret(d, sname)
                if self.is_k8s:
                    mark_envsecret(d, sname)
            defs.append(d)
        text = yaml.safe_dump({"sidecars": defs}, sort_keys=False)
        info(f"Declaring {len(defs)} sidecar(s) via canasta sidecar add --file")
        print(_indent(text))
        sc_file = self._staging_tmpdir("wicker-") / "sidecars.yaml"
        if not self.dry_run:
            sc_file.write_text(text)
        run(["canasta", "sidecar", "add", "-i", self.id, "--file", str(sc_file)],
            dry_run=self.dry_run)

    def _stage_sidecar_files(self, sc: dict) -> list[dict]:
        """Stage a sidecar's `files` sources into the instance dir and return
        the entries with `source` rewritten to instance-relative paths —
        Canasta resolves them against the instance (a bind mount on Compose, a
        ConfigMap on k8s), while the manifest declares them app-relative. An
        index prefix keeps same-named sources from colliding."""
        staged = []
        for i, f in enumerate(sc["files"]):
            src = self.rel(f["source"])
            rel = f"sidecars/{sc['name']}-files/{i}-{Path(f['source']).name}"
            dest = self.instance_dir / rel
            info(f"Staging sidecar file: {src} -> {dest}")
            if not self.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            entry = {"source": rel, "mountPath": f["mountPath"]}
            if "readOnly" in f:
                entry["readOnly"] = f["readOnly"]
            staged.append(entry)
        return staged

    def install_env(self) -> None:
        """Write the app's instance env (manifest `env`) to the instance .env via
        `canasta config set`, so the .env carries them (and the maintenance
        container, whose env is uncurated, picks them up). Values support
        ${VAR:-default} interpolation from the deploy environment, so a shared
        secret resolves to the SAME value here and in a sidecar's env. The
        web tier's getenv() is delivered separately by install_web_env_bridge()
        (the web pod env is a curated allowlist; see that method). Note: values
        land in .env / the settings ConfigMap (not a Secret) — a real Secret for
        sensitive values is deferred (Canasta #774)."""
        env = self.m.get("env", {}) or {}
        for key, raw in env.items():
            value = resolve_env_value(raw)
            info(f"Setting instance env {key}")
            run(["canasta", "config", "set", "-i", self.id,
                 "--no-restart", "--force", f"{key}={value}"],
                dry_run=self.dry_run,
                show=f"canasta config set -i {self.id} --no-restart --force {key}=***")

    def install_secrets(self) -> None:
        """Write declared secret values via `canasta config set` (value redacted
        in the log). On k8s, `--secret` writes the gitignored config/secrets.env,
        which Canasta upserts into a per-instance Secret consumed by sidecars via
        secretKeyRef (envSecret) — the value never touches .env or the gitops
        repo. On Compose there is no Secret; the value goes to the gitignored
        .env (`--force`, an app key). Either way it's redacted in the log.
        Sidecar wiring is in install_sidecars(). A web-targeted secret on k8s
        adds --web, so Canasta exposes it to the web/jobrunner pods via
        secretKeyRef; on Compose it rides install_web_env_bridge() instead."""
        for decl in self.secret_decls:
            name = decl["name"]
            value = self._secret_value(name)
            if value is None:
                continue  # optional and unset
            info(f"Setting secret {name}")
            cmd = ["canasta", "config", "set", "-i", self.id, "--no-restart"]
            if self.is_k8s:
                cmd.append("--secret")
                if "web" in secret_targets(decl):
                    cmd.append("--web")
            else:
                cmd.append("--force")
            cmd.append(f"{name}={value}")
            run(cmd, dry_run=self.dry_run,
                show=" ".join(cmd[:-1]) + f" {name}=***")

    def _web_bridge_env(self) -> dict:
        """The resolved env map re-exported to the web tier's getenv() via the
        settings fragment: the manifest `env` always, plus web-targeted secrets
        only on Compose (on k8s those reach the pod via secretKeyRef, so
        bridging them would re-introduce the cleartext value in the repo)."""
        resolved = {k: resolve_env_value(v)
                    for k, v in (self.m.get("env") or {}).items()}
        if not self.is_k8s:
            for name in web_secret_names(self.secret_decls):
                value = self._secret_value(name)
                if value is not None:
                    resolved[name] = value
        return resolved

    def install_web_env_bridge(self) -> None:
        """Deliver the instance env to the web tier's getenv() on BOTH
        orchestrators.

        The web/jobrunner pod environment is a curated allowlist on Compose
        AND Kubernetes (Canasta #789), so an app-defined key written to .env by
        install_env() does not reach the web container's getenv(). Generate a
        global settings fragment that re-exports the resolved instance env, so
        the app's getenv() sees the SAME value the sidecar got. It loads first
        (the 00- prefix; Canasta sorts config/settings/global/*.php) so later
        app fragments can read it. Plaintext in the settings ConfigMap, so a
        web-targeted secret is bridged here only on Compose (which has no
        Secret); on k8s Canasta delivers it via secretKeyRef instead (--web),
        keeping it out of the gitops repo."""
        resolved = self._web_bridge_env()
        if not resolved:
            return
        content = render_env_bridge_php(resolved)
        dest = (self.instance_dir / "config" / "settings" / "global"
                / "00-wicker-env.php")
        info(f"Bridging {len(resolved)} instance env key(s) to the web tier "
             f"({dest.name})")
        if self.dry_run:
            print(f"  $ write {dest}")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    def install_extensions(self, overwrite: bool = False) -> None:
        self._install_components("extensions", overwrite=overwrite)

    def install_skins(self, overwrite: bool = False) -> None:
        self._install_components("skins", overwrite=overwrite)

    def _install_components(self, section: str, *, overwrite: bool) -> None:
        """Install the custom extensions or skins in `section` into
        `<instance>/<section>/`. Extensions and skins share this path: a
        name-only entry is bundled (present on the image, loaded by settings)
        and skipped; `source` is copied, `git` is cloned. `overwrite` replaces
        an existing checkout (see the source/`_git_clone` notes)."""
        kind = section[:-1]  # "extension" / "skin"
        items = self.m.get(section, []) or []
        bundled = [c["name"] for c in items if not (c.get("source") or c.get("git"))]
        if bundled:
            info(f"Bundled {section} (the app's settings fragment loads these): "
                 f"{', '.join(bundled)}")
        dest_root = self.instance_dir / section
        for c in items:
            if not (c.get("source") or c.get("git")):
                continue  # bundled: present on the image, loaded by settings
            dest = dest_root / c["name"]
            if c.get("source"):
                src = self.rel(c["source"])
                info(f"Installing {kind} {c['name']} (copy from {src})")
                if self.dry_run:
                    if overwrite and dest.exists():
                        print(f"  $ rm -rf {dest}")
                    print(f"  $ cp -R {src} {dest}")
                else:
                    # Delete-then-copy so a re-sync also drops files removed
                    # from the source; a fresh deploy has no dest to remove.
                    if overwrite and dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
            else:
                info(f"Installing {kind} {c['name']} (git clone)")
                self._git_clone(c, dest, overwrite=overwrite)

    def _git_clone(self, ext: dict, dest: Path, overwrite: bool = False) -> None:
        """Clone a git-sourced extension/skin at `ref` (branch, tag, or commit).
        A branch or tag is cloned directly (shallow). `git clone --branch`
        rejects a commit SHA, so on failure re-clone without it — bounded by
        `depth` if set, else full — and check the commit out. On a re-sync
        (`overwrite`) the existing checkout is removed and re-cloned, which
        re-pins to the manifest `ref` and avoids shallow-pull edge cases."""
        if overwrite and dest.exists():
            if self.dry_run:
                print(f"  $ rm -rf {dest}")
            else:
                shutil.rmtree(dest)
        depth = ext.get("depth")
        ref = ext.get("ref")
        cmd = ["git", "clone", "--depth", str(depth or 1)]
        if ref:
            cmd += ["--branch", ref]
        try:
            run(cmd + [ext["git"], str(dest)], dry_run=self.dry_run)
        except BuildError:
            if not ref:
                raise
            info(f"'{ref}' is not a branch or tag; cloning to check out the commit")
            cmd = ["git", "clone"]
            if depth:
                cmd += ["--depth", str(depth)]
            run(cmd + [ext["git"], str(dest)], dry_run=self.dry_run)
            run(["git", "-C", str(dest), "checkout", ref], dry_run=self.dry_run)

    def install_settings(self) -> None:
        for s in self.m.get("settings", []) or []:
            src = self.rel(s["path"])
            dest = self.instance_dir / s["install"]
            # An empty `vars:` mapping still means "templated" — the values
            # can come entirely from deploy-time --var.
            svars = s.get("vars")
            info(f"Installing settings {dest.name}"
                 + (" (templated)" if svars is not None else ""))
            if self.dry_run:
                print(f"  $ install {src} -> {dest}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if svars is not None:
                text = src.read_text()
                # Manifest vars are defaults; deploy-time --var overrides them.
                for k, v in {**svars, **self.vars}.items():
                    text = text.replace("{{" + k + "}}", v).replace("{{ " + k + " }}", v)
                dest.write_text(text)
            else:
                shutil.copy2(src, dest)

    def canasta_restart(self) -> None:
        info("Restarting Canasta instance")
        run(["canasta", "restart", "-i", self.id], dry_run=self.dry_run)

    def wait_healthy(self, attempts: int = 60) -> None:
        if self.dry_run:
            return
        info(f"Waiting for {self.container} to become healthy")
        import time
        for _ in range(attempts):
            probe = subprocess.run(
                ["docker", "inspect", self.container,
                 "--format", "{{ .State.Health.Status }}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            status = (probe.stdout or "").strip()
            if status == "healthy":
                return
            if status == "unhealthy":
                raise BuildError(f"{self.container} reported unhealthy")
            time.sleep(3)
        warn(f"{self.container} did not report healthy within "
             f"{attempts * 3}s; continuing anyway")

    def _content_pages(self):
        """Yield (title, source Path) for every starter page in the manifest,
        resolving each entry's file exactly as install_content imports it —
        the single source of that mapping for import, state recording, and
        upgrade reconciliation."""
        yield from iter_content_pages(self.m, self.app_dir)

    def install_content(self) -> None:
        entries = list(self._content_pages())
        if not entries:
            return
        info(f"Importing {len(entries)} starter page(s)")
        for title, src in entries:
            if not src.is_file():
                warn(f"missing source for {title}: {src}")
                continue
            print(f"  -> {title}")
            self._mw_edit(title, src)

    def install_public_assets(self) -> None:
        """Copy the app's public_assets tree into the instance's
        public_assets/ (served statically under the web docroot). On Compose
        the directory is bind-mounted into the web container; on k8s it is a
        PVC that `canasta restart` mirrors the instance dir into (via the CLI's
        k8s_sync_user_dirs). Either way the files must be staged into the
        instance dir before the restart — so run this before canasta_restart."""
        src_rel = self.m.get("publicAssets")
        if not src_rel:
            return
        src = self.rel(src_rel)
        if not src.is_dir():
            warn(f"publicAssets: {src} is not a directory; skipping")
            return
        dest = self.instance_dir / "public_assets"
        info(f"Copying public assets into {dest}")
        if self.dry_run:
            print(f"  $ cp -R {src}/. {dest}")
            return
        shutil.copytree(src, dest, dirs_exist_ok=True)

    def install_uploads(self) -> None:
        """Upload the app's media into the wikis via importImages.php. Files
        directly in the uploads dir go to the primary wiki; files in a
        <wiki-id>/ subdirectory go to that wiki. Files are staged into the
        bind-mounted images/ dir so the web container can read them, then
        removed. Compose-only (importImages needs the source on a path the
        container sees; on k8s images/ is a PVC, not a host dir)."""
        uploads = (self.m.get("content", {}) or {}).get("uploads")
        if uploads is None:  # present-but-empty ({}) still uses the default dir
            return
        if self.is_k8s:
            warn("content.uploads is Compose-only for now; skipping")
            return
        src_dir = self.rel(uploads.get("dir", "./content/uploads"))
        if not src_dir.is_dir():
            warn(f"content.uploads dir not found: {src_dir}")
            return
        wiki_ids = {w["id"] for w in self.wikis}
        # (target wiki, source dir) groups: loose files -> primary; each
        # <wiki-id>/ subdir -> that wiki. Unknown subdirs are skipped.
        groups: list[tuple[str, Path, list[Path]]] = []
        loose = [p for p in sorted(src_dir.iterdir()) if p.is_file()]
        if loose:
            groups.append((self.wiki, src_dir, loose))
        for sub in sorted(p for p in src_dir.iterdir() if p.is_dir()):
            if sub.name not in wiki_ids:
                warn(f"content.uploads: subdirectory '{sub.name}' matches no "
                     f"wiki; skipped")
                continue
            files = [p for p in sorted(sub.iterdir()) if p.is_file()]
            if files:
                groups.append((sub.name, sub, files))
        if not groups:
            return
        info(f"Importing media into {len(groups)} wiki target(s)")
        stage_root = self.instance_dir / "images" / ".wicker-uploads"
        try:
            for wiki, _src, files in groups:
                self._import_media(wiki, files)
        finally:
            if not self.dry_run and stage_root.exists():
                shutil.rmtree(stage_root, ignore_errors=True)

    def _import_media(self, wiki: str, files: list[Path]) -> None:
        """Stage `files` into the bind-mounted images/ dir and register them in
        `wiki` with importImages.php. The stage dir maps to the same path inside
        the web container (./images -> /mediawiki/images)."""
        rel_stage = f".wicker-uploads/{wiki}"
        stage = self.instance_dir / "images" / rel_stage
        container_dir = f"/mediawiki/images/{rel_stage}"
        info(f"  -> {len(files)} file(s) to wiki '{wiki}'")
        if not self.dry_run:
            stage.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(f, stage / f.name)
        self._canasta_exec(
            f"{MW_RUN} importImages {shlex.quote(container_dir)} --wiki={wiki}")

    @staticmethod
    def _page_filename(title: str) -> str:
        """Derive a page's file path from its title, mirroring the wiki
        namespace as a directory and the title verbatim otherwise. The first
        ':' (the namespace separator) becomes a '/' so the file nests under a
        namespace directory; spaces and subpage '/' are kept as-is; a main-
        namespace page sits at the root. '.mediawiki' is appended unless the
        title already ends in .css/.js.
            Template:Expense                  -> Template/Expense.mediawiki
            Template:Receipt dashboard/styles.css
                                              -> Template/Receipt dashboard/styles.css
            Main Page                         -> Main Page.mediawiki
        """
        name = title.replace(":", "/", 1)
        if not title.lower().endswith((".css", ".js")):
            name += ".mediawiki"
        return name

    # ---- post-install actions ----------------------------------------- #

    def run_actions(self, actions: list[dict], *, phase: str) -> None:
        if not actions:
            return
        info(f"Running {phase} actions ({len(actions)})")
        for action in actions:
            kind = action["kind"]
            handler = getattr(self, f"_action_{kind.replace('-', '_')}", None)
            if handler is None:
                raise BuildError(f"unsupported action kind: {kind}")
            handler(action)

    def _action_maintenance(self, action: dict) -> None:
        script = self._resolve_script(action.get("extension"), action["script"])
        # The exec path shlex-splits the command string, so args carrying
        # spaces or quotes must be quoted here.
        args = " ".join(shlex.quote(str(a)) for a in action.get("args", []))
        label = (f"{action['extension']}/{action['script']}"
                 if action.get("extension") else action["script"])
        info(f"Running maintenance script: {label}")
        self._canasta_exec(f"{MW_RUN} {script} {args} --wiki={self.wiki}".strip())

    def _resolve_script(self, extension: str | None, script: str) -> str:
        """Resolve a maintenance script's in-container path. Bundled extensions
        live under canasta-extensions/, custom ones (source/git) under
        extensions/; with no extension, it's a core maintenance script."""
        if not extension:
            return f"{MW_ROOT}/maintenance/{script}"
        custom = {e["name"] for e in (self.m.get("extensions") or [])
                  if e.get("source") or e.get("git")}
        base = "extensions" if extension in custom else "canasta-extensions"
        return f"{MW_ROOT}/{base}/{extension}/maintenance/{script}"

    def _action_update(self, action: dict) -> None:
        info("Running update.php")
        self._canasta_exec(f"{MW_RUN} update --quick --wiki={self.wiki}")

    def _action_page_edit(self, action: dict) -> None:
        src = self.rel(action["source"])
        info(f"Editing page: {action['page']}")
        if src.is_file():
            self._mw_edit(action["page"], src)
        else:  # literal content
            with tmp_text_file(action["source"], ".wikitext") as tmp:
                self._mw_edit(action["page"], tmp)

    def _action_shell(self, action: dict) -> None:
        target = action.get("container", "web")
        info(f"Shell ({target}): {action['command']}")
        if target == "web":
            self._canasta_exec(action["command"])
        elif target == "host":
            run(["bash", "-c", action["command"]], dry_run=self.dry_run)
        elif target.startswith("sidecar:"):
            name = target.split(":", 1)[1]
            # Route through canasta (orchestrator-agnostic + remote-capable)
            # rather than `docker exec` by the Compose container name, which is
            # local-Compose-only. Wrap in `sh -c` to keep shell semantics; sh is
            # present even on slim sidecar images that lack bash.
            self._canasta_exec("sh -c " + shlex.quote(action["command"]),
                               service=name)
        else:
            raise BuildError(f"unknown shell container target: {target}")

    # ---- container exec helpers --------------------------------------- #

    def _canasta_exec(self, command: str, *, stdin_path: Path | None = None,
                      service: str | None = None, capture: bool = False,
                      check: bool = True) -> subprocess.CompletedProcess | None:
        """Run a command in a container via `canasta maintenance exec` — not raw
        docker — so the builder works against local, remote, and K8s targets,
        and against any service (the web container by default, or a sidecar via
        `service`). `canasta maintenance exec` shlex-splits the command string
        and exec's the resulting argv directly (no shell), so callers needing
        shell semantics must pass an explicit `sh -c …`, and tokens containing
        spaces must be shlex-quoted (see _mw_edit). `capture` and `check` pass
        through to run(), whose result is returned for callers that read
        output or inspect the returncode."""
        cmd = ["canasta", "maintenance", "exec", "-i", self.id]
        if service is not None:
            cmd += ["--service", service]
        if stdin_path is not None:
            cmd += ["--stdin-file", str(stdin_path)]
        cmd += ["--", command]
        return run(cmd, dry_run=self.dry_run, capture=capture, check=check)

    def _mw_edit(self, page_name: str, src: Path,
                 summary: str = "Initial import (wicker)") -> None:
        """Write a wiki page, feeding its wikitext on stdin."""
        argv = ["php", f"{MW_ROOT}/maintenance/run.php", "edit",
                "--user=WikiSysop",
                f"--summary={summary}",
                "--no-rc", page_name]
        self._canasta_exec(" ".join(shlex.quote(a) for a in argv), stdin_path=src)

    def _mw_delete(self, page_name: str, reason: str) -> None:
        """Delete a wiki page (the title is fed to deleteBatch on stdin)."""
        argv = ["php", f"{MW_ROOT}/maintenance/run.php", "deleteBatch",
                "-u", "WikiSysop", "-r", reason]
        with tmp_text_file(page_name + "\n", ".titles") as tmp:
            self._canasta_exec(" ".join(shlex.quote(a) for a in argv),
                               stdin_path=tmp)

    # ---- state --------------------------------------------------------- #

    def record_state(self, op: str = "deploy") -> None:
        """Write the deployed app's state into <instance>/.wicker-app/:
        state.json (app, version, content hashes), a verbatim copy of the
        applied manifest, and a snapshot of every imported page file under
        content/ (keyed by derived filename, regardless of any per-page
        `file` override). `wicker upgrade` uses the snapshot as the
        pristine-test and merge base for shipped pages.
        The instance dir travels with the instance and is captured by
        `canasta backup`, so the state does too."""
        state_dir = self.instance_dir / STATE_DIR_NAME
        info(f"Recording app state in {state_dir}")
        if self.dry_run:
            print(f"  $ write {state_dir}/{{state.json,manifest.yaml,content/}}")
            return
        manifest_bytes = (self.app_dir / MANIFEST_NAME).read_bytes()
        content_dir = state_dir / "content"
        if content_dir.exists():
            shutil.rmtree(content_dir)
        pages = {}
        seen_files = {}
        for title, src in self._content_pages():
            if not src.is_file():
                continue  # install_content already warned
            data = src.read_bytes()
            pages[title] = hashlib.sha256(data).hexdigest()
            fname = self._page_filename(title)
            if fname in seen_files:
                raise BuildError(
                    f"content pages '{seen_files[fname]}' and '{title}' "
                    f"both map to the snapshot file '{fname}' — rename one "
                    f"(the upgrade merge base cannot be shared)")
            seen_files[fname] = title
            dest = content_dir / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        state_path = state_dir / "state.json"
        history = []
        if state_path.is_file():
            try:
                history = json.loads(state_path.read_text()).get("history", [])
            except (json.JSONDecodeError, OSError):
                warn(f"unreadable {state_path}; starting history afresh")
        history.append({
            "op": op,
            "version": self.m["version"],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        state = {
            "app": self.m["name"],
            "version": self.m["version"],
            "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "pages": pages,
            "history": history,
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "manifest.yaml").write_bytes(manifest_bytes)
        state_path.write_text(json.dumps(state, indent=2) + "\n")

    # ---- reporting ---------------------------------------------------- #

    def _derived_tag(self, component: str) -> str:
        # A semver like 1.0.0+build.1 is a valid version but '+' is not a
        # valid Docker tag character — map anything outside the tag alphabet.
        version = re.sub(r"[^A-Za-z0-9_.-]", "-", self.m["version"])
        return f"{self.m['name']}-{component}:{version}"

    def summary(self) -> None:
        pw_file = self.instance_dir / f"admin-password_{self.wiki}"
        print(f"""
==> Done: {self.m['name']} {self.m['version']}

  instance dir:    {self.instance_dir}
  admin password:  {pw_file}
  wiki URL:        http://{self.domain}/wiki/Main_Page

  Log in as WikiSysop with the password in the file above.
""")


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #

def iter_content_pages(manifest: dict, app_dir: Path):
    """Yield (title, source Path) for every starter page in `manifest`,
    with paths resolved against `app_dir`."""
    pages = (manifest.get("content", {}) or {}).get("pages")
    if not pages:
        return
    base = (app_dir / pages["dir"]).resolve() if pages.get("dir") else app_dir
    for entry in pages.get("list", []):
        if isinstance(entry, str):
            title, filename = entry, Deploy._page_filename(entry)
        else:
            title = entry["page"]
            filename = entry.get("file") or Deploy._page_filename(title)
        yield title, (base / filename).resolve()


def _canasta_registry_instance(instance_id: str) -> dict | None:
    """The instance's record in the Canasta registry (conf.json) — id,
    path, orchestrator, host — or None when the registry or the instance
    is absent. Mirrors the CLI's config-dir convention (canasta_config.py):
    $CANASTA_CONFIG_DIR, /etc/canasta for root, the platform config home
    otherwise."""
    config_dir = os.environ.get("CANASTA_CONFIG_DIR")
    if not config_dir:
        if hasattr(os, "getuid") and os.getuid() == 0:
            config_dir = "/etc/canasta"
        elif platform.system() == "Darwin":
            config_dir = os.path.expanduser(
                "~/Library/Application Support/canasta")
        else:
            config_dir = os.path.join(
                os.environ.get("XDG_CONFIG_HOME",
                               os.path.expanduser("~/.config")),
                "canasta")
    conf = Path(config_dir) / "conf.json"
    if not conf.is_file():
        return None
    try:
        data = json.loads(conf.read_text())
    except (OSError, ValueError):
        return None
    return (data.get("Instances") or {}).get(instance_id) or None


def _custom_component_names(manifest: dict, section: str) -> set:
    """Names of the custom (source/git) extensions or skins in a manifest —
    the entries Wicker installs into the instance, so the ones upgrade may
    have to prune."""
    return {c["name"] for c in (manifest.get(section) or [])
            if c.get("source") or c.get("git")}


def semver_triplet(version: str) -> tuple:
    """(major, minor, patch) of a manifest version. Pre-release/build
    suffixes are ignored for ordering."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        raise BuildError(f"unparseable version: {version}")
    return tuple(int(x) for x in m.groups())


def _norm_lines(text: str) -> list[str]:
    """Text as newline-terminated lines with trailing-newline variance
    removed (maintenance edit trims a trailing newline on save, so
    file-vs-wiki comparisons must not hinge on one)."""
    return (text.rstrip("\n") + "\n").splitlines(keepends=True)


def merge_page(base: str, live: str, new: str, *, version: str):
    """Three-way merge of a shipped page: base = what the old app version
    shipped, live = the wiki's current text, new = what the new version
    ships. Returns (merged_text, None) on a clean merge or (None,
    marker_text) on conflict — conflict markers are never written to a
    live page."""
    try:
        import merge3
    except ImportError:
        raise BuildError("the merge3 package is required by upgrade "
                         "(pip install -r requirements.txt)")
    m = merge3.Merge3(_norm_lines(base), _norm_lines(live), _norm_lines(new))
    text = "".join(m.merge_lines(name_a="live wiki page",
                                 name_b=f"shipped in {version}"))
    if any(r[0] == "conflict" for r in m.merge_regions()):
        return None, text
    return text, None


# MediaWiki's built-in namespaces, for turning (page_namespace, page_title)
# rows into titles without a round-trip. Namespaces outside this map
# (extension namespaces such as an app's custom content namespaces) are
# resolved by asking the wiki itself — see _wiki_namespaces().
CANONICAL_NAMESPACES = {
    0: "", 1: "Talk", 2: "User", 3: "User talk", 4: "Project",
    5: "Project talk", 6: "File", 7: "File talk", 8: "MediaWiki",
    9: "MediaWiki talk", 10: "Template", 11: "Template talk",
    12: "Help", 13: "Help talk", 14: "Category", 15: "Category talk",
}


def normalize_title(title: str) -> str:
    """A page title in comparison form: underscores to spaces, first
    letter uppercased (MW titles are first-letter case-insensitive)."""
    s = str(title).strip().replace("_", " ")
    return (s[:1].upper() + s[1:]) if s else s


def normalize_template_name(name) -> str:
    """A template name as wikitext-call matching requires: stripped,
    underscores to spaces, first letter uppercased ({{expense}} and
    {{Expense}} call the same template). A Template: prefix is also
    normalized after the colon, so {{template:expense}} matches too."""
    s = str(name).strip().replace("_", " ")
    s = (s[:1].upper() + s[1:]) if s else s
    if s.lower().startswith("template:"):
        rest = s[len("template:"):].strip()
        s = "Template:" + ((rest[:1].upper() + rest[1:]) if rest else rest)
    return s


def target_canasta_version() -> str | None:
    """The Canasta version the installed canasta CLI targets, read from the
    "Target Canasta version" line of `canasta version`, or None if that line
    cannot be read."""
    result = run(["canasta", "version"], dry_run=False, capture=True,
                 check=False)
    out = (result.stdout or "") + (result.stderr or "")
    vm = re.search(r"Target Canasta version:\s*(\d+(?:\.\d+){0,2})", out)
    return vm.group(1) if vm else None


def default_canasta_image() -> str:
    """The base image a web overlay layers on: the Canasta release the
    installed CLI targets, so a rebuild of one app version lands on the same
    image every time. Falls back to DEFAULT_CANASTA_IMAGE (:latest) with a
    warning when the version is unreadable or is not a full X.Y.Z."""
    try:
        version = target_canasta_version()
    except OSError:
        # canasta is not runnable (for example, not installed): the same
        # fallback as an unreadable version.
        version = None
    if version is None or version.count(".") != 2:
        warn(f"cannot read a full Canasta version from `canasta version`; "
             f"the web overlay base falls back to {DEFAULT_CANASTA_IMAGE}")
        return DEFAULT_CANASTA_IMAGE
    return f"ghcr.io/canastawiki/canasta:{version}"


def check_canasta_requirement(constraint: str, dry_run: bool = False) -> None:
    """Enforce the manifest's requires.canasta constraint (">=X[.Y[.Z]]",
    also >, <=, <, ==) against `canasta version`'s "Target Canasta
    version" line. A missing/unparseable line warns instead of failing —
    dev builds may drift — but a parseable version that misses the
    constraint aborts before anything runs."""
    m = re.match(r"^\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})\s*$", constraint)
    if not m:
        raise BuildError(
            f"requires.canasta constraint '{constraint}' is not of the "
            f"form '>=X.Y' (supported: >=, >, <=, <, ==)")
    op, want = m.group(1), m.group(2)
    version = target_canasta_version()
    if version is None:
        warn(f"cannot determine the Canasta version from `canasta version` "
             f"output; skipping the requires.canasta ({constraint}) check")
        return
    def pad(v: str) -> str:
        parts = v.split(".")
        return ".".join(parts + ["0"] * (3 - len(parts)))
    have = semver_triplet(pad(version))
    need = semver_triplet(pad(want))
    ok = {"<": have < need, "<=": have <= need, "==": have == need,
          ">": have > need, ">=": have >= need}[op]
    if not ok:
        raise BuildError(
            f"this app requires Canasta {constraint}, but the installed "
            f"CLI targets Canasta {version} — upgrade the canasta CLI")


def require_modules(*modules: str) -> None:
    """Fail fast when a lazily-imported dependency is missing — preflight
    calls this so a missing package aborts before anything runs, not
    mid-upgrade after the restart. The fix in the message is tailored to
    the interpreter Wicker is actually running under."""
    import importlib.util
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return
    here = Path(__file__).resolve().parent
    req = here / "requirements.txt"
    py = sys.executable or "python3"
    if importlib.util.find_spec("pip") is not None:
        fix = f"install them with:\n       {py} -m pip install -r {req}"
    else:
        apt = " ".join(f"python3-{m}" for m in missing)
        venv = here / ".venv"
        fix = (f"this python ({py}) has no pip. Either install the distro "
               f"packages:\n       sudo apt install {apt}\n"
               f"       or give Wicker a virtualenv (the launcher uses "
               f"{venv} automatically):\n"
               f"       {py} -m venv {venv} && {venv}/bin/pip install -r {req}")
    raise BuildError(
        f"missing Python package(s): {', '.join(missing)} — {fix}")


def applicable_migrations(migrations, old_version: str, target: str) -> list:
    """The migration entries an upgrade from old_version to target must run,
    in ascending `to` order. Validates every entry (unique `to`, none beyond
    the target) so a bad manifest fails preflight, before anything runs."""
    entries = migrations or []
    seen = set()
    for e in entries:
        t = semver_triplet(e["to"])
        if t in seen:
            raise BuildError(f"duplicate migration 'to: {e['to']}'")
        seen.add(t)
        if t > semver_triplet(target):
            raise BuildError(
                f"migration 'to: {e['to']}' is beyond the manifest "
                f"version {target}")
    old_t = semver_triplet(old_version)
    return sorted((e for e in entries if semver_triplet(e["to"]) > old_t),
                  key=lambda e: semver_triplet(e["to"]))


def transform_template_calls(text: str, names: set, transforms: list):
    """Apply the step's transforms to every invocation of the template
    (any name in `names`) in `text`. Returns (new_text, notes): new_text is
    None when nothing changed, notes list anything a human should review.
    Deterministic only — parameter values are moved verbatim (nested
    templates and parser functions inside a value survive a rename), and a
    page whose wikitext the parser cannot reproduce byte-identically is
    left untouched and reported rather than guessed at."""
    try:
        import mwparserfromhell
    except ImportError:
        raise BuildError("the mwparserfromhell package is required by "
                         "template-calls migrations "
                         "(pip install -r requirements.txt)")
    code = mwparserfromhell.parse(text)
    if str(code) != text:
        return None, ["wikitext does not round-trip through the parser; "
                      "page skipped"]
    notes = []
    for tpl in code.filter_templates():
        if normalize_template_name(tpl.name) not in names:
            continue
        for tr in transforms:
            op = tr["op"]
            if op == "rename-param":
                if not tpl.has(tr["from"]):
                    continue
                if tpl.has(tr["to"]):
                    notes.append(f"rename-param: both '{tr['from']}' and "
                                 f"'{tr['to']}' present; call left alone")
                    continue
                p = tpl.get(tr["from"])
                p.name = str(p.name).replace(tr["from"], tr["to"], 1)
            elif op == "add-param":
                if tr.get("ifAbsent", True) and tpl.has(tr["name"]):
                    continue
                if "from" in tr:
                    if not tpl.has(tr["from"]):
                        continue
                    value = str(tpl.get(tr["from"]).value).strip()
                else:
                    value = tr["value"]
                tpl.add(tr["name"], value)
            elif op == "remove-param":
                if tpl.has(tr["name"]):
                    tpl.remove(tr["name"])
            elif op == "map-values":
                if not tpl.has(tr["name"]):
                    continue
                p = tpl.get(tr["name"])
                raw = str(p.value)
                stripped = raw.strip()
                mapping = tr["map"]
                if stripped in mapping:
                    p.value = raw.replace(stripped, mapping[stripped], 1)
                elif (tr.get("unmapped", "report") == "report"
                        and stripped not in mapping.values()):
                    # already-mapped values stay quiet so re-runs are clean
                    notes.append(f"map-values: {tr['name']}='{stripped}' "
                                 f"not in the map; kept")
            else:
                raise BuildError(f"unknown template transform op: {op}")
    new_text = str(code)
    return (new_text if new_text != text else None), notes


def plan_page(title: str, base: str | None, live: str | None,
              new: str | None, *, version: str) -> dict:
    """Decide one shipped page's upgrade disposition from its three texts:
    base = the old version's shipped text (None if new in this version),
    live = the wiki's current text (None if the page does not exist),
    new = this version's shipped text (None if removed). Returns
    {title, action, text, markers, note}; `text` is the content to write
    for import/overwrite/merge. Only pristine pages (live == base) are
    overwritten or deleted; anything ambiguous is reported, never
    guessed."""
    def entry(action, text=None, markers=None, note=""):
        return {"title": title, "action": action, "text": text,
                "markers": markers, "note": note}

    def norm(t):
        return t.rstrip("\n") if t is not None else None

    if new is not None and norm(live) == norm(new):
        return entry("unchanged", note="already at the new version")
    if base is None:
        # New in this version.
        if live is None:
            return entry("import", text=new)
        return entry("exists-differs",
                     note="already exists on the wiki with different "
                          "content; not touched")
    if live is None:
        # Shipped before, absent from the wiki: the user deleted it.
        if new is None:
            return entry("skip", note="removed from the app and already absent")
        return entry("user-deleted",
                     note="user deleted this shipped page; not resurrected")
    pristine = norm(live) == norm(base)
    if new is None:
        # Removed in this version.
        if pristine:
            return entry("delete")
        return entry("modified-removed",
                     note="removed from the app but locally modified; "
                          "not deleted")
    if norm(base) == norm(new):
        return entry("skip", note="app did not change this page")
    if pristine:
        return entry("overwrite", text=new)
    merged, markers = merge_page(base, live, new, version=version)
    if merged is not None:
        return entry("merge", text=merged,
                     note="local edits merged with the app's changes")
    return entry("conflict", markers=markers,
                 note="local edits overlap the app's changes; resolve manually")


class Upgrade(Deploy):
    """Upgrade a deployed app instance to the version in the manifest,
    within the limited-upgrade contract: code and config always; pristine
    shipped pages automatically; user-modified shipped pages
    merge-or-report; everything else reported, never guessed. User
    content changes only through the manifest's explicit migrations."""

    def __init__(self, manifest: dict, app_dir: Path, args: argparse.Namespace):
        super().__init__(manifest, app_dir, args)
        # The registry records the instance's orchestrator; never run the
        # wrong mode against it (a compose-mode upgrade of a k8s instance
        # points the Helm release at an unpullable local-daemon image).
        recorded = (getattr(self, "_registry_record", None) or {}).get("orchestrator")
        supplied = getattr(args, "orchestrator", None)
        if recorded:
            recorded_k8s = recorded in ("kubernetes", "k8s")
            if supplied and (supplied in ("kubernetes", "k8s")) != recorded_k8s:
                raise BuildError(
                    f"instance '{self.id}' is recorded as '{recorded}' in "
                    f"the canasta registry, but -o says '{supplied}' — "
                    f"drop -o; upgrade uses the recorded orchestrator")
            self.orchestrator = recorded
            self.is_k8s = recorded_k8s
        elif not supplied:
            warn(f"instance '{self.id}' is not in this host's canasta "
                 f"registry; assuming {self.orchestrator} — pass -o if wrong")
        self.skip_backup = getattr(args, "skip_backup", False)
        self.report_only = getattr(args, "report_only", False)
        self.baseline = getattr(args, "baseline", None)
        # Set by preflight_upgrade (from .wicker-app/ state or --baseline).
        self.old_app: str | None = None
        self.old_version: str | None = None
        self.old_manifest: dict = {}
        self.base_pages: dict = {}
        self.migrations: list = []

    @property
    def state_dir(self) -> Path:
        return self.instance_dir / STATE_DIR_NAME

    # ---- instance location ---------------------------------------------- #

    def _locate_instance(self, args: argparse.Namespace) -> None:
        self._locate_instance_from_registry(args)

    # ---- wiki/domain resolution ----------------------------------------- #

    def _instance_wiki_urls(self) -> dict | None:
        """The live wiki-id -> domain map from the instance's
        config/wikis.yaml, or None when the file is absent (instance not
        deployed here, or pre-dates wikis.yaml)."""
        path = self.instance_dir / "config" / "wikis.yaml"
        if not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return {w["id"]: w.get("url", "")
                    for w in (data.get("wikis") or [])}
        except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
            raise BuildError(f"cannot parse {path}: {e}")

    def _resolve_wikis(self, domains: str | None) -> list[dict]:
        """Upgrade never re-points a wiki: existing wikis keep the domains
        recorded in the instance's config/wikis.yaml, so -n is only needed
        when the manifest adds wikis (then it is the full list, manifest
        order — the same contract as deploy — and the entries for existing
        wikis must match the live domains). Changing a domain is a canasta
        operation, not an upgrade."""
        live = self._instance_wiki_urls()
        if domains is None:
            declared = super()._resolve_wikis(None)
            wikis_yaml = self.instance_dir / "config" / "wikis.yaml"
            if live is None:
                raise BuildError(
                    f"{wikis_yaml} not found — upgrade reads the instance's "
                    f"existing domains from it. Deploy first, or pass "
                    f"-n/--domain-name.")
            added = [w["id"] for w in declared if w["id"] not in live]
            if added:
                raise BuildError(
                    f"the manifest adds wiki(s) the instance does not have: "
                    f"{', '.join(added)}. Pass -n/--domain-name with all "
                    f"{len(declared)} domain(s) in manifest order; the "
                    f"entries for existing wikis must match {wikis_yaml}.")
            return [{**w, "url": live[w["id"]]} for w in declared]
        wikis = super()._resolve_wikis(domains)
        for w in wikis:
            if live and w["id"] in live and w["url"] != live[w["id"]]:
                raise BuildError(
                    f"wiki '{w['id']}' is at '{live[w['id']]}' "
                    f"(config/wikis.yaml) but -n says '{w['url']}'. Upgrade "
                    f"never re-points a wiki — change the domain with "
                    f"canasta commands, then upgrade without -n.")
        return wikis

    # ---- preflight / state --------------------------------------------- #

    def preflight_upgrade(self) -> None:
        info("Preflight (upgrade)")
        missing = [t for t in ("docker", "canasta", "git") if not have(t)]
        if missing:
            raise BuildError(f"required tool(s) not in PATH: {', '.join(missing)}")
        constraint = (self.m.get("requires") or {}).get("canasta")
        if constraint:
            check_canasta_requirement(constraint)
        self.validate_secrets()
        if not self.instance_dir.is_dir():
            raise BuildError(
                f"instance directory not found: {self.instance_dir}\n"
                f"       upgrade targets an existing instance; deploy first")
        self._load_old_state()
        if self.old_app != self.m["name"]:
            raise BuildError(
                f"instance was deployed from app '{self.old_app}', not "
                f"'{self.m['name']}' — refusing to upgrade across apps")
        old_t = semver_triplet(self.old_version)
        new_t = semver_triplet(self.m["version"])
        if new_t == old_t:
            raise BuildError(
                f"instance is already at {self.old_version} — nothing to upgrade")
        if new_t < old_t:
            raise BuildError(
                f"downgrade {self.old_version} -> {self.m['version']} "
                f"is not supported")
        old_primary = (self.old_manifest.get("wikis") or [{}])[0].get("id")
        if old_primary and old_primary != self.wikis[0]["id"]:
            raise BuildError(
                f"the primary wiki cannot change on upgrade "
                f"('{old_primary}' -> '{self.wikis[0]['id']}')")
        self.migrations = applicable_migrations(
            self.m.get("migrations"), self.old_version, self.m["version"])
        # Fail fast on every lazily-imported dependency this run could
        # need: merge3 whenever shipped pages might three-way-merge, and
        # mwparserfromhell when template-calls migrations apply.
        deps = ["merge3"]
        if any(s.get("kind") == "template-calls"
               for e in self.migrations for s in e["steps"]):
            deps.append("mwparserfromhell")
        require_modules(*deps)
        info(f"Upgrading {self.m['name']} "
             f"{self.old_version} -> {self.m['version']}")

    def _load_old_state(self) -> None:
        """Populate old_app/old_version/old_manifest/base_pages from the
        instance's .wicker-app/ state, or from --baseline (a checkout of
        the deployed app version) for instances that predate state
        recording."""
        state_path = self.state_dir / "state.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text())
            except json.JSONDecodeError as e:
                raise BuildError(f"corrupt {state_path}: {e}")
            manifest_copy = self.state_dir / "manifest.yaml"
            if not manifest_copy.is_file():
                raise BuildError(f"missing {manifest_copy}")
            self.old_app = state.get("app")
            self.old_version = state.get("version")
            self.old_manifest = yaml.safe_load(manifest_copy.read_text())
            for title in state.get("pages", {}):
                f = self.state_dir / "content" / self._page_filename(title)
                if f.is_file():
                    self.base_pages[title] = f.read_text()
                else:
                    warn(f"state snapshot missing for '{title}'; it will be "
                         f"reported, not auto-upgraded")
            return
        if self.baseline:
            bman, bdir = load_manifest(Path(self.baseline).expanduser())
            validate_manifest(bman)
            self.old_app = bman["name"]
            self.old_version = bman["version"]
            self.old_manifest = bman
            self.base_pages = {
                title: src.read_text()
                for title, src in iter_content_pages(bman, bdir)
                if src.is_file()}
            info(f"Baseline state from {bdir} ({bman['name']} "
                 f"{bman['version']}, {len(self.base_pages)} page(s))")
            return
        raise BuildError(
            f"no app state at {state_path} and no --baseline given.\n"
            "       This instance predates state recording: pass "
            "--baseline <old-app-checkout> (the app directory of the "
            "version that was deployed), or use `wicker sync-code` for a "
            "code-only sync.")

    # ---- pipeline -------------------------------------------------------- #

    def run_upgrade(self) -> None:
        try:
            self.preflight_upgrade()
            if self.report_only:
                plan = self.plan_content()
                self.print_sync_plan()
                self.print_report(plan, applied=False)
                self.print_migration_preview()
                return
            self.backup()
            images = self.build_images()
            self.apply_images(images)
            self.install_env()
            self.install_secrets()
            self.remove_old_sidecars()
            self.install_sidecars()
            self.install_web_env_bridge()
            self.prune_components()
            self.install_extensions(overwrite=True)
            self.install_skins(overwrite=True)
            self.prune_settings()
            self.install_settings()
            # Stage public assets before the restart so the k8s PVC sync picks
            # them up (see run_pipeline).
            self.install_public_assets()
            self.canasta_restart()
            if not self.is_k8s:
                self.wait_healthy()
            self.add_new_wikis()
            plan = self.plan_content()
            self.apply_content(plan)
            migration_results = self.run_migrations()
            self.record_state(op="upgrade")
            self.print_report(plan, applied=True)
            self.print_migration_results(migration_results)
            info(f"Upgrade complete: {self.m['name']} "
                 f"{self.old_version} -> {self.m['version']}")
        finally:
            self._cleanup_staging()

    def backup(self) -> None:
        if self.skip_backup:
            info("Skipping pre-upgrade backup (--skip-backup)")
            return
        info("Backing up before upgrade")
        try:
            run(["canasta", "backup", "create", "-i", self.id,
                 "--tag", f"pre-upgrade-{self.m['version']}"],
                dry_run=self.dry_run)
        except BuildError as e:
            raise BuildError(
                f"{e}\n       upgrade mutates wiki content, so it insists "
                f"on a backup first: initialize one with `canasta backup "
                f"init -i {self.id}`, or pass --skip-backup to proceed "
                f"without")

    # ---- code & config sync --------------------------------------------- #

    def apply_images(self, images: dict) -> None:
        """Point the instance at the rebuilt web image via a config set the
        pipeline's restart picks up — the local tag on Compose, or a fresh
        `canasta image push` of the overlay context on k8s (create used
        --build-from; upgrade re-pushes to the same in-cluster registry)."""
        if self.is_k8s:
            web = (self.m.get("images", {}) or {}).get("web")
            if web:
                tag = self._push_image_k8s("web", web)
                # `canasta upgrade` rebuilds the web image from the tree
                # deploy recorded as --build-from. Re-stage it so that
                # rebuild reproduces this app version rather than the one
                # first deployed. Only for instances that have such a tree;
                # never mint one the canasta registry does not point at.
                if self._build_from_root().is_dir():
                    self._stage_web_build_from(web)
            elif self.base_image:
                # No web overlay: run the custom image directly. Same rule
                # as deploy — the cluster must be able to pull it.
                if not is_registry_qualified(self.base_image):
                    raise BuildError(
                        f"--base-image '{self.base_image}' is a local-only "
                        "tag; a Kubernetes cluster can't pull it. Push it "
                        "to a registry the cluster can reach and pass the "
                        "registry-qualified ref.")
                tag = self.base_image
            else:
                tag = None
        else:
            tag = images.get("web") or self.base_image
        if tag:
            info(f"Pointing the instance at web image {tag}")
            run(["canasta", "config", "set", "-i", self.id,
                 "--no-restart", f"CANASTA_IMAGE={tag}"],
                dry_run=self.dry_run)
        self.install_elasticsearch_image(images)

    def remove_old_sidecars(self) -> None:
        """Withdraw every sidecar the old manifest declared so
        install_sidecars can re-declare the new set (`canasta sidecar add`
        rejects duplicate names). Config-file level only — the actual
        containers change at the pipeline's restart."""
        new_names = {s["name"] for s in (self.m.get("sidecars") or [])}
        for name in sorted(s["name"]
                           for s in (self.old_manifest.get("sidecars") or [])):
            if name in new_names:
                info(f"Withdrawing sidecar declaration {name} "
                     f"(re-declared from the new manifest)")
            else:
                info(f"Removing sidecar {name} (dropped from the app)")
            run(["canasta", "sidecar", "remove", "-i", self.id, "--name", name],
                dry_run=self.dry_run)

    def prune_settings(self) -> None:
        """Remove settings fragments the old manifest installed but the
        new one no longer does. Canasta loads every file under
        config/settings/global/, so a renamed fragment would otherwise
        stay in place with its stale PHP still active."""
        new_installs = {s["install"] for s in (self.m.get("settings") or [])}
        for s in (self.old_manifest.get("settings") or []):
            rel = s["install"]
            if rel in new_installs:
                continue
            dest = self.instance_dir / rel
            info(f"Removing settings {Path(rel).name} (dropped from the app)")
            if self.dry_run:
                print(f"  $ rm -f {dest}")
            elif dest.is_file():
                dest.unlink()

    def prune_components(self) -> None:
        """Remove custom extensions/skins dropped from the manifest. The
        re-installs that follow overwrite the survivors."""
        for section in ("extensions", "skins"):
            removed = sorted(
                _custom_component_names(self.old_manifest, section)
                - _custom_component_names(self.m, section))
            for name in removed:
                dest = self.instance_dir / section / name
                info(f"Removing {section[:-1]} {name} (dropped from the app)")
                if self.dry_run:
                    print(f"  $ rm -rf {dest}")
                elif dest.exists():
                    shutil.rmtree(dest)
            if removed and self.is_k8s:
                warn(f"k8s: removed {section} linger in the instance PVC "
                     f"(no pruning support there yet)")

    def add_new_wikis(self) -> None:
        """Add wikis that are new in this manifest to the farm. Wikis
        dropped from the manifest are never deleted — only reported."""
        old_ids = {w["id"] for w in (self.old_manifest.get("wikis") or [])}
        dropped = sorted(old_ids - {w["id"] for w in self.wikis})
        if dropped:
            warn(f"wiki(s) dropped from the manifest are NOT deleted: "
                 f"{', '.join(dropped)}")
        added = [w for w in self.wikis[1:] if w["id"] not in old_ids]
        if not added:
            return
        info(f"Adding {len(added)} new wiki(s) to the farm")
        for w in added:
            cmd = ["canasta", "add", "-i", self.id, "-w", w["id"],
                   "-u", w["url"]]
            if w.get("siteName"):
                cmd += ["-t", w["siteName"]]
            run(cmd, dry_run=self.dry_run)

    # ---- shipped-content reconciliation ---------------------------------- #

    def _live_page_text(self, title: str) -> str | None:
        """The page's current wikitext, or None if it does not exist. Read
        via maintenance getText through the same exec path imports use, so
        it works against local, remote, and k8s instances."""
        argv = ["php", f"{MW_ROOT}/maintenance/run.php", "getText", title]
        result = self._canasta_exec(" ".join(shlex.quote(a) for a in argv),
                                    capture=True, check=False)
        if result.returncode != 0:
            err = ((result.stderr or "") + (result.stdout or "")).strip()
            if "does not exist" in err:
                return None
            raise BuildError(f"could not read page '{title}': {err}")
        return result.stdout

    def plan_content(self) -> list[dict]:
        """Compute every shipped page's disposition (see plan_page). Reads
        the live wiki but writes nothing — apply_content acts on the plan."""
        new_pages = {}
        for title, src in self._content_pages():
            if src.is_file():
                new_pages[title] = src.read_text()
            else:
                warn(f"missing source for {title}: {src}")
        titles = sorted(set(self.base_pages) | set(new_pages))
        if not titles:
            return []
        if self.dry_run:
            info(f"Would reconcile {len(titles)} shipped page(s) "
                 f"(content analysis needs a live instance)")
            return []
        info(f"Reconciling {len(titles)} shipped page(s)")
        return [plan_page(t, self.base_pages.get(t),
                          self._live_page_text(t), new_pages.get(t),
                          version=self.m["version"])
                for t in titles]

    def apply_content(self, plan: list[dict]) -> None:
        version = self.m["version"]
        conflicts_dir = self.state_dir / "conflicts"
        if conflicts_dir.exists():
            shutil.rmtree(conflicts_dir)
        for e in plan:
            action, title = e["action"], e["title"]
            if action in ("import", "overwrite", "merge"):
                summary = (f"Upgrade to {version} (wicker, merged with "
                           f"local edits)" if action == "merge"
                           else f"Upgrade to {version} (wicker)")
                print(f"  -> {title} ({action})")
                with tmp_text_file(e["text"], ".wikitext") as tmp:
                    self._mw_edit(title, tmp, summary=summary)
            elif action == "delete":
                print(f"  -> {title} (delete)")
                self._mw_delete(
                    title,
                    f"Upgrade to {version}: page removed from the app (wicker)")
            elif action == "conflict":
                dest = conflicts_dir / (self._page_filename(title) + ".merge")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(e["markers"])
                e["note"] += f"; markers written to {dest}"

    # ---- user-content migrations ------------------------------------------ #

    def _sql_json(self, query: str) -> list[dict]:
        """Rows of a read-only query, via `maintenance sql --query … --json`
        through the same exec path everything else uses."""
        argv = ["php", f"{MW_ROOT}/maintenance/run.php", "sql",
                f"--query={query}", "--json"]
        result = self._canasta_exec(" ".join(shlex.quote(a) for a in argv),
                                    capture=True)
        out = result.stdout
        # PHP notices may precede the JSON; take the last line that parses
        # as a JSON array rather than the first "[" in the stream.
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("["):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        raise BuildError(f"unexpected sql --json output: {out.strip()[:300]}")

    def _wiki_namespaces(self) -> dict:
        """The wiki's live namespace map (id -> name), extension
        namespaces included. MediaWiki owns these names — they live in PHP
        config, not the database — so ask the wiki via eval. Cached."""
        if getattr(self, "_namespaces_cache", None) is not None:
            return self._namespaces_cache
        code = ('print "WICKER_NS=" . json_encode( '
                'MediaWiki\\MediaWikiServices::getInstance()'
                '->getContentLanguage()->getNamespaces() ) . "\\n";')
        argv = ["php", f"{MW_ROOT}/maintenance/run.php", "eval"]
        with tmp_text_file(code + "\n", ".php") as tmp:
            result = self._canasta_exec(" ".join(shlex.quote(a) for a in argv),
                                        stdin_path=tmp, capture=True)
        out = result.stdout or ""
        m = re.search(r'WICKER_NS=(\{.*\})', out)
        if not m:
            raise BuildError(
                f"cannot read the wiki's namespace map: {out.strip()[:200]}")
        self._namespaces_cache = {
            int(k): v for k, v in json.loads(m.group(1)).items()}
        return self._namespaces_cache

    @staticmethod
    def _sql_dbkey(name: str) -> str:
        """A normalized name as a quoted-SQL-safe DB key. Backslashes are
        escaped too — MariaDB's default mode treats \\' as an escaped
        quote, so doubling quotes alone would let a trailing backslash
        break out of the literal."""
        return (name.replace(" ", "_")
                .replace("\\", "\\\\")
                .replace("'", "''"))

    def _template_match_names(self, template: str) -> set:
        """Every name a wikitext call of this template may use: the template
        itself and its Template-namespace redirects, each with and without
        the Template: prefix."""
        names = {normalize_template_name(template)}
        rows = self._sql_json(
            "SELECT page_title FROM redirect JOIN page ON page_id = rd_from "
            "WHERE rd_namespace = 10 AND page_namespace = 10 "
            f"AND rd_title = '{self._sql_dbkey(normalize_template_name(template))}'")
        names |= {normalize_template_name(r["page_title"]) for r in rows}
        # {{template:expense}} and {{Template:expense}} are valid calls of
        # the same template; normalize_template_name upcases the first
        # letter, so add prefixed forms of every name too.
        return names | {f"Template:{n}" for n in names}

    def _pages_transcluding(self, template: str):
        """(titles, notes): titles of every page transcluding the template
        — including via a redirect, since the links table records the
        resolved target — plus notes for pages in namespaces whose names
        Wicker cannot derive (extension namespaces live in PHP config)."""
        rows = self._sql_json(
            "SELECT page_namespace, page_title FROM templatelinks "
            "JOIN linktarget ON lt_id = tl_target_id "
            "JOIN page ON page_id = tl_from "
            "WHERE lt_namespace = 10 "
            f"AND lt_title = '{self._sql_dbkey(normalize_template_name(template))}'")
        titles, notes = [], []
        for r in rows:
            ns = int(r["page_namespace"])
            base = r["page_title"].replace("_", " ")
            if ns == 0:
                titles.append(base)
                continue
            name = (CANONICAL_NAMESPACES.get(ns)
                    or self._wiki_namespaces().get(ns))
            if name:
                titles.append(f"{name}:{base}")
            else:
                notes.append(f"{r['page_title']} skipped: the wiki has no "
                             f"name for namespace {ns}")
        return titles, notes

    def run_migrations(self) -> list[dict]:
        """Run every applicable migration entry (ascending `to`). Steps are
        idempotent by construction — a rename already applied finds no old
        parameter — so an upgrade interrupted after this phase can simply
        be re-run."""
        if not self.migrations:
            return []
        if self.dry_run:
            self.print_migration_preview()
            return []
        results = []
        for entry in self.migrations:
            desc = entry.get("description")
            info(f"Migration to {entry['to']}"
                 + (f": {desc}" if desc else ""))
            for step in entry["steps"]:
                if step["kind"] == "template-calls":
                    results.append(self._migrate_template_calls(step, entry))
                else:
                    self.run_actions([step],
                                     phase=f"migration to {entry['to']}")
        return results

    def _migrate_template_calls(self, step: dict, entry: dict) -> dict:
        template = step["template"]
        info(f"Rewriting calls of Template:{template}")
        names = self._template_match_names(template)
        titles, notes = self._pages_transcluding(template)
        shipped = {normalize_title(t) for t, _ in self._content_pages()}
        summary = f"{self.m['name']} {entry['to']} migration (wicker)"
        edited = unchanged = 0
        for title in titles:
            if normalize_title(title) in shipped:
                continue  # reconciliation already delivered the new text
            live = self._live_page_text(title)
            if live is None:
                continue
            new_text, page_notes = transform_template_calls(
                live, names, step["transforms"])
            notes += [f"{title}: {n}" for n in page_notes]
            if new_text is None:
                unchanged += 1
                continue
            print(f"  -> {title}")
            with tmp_text_file(new_text, ".wikitext") as tmp:
                self._mw_edit(title, tmp, summary=summary)
            edited += 1
        info(f"  {edited} page(s) rewritten, {unchanged} unchanged"
             + (f", {len(notes)} note(s)" if notes else ""))
        return {"to": entry["to"], "template": template, "edited": edited,
                "unchanged": unchanged, "notes": notes}

    def print_sync_plan(self) -> None:
        """The deterministic code/config half of the upgrade. It is always
        applied wholesale (no per-file diffing), so report-only spells it
        out instead of staying silent about everything but pages."""
        info(f"Code/config sync plan "
             f"({self.old_version} -> {self.m['version']}):")
        for svc in sorted(self.m.get("images") or {}):
            print(f"  rebuild image      {svc} (instance re-pointed at it)")
        for section in ("extensions", "skins"):
            kind = section[:-1]
            for n in sorted(_custom_component_names(self.m, section)):
                print(f"  re-sync {kind:<10} {n}")
            removed = sorted(
                _custom_component_names(self.old_manifest, section)
                - _custom_component_names(self.m, section))
            for n in removed:
                print(f"  remove {kind:<11} {n} (dropped from the app)")
        for s in self.m.get("settings", []) or []:
            print(f"  re-sync settings   {s['install']}")
        dropped_settings = (
            {s["install"] for s in (self.old_manifest.get("settings") or [])}
            - {s["install"] for s in (self.m.get("settings") or [])})
        for rel in sorted(dropped_settings):
            print(f"  remove settings    {rel} (dropped from the app)")
        new_sc = {s["name"] for s in (self.m.get("sidecars") or [])}
        old_sc = {s["name"] for s in (self.old_manifest.get("sidecars") or [])}
        for n in sorted(new_sc):
            print(f"  re-declare sidecar {n}"
                  + (" (new)" if n not in old_sc else ""))
        for n in sorted(old_sc - new_sc):
            print(f"  remove sidecar     {n} (dropped from the app)")
        old_ids = {w["id"] for w in (self.old_manifest.get("wikis") or [])}
        for w in self.wikis:
            if w["id"] not in old_ids:
                print(f"  add wiki           {w['id']} at {w.get('url')}")
        for wid in sorted(old_ids - {w["id"] for w in self.wikis}):
            print(f"  keep wiki          {wid} "
                  f"(dropped from the manifest; never deleted)")
        print("  restart instance   "
              "(env, secrets, and public assets re-applied)")

    def print_migration_preview(self) -> None:
        if not self.migrations:
            return
        info(f"Migration entries that would run ({len(self.migrations)}):")
        for e in self.migrations:
            desc = f" — {e['description']}" if e.get("description") else ""
            print(f"  to {e['to']}{desc}")
            for s in e["steps"]:
                target = (f" Template:{s['template']}"
                          if s["kind"] == "template-calls" else "")
                print(f"    {s['kind']}{target}")

    def print_migration_results(self, results: list[dict]) -> None:
        if not results:
            return
        info("Template-call migrations:")
        for r in results:
            print(f"  to {r['to']}  Template:{r['template']}: "
                  f"{r['edited']} page(s) rewritten, {r['unchanged']} unchanged")
            for n in r["notes"]:
                print(f"    NOTE {n}")
        total = sum(len(r["notes"]) for r in results)
        if total:
            warn(f"{total} migration note(s) need review (see above)")

    # Plan actions that require a human, surfaced at the end of the report.
    ATTENTION = ("conflict", "exists-differs", "modified-removed",
                 "user-deleted")

    def print_report(self, plan: list[dict], *, applied: bool) -> None:
        if not plan:
            info("No shipped content to reconcile")
            return
        info("Shipped-content reconciliation "
             + ("(applied):" if applied
                else "(report only — nothing written):"))
        for e in plan:
            line = f"  {e['action']:<17} {e['title']}"
            if e.get("note"):
                line += f"  — {e['note']}"
            print(line)
        attention = [e for e in plan if e["action"] in self.ATTENTION]
        if attention:
            warn(f"{len(attention)} page(s) need human attention (see above)")


class SyncCode(Deploy):
    """sync-code targets an existing instance, so its location comes from
    the canasta registry exactly like upgrade's does."""

    def _locate_instance(self, args: argparse.Namespace) -> None:
        self._locate_instance_from_registry(args)


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.rstrip().splitlines())


# --------------------------------------------------------------------------- #
# init — scaffold a new application
# --------------------------------------------------------------------------- #

def slug(text: str) -> str:
    """Derive a kebab-case app name from arbitrary text (e.g. a directory
    name): lowercase, non-alphanumeric runs collapse to a single '-', and
    leading/trailing '-' are trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def scaffold(target: Path, name: str, description: str) -> list[Path]:
    """Copy the skeleton tree into `target`, substituting the app name and
    description into the templated files. Returns the created file paths."""
    created: list[Path] = []
    for src in sorted(SKELETON_PATH.rglob("*")):
        if src.is_dir():
            continue
        dest = target / src.relative_to(SKELETON_PATH)
        text = (src.read_text()
                .replace("{{name}}", name)
                .replace("{{description}}", description))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        created.append(dest)
    return created


def init_app(args: argparse.Namespace) -> None:
    target = Path(args.path).expanduser().resolve()
    name = args.name or slug(target.name)
    if not NAME_RE.match(name):
        raise BuildError(
            f"invalid app name {name!r}: must be kebab-case "
            f"(lowercase letters, digits, and '-'); pass --name to override")
    if target.exists():
        if not target.is_dir():
            raise BuildError(f"target exists and is not a directory: {target}")
        if any(target.iterdir()):
            raise BuildError(
                f"target directory is not empty: {target}\n"
                f"       pick an empty or non-existent path")
    description = args.description or f"A Canasta wiki application ({name})"
    info(f"Scaffolding application '{name}' in {target}")
    created = scaffold(target, name, description)
    for path in created:
        print(f"  + {path.relative_to(target)}")
    print(f"""
==> Created {name} in {target}

  Next steps:
    1. Edit {MANIFEST_NAME} — add your extensions, settings, sidecars, and content.
    2. wicker validate {target}
    3. wicker deploy {target} -i <id> -n <domain> --dry-run
""")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wicker",
        description="Deploy a Canasta wiki application from its manifest.")
    sub = parser.add_subparsers(dest="command", required=True)

    ini = sub.add_parser("init", help="Scaffold a new application directory")
    ini.add_argument("path", help="Directory to create the application in")
    ini.add_argument("--name", help="App name (kebab-case; default: directory name)")
    ini.add_argument("--description", help="One-line app description")

    dep = sub.add_parser("deploy", help="Deploy an app onto a new Canasta instance")
    dep.add_argument("app_path", help=f"Path to the app dir or {MANIFEST_NAME}")
    dep.add_argument("-i", "--id", required=True, help="Canasta instance ID")
    dep.add_argument("-n", "--domain-name", required=True,
                     metavar="DOMAIN[,DOMAIN...]",
                     help="Instance domain(s), one per wiki in the manifest, "
                          "comma-separated and in manifest order. The primary "
                          "wiki takes a bare hostname (subdomains fine, no "
                          "path); each farm wiki takes its own hostname or a "
                          "domain/path (e.g. localhost/docs).")
    dep.add_argument("-p", "--path", default=".",
                     help="Parent directory for the instance (default: current "
                          "directory, matching `canasta create`)")
    dep.add_argument("-o", "--orchestrator", default="compose",
                     choices=["compose", "kubernetes", "k8s"],
                     help="Target orchestrator (default: compose)")
    dep.add_argument("--storage-class", help="k8s storage class (create -o k8s)")
    dep.add_argument("--access-mode", help="k8s PVC access mode (ReadWriteOnce/Many)")
    dep.add_argument("--ingress-class", help="k8s ingress class")
    dep.add_argument("--skip-tls", action="store_true", help="k8s: skip Let's Encrypt TLS")
    dep.add_argument("--base-image", metavar="IMAGE",
                     help="Deploy against a custom/unreleased Canasta image. "
                          "With a web image overlay: forwarded as --build-arg "
                          "BASE_IMAGE=IMAGE (the web Dockerfile must use ARG "
                          "BASE_IMAGE). Without an overlay: passed to canasta "
                          "create --canasta-image (on k8s the ref must be "
                          "registry-reachable, not a local-only tag).")
    dep.add_argument("--var", action="append", metavar="KEY=VALUE",
                     help="Template variable (repeatable)")
    dep.add_argument("--secret", action="append", metavar="KEY[=VALUE]",
                     help="Provide a value for a manifest-declared secret "
                          "(repeatable). KEY=VALUE sets it; bare KEY reads it "
                          "from the environment.")
    dep.add_argument("--skip-build", action="store_true",
                     help="Reuse existing images instead of rebuilding")
    dep.add_argument("--dry-run", action="store_true",
                     help="Print the steps without executing")

    upg = sub.add_parser(
        "upgrade",
        help="Upgrade a deployed app instance to this manifest's version "
             "(code and settings re-synced; shipped pages reconciled — "
             "pristine ones updated, user-modified ones merged or reported)")
    upg.add_argument("app_path", help=f"Path to the app dir or {MANIFEST_NAME}")
    upg.add_argument("-i", "--id", required=True, help="Canasta instance ID")
    upg.add_argument("-n", "--domain-name", required=False, default=None,
                     metavar="DOMAIN[,DOMAIN...]",
                     help="Only needed when the manifest adds wikis: the full "
                          "domain list, one per wiki in manifest order (same "
                          "rule as deploy). Existing wikis keep the domains "
                          "in the instance's config/wikis.yaml, and entries "
                          "for them must match it — upgrade never re-points "
                          "a wiki.")
    upg.add_argument("-p", "--path", default=None,
                     help="Normally omitted: upgrade uses the instance path "
                          "recorded in the canasta registry, and an explicit "
                          "mismatch is an error. Only consulted when the "
                          "instance is missing from this host's registry "
                          "(default then: current directory).")
    upg.add_argument("-o", "--orchestrator", default=None,
                     choices=["compose", "kubernetes", "k8s"],
                     help="Normally omitted: upgrade uses the orchestrator "
                          "recorded for the instance in the canasta "
                          "registry, and an explicit mismatch is an error. "
                          "Only consulted when the instance is missing from "
                          "this host's registry (default then: compose).")
    upg.add_argument("--baseline", metavar="OLD_APP_DIR",
                     help="Checkout of the app version this instance was "
                          "deployed with; bootstraps state for instances "
                          "deployed before state recording existed")
    upg.add_argument("--skip-backup", action="store_true",
                     help="Skip the pre-upgrade `canasta backup create`")
    upg.add_argument("--report-only", action="store_true",
                     help="Analyze shipped content against the live wiki and "
                          "print the reconciliation plan; write nothing")
    upg.add_argument("--base-image", metavar="IMAGE",
                     help="Base for the web image overlay (see deploy)")
    upg.add_argument("--var", action="append", metavar="KEY=VALUE",
                     help="Template variable (repeatable)")
    upg.add_argument("--secret", action="append", metavar="KEY[=VALUE]",
                     help="Provide a value for a manifest-declared secret "
                          "(repeatable)")
    upg.add_argument("--skip-build", action="store_true",
                     help="Reuse existing images instead of rebuilding")
    upg.add_argument("--dry-run", action="store_true",
                     help="Print the steps without executing (content "
                          "analysis is skipped; use --report-only for it)")

    val = sub.add_parser("validate", help="Validate a manifest against the schema")
    val.add_argument("app_path", help=f"Path to the app dir or {MANIFEST_NAME}")

    syn = sub.add_parser(
        "sync-code",
        help="Re-sync extension and skin code into an existing instance and "
             "restart (code only; leaves settings, env, secrets, and content "
             "untouched)")
    syn.add_argument("app_path", help=f"Path to the app dir or {MANIFEST_NAME}")
    syn.add_argument("-i", "--id", required=True, help="Canasta instance ID")
    syn.add_argument("-p", "--path", default=None,
                     help="Normally omitted: sync-code uses the instance "
                          "path recorded in the canasta registry, and an "
                          "explicit mismatch is an error. Only consulted "
                          "when the instance is missing from this host's "
                          "registry (default then: current directory).")
    syn.add_argument("--dry-run", action="store_true",
                     help="Print actions without changing anything")
    # Deploy.__init__ reads these directly; they do not apply to a code sync.
    syn.set_defaults(skip_build=False, var=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            init_app(args)
            return 0
        manifest, app_dir = load_manifest(Path(args.app_path).expanduser())
        validate_manifest(manifest)
        if args.command == "validate":
            print(f"OK: {manifest['name']} {manifest['version']} "
                  f"({app_dir / MANIFEST_NAME})")
            return 0
        if args.command == "upgrade":
            Upgrade(manifest, app_dir, args).run_upgrade()
        elif args.command == "sync-code":
            SyncCode(manifest, app_dir, args).sync_code()
        else:
            Deploy(manifest, app_dir, args).run_pipeline()
    except BuildError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
