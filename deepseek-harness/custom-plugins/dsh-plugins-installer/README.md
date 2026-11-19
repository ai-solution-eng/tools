# Plugins installer (`@local/dsh-plugins-installer`)

Installs **every sibling bundle** in the `custom-plugins` folder, so one install
brings the whole collection into the profile — and each plugin keeps its own
on/off switch on the Plugins page.

## Install

```
Plugins → Add plugin → link:/path/to/tools/deepseek-harness/custom-plugins/dsh-plugins-installer
```

That installs the installer. On its first activation it scans its own parent
directory and installs every sibling that declares a `dsh.bundle` patch.

Installing one plugin on its own still works exactly as before — point at that
plugin's directory instead:

```
Plugins → Add plugin → link:/path/to/tools/deepseek-harness/custom-plugins/dsh-mcp
```

## What it installs

A sibling is installed when its `package.json` declares `dsh.bundle`. A plain
package (no bundle patch) is skipped, and the installer never installs itself.

Each sibling goes in as its **own bundle** through
`pluginManager.installBundle()` — the same call the Plugins page makes — so
afterwards they are ordinary entries in `dsh.profile.bundles` and can be
switched on or off individually. Nothing here is a meta-package: the plugins
stay independent.

## Behaviour

- **One pass, no restarts.** An install writes `package.json` and `node_modules`;
  it does not touch `cordis.patch.yml`. A supervisor that watches the profile
  patch (the `DSH_WATCH_CONFIG_FILES` list, by default `.env`,
  `profiles/web/cordis.patch.yml`, `profiles/web/dsh.profile`) therefore does not
  restart the Host for an install, and the newly added bundles apply live
  through HMR.
- **Idempotent.** A sibling already declared in the profile is skipped, so a
  pass that *is* interrupted resumes on the next boot.
- **Sequential.** One install at a time; `pnpm` runs are not overlapped.
- **Waits for the manager.** The pass starts when `pluginManager` mounts, not on
  a guessed delay — this row can activate before that service exists.
- **Silent when done.** With everything installed it logs one line and stops.

### Removals come back

The pass runs on every activation, so a plugin you *uninstall* while the
installer is enabled will be reinstalled on the next boot. Disable the installer
first if you want a removal to stick.

## Settings

```yaml
- id: plugins-installer
  name: '@local/dsh-plugins-installer'
  config:
    installOnStart: true   # run the pass at all
    delayMs: 2000          # wait for the Host to settle before writing
```

## Verify

```bash
node test/check.mjs
```

Covers the scan (bundle vs plain package vs missing manifest), that it never
lists itself, and the settings bounds.
