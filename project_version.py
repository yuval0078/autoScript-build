"""Central application and release version metadata.

The project uses Calendar Versioning in ``YYYY.MM.DD`` format. Update
``APP_VERSION`` once when preparing a release; the displayed version, Git tag,
and portable archive name are derived from it.
"""

APP_NAME = "Touchpad Writing Experiment"
APP_VERSION = "2026.01.25"
APP_VERSION_LABEL = f"Version {APP_VERSION}"
RELEASE_TAG = f"v{APP_VERSION}"
PORTABLE_ARCHIVE_NAME = f"TouchpadExperiment-{RELEASE_TAG}-portable.zip"
