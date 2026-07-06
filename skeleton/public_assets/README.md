# Public assets

Static files served directly by the wiki's web tier — logos, favicons,
`robots.txt`, downloadable files referenced from wiki pages. At deploy time
Wicker copies this directory's contents into the instance's `public_assets/`.

The tree is copied **verbatim**, so lay it out the way it should appear under
`public_assets/`. Canasta keys per-wiki assets by a `<wiki-id>/` subdirectory
(that's where a wiki's logo lives), so a farm's logos go under those:

```
public_assets/logo.png            -> public_assets/logo.png   (shared)
public_assets/main/logo.png       -> the "main" wiki's logo
public_assets/docs/logo.png       -> the "docs" wiki's logo
```

## How it maps to `wicker.yaml`

One line in [`wicker.yaml`](../wicker.yaml) points at this directory:

```yaml
publicAssets: ./public_assets
```

Your settings fragment references the files by their served path, e.g.
`$wgLogos['1x'] = '/public_assets/main/logo.png';`.
