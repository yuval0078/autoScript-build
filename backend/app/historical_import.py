"""Administrative CLI for audited historical Run imports."""

import argparse
import json

from .database import get_session_factory
from .services.historical_import import import_historical_archive
from .services.storage import get_object_storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--actor-username", default="admin")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with get_session_factory()() as database:
        summary = import_historical_archive(
            database,
            get_object_storage(),
            args.archive,
            actor_username=args.actor_username,
            apply=args.apply,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
