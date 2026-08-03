# Versioning and releases

AutoScript uses **Calendar Versioning** in `YYYY.MM.DD` format. The current baseline is `2026.01.25`, normalized from the latest legacy portable archive, `TouchpadExperiment_Portable_20260125.zip`.

## Single source of truth

Change the version only in `project_version.py`:

```python
APP_VERSION = "2026.01.25"
```

The following values are derived automatically:

- application label: `Version 2026.01.25`
- Git tag: `v2026.01.25`
- release archive: `TouchpadExperiment-v2026.01.25-portable.zip`

Do not duplicate or manually edit these derived values elsewhere.

## Choosing the next version

Use the date on which the release is prepared:

- first release on August 3, 2026: `2026.08.03`
- another release on a later date: use that later date
- more than one release on the same date: append a revision suffix only after deliberately extending the versioning scheme; the current scripts require one release per date

## Preparing a release

1. Update `APP_VERSION` in `project_version.py`.
2. Commit and merge the release changes into `main`.
3. Make sure the local `main` branch is clean and up to date.
4. Run:

```powershell
.\release.ps1
```

The release script:

1. verifies that the current branch is `main` and the working tree is clean;
2. runs the focused unit tests;
3. builds the versioned portable archive;
4. creates the annotated Git tag only after tests and the build succeed;
5. refuses to move an existing release tag to another commit.

After it succeeds, push the branch and tag:

```powershell
git push origin main
git push origin v2026.01.25
```

Then create a GitHub Release for the same tag and attach the generated portable ZIP.

## Repository policy

Portable ZIP files are release artifacts, not source files. Publish new packages through GitHub Releases rather than committing them to the repository. The older `portable build` content can remain as historical material, but it should not be used as the version source for future releases.

Release tags are immutable. When a release changes, increase `APP_VERSION` and create a new tag; do not delete, overwrite, or force-move an existing release tag.
