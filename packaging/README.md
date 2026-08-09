# Independent component packages

`scripts/build_components.ps1` creates one isolated Windows onedir build and
one ZIP per AutoScript component. Versions are read only from
`component_versions.json`.

```powershell
.\scripts\build_components.ps1
.\scripts\build_components.ps1 -Components builder,runner
```

Each ZIP has its executable, component-specific runtime directory, and
`component.json` descriptor at the archive root. A sibling `.sha256` file authenticates the exact
archive bytes, and `component-artifacts.json` contains the catalog fields used
when drafting a signed update release.

When `interface` is built, the script also emits an external bootstrap ZIP.
That package contains the stable `AutoScriptLauncher.exe` and the initial
`AutoScriptInterface.exe` version, each with an isolated runtime directory. It
does not contain Builder, Runner, or Analyzer. Later Interface updates replace
the managed Interface component while the bootstrap remains stable.

The opt-in GitHub Actions draft-release step validates all archives, constructs
`autoscript-update-catalog.json`, and signs its exact bytes with the Ed25519 key
stored in the `AUTOSCRIPT_UPDATE_SIGNING_KEY` repository secret. The matching
public key is pinned in `update_trust.json`; the private seed is never written
to an artifact or log.

The component packages intentionally do not share a Python or Qt runtime.
This makes installation, activation, rollback, and updates independent. The
legacy `TouchpadExperiment.spec` and portable release remain available during
the migration period.
