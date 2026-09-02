# AutoScript component updates

AutoScript is delivered as a small, stable launcher plus four independently
versioned components:

| Component | Installed by the bootstrap | Version source |
| --- | --- | --- |
| Main Interface (`interface`) | Yes, initially | `component_versions.json` |
| Builder (`builder`) | No | `component_versions.json` |
| Runner (`runner`) | No | `component_versions.json` |
| Analyzer (`analyzer`) | No | `component_versions.json` |

All four component versions start at `0.0`. Experiment, Block, raw-result and
analysis schema versions are separate data-contract versions and must never be
derived from a component version.

## Distribution model

The application does not update by running `git pull`. GitHub Releases stores
immutable component ZIPs and a small signed catalog. The catalog contains the
version, exact byte length, SHA-256 digest, entry point, protocol version and
minimum launcher version for every component. The client accepts a catalog
only after an Ed25519 signature verifies against `update_trust.json`.

New release catalogs are valid for 180 days by default. The manual release
workflow accepts a bounded 1-365 day validity period. Expiration prevents a new
install or update from trusting stale release metadata; it does not disable
components that are already installed. Publishing the next signed release
renews the validity window.

Each component ZIP also contains a root `component.json`. The updater verifies
that its component ID, version, platform, architecture, entry point, protocol
and source commit agree with the signed catalog before activation. An archive
is rejected for unsafe paths, links, Windows device names, duplicate paths,
unexpected expansion, checksum mismatch or descriptor mismatch.

The current GitHub transport is deliberately replaceable. If download volume
or organization policy later requires object storage/CDN, the signed catalog
and ZIP contract can remain unchanged while only the allowed download host and
publishing workflow change.

## Local installation and rollback

The external bootstrap contains only `AutoScriptLauncher.exe` and the initial
Main Interface. Managed components are installed per user below:

```text
%LOCALAPPDATA%\AutoScript\
  components\<component>\<version>\
  state\active\<component>.json
  state\installed.json
  cache\
  .staging\
```

Downloads first enter same-volume staging. After validation, the updater moves
the new immutable version directory into place and atomically switches the
active pointer. It never overwrites a running executable. The previous version
remains available for rollback. A process/OS lock prevents concurrent updates.
Existing data below `%APPDATA%\TouchpadExperimentManager` is not moved or
deleted, and the legacy all-in-one portable build remains usable during the
transition.

## Release procedure

1. Change only the intended values in `component_versions.json`.
2. Open a pull request and let the Windows matrix build and test every isolated
   package. Pull-request builds do not receive signing secrets.
3. After review, run **Independent component builds** manually with draft
   release creation enabled. The workflow requires the repository secret
   `AUTOSCRIPT_UPDATE_SIGNING_KEY` and fails closed if it is unavailable.
4. Verify the draft assets, checksums, `component.json` files, source commit and
   signed catalog. The workflow also records GitHub build-provenance
   attestations for the ZIPs. Publish/promote only after the source commit is
   approved.
5. Never move or replace an existing component release tag. Increase the
   relevant component version and catalog sequence for any correction.

The signing secret is a base64-encoded raw 32-byte Ed25519 private seed. It is
stored only as a GitHub Actions secret. The corresponding public key is pinned
under key ID `release-2026-08` in `update_trust.json`.

### Key rotation

1. Generate the new key off-repository and store only its public key in
   `update_trust.json`.
2. Ship that trust update through a catalog signed by the existing trusted key.
3. Change the release workflow key ID and secret only after clients have the
   expanded trust set.
4. During a planned overlap, increase the trust threshold or include signatures
   from both keys as required by the bundled trust policy.
5. Remove the old public key only in a later Main Interface release.

## Why GitHub now, and what may replace it

GitHub Releases is a good fit at the current project size because releases are
tied to reviewed commits, assets can be immutable, and Actions can build and
sign without exposing a private key to clients. The updater still verifies its
own signed metadata and hashes instead of treating the transport as trusted.

For a conventional Windows installation channel, the preferred later step is
an MSIX/App Installer package for the launcher and Main Interface. Windows can
then handle Start-menu registration, uninstall and shell self-update, while the
Builder, Runner and Analyzer continue using the independent signed-package
protocol. For larger public distribution, the same signed assets can move to
S3-compatible storage or a CDN.

Authoritative references:

- [GitHub immutable releases](https://docs.github.com/en/enterprise-cloud@latest/code-security/concepts/supply-chain-security/immutable-releases)
- [GitHub release integrity](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/verify-release-integrity)
- [Microsoft App Installer automatic updates](https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview)
- [Microsoft MSIX overview](https://learn.microsoft.com/en-us/windows/msix/overview)
