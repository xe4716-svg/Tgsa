"""Run ordered SQL migrations with psql; connection secrets belong in PG* environment."""
from pathlib import Path
import argparse
import hashlib
import re
import os
import subprocess
import sys


def migration_body(text):
    # Released migrations either have no transaction wrapper or one outer pair.
    lines = text.splitlines()
    controls = [(i, line.strip().upper()) for i, line in enumerate(lines)
                if re.fullmatch(r'\s*(BEGIN|COMMIT|ROLLBACK)\s*;\s*', line, re.I)]
    if controls:
        if len(controls) != 2 or controls[0][1] != 'BEGIN;' or controls[1][1] != 'COMMIT;':
            raise ValueError('Unsupported transaction controls')
        first, last = controls[0][0], controls[1][0]
        if any(line.strip() and not line.lstrip().startswith('--')
               for line in lines[:first] + lines[last + 1:]):
            raise ValueError('Transaction wrapper must surround migration')
        lines = [line for i, line in enumerate(lines) if i not in (first, last)]
    return '\n'.join(lines)


def build_script(directory):
    files = sorted(Path(directory).glob('*.sql'))
    if not files:
        raise ValueError('No migrations found')
    script = [r'\set ON_ERROR_STOP on',
              "SELECT pg_advisory_lock(847293018421);",
              'CREATE TABLE IF NOT EXISTS public.schema_migrations '
              '(name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());',
              'REVOKE ALL ON public.schema_migrations FROM PUBLIC;']
    expected = []
    for path in files:
        if not re.fullmatch(r'[0-9A-Za-z_.-]+', path.name):
            raise ValueError('Unsafe migration name')
        expected.append("('%s','%s')" % (path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    script.append("DO $$ BEGIN IF EXISTS (SELECT FROM public.schema_migrations m LEFT JOIN (VALUES "
        + ','.join(expected) + ") AS expected(name,sha256) ON expected.name=m.name "
        "WHERE expected.name IS NULL OR expected.sha256<>m.sha256) THEN "
        "RAISE EXCEPTION 'Applied migration missing or hash drift'; END IF; END $$;")
    for path in files:
        if not re.fullmatch(r'[0-9A-Za-z_.-]+', path.name):
            raise ValueError('Unsafe migration name')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        body = migration_body(path.read_text(encoding='utf-8-sig'))
        # The foundation originally named its database tgs. Bind that exact grant
        # to this connection so isolated and deployment databases can differ.
        if path.name == '001_core.sql':
            body = body.replace('GRANT CONNECT ON DATABASE tgs TO tgs_runtime;',
                "DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO tgs_runtime', current_database()); END $$;")
        name = path.name
        script += [f"DO $$ BEGIN IF EXISTS (SELECT FROM public.schema_migrations WHERE name='{name}' AND sha256<>'{digest}') THEN RAISE EXCEPTION 'Migration hash drift: {name}'; END IF; END $$;",
                   f"SELECT EXISTS (SELECT FROM public.schema_migrations WHERE name='{name}') AS applied \\gset",
                   r'\if :applied', r'\else', 'BEGIN;', body,
                   f"INSERT INTO public.schema_migrations(name,sha256) VALUES ('{name}','{digest}');",
                   'COMMIT;', r'\endif']
    script += ['SELECT pg_advisory_unlock(847293018421);']
    return '\n'.join(script) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1] / 'migrations')
    parser.add_argument('--psql', default='psql')
    args = parser.parse_args()
    try:
        script = build_script(args.directory)
        # Never echo SQL or connection diagnostics: SQL includes password interpolation.
        result = subprocess.run([args.psql, '-X', '-q', '-v', 'ON_ERROR_STOP=1'],
                                input=script, text=True, encoding='utf-8', errors='replace',
                                env=dict(os.environ, PGCLIENTENCODING='UTF8'), capture_output=True)
    except (OSError, ValueError):
        print('Migration setup failed; check executable and migration files.', file=sys.stderr)
        return 1
    if result.returncode:
        print('Migration failed; current file rolled back. Check database access and migration hashes.', file=sys.stderr)
        return 1
    print('Migrations applied or already current.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
