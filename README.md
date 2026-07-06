# Wicker

Wicker is a builder for **Canasta wiki applications** — packaged,
multi-component MediaWiki apps (custom extensions and skins, sidecars, a
derived web image, operator settings, starter content, and post-install
maintenance) described by a single declarative manifest and deployed with
one command.

v1 is a standalone Python script that reads a `wicker.yaml` manifest
(`apiVersion: wicker-app/v1`) and drives the existing `canasta` CLI.
It requires Canasta CLI version 4.9.1 or above.

## Commands

**`wicker init <path>`** scaffolds a new application: the directory tree
(`extensions/`, `skins/`, `images/`, `settings/`, `sidecars/`,
`content/pages/`, `content/uploads/`, `public_assets/`), a `README.md` in
each directory explaining what goes there, and a skeleton `wicker.yaml` —
a valid minimal manifest with every optional section present but
commented out. Pass `--name` (default: the directory name, kebab-cased)
and `--description` to seed the manifest.

**`wicker validate <app>`** checks the manifest against the schema.

**`wicker deploy <app> -i <id> -n <domain[,domain...]>`** creates a fresh
Canasta instance (Docker Compose or Kubernetes) and installs the whole
app onto it:

1. Create the instance (`canasta create`), building and wiring in the
   app's derived `web` and `elasticsearch` images if it has them.
2. Apply instance `env` and deliver declared `secrets`
   (see [Secrets](#secrets)).
3. Install the app's sidecars, custom extensions and skins, settings
   fragments, and public assets, then restart.
4. Add the remaining farm wikis, import the starter pages and media, and
   run the `postInstall` actions.
5. Record the app state in `<instance>/.wicker-app/` — the baseline a
   later `wicker upgrade` compares shipped pages against. It lives in the
   instance directory, so backups capture it too.

Domains are supplied at deploy time, not in the manifest: one per wiki,
comma-separated, in manifest order. The primary (first) wiki takes a
hostname — any domain or subdomain (`wiki.example.com`), but no path.
Each additional farm wiki takes either its own hostname
(`docs.example.com`) or a path on a shared one (`example.com/docs`),
matching `canasta add`.

**`wicker sync-code <app> -i <id>`** refreshes extension and skin code in
an already-deployed instance, then restarts. Code only — settings, env,
secrets, and content are left untouched.

**`wicker upgrade <app> -i <id>`** moves a deployed instance
to the manifest's (higher) version — see
[Upgrading a deployed app](#upgrading-a-deployed-app). Existing wikis
keep the domains in the instance's `config/wikis.yaml`; `-n` is only
needed when the manifest adds wikis (changing a domain is a `canasta`
operation, not an upgrade).

A deploy targets the machine Wicker runs on: the instance directory is
written and images are built locally. To deploy a Compose app onto
another machine, run Wicker on that machine. A Kubernetes cluster may be
remote — the instance directory stays on the local controller and Canasta
delivers the app to the cluster — but an app with derived images needs
the controller to be the cluster's control-plane node, where the image
builds and the in-cluster registry's push endpoint live.

## Application layout

One file is the spine: `wicker.yaml`, the application manifest. Every
other directory exists because the manifest points at it, so the tree
mirrors the manifest section-for-section. `wicker init` scaffolds this
layout, with a `README.md` in each directory explaining what goes there.

| `wicker.yaml` section | Directory | What it holds |
|---|---|---|
| `wikis` | (in `wicker.yaml`) | The instance's wikis (required). The first is the primary — its id names the instance; list more to build a farm. Each may set `siteName` (defaults to the id). Domains are not declared here — they are passed at deploy time via `-n/--domain-name`, one per wiki in this order. |
| `extensions[].source` / `.git` | `extensions/` | Custom MediaWiki extensions, one subdir each, copied into `<instance>/extensions/`. (Bundled extensions are name-only in the manifest — no directory.) |
| `skins[].source` / `.git` | `skins/` | Custom MediaWiki skins, one subdir each, copied into `<instance>/skins/`. (Bundled skins are name-only in the manifest — no directory.) |
| `images.web` / `.elasticsearch` | `images/` | Derived-image build contexts for the two services Canasta can point at a custom image — e.g. OS packages or php.ini settings for `web`, an analysis plugin or alternate collation for `elasticsearch`. Additional containers are sidecars. |
| `sidecars[].build` | `sidecars/` | Sidecar service build contexts, one subdir each, deployed via Canasta's `canasta sidecar` primitive. |
| `settings[]` | `settings/` | Operator settings fragments installed into `<instance>/config/settings/global/`. |
| `content.pages` | `content/pages/` | Starter wiki content. Each file's path mirrors its page title (`content/pages/Template/Foo.mediawiki` → `Template:Foo`). |
| `content.uploads` | `content/uploads/` | Media imported into the wikis via `importImages.php`. Loose files go to the first wiki; a `<wiki-id>/` subdir targets that wiki. Compose-only. |
| `content.postInstall` | (in `wicker.yaml`) | Post-install maintenance (e.g. `update.php`, Cargo table creation). |
| `publicAssets` | `public_assets/` | Static files (logos, favicons, `robots.txt`) copied verbatim into `<instance>/public_assets/`, served by the web tier. |

A directory is only needed if the corresponding manifest section is —
delete the ones you don't use.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Scaffold a new application, then fill in the manifest and add your
# pages, extensions, sidecars, etc.:
./wicker init ~/apps/MyApp

# Validate a manifest:
./wicker validate ~/apps/MyApp

# Preview the steps without touching Docker or canasta:
./wicker deploy ~/apps/MyApp -i myapp -n localhost -p ~/instances --dry-run

# Deploy for real:
./wicker deploy ~/apps/MyApp -i myapp -n localhost -p ~/instances

# Refresh extension and skin code in an already-deployed instance:
./wicker sync-code ~/apps/MyApp -i myapp

# Upgrade a deployed instance to the manifest's (higher) version:
./wicker upgrade ~/apps/MyApp -i myapp

# See what an upgrade would do to shipped pages, without writing anything:
./wicker upgrade ~/apps/MyApp -i myapp --report-only
```

Flags:

- `-n/--domain-name` (required for deploy): the instance domain(s), one
  per wiki in manifest order, comma-separated (see above for the accepted
  forms). On upgrade it is optional — needed only when the manifest adds
  wikis, and the entries for existing wikis must match the instance's
  `config/wikis.yaml`.
- `-p/--path`: parent directory the instance is created under (default:
  the current directory, matching `canasta create`). Deploy-time choice
  only — upgrade uses the instance path recorded in the canasta registry
  and rejects an explicit mismatch.
- `-o/--orchestrator`: `compose` (default), `kubernetes`, or `k8s`.
  Deploy-time choice only — upgrade uses the orchestrator recorded for
  the instance in the canasta registry and rejects an explicit mismatch.
- `--var KEY=VALUE` (repeatable): template substitution for settings
  fragments.
- `--secret KEY=VALUE` (repeatable): a value for a declared secret; bare
  `--secret KEY` reads it from the environment.
- `--skip-build`: reuse existing images.
- `--base-image IMAGE`: deploy against a custom/unreleased Canasta image.
  With a web image overlay it is passed as the `BASE_IMAGE` build
  argument (see `images/README.md` in a scaffolded app); without one it
  is passed to `canasta create --canasta-image`. On k8s the ref must be
  registry-reachable, not a local-only tag.
- `--dry-run`: print the steps without executing them.
- Kubernetes pass-throughs to `canasta create`: `--storage-class`,
  `--access-mode`, `--ingress-class`, `--skip-tls`.

### Secrets

A `secrets:` entry declares a name and a `target` — `web`, `env`, or
`sidecar:<name>` (a scalar or a list). The value is supplied at deploy
time with `--secret` (or from the environment) — never stored in the
manifest, and redacted in the deploy log. Wicker routes it by target: a
`sidecar:<name>` secret is delivered to that sidecar's environment and
withheld from the web tier; a `web` secret reaches MediaWiki's
`getenv()`; `env` keeps it in the instance `.env` only. On Kubernetes
every secret value lives in the per-instance Secret, so none lands in the
gitops repo. Required secrets (the default) fail fast in preflight if no
value is provided.

## Upgrading a deployed app

`wicker upgrade` moves a deployed instance to the manifest's version
under a limited, explicit contract — deterministic changes are applied,
anything ambiguous is reported and left alone:

1. **Preflight**: same app, strictly higher version, primary wiki
   unchanged, instance state present (`.wicker-app/`, recorded at
   deploy). An instance deployed before state recording existed needs
   `--baseline <old-app-checkout>` once, to reconstruct the state.
2. **Backup**: `canasta backup create --tag pre-upgrade-<version>` (opt
   out with `--skip-backup`; the backup repo must be initialized).
3. **Code & config re-sync**: images rebuilt and re-delivered,
   env/secrets re-set, sidecars re-declared (dropped ones removed),
   custom extensions/skins overwritten (dropped ones pruned), settings
   and public assets re-installed, new farm wikis added (dropped wikis
   are never deleted), then restart.
4. **Shipped-page reconciliation**, per page: pristine pages (live text
   == what the old version shipped) are overwritten or deleted to match
   the new version; user-modified pages get a three-way merge — clean
   merges are saved, conflicts are written to `.wicker-app/conflicts/`
   and reported, never saved to the wiki. Pages the user deleted are not
   resurrected; pages removed from the app but locally modified are not
   deleted.
5. **User-content migrations**: the manifest's `migrations:` entries with
   installed-version < `to` <= new version run in ascending order. Steps
   are the post-install action kinds plus `template-calls`, which
   rewrites every call of a template across user pages with deterministic
   transforms: `rename-param`, `add-param`, `remove-param`, and
   `map-values`. Calls via the template's redirects are matched; pages
   whose wikitext can't be rewritten safely are reported, never guessed.
   Rebuild derived storage (e.g. Cargo) with a `maintenance` step after
   the rewrites.
6. **State rewrite + report**: `.wicker-app/` now reflects the new
   version (the next upgrade's base), and every page's disposition is
   printed.

`--report-only` prints the reconciliation plan and the migrations that
would run, and writes nothing — run it first. Migration steps are
idempotent (a rename already applied finds nothing to do), so an
interrupted upgrade can simply be re-run.

## Multiple applications on one cluster

Wicker is one instance per application, but a single Canasta
**Kubernetes** cluster — even a single node — can host many apps. Each
`wicker deploy` creates its own Canasta instance in its own namespace;
the apps share the cluster's infra (ingress controller, cert-manager,
Argo CD, image registry). Give each app a distinct `-i` (id) and a
distinct `-n` domain:

```bash
./wicker deploy <appA> -i appa -n appa.example.com -p <path> -o kubernetes …
./wicker deploy <appB> -i appb -n appb.example.com -p <path> -o kubernetes …
```

The **domain is the differentiator** — Canasta's k8s Ingress routes
purely by host. Paths separate *wikis within one app's farm*
(`example.com/wikiA` vs `/wikiB` inside a single instance), not separate
apps — two apps can't share a hostname distinguished only by path.

This is where k8s beats Compose for co-hosting: the shared ingress fronts
every app on `:80`/`:443` and routes by hostname. With Compose each
instance binds the host ports directly, so co-hosted instances would need
distinct `HTTP_PORT`/`HTTPS_PORT`.

## Status

Implemented: `init`, `validate`, `deploy` (on both Compose and
Kubernetes), `sync-code`, and `upgrade`.

**Current limits.** `content.uploads` media import is Compose-only. A
sidecar's `sysctls` and `secrets` fields are not yet supported by
Canasta's sidecar primitive — Wicker warns and drops them; for secret
values use the top-level [`secrets:`](#secrets) section instead.

**Out of scope by design.** One instance is one application. Wicker does
not do `uninstall` (that's `canasta delete` of the whole instance),
`list`/`status` (that's `canasta list`), or add-on installs into an
existing instance.

## License

MIT.
