# Custom extensions

Custom MediaWiki extensions that ship with this app go here, one
directory per extension. Wicker copies each into the instance's
`extensions/` directory at deploy time.

This directory is only for **custom** extensions — ones Wicker installs
from a local path or a git URL. Canasta-**bundled** extensions (already
present on the image) don't live here; you list them by name in
`wicker.yaml` so your settings fragment can load them, but Wicker doesn't
install them.

## How it maps to `wicker.yaml`

Each extension is one entry under `extensions:` in
[`../wicker.yaml`](../wicker.yaml):

```yaml
extensions:
  - name: PageForms                       # bundled — name only, loaded by settings
  - name: MyExtension                     # custom from a local path
    source: ./extensions/MyExtension
  - name: SomeUpstream                    # custom from git
    git: https://github.com/example/SomeUpstream.git
    ref: master                           # branch, tag, or commit (shallow clone)
```

A local extension placed in `./extensions/MyExtension/` is referenced with
`source: ./extensions/MyExtension`. Loading and configuring the extension
(`wfLoadExtension`, settings) is the job of your fragment in `../settings/`.
