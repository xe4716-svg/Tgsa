"""Apply the reviewed V2 schema after root migrations; credentials use PG*."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.migrate import build_script as root_script

BASELINE_FILES = (
    'source_001_003/001_core_foundation.sql',
    'source_001_003/002_broadcast_channels.sql',
    'source_001_003/003_group_moderation.sql',
    'tgmate_v2_004_bidirectional.sql',
    '004a_relay_runtime.sql', '004b_moderation_control_hardening.sql',
    '004c_web_auth.sql', '004d_bot_credentials.sql', '004e_bot_webhooks.sql',
    '004f_relay_operator_auth.sql', '004g_operator_identity_invalidation.sql',
    '004h_relay_privacy_notice.sql',
)
FILES = BASELINE_FILES + ('004i_telegram_ai_safety.sql','004j_bot_runtime_limits.sql')
LOCK = '847293018422'


def build_script(directory):
    directory = Path(directory)
    if any(not (directory / name).is_file() for name in FILES):
        raise ValueError('Released business migration missing')
    # Reuse the reviewed root transaction, hash-preflight and psql machinery.
    # Numeric staging prefixes preserve the explicit release dependency order.
    with tempfile.TemporaryDirectory() as staging:
        staged_names = []
        for index, name in enumerate(FILES, 1):
            staged_name = f'{index:03d}_{Path(name).name}'
            shutil.copyfile(directory / name, Path(staging) / staged_name)
            staged_names.append(staged_name)
        script = root_script(staging, manifest=staged_names)
    script = script.replace('public.schema_migrations', 'public.business_schema_migrations')
    script = script.replace('847293018421', LOCK)
    marker = f'SELECT pg_advisory_lock({LOCK});'
    prerequisite = """
DO $$ BEGIN
 IF to_regclass('public.schema_migrations') IS NULL THEN
   RAISE EXCEPTION 'Root migrations required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.schema_migrations WHERE name='010_identity_bridge.sql') THEN
   RAISE EXCEPTION 'Root identity migration required';
 END IF;
 IF to_regnamespace('tgmate') IS NOT NULL AND
    to_regclass('public.business_schema_migrations') IS NULL THEN
   RAISE EXCEPTION 'Untracked business schema requires reviewed adoption';
 END IF;
END $$;
"""
    # Also refuse an empty ledger alongside an existing schema (failed adoption).
    guard = """
DO $$ BEGIN
 IF to_regnamespace('tgmate') IS NOT NULL AND NOT EXISTS
   (SELECT 1 FROM public.business_schema_migrations) THEN
   RAISE EXCEPTION 'Untracked business schema requires reviewed adoption';
 END IF;
END $$;
"""
    script = script.replace(marker, marker + prerequisite)
    script = script.replace('REVOKE ALL ON public.business_schema_migrations FROM PUBLIC;',
                            'REVOKE ALL ON public.business_schema_migrations FROM PUBLIC;' + guard)
    return script


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1] / 'workstreams/tgmate-v2')
    parser.add_argument('--psql', default='psql')
    args = parser.parse_args(argv)
    try:
        script = build_script(args.directory)
        result = subprocess.run([args.psql, '-X', '-q', '-v', 'ON_ERROR_STOP=1'],
            input=script, text=True, encoding='utf-8', errors='replace',
            env=dict(os.environ, PGCLIENTENCODING='UTF8'), capture_output=True)
        if result.returncode:
            raise RuntimeError('Business migration failed')
    except (OSError, ValueError, RuntimeError):
        print('Business migration rejected; inspect configuration, prerequisites and hashes privately.', file=sys.stderr)
        return 1
    print('Business migrations applied or already current.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
