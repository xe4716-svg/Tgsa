"""Real legacy adoption and controlled reconciliation; dedicated test cluster only."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4
import pytest
psycopg=pytest.importorskip('psycopg')
from psycopg import sql
from scripts.migrate_business import BASELINE_FILES as FILES

@pytest.mark.skipif(os.getenv('TGMATE_SCHEMA_ADOPTION_INTEGRATION')!='1',reason='disposable cluster opt-in required')
def test_legacy_adoption_preserves_data_and_rolls_back_failures(tmp_path):
    root=Path(__file__).resolve().parents[1]
    cfg=dict(host=os.getenv('PGHOST','localhost'),port=int(os.getenv('PGPORT','5432')),
             user=os.environ['PGUSER'],password=os.environ['PGPASSWORD'],dbname='postgres')
    name='tgs_adopt_'+uuid4().hex[:12]
    owner_role='legacy_owner_'+uuid4().hex[:10]
    psql=os.getenv('PG_PSQL','psql')
    suffix='.exe' if Path(psql).suffix.lower()=='.exe' else ''
    sibling=lambda tool:str(Path(psql).with_name(tool+suffix))
    env=dict(os.environ,PGDATABASE=name,PGCLIENTENCODING='UTF8',APP_DB_PASSWORD=secrets.token_urlsafe(32))
    command=[sys.executable,str(root/'scripts/adopt_business_schema.py'),'--psql',psql,
             '--createdb',sibling('createdb'),'--dropdb',sibling('dropdb'),'--pg-dump',sibling('pg_dump')]
    def cli(*options):
        result=subprocess.run(command+list(options),env=env,capture_output=True,text=True,encoding='utf-8')
        return result.returncode,json.loads(result.stdout)
    def query(statement,parameters=None):
        with psycopg.connect(**dict(cfg,dbname=name),autocommit=True) as db:
            result=db.execute(statement,parameters)
            return result.fetchall() if result.description else []
    with psycopg.connect(**cfg,autocommit=True) as admin:
        old=admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname='tgs_runtime'").fetchone()
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        admin.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(owner_role)))
        try:
            assert subprocess.run([sys.executable,str(root/'scripts/migrate.py'),'--psql',psql],env=env,capture_output=True).returncode==0
            for filename in FILES:
                result=subprocess.run([psql,'-X','-q','-v','ON_ERROR_STOP=1'],env=env,
                    input=(root/'workstreams/tgmate-v2'/filename).read_text(encoding='utf-8-sig'),
                    text=True,encoding='utf-8',capture_output=True)
                assert result.returncode==0,'Legacy test schema initialization failed'
            sentinel=uuid4()
            query("INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,'preserve-business-data',%s)",(sentinel,str(sentinel)))
            code,report=cli()
            assert code==0 and report['schema_equal'] and report['adoptable'] and not report['applied']
            assert query("SELECT to_regclass('public.business_schema_migrations')")[0][0] is None
            query('ALTER TABLE tgmate.tenants ADD COLUMN unknown_probe text')
            code,report=cli('--apply','--backup-output',str(tmp_path/'must-not-exist.dump'))
            assert code!=0 and not report['schema_equal']
            assert not (tmp_path/'must-not-exist.dump').exists()
            query('ALTER TABLE tgmate.tenants DROP COLUMN unknown_probe')
            query('ALTER TABLE tgmate.relay_privacy_notices RENAME CONSTRAINT relay_privacy_notices_check TO relay_privacy_requires_evidence_path')
            query('ALTER TABLE tgmate.relay_privacy_notices RENAME CONSTRAINT relay_privacy_notices_check1 TO relay_privacy_notices_check')
            query("""CREATE FUNCTION tgmate.auth_create_session(p_user uuid,p_tenant uuid,p_token text,p_csrf text,p_expires timestamptz)
                RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$ BEGIN RETURN; END $$""")
            signature='tgmate.auth_create_session(uuid,uuid,text,text,timestamp with time zone)'
            query('REVOKE ALL ON FUNCTION '+signature+' FROM PUBLIC')
            query(sql.SQL('ALTER FUNCTION '+signature+' OWNER TO {}').format(sql.Identifier(owner_role)))
            code,report=cli()
            assert code!=0 and report['known_legacy_repairable'] and not report['applied']
            existing=tmp_path/'existing.dump';existing.write_bytes(b'keep-existing')
            assert cli('--apply','--reconcile-known-legacy','--backup-output',str(existing))[0]!=0
            assert existing.read_bytes()==b'keep-existing'
            query('GRANT EXECUTE ON FUNCTION '+signature+' TO PUBLIC')
            code,report=cli('--apply','--reconcile-known-legacy','--backup-output',str(tmp_path/'unsafe.dump'))
            assert code!=0 and not report['known_legacy_repairable']
            assert not (tmp_path/'unsafe.dump').exists()
            query('REVOKE ALL ON FUNCTION '+signature+' FROM PUBLIC')
            query('CREATE VIEW public.legacy_dependency AS SELECT '+signature.split('(')[0]+'(NULL::uuid,NULL::uuid,NULL::text,NULL::text,NULL::timestamptz) IS NULL AS marker')
            failed_backup=tmp_path/'dependency-backup.dump'
            code,report=cli('--apply','--reconcile-known-legacy','--backup-output',str(failed_backup))
            assert code!=0 and report['backup_created'] and failed_backup.is_file()
            assert query("SELECT to_regclass('public.business_schema_migrations')")[0][0] is None
            assert query("SELECT count(*) FROM pg_constraint WHERE conrelid='tgmate.relay_privacy_notices'::regclass AND conname='relay_privacy_requires_evidence_path'")[0][0]==1
            query('DROP VIEW public.legacy_dependency')
            archive=tmp_path/'verified-before-adoption.dump'
            code,report=cli('--apply','--reconcile-known-legacy','--backup-output',str(archive))
            assert code==0,report
            assert report['applied'] and report['reconciled_legacy'] and report['reference_removed']
            assert hashlib.sha256(archive.read_bytes()).hexdigest()==report['backup_sha256']
            assert query('SELECT name FROM tgmate.tenants WHERE id=%s',(sentinel,))[0][0]=='preserve-business-data'
            assert query('SELECT count(*) FROM public.business_schema_migrations')[0][0]==12
            assert query('SELECT count(*) FROM public.business_schema_adoptions WHERE reconciled_legacy')[0][0]==1
            assert query('SELECT to_regprocedure(%s)',(signature,))[0][0] is None
            assert subprocess.run([sys.executable,str(root/'scripts/migrate_business.py'),'--psql',psql],env=env,capture_output=True).returncode==0
            code,report=cli()
            # Forward migrations intentionally change the adopted baseline.
            assert code!=0 and not report['schema_equal'] and report['already_tracked']
            assert query("SELECT ai_mode FROM tgmate.moderation_platform_defaults")[0][0]=='off'
            assert cli('--apply','--backup-output',str(tmp_path/'duplicate.dump'))[0]!=0
            assert not (tmp_path/'duplicate.dump').exists()
            query(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(owner_role)))
            admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(owner_role)))
        finally:
            if old:admin.execute(sql.SQL('ALTER ROLE tgs_runtime PASSWORD {}').format(sql.Literal(old[0])))
