# Operator settings

PHP configuration fragments for the app go here. Wicker installs each into
the instance at deploy time; Canasta loads everything under
`config/settings/global/` automatically.

This is where you load and configure your extensions — `wfLoadExtension`
calls, `$wg…` settings, MIME registrations, skin tweaks. A single
`Settings.php` is usually enough.

## How it maps to `wicker.yaml`

Each fragment is one entry under `settings:` in
[`../wicker.yaml`](../wicker.yaml):

```yaml
settings:
  - path: ./settings/Settings.php
    install: config/settings/global/Settings.php
```

The `install` path is relative to the instance directory. A fragment can
be templated — give it a `vars:` map and `{{KEY}}` placeholders in the file
are substituted (values may reference deploy-time `--var KEY=VALUE`):

```yaml
settings:
  - path: ./settings/Settings.php
    install: config/settings/global/Settings.php
    vars:
      MY_DEFAULT: "value"
```

Example `settings/Settings.php`:

```php
<?php
wfLoadExtension( 'PageForms' );
wfLoadExtension( 'Cargo' );
// $wgMyAppSomething = '{{MY_DEFAULT}}';
```
