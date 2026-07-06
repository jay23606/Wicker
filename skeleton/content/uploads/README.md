# Wiki media

Media files (images, PDFs, other uploads) imported into the wikis at deploy
time via MediaWiki's `importImages.php`. Each becomes a `File:` page the wiki
users own from then on — just like starter [`pages/`](../pages/), but binary.

The **directory layout picks the target wiki**. Files sitting directly in
this directory go to the first wiki; files in a `<wiki-id>/` subdirectory go
to that wiki:

```
uploads/logo.png              -> uploaded to the first wiki
uploads/diagram.png           -> uploaded to the first wiki
uploads/docs/screenshot.png   -> uploaded to the "docs" wiki
uploads/team/photo.jpg        -> uploaded to the "team" wiki
```

Only one level of subdirectory is meaningful (the wiki id); a subdirectory
whose name matches no wiki in the instance is skipped with a warning. The
wikis come from the required `wikis:` list in
[`../../wicker.yaml`](../../wicker.yaml).

## How it maps to `wicker.yaml`

```yaml
content:
  uploads:
    dir: ./content/uploads      # this directory (the default)
```

## Compose-only for now

Importing stages the files through the instance's bind-mounted `images/`
directory so the web container can read them. That's the Compose layout; on
Kubernetes `images/` is a PVC, so this step warns and is skipped there.
