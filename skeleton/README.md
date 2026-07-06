# {{name}}

A Canasta wiki application, built and deployed with
[Wicker](https://github.com/amethyst-ck/Wicker).

This directory was scaffolded by `wicker init`. Everything the app is
made of — its extensions, skins, derived images, sidecars, settings, and
starter content — is declared in [`wicker.yaml`](wicker.yaml) and lives in
the subdirectories below. Deploy the whole thing onto a fresh Canasta
instance with one command.

## Layout

```
wicker.yaml      The manifest — the single source of truth for the app.
extensions/      Custom MediaWiki extensions Wicker installs (source or git).
skins/           Custom MediaWiki skins Wicker installs (source or git).
images/          Dockerfiles for per-service derived images (e.g. a web image).
settings/        Operator config fragments installed into the instance.
sidecars/        Companion containers (build contexts) the app runs alongside.
content/         Starter wiki pages, media uploads, and post-install steps.
public_assets/   Static files (logos, favicons) served by the web tier.
```

Each subdirectory has its own `README.md` explaining what goes there and
how it maps to a section of `wicker.yaml`. Sections you don't need can be
deleted — only `apiVersion`, `name`, `version`, `description`, and `wikis`
are required.

## Build it up

The `wicker` command is the script at the top of a
[Wicker](https://github.com/amethyst-ck/Wicker) checkout — run it as
`./wicker` from that checkout, or put it on your `PATH`. Run the commands
below from outside this directory, passing the app's path (shown here as
`~/apps/{{name}}` — substitute wherever this directory actually lives).

1. Fill in `wicker.yaml`, uncommenting and editing the sections you need.
2. Validate as you go:
   ```bash
   ./wicker validate ~/apps/{{name}}
   ```
3. Preview a deploy without touching Docker or canasta:
   ```bash
   ./wicker deploy ~/apps/{{name}} -i {{name}} -n localhost -p ~/canasta-apps --dry-run
   ```
4. Deploy for real (greenfield — Wicker creates the Canasta instance):
   ```bash
   ./wicker deploy ~/apps/{{name}} -i {{name}} -n localhost -p ~/canasta-apps
   ```
