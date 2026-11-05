"""Regenerate the reviewed root SQL migration manifest without connecting to PostgreSQL."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.migrate import write_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--directory',
        type=Path,
        default=Path(__file__).resolve().parents[1] / 'migrations',
    )
    args = parser.parse_args(argv)
    try:
        target = write_manifest(args.directory)
    except (OSError, ValueError):
        print('Manifest update rejected; check the migration directory and SQL files.', file=sys.stderr)
        return 1
    print(f'Updated {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
