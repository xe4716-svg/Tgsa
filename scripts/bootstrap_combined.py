"""Initialize both tracked schemas and the read-only integration grants."""
import argparse
from pathlib import Path
import subprocess
import sys

def commands(psql='psql', enable_relay_control=False):
    base = Path(__file__).resolve().parent
    plan = [
        [sys.executable, str(base/'migrate.py'), '--psql', psql],
        [sys.executable, str(base/'migrate_business.py'), '--psql', psql],
        [sys.executable, str(base/'link_identity.py'), '--psql', psql, '--configure-runtime-read'],
        [sys.executable, str(base/'configure_business_reads.py'), '--psql', psql],
        [sys.executable, str(base/'configure_catalog_reads.py'), '--psql', psql],
    ]
    if enable_relay_control:
        plan.append([sys.executable,str(base/'configure_relay_control.py'),'--psql',psql])
    return plan

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--psql', default='psql')
    parser.add_argument('--enable-relay-control', action='store_true')
    args = parser.parse_args(argv)
    for index, command in enumerate(commands(args.psql,args.enable_relay_control), 1):
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
