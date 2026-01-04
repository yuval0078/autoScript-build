# TouchpadExperimentManager Setup Guide

This guide provides step-by-step instructions for setting up and releasing the TouchpadExperimentManager application.

## Initial Repository Setup

Follow these steps to set up your repository for the first time:

1. Initialize a git repository (if not already done):
   ```bash
   git init
   ```

2. Add the remote origin (replace `<your-user>` with your GitHub username):
   ```bash
   git remote add origin https://github.com/<your-user>/touchpad-exp.git
   ```

3. Add all files to the repository:
   ```bash
   git add .
   ```

4. Create the initial commit:
   ```bash
   git commit -m "Initial import"
   ```

5. Push to the main branch:
   ```bash
   git push -u origin main
   ```

## Creating a Release

To create a new release of TouchpadExperimentManager:

### 1. Prepare Release Artifacts

Ensure you have the following files ready:
- `TouchpadExperimentManager-portable.zip` - The portable application package
- `version.json` - Version metadata file

### 2. Create and Push a Git Tag

Create a tag for the release (e.g., v1.0.0):
```bash
git tag v1.0.0
git push origin v1.0.0
```

### 3. Create GitHub Release

1. Go to your repository on GitHub
2. Navigate to "Releases" section
3. Click "Create a new release"
4. Select the tag you just created (e.g., v1.0.0)
5. Upload the following files:
   - `TouchpadExperimentManager-portable.zip`
   - `version.json`

### 4. Update Configuration Files

Ensure that the URLs in `update_config.json` match the GitHub release URLs:

```json
{
  "version_url": "https://github.com/<your-user>/touchpad-exp/releases/download/v1.0.0/version.json",
  "download_url": "https://github.com/<your-user>/touchpad-exp/releases/download/v1.0.0/TouchpadExperimentManager-portable.zip"
}
```

**Important**: Replace `<your-user>` with your actual GitHub username in all URLs.

## Version Management

The `version.json` file should follow this structure:

```json
{
  "version": "1.0.0",
  "release_date": "2026-01-04",
  "download_url": "https://github.com/<your-user>/touchpad-exp/releases/download/v1.0.0/TouchpadExperimentManager-portable.zip"
}
```

## Release Checklist

Before creating a release, ensure:

- [ ] All code changes are committed and pushed
- [ ] Version numbers are updated in all relevant files
- [ ] `TouchpadExperimentManager-portable.zip` is built and tested
- [ ] `version.json` contains correct version information
- [ ] `update_config.json` URLs match the release tag
- [ ] Git tag is created and pushed
- [ ] GitHub release is created with all required files
- [ ] URLs are updated with your GitHub username

## Troubleshooting

### Issue: URLs don't match

Ensure that:
- The tag name in the URL matches the actual tag (e.g., v1.0.0)
- Your GitHub username is correctly specified in all URLs
- The file names match exactly (case-sensitive)

### Issue: Release files not accessible

- Verify that the repository is public or you have appropriate access permissions
- Check that the files were uploaded to the correct release
- Ensure the tag exists in the repository
