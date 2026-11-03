"""Custom-format backups and restore into a new database; use PG* credentials."""
import argparse
import os
import re
from pathlib import Path
import subprocess
import tempfile
import sys

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def run(command, *, stdin=None):
    result = subprocess.run(command, capture_output=True, stdin=stdin)
    if result.returncode:
        raise RuntimeError('Database tool failed; existing data was not replaced')


def backup(output, pg_dump='pg_dump'):
    output = Path(output)
    if output.exists():
        raise ValueError('Backup destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.backup-', dir=output.parent)
    os.close(handle)
    try:
        run([pg_dump, '--format=custom', '--file', temporary])
        # Hard-link creation is atomic and refuses to replace an existing file.
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_destination(database):
    # pg_restore treats --dbname values containing '=' or URI syntax as a
    # connection string. Accept only a plain identifier to prevent redirection.
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', database):
        raise ValueError('Invalid destination database')


def restore(source, database, createdb='createdb', pg_restore='pg_restore'):
    if not Path(source).is_file():
        raise ValueError('Backup source is missing')
    validate_destination(database)
    # createdb rejects an existing target; never drop or clean a database.
    run([createdb, '--', database])
    run([pg_restore, '--exit-on-error', '--single-transaction', '--dbname', database,
         '--', str(source)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    dump = commands.add_parser('backup')
    dump.add_argument('output', type=Path)
    dump.add_argument('--pg-dump', default='pg_dump')
    dump.add_argument('--key-file',type=Path,help='32-byte private external key; encrypt archive')
    load = commands.add_parser('restore')
    load.add_argument('source', type=Path)
    load.add_argument('database')
    load.add_argument('--createdb', default='createdb')
    load.add_argument('--pg-restore', default='pg_restore')
    load.add_argument('--key-file',type=Path,help='Authenticate and decrypt archive before creating destination')
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            if args.key_file:
                from scripts.encrypted_backup import backup_encrypted
                backup_encrypted(args.output,args.key_file,args.pg_dump)
            else:
                backup(args.output, args.pg_dump)
        else:
            if args.key_file:
                from scripts.encrypted_backup import restore_encrypted
                restore_encrypted(args.source,args.database,args.key_file,args.createdb,args.pg_restore)
            else:
                restore(args.source, args.database, args.createdb, args.pg_restore)
    except (ValueError, RuntimeError, OSError):
        parser.exit(1, 'Database operation failed. Existing targets are never overwritten; a failed restore database is retained for inspection.\n')
    print('Database operation completed.')


if __name__ == '__main__':
    main()
