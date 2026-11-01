from pathlib import Path
import re,json,hashlib
from pglast import parse_sql,parse_plpgsql
files=sorted(Path('source_001_003').glob('*.sql'))+[Path('tgmate_v2_004_bidirectional.sql'),Path('004a_relay_runtime.sql'),Path('004b_moderation_control_hardening.sql'),Path('004c_web_auth.sql'),Path('004d_bot_credentials.sql'),Path('004e_bot_webhooks.sql'),Path('004f_relay_operator_auth.sql'),Path('004g_operator_identity_invalidation.sql'),Path('004h_relay_privacy_notice.sql')]
results=[]
known=set()
for p in files:
 s=p.read_text(encoding='utf-8-sig')
 row={'file':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 try:
  row['sql_statements_parsed']=len(parse_sql(s))
  functions=re.findall(r'CREATE (?:OR REPLACE )?FUNCTION\b.*?\$\$;',s,re.S|re.I)
  blocks=[f for f in functions if re.search(r'LANGUAGE\s+plpgsql',f,re.I)]
  for f in blocks: parse_plpgsql(f)
  row['plpgsql_functions_parsed']=len(blocks)
  tables=re.findall(r'CREATE TABLE tgmate\.(\w+)',s)
  known.update(tables)
  refs=set(re.findall(r'REFERENCES tgmate\.(\w+)',s))
  row['missing_referenced_tables']=sorted(refs-known)
  row['status']='PASS' if not row['missing_referenced_tables'] else 'FAIL'
 except Exception as e: row.update(status='FAIL',error=str(e))
 results.append(row)
report={'checks':results,'scope':'PostgreSQL parser plus referenced-table name checks only; not catalog/type/privilege validation','database_migrations_executed_by_this_script':False,'database_execution_report':'DATABASE_MIGRATION_RESULTS.json','telegram_tests_executed':False}
Path('VALIDATION_REPORT.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
if any(r['status']!='PASS' for r in results): raise SystemExit(1)
