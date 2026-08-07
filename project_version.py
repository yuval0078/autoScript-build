"""Central application and release version metadata.

Update ``APP_VERSION`` once when preparing a release. The displayed version,
Git tag, and portable archive name are derived from it.
"""

APP_NAME = "Touchpad Writing Experiment"
APP_VERSION = "1.0.3.1"
APP_VERSION_LABEL = f"Version {APP_VERSION}"
RELEASE_TAG = f"v{APP_VERSION}"
PORTABLE_ARCHIVE_NAME = f"TouchpadExperiment-{RELEASE_TAG}-portable.zip"
