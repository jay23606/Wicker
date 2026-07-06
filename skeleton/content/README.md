# Starter content

Seed wiki content and post-install steps for the app. Wicker imports the
pages and runs the post-install actions once, at deploy time; from then on
they're ordinary wiki pages the users own and adapt.

## Pages

Starter pages live under [`pages/`](pages/) — see that directory's README
for the file-naming convention. The authoritative import list (and its
order) is `content.pages.list` in [`../wicker.yaml`](../wicker.yaml):

```yaml
content:
  pages:
    dir: ./content/pages
    list:
      - Main Page
      - Template:Example
```

## Media

Binary uploads (images, PDFs) live under [`uploads/`](uploads/) — see that
directory's README. The subdirectory layout picks the target wiki (loose
files go to the first wiki, `uploads/<wiki-id>/` to that wiki):

```yaml
content:
  uploads:
    dir: ./content/uploads
```

## Post-install actions

After the pages and media are imported, Wicker runs `content.postInstall`
actions in
order. The kinds are `update` (run `update.php`), `maintenance` (run a core
or extension maintenance script), `page-edit` (write one page), and `shell`
(run a command in the web container, a sidecar, or the host):

```yaml
content:
  postInstall:
    - kind: update
    - kind: maintenance
      extension: Cargo
      script: cargoRecreateData.php
      args: ["--table=Example"]
```
