# Versioning and releases

AutoScript uses **Semantic Versioning** in `MAJOR.MINOR.PATCH` format. The current application version is `1.0.3`, matching the version displayed by the latest portable build.

## Single source of truth

Change the version only in `project_version.py`:

```python
APP_VERSION = "1.0.3"
```

The following values are derived automatically:

- application label: `Version 1.0.3`
- Git tag: `v1.0.3`
- release archive: `TouchpadExperiment-v1.0.3-portable.zip`

Do not duplicate or manually edit these derived values elsewhere.

## Choosing the next version

Increment:

- **PATCH** for bug fixes that do not intentionally change existing behavior, for example `1.0.3` to `1.0.4`;
- **MINOR** for backward-compatible features, for example `1.0.3` to `1.1.0`;
- **MAJOR** for incompatible changes to workflows or data formats, for example `1.0.3` to `2.0.0`.

## Preparing a release

1. Update `APP_VERSION` in `project_version.py`.
2. Commit and merge the release changes into `main`.
3. Make sure the local `main` branch is clean and up to date.
4. Run:

```powershell
.\release.ps1
```

The release script:

1. verifies that the version, tag, and archive name agree;
2. verifies that the current branch is `main` and the working tree is clean;
3. runs the focused unit tests;
4. builds the versioned portable archive;
5. creates the annotated Git tag only after tests and the build succeed;
6. refuses to move an existing release tag to another commit.

After it succeeds, push the branch and tag:

```powershell
git push origin main
git push origin v1.0.3
```

Then create a GitHub Release for the same tag and attach `TouchpadExperiment-v1.0.3-portable.zip`.

## Repository policy

Portable ZIP files are release artifacts, not source files. Publish new packages through GitHub Releases rather than committing them to the repository. The older `portable build` content can remain as historical material, but it should not be used as the version source for future releases.

Release tags are immutable. When a release changes, increase `APP_VERSION` and create a new tag; do not delete, overwrite, or force-move an existing release tag.
