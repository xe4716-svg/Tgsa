"""Strictly verify a legacy V2 schema before adopting its migration ledger."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.migrate_business import FILES,LOCK

CATALOG_SQL = """
SET search_path=pg_catalog,public;
SELECT jsonb_build_object(
 'database',(SELECT jsonb_build_object('encoding',encoding,'collate',datcollate,'ctype',datctype)
             FROM pg_database WHERE datname=current_database()),
 'relations',COALESCE((SELECT jsonb_agg(jsonb_build_object(
   'name',c.relname,'kind',c.relkind,'rls',c.relrowsecurity,'force',c.relforcerowsecurity,
   'options',c.reloptions,'partition',pg_get_partkeydef(c.oid),
   'columns',(SELECT jsonb_agg(jsonb_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod),
       'notnull',a.attnotnull,'identity',a.attidentity,'generated',a.attgenerated,
       'default',pg_get_expr(d.adbin,d.adrelid),'collation',co.collname) ORDER BY a.attnum)
     FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
     LEFT JOIN pg_collation co ON co.oid=a.attcollation
     WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped)) ORDER BY c.relname)
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='tgmate' AND c.relkind IN ('r','p','v','m')),'[]'::jsonb),
 'constraints',COALESCE((SELECT jsonb_agg(jsonb_build_object(
   'name',co.conname,'table',c.relname,'domain',t.typname,'definition',pg_get_constraintdef(co.oid,true),
   'validated',co.convalidated) ORDER BY c.relname,t.typname,co.conname)
   FROM pg_constraint co JOIN pg_namespace n ON n.oid=co.connamespace
   LEFT JOIN pg_class c ON c.oid=co.conrelid LEFT JOIN pg_type t ON t.oid=co.contypid
   WHERE n.nspname='tgmate'),'[]'::jsonb),
 'indexes',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',c.relname,
   'definition',pg_get_indexdef(i.indexrelid),'valid',i.indisvalid,'ready',i.indisready) ORDER BY c.relname)
   FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='tgmate'),'[]'::jsonb),
 'triggers',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',t.tgname,'table',c.relname,
   'enabled',t.tgenabled,'definition',pg_get_triggerdef(t.oid,true)) ORDER BY c.relname,t.tgname)
   FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='tgmate' AND NOT t.tgisinternal),'[]'::jsonb),
 'policies',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',p.polname,'table',c.relname,
   'command',p.polcmd,'permissive',p.polpermissive,'using',pg_get_expr(p.polqual,p.polrelid),
   'check',pg_get_expr(p.polwithcheck,p.polrelid),
   'roles',(SELECT jsonb_agg(COALESCE(r.rolname,'PUBLIC') ORDER BY r.rolname)
            FROM unnest(p.polroles) roleid LEFT JOIN pg_roles r ON r.oid=roleid))
   ORDER BY c.relname,p.polname)
   FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='tgmate'),'[]'::jsonb),
 'functions',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',p.proname,
   'arguments',pg_get_function_identity_arguments(p.oid),'definition',pg_get_functiondef(p.oid))
   ORDER BY p.proname,pg_get_function_identity_arguments(p.oid))
   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='tgmate' AND p.prokind IN ('f','p','w')),'[]'::jsonb),
 'types',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',t.typname,'kind',t.typtype,
   'base',format_type(t.typbasetype,t.typtypmod),'notnull',t.typnotnull,'default',t.typdefault,
   'labels',(SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid))
   ORDER BY t.typname) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
   WHERE n.nspname='tgmate' AND t.typtype IN ('e','d')),'[]'::jsonb),
 'sequences',COALESCE((SELECT jsonb_agg(jsonb_build_object('name',c.relname,
   'type',format_type(s.seqtypid,-1),'start',s.seqstart,'increment',s.seqincrement,
   'min',s.seqmin,'max',s.seqmax,'cache',s.seqcache,'cycle',s.seqcycle) ORDER BY c.relname)
   FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='tgmate'),'[]'::jsonb));
"""
OWNER_SQL = """
WITH unsafe_roles AS (
 SELECT r.oid FROM pg_roles r
 WHERE r.rolname IN ('tgs_runtime','tgmate_app','tgmate_auth') OR EXISTS(
   SELECT FROM pg_roles caller WHERE caller.rolcanlogin AND NOT caller.rolsuper
   AND caller.rolname<>current_user AND pg_has_role(caller.oid,r.oid,'MEMBER')))
SELECT COALESCE(jsonb_agg(object_name ORDER BY object_name),'[]'::jsonb) FROM (
 SELECT c.relname::text object_name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname='tgmate'
 AND r.oid IN (SELECT oid FROM unsafe_roles)
 UNION ALL
 SELECT p.proname::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='tgmate'
 AND r.oid IN (SELECT oid FROM unsafe_roles)
 UNION ALL
 SELECT 'schema:tgmate' FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
 WHERE n.nspname='tgmate' AND r.oid IN (SELECT oid FROM unsafe_roles)) q;
"""

LEGACY_ARGS='p_user uuid, p_tenant uuid, p_token text, p_csrf text, p_expires timestamp with time zone'
LEGACY_SAFE_SQL="""SELECT NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls
 AND NOT EXISTS(SELECT FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
   WHERE a.privilege_type='EXECUTE' AND a.grantee<>p.proowner)
 AND NOT EXISTS(SELECT FROM pg_roles caller WHERE caller.rolcanlogin AND NOT caller.rolsuper
   AND caller.rolname<>current_user AND pg_has_role(caller.oid,p.proowner,'MEMBER'))
 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
 WHERE p.oid=to_regprocedure('tgmate.auth_create_session(uuid,uuid,text,text,timestamp with time zone)')"""

def known_legacy_profile(current,canonical):
    copy=json.loads(json.dumps(current))
    extra=[item for item in copy.get('functions',[]) if item['name']=='auth_create_session'
           and item['arguments']==LEGACY_ARGS]
    if len(extra)!=1:return None
    copy['functions'].remove(extra[0])
    renamed={'relay_privacy_notices_check':'relay_privacy_notices_check1',
             'relay_privacy_requires_evidence_path':'relay_privacy_notices_check'}
    changed=[]
    for item in copy.get('constraints',[]):
        if item.get('table')=='relay_privacy_notices' and item['name'] in renamed:
            changed.append(item['name']);item['name']=renamed[item['name']]
    if set(changed)!=set(renamed):return None
    def key(item):return (item.get('table'),item.get('domain'),item['name'])
    indexed={key(item):item for item in copy['constraints']}
    if len(indexed)!=len(copy['constraints']) or set(indexed)!=set(map(key,canonical['constraints'])):
        return None
    copy['constraints']=[indexed[key(item)] for item in canonical['constraints']]
    return copy if copy==canonical else None

def plain_database(value):
    if not value or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',value):
        raise ValueError('A plain target database name is required')
    return value

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def differences(current,canonical):
    result={}
    for section,expected in canonical.items():
        actual=current.get(section)
        if actual==expected:continue
        if not isinstance(expected,list) or not isinstance(actual,list):
            result[section]=[{'object':section,'change':'changed'}]
            continue
        def key(item):
            return '/'.join(str(item.get(field) or '') for field in ('table','domain','name','arguments'))
        left={key(item):item for item in actual};right={key(item):item for item in expected}
        result[section]=[]
        for name in sorted(set(left)|set(right)):
            if left.get(name)==right.get(name):continue
            if name not in left:change='missing'
            elif name not in right:change='extra'
            else:change='changed'
            entry={'object':name,'change':change}
            if change=='changed':
                entry['fields']=[field for field in sorted(set(left[name])|set(right[name]))
                                 if left[name].get(field)!=right[name].get(field)]
            result[section].append(entry)
    return result

def release(source):
    manifest=json.loads((source/'UPLOAD_MANIFEST.json').read_text(encoding='utf-8'))
    result={}
    for index,name in enumerate(FILES,1):
        value=hashlib.sha256((source/name).read_bytes()).hexdigest()
        if manifest.get(name)!=value:
            raise ValueError('Released SQL does not match its import manifest')
        result[f'{index:03d}_{Path(name).name}']=value
    return result

def run(command,env,stage,input=None):
    result=subprocess.run(command,env=env,input=input,text=True,encoding='utf-8',
                          errors='replace',capture_output=True)
    if result.returncode:
        raise RuntimeError(stage+' failed; inspect diagnostics privately')
    return result.stdout.strip()

def query(psql,env,statement):
    return run([psql,'-X','-q','-A','-t','-v','ON_ERROR_STOP=1'],env,'Schema inspection',statement)

def adopt_sql(expected,profile,backup_hash,before_profile=None,reconcile=False):
    values=','.join("('%s','%s')"%(name,hashvalue) for name,hashvalue in expected.items())
    catalog_query=CATALOG_SQL.split(';',1)[1].strip().rstrip(';')
    before_profile=profile if before_profile is None else before_profile
    encoded_before=json.dumps(before_profile).replace("'","''")
    encoded_profile=json.dumps(profile).replace("'","''")
    repair=f"""DO $$ BEGIN IF NOT COALESCE(({LEGACY_SAFE_SQL}),false) THEN
 RAISE EXCEPTION 'Legacy overload is not private to a safe owner'; END IF; END $$;
ALTER TABLE tgmate.relay_privacy_notices RENAME CONSTRAINT relay_privacy_notices_check TO relay_privacy_notices_check1;
ALTER TABLE tgmate.relay_privacy_notices RENAME CONSTRAINT relay_privacy_requires_evidence_path TO relay_privacy_notices_check;
DROP FUNCTION tgmate.auth_create_session(uuid,uuid,text,text,timestamp with time zone) RESTRICT;
""" if reconcile else ''
    return f"""\\set ON_ERROR_STOP on
BEGIN;
SELECT pg_advisory_xact_lock({LOCK});
DO $$ BEGIN
 IF to_regclass('public.business_schema_migrations') IS NOT NULL THEN
   RAISE EXCEPTION 'Business schema already tracked; use the normal runner';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.schema_migrations WHERE name='010_identity_bridge.sql') THEN
   RAISE EXCEPTION 'Root identity migration required';
 END IF;
 IF EXISTS(SELECT FROM pg_stat_activity WHERE datname=current_database()
   AND pid<>pg_backend_pid() AND backend_type='client backend') THEN
   RAISE EXCEPTION 'Target must be quiescent for adoption';
 END IF;
END $$;
SET LOCAL search_path=pg_catalog,public;
SET LOCAL standard_conforming_strings=on;
SELECT 1 / CASE WHEN ({catalog_query}) = '{encoded_before}'::jsonb THEN 1 ELSE 0 END;
{repair}
SELECT 1 / CASE WHEN ({catalog_query}) = '{encoded_profile}'::jsonb THEN 1 ELSE 0 END;
CREATE TABLE public.business_schema_migrations
 (name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now());
REVOKE ALL ON public.business_schema_migrations FROM PUBLIC,tgs_runtime;
INSERT INTO public.business_schema_migrations(name,sha256) VALUES {values};
CREATE TABLE IF NOT EXISTS public.business_schema_adoptions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),adopted_at timestamptz NOT NULL DEFAULT now(),
 operator_name text NOT NULL DEFAULT current_user,source_hash text NOT NULL,
 schema_hash text NOT NULL,backup_hash text NOT NULL,before_schema_hash text NOT NULL,
 reconciled_legacy boolean NOT NULL);
REVOKE ALL ON public.business_schema_adoptions FROM PUBLIC,tgs_runtime;
INSERT INTO public.business_schema_adoptions(source_hash,schema_hash,backup_hash,before_schema_hash,reconciled_legacy)
 VALUES('{digest(expected)}','{digest(profile)}','{backup_hash}','{digest(before_profile)}',{'true' if reconcile else 'false'});
COMMIT;
"""

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default=os.getenv('PGDATABASE'))
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1]/'workstreams/tgmate-v2')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--reconcile-known-legacy',action='store_true')
    parser.add_argument('--backup-output',type=Path)
    for tool in ('psql','createdb','dropdb','pg-dump'):
        parser.add_argument('--'+tool,default=tool.replace('-','_'))
    args=parser.parse_args(argv)
    reference=None
    report={'applied':False,
            'comparison_excludes':['table data','ACLs','owners except unsafe runtime-owner check']}
    status=1
    try:
        target=plain_database(args.database)
        if args.apply and not args.backup_output:
            raise ValueError('Apply requires a new backup destination')
        expected=release(args.source)
        env=dict(os.environ,PGDATABASE=target,PGCLIENTENCODING='UTF8')
        current=json.loads(query(args.psql,env,CATALOG_SQL))
        unsafe=json.loads(query(args.psql,env,OWNER_SQL))
        root_ready=query(args.psql,env,"SELECT to_regclass('public.schema_migrations') IS NOT NULL")=='t'
        if root_ready:
            root_ready=query(args.psql,env,"SELECT EXISTS(SELECT FROM public.schema_migrations WHERE name='010_identity_bridge.sql')")=='t'
        tracked=query(args.psql,env,"SELECT to_regclass('public.business_schema_migrations') IS NOT NULL")=='t'
        clients=int(query(args.psql,env,"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'"))
        name='tgs_schema_ref_'+uuid4().hex[:12]
        run([args.createdb,'--',name],env,'Reference database creation')
        reference=name
        ref_env=dict(env,PGDATABASE=reference)
        for filename in FILES:
            run([args.psql,'-X','-q','-v','ON_ERROR_STOP=1'],ref_env,
                'Reference migration',(args.source/filename).read_text(encoding='utf-8-sig'))
        canonical=json.loads(query(args.psql,ref_env,CATALOG_SQL))
        equal=current==canonical
        repairable=known_legacy_profile(current,canonical) is not None
        private_legacy=query(args.psql,env,LEGACY_SAFE_SQL)=='t' if repairable else False
        use_repair=bool(args.reconcile_known_legacy and repairable and private_legacy)
        report.update(database=target,schema_equal=equal,root_ready=root_ready,
            already_tracked=tracked,unsafe_runtime_owners=unsafe,release_count=len(expected),
            source_hash=digest(expected),schema_hash=digest(current),
            reference_hash=digest(canonical),table_count=len(current['relations']),
            differing_sections=[key for key in canonical if current.get(key)!=canonical[key]],
            differences=differences(current,canonical),
            known_legacy_repairable=bool(repairable and private_legacy),
            active_other_clients=clients,
            adoptable=bool((equal or use_repair) and root_ready and not tracked and not unsafe and clients==0))
        status=0 if equal and not unsafe else 1
        if args.apply:
            if not report['adoptable']:
                raise ValueError('Adoption refused; inspect the comparison and prerequisites')
            run([sys.executable,str(Path(__file__).with_name('database_backup.py')),
                 'backup',str(args.backup_output),'--pg-dump',args.pg_dump],env,'Target backup')
            with args.backup_output.open('rb') as archive:
                backup_hash=hashlib.file_digest(archive,'sha256').hexdigest()
            report.update(backup_created=True,backup_sha256=backup_hash)
            again=json.loads(query(args.psql,env,CATALOG_SQL))
            if again!=current:
                raise ValueError('Schema changed while backup was running')
            if json.loads(query(args.psql,env,OWNER_SQL)):
                raise ValueError('Unsafe runtime ownership detected after backup')
            after=canonical if use_repair else current
            query(args.psql,env,adopt_sql(expected,after,backup_hash,current,use_repair))
            report.update(applied=True,reconciled_legacy=use_repair,after_schema_hash=digest(after))
            status=0
    except (OSError,ValueError,RuntimeError) as error:
        report['error']=str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Local tool or file unavailable'
        status=1
    finally:
        if reference:
            try:
                run([args.dropdb,'--',reference],env,'Reference cleanup')
                report['reference_removed']=True
            except (OSError,RuntimeError):
                report['reference_removed']=False
                status=1
    print(json.dumps(report,sort_keys=True))
    return status

if __name__=='__main__':
    raise SystemExit(main())
