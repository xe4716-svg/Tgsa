"""Initialize both tracked schemas and the read-only integration grants."""
import argparse
from pathlib import Path
import subprocess
import sys

def commands(psql='psql'):
    base = Path(__file__).resolve().parent
    return [
        [sys.executable, str(base/'migrate.py'), '--psql', psql],
        [sys.executable, str(base/'migrate_business.py'), '--psql', psql],
        [sys.executable, str(base/'link_identity.py'), '--psql', psql, '--configure-runtime-read'],
        [sys.executable, str(base/'configure_business_reads.py'), '--psql', psql],
    ]

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--psql', default='psql')
    args = parser.parse_args(argv)
    for index, command in enumerate(commands(args.psql), 1):
        try:
            result = subprocess.run(command, capture_output=True)
        except OSError:
            print(f'Initialization step {index} could not start.', file=sys.stderr)
            return 1
        if result.returncode:
            print(f'Initialization step {index} rejected; later steps were not run.', file=sys.stderr)
            return 1
    print('Tracked schemas and read-only integration grants ready.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
