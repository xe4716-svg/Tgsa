from pathlib import Path
import json,os,subprocess
import psycopg
from psycopg import sql
root=Path(__file__).resolve().parents[1]
c=json.loads((root/'.local/db.json').read_text())
env=dict(os.environ,PGPASSWORD=c['password'])
bin=root/'.tools/pgsql/bin'; backup=root/'.local/tgmate_test.dump'
args=['-h',c['host'],'-p',str(c['port']),'-U',c['user']]
subprocess.run([str(bin/'pg_dump.exe'),*args,'-Fc','-f',str(backup),c['dbname']],env=env,check=True,capture_output=True)
target='tgmate_restore_audit'
with psycopg.connect(**dict(c,dbname='postgres'),autocommit=True) as db:
 if db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(target,)).fetchone(): raise SystemExit('Restore target already exists; no data changed')
 db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
subprocess.run([str(bin/'pg_restore.exe'),*args,'--exit-on-error','-d',target,str(backup)],env=env,check=True,capture_output=True)
def snapshot(name):
 with psycopg.connect(**dict(c,dbname=name)) as db:
  return {'tables':db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='tgmate'").fetchone()[0], 'forced_rls_tables':db.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='tgmate' AND c.relforcerowsecurity").fetchone()[0], 'relay_operations':db.execute('SELECT count(*) FROM tgmate.relay_operations').fetchone()[0]}
a=snapshot(c['dbname']); b=snapshot(target)
assert a==b
report={'source':c['dbname'],'restored_to':target,'backup_restored':True,'snapshot_matches':True,'snapshot':a,'production':False}
(root/'BACKUP_RESTORE_RESULTS.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
