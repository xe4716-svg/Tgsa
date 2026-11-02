from pathlib import Path
import json,psycopg
root=Path(__file__).resolve().parents[1]
c=json.loads((root/'.local/db.json').read_text())
admin=dict(c,dbname='postgres')
with psycopg.connect(**admin,autocommit=True) as db:
 if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(c['dbname'],)).fetchone():
  from psycopg import sql
  db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(c['dbname'])))
files=sorted((root/'source_001_003').glob('*.sql'))+[root/'tgmate_v2_004_bidirectional.sql']
results=[]
with psycopg.connect(**c,autocommit=True) as db:
 for p in files:
  db.execute(p.read_text(encoding='utf-8-sig'))
  results.append({'file':p.name,'executed':True})
 report={'server':db.execute('select version()').fetchone()[0],'migrations':results,'production':False}
(root/'DATABASE_MIGRATION_RESULTS.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
