"""Opt-in unified-schema identity integration using a disposable child database."""
import asyncio
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4
import asyncpg
import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from app.auth.dependencies import AuthPrincipal
from app.integration.identity import resolve_identity, BridgeDenied, BridgeUnavailable

@pytest.mark.skipif(os.getenv("TGMATE_BUSINESS_READS_INTEGRATION") != "1", reason="disposable database opt-in required")
def test_business_reads_database(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = dict(host=os.getenv("PGHOST","localhost"), port=int(os.getenv("PGPORT","5432")),
                  user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], dbname="postgres")
    name = "tgs_identity_" + uuid4().hex[:12]
    role = "identity_probe_" + uuid4().hex[:10]
    password = secrets.token_urlsafe(32)
    psql = os.getenv("PG_PSQL","psql")
    env = dict(os.environ, PGDATABASE=name, PGCLIENTENCODING="UTF8", APP_DB_PASSWORD=secrets.token_urlsafe(32))
    with psycopg.connect(**config, autocommit=True) as admin:
        old = admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname='tgs_runtime'").fetchone()
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            proc = subprocess.run([sys.executable,str(root/"scripts/migrate.py"),"--psql",psql], env=env,capture_output=True)
            assert proc.returncode == 0, "Root migration failed"
            v2 = root/"workstreams/tgmate-v2"
            paths = sorted((v2/"source_001_003").glob("*.sql")) + [v2/"tgmate_v2_004_bidirectional.sql"] + sorted(v2.glob("004[a-h]_*.sql"))
            for path in paths:
                result = subprocess.run([psql,"-X","-q","-v","ON_ERROR_STOP=1","-f",str(path)],env=env,capture_output=True)
                assert result.returncode == 0, "Business migration failed: " + path.name
            with psycopg.connect(**dict(config,dbname=name),autocommit=True) as owner:
                owner.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(sql.Identifier(role),sql.Literal(password)))
                owner.execute(sql.SQL("GRANT USAGE ON SCHEMA public,tgmate TO {}").format(sql.Identifier(role)))
                owner.execute(sql.SQL("GRANT SELECT ON public.tenants,public.users,public.tenant_memberships,public.auth_refresh_tokens,public.integration_tenant_map,public.integration_user_map,public.integration_member_map,tgmate.tenant_members,tgmate.tenant_member_roles,tgmate.role_permissions TO {}").format(sql.Identifier(role)))
                owner.execute(sql.SQL("GRANT SELECT(id,status) ON tgmate.tenants,tgmate.users TO {}").format(sql.Identifier(role)))
                tenant,user,btenant,buser,family,rid = [uuid4() for _ in range(6)]
                owner.execute("INSERT INTO public.tenants(id,name) VALUES(%s,'bridge')",(tenant,))
                owner.execute("INSERT INTO public.users(id,auth_subject,email,status) VALUES(%s,%s,%s,'ACTIVE')",(user,str(user),str(user)+"@example.test"))
                owner.execute("INSERT INTO public.tenant_memberships(tenant_id,user_id,role) VALUES(%s,%s,'ADMIN')",(tenant,user))
                owner.execute("INSERT INTO public.auth_refresh_tokens(token_hash,tenant_id,user_id,family_id,expires_at) VALUES(%s,%s,%s,%s,now()+interval '1 day')",("a"*64,tenant,user,family))
                owner.execute("INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,'bridge',%s)",(btenant,str(btenant)))
                owner.execute("INSERT INTO tgmate.users(id,email) VALUES(%s,%s)",(buser,str(buser)+"@example.test"))
                owner.execute("INSERT INTO tgmate.tenant_members(tenant_id,user_id,status) VALUES(%s,%s,'active')",(btenant,buser))
                owner.execute("INSERT INTO tgmate.permissions(code,description) VALUES('relay.read','read'),('relay.manage','manage') ON CONFLICT DO NOTHING")
                owner.execute("INSERT INTO tgmate.roles(id,tenant_id,code,name) VALUES(%s,%s,'bridge','bridge')",(rid,btenant))
                owner.execute("INSERT INTO tgmate.tenant_member_roles(tenant_id,user_id,role_id) VALUES(%s,%s,%s)",(btenant,buser,rid))
                owner.execute("INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) VALUES(%s,%s,'relay.read'),(%s,%s,'relay.manage')",(btenant,rid,btenant,rid))
                principal=AuthPrincipal(user,tenant,family,"ADMIN","test@example.test",False)
                owner.execute("INSERT INTO public.integration_tenant_map VALUES(%s,%s)",(tenant,btenant))
                owner.execute("INSERT INTO public.integration_user_map VALUES(%s,%s)",(user,buser))
                owner.execute("INSERT INTO public.integration_member_map VALUES(%s,%s,%s)",(tenant,user,btenant))
                def seed_relay(t):
                    r,b,e,c=[uuid4() for _ in range(4)]
                    owner.execute("INSERT INTO tgmate.robot_instances(id,tenant_id,product_type_code,name) VALUES(%s,%s,'bidirectional','test')",(r,t))
                    owner.execute("INSERT INTO tgmate.telegram_bots(id,tenant_id,token_vault_ref,token_fingerprint,status) VALUES(%s,%s,'private-token',%s,'active')",(b,t,str(b)))
                    owner.execute("INSERT INTO tgmate.executor_bindings(id,tenant_id,robot_instance_id,telegram_bot_id,purpose) VALUES(%s,%s,%s,%s,'send')",(e,t,r,b))
                    owner.execute("INSERT INTO tgmate.relay_configs(id,tenant_id,robot_instance_id,telegram_bot_id,executor_binding_id,operator_user_id) VALUES(%s,%s,%s,%s,%s,999991)",(c,t,r,b,e))
                    ids=[]
                    for customer in [999992,999993]:
                        v=uuid4(); ids.append(str(v))
                        owner.execute("INSERT INTO tgmate.relay_conversations(id,tenant_id,config_id,customer_user_id,initiated_at,privacy_notice_version,created_at) VALUES(%s,%s,%s,%s,now(),'v1','2026-01-01Z')",(v,t,c,customer))
                    return ids
                expected=set(seed_relay(btenant))
                other=uuid4()
                owner.execute("INSERT INTO tgmate.tenants(id,name,slug) VALUES(%s,'other',%s)",(other,str(other)))
                seed_relay(other)
                grants=subprocess.run([sys.executable,str(root/'scripts/configure_business_reads.py'),'--psql',psql,'--role',role],env=env,capture_output=True)
                assert grants.returncode==0
                assert not owner.execute("SELECT has_column_privilege(%s,'tgmate.relay_conversations','customer_user_id','SELECT')",(role,)).fetchone()[0]
                assert not owner.execute("SELECT has_column_privilege(%s,'tgmate.relay_payloads','ciphertext','SELECT')",(role,)).fetchone()[0]
                from contextlib import asynccontextmanager
                from dataclasses import replace
                from fastapi import FastAPI
                from fastapi.testclient import TestClient
                from app.integration.router import router
                import app.auth.security as security
                monkeypatch.setattr(security,'settings',replace(security.settings,jwt_signing_secret=secrets.token_urlsafe(48)))
                token=security.create_access_token(user_id=user,tenant_id=tenant,family_id=family,role='ADMIN')
                @asynccontextmanager
                async def lifespan(app):
                    app.state.db_pool=await asyncpg.create_pool(host=config['host'],port=config['port'],database=name,user=role,password=password,min_size=1,max_size=1)
                    try: yield
                    finally: await app.state.db_pool.close()
                app=FastAPI(lifespan=lifespan); app.include_router(router)
                with TestClient(app) as client:
                    headers={'Authorization':'Bearer '+token}
                    first=client.get('/api/v1/relay/conversations',headers=headers,params={'limit':1,'tenant_id':str(other)})
                    assert first.status_code==200, first.text
                    page=first.json()['data']
                    second=client.get('/api/v1/relay/conversations',headers=headers,params={'limit':1,'cursor':page['next_cursor']})
                    assert second.status_code==200
                    assert {page['items'][0]['id'],second.json()['data']['items'][0]['id']}==expected
                    assert 'customer_user_id' not in first.text and '999992' not in first.text
                    owner.execute("DELETE FROM tgmate.role_permissions WHERE tenant_id=%s",(btenant,))
                    assert client.get('/api/v1/relay/conversations',headers=headers).status_code==403
                    owner.execute("DELETE FROM public.integration_member_map WHERE tenant_id=%s",(tenant,))
                    assert client.get('/api/v1/relay/conversations',headers=headers).status_code==403
                    owner.execute("UPDATE public.auth_refresh_tokens SET revoked_at=now()")
                    assert client.get('/api/v1/relay/conversations',headers=headers).status_code==401
                owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        finally:
            if old: admin.execute(sql.SQL("ALTER ROLE tgs_runtime PASSWORD {}").format(sql.Literal(old[0])))
