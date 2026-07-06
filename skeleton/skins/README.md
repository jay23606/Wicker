# Custom skins

Custom MediaWiki skins that ship with this app go here, one directory per
skin. Wicker copies each into the instance's `skins/` directory at deploy
time.

This directory is only for **custom** skins — ones Wicker installs from a
local path or a git URL. Canasta-**bundled** skins (already present on the
image) don't live here; you list them by name in `wicker.yaml` so your
settings fragment can load them, but Wicker doesn't install them.

## How it maps to `wicker.yaml`

Each skin is one entry under `skins:` in
[`../wicker.yaml`](../wicker.yaml):

```yaml
skins:
  - name: Vector                          # bundled — name only, loaded by settings
  - name: MySkin                          # custom from a local path
    source: ./skins/MySkin
  - name: SomeUpstream                    # custom from git
    git: https://github.com/example/SomeUpstream.git
    ref: master                           # branch, tag, or commit (shallow clone)
```

A local skin placed in `./skins/MySkin/` is referenced with
`source: ./skins/MySkin`. Loading and configuring the skin
(`wfLoadSkin`, `$wgDefaultSkin`, settings) is the job of your fragment in
`../settings/`.
