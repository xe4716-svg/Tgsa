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

@pytest.mark.skipif(os.getenv("TGMATE_IDENTITY_INTEGRATION") != "1", reason="disposable database opt-in required")
def test_unified_identity_database():
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
                async def exercise():
                    conn=await asyncpg.connect(host=config["host"],port=config["port"],database=name,user=role,password=password)
                    try:
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.read")
                        def link(*extra, business_user=buser):
                            return subprocess.run([sys.executable,str(root/'scripts/link_identity.py'),
                                '--psql',psql,'--tenant',str(tenant),'--user',str(user),
                                '--business-tenant',str(btenant),'--business-user',str(business_user),*extra],
                                env=env,capture_output=True).returncode
                        assert link()==0
                        assert owner.execute('SELECT count(*) FROM public.integration_member_map').fetchone()[0]==0
                        assert link('--apply')==0
                        assert link('--apply')==0
                        other_business_user=uuid4()
                        owner.execute("INSERT INTO tgmate.users(id,email) VALUES(%s,%s)",(other_business_user,str(other_business_user)+'@example.test'))
                        owner.execute("INSERT INTO tgmate.tenant_members(tenant_id,user_id,status) VALUES(%s,%s,'active')",(btenant,other_business_user))
                        assert link('--apply',business_user=other_business_user)!=0
                        assert owner.execute('SELECT business_user_id FROM public.integration_user_map WHERE user_id=%s',(user,)).fetchone()[0]==buser
                        configured=subprocess.run([sys.executable,str(root/'scripts/link_identity.py'),'--psql',psql,'--configure-runtime-read'],env=env,capture_output=True)
                        assert configured.returncode==0
                        assert owner.execute("SELECT has_column_privilege('tgs_runtime','tgmate.users','id','SELECT')").fetchone()[0]
                        assert not owner.execute("SELECT has_column_privilege('tgs_runtime','tgmate.web_credentials','password_hash','SELECT')").fetchone()[0]
                        assert not owner.execute("SELECT has_column_privilege('tgs_runtime','tgmate.relay_payloads','ciphertext','SELECT')").fetchone()[0]

                        other_tenant, other_user = uuid4(), uuid4()
                        owner.execute("INSERT INTO public.tenants(id,name) VALUES(%s,'other')",(other_tenant,))
                        owner.execute("INSERT INTO public.users(id,auth_subject) VALUES(%s,%s)",(other_user,str(other_user)))
                        with pytest.raises(psycopg.errors.UniqueViolation):
                            owner.execute("INSERT INTO public.integration_tenant_map VALUES(%s,%s)",(other_tenant,btenant))
                        with pytest.raises(psycopg.errors.UniqueViolation):
                            owner.execute("INSERT INTO public.integration_user_map VALUES(%s,%s)",(other_user,buser))
                        owner.execute("INSERT INTO public.integration_tenant_map VALUES(%s,%s)",(other_tenant,uuid4()))
                        owner.execute("INSERT INTO public.tenant_memberships(tenant_id,user_id) VALUES(%s,%s)",(other_tenant,user))
                        with pytest.raises(psycopg.errors.ForeignKeyViolation):
                            owner.execute("INSERT INTO public.integration_member_map VALUES(%s,%s,%s)",(other_tenant,user,btenant))
                        for statement in ["INSERT INTO public.integration_tenant_map VALUES($1,$2)",
                                          "UPDATE public.integration_tenant_map SET business_tenant_id=$2 WHERE tenant_id=$1",
                                          "DELETE FROM public.integration_tenant_map WHERE tenant_id=$1 AND business_tenant_id=$2"]:
                            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                                await conn.execute(statement,tenant,btenant)
                        for table, column, disabled, active in [('users','status','DISABLED','ACTIVE'),('tenants','status','SUSPENDED','ACTIVE')]:
                            key=user if table=='users' else tenant
                            owner.execute(sql.SQL("UPDATE public.{} SET status=%s WHERE id=%s").format(sql.Identifier(table)),(disabled,key))
                            with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,'relay.read')
                            owner.execute(sql.SQL("UPDATE public.{} SET status=%s WHERE id=%s").format(sql.Identifier(table)),(active,key))
                        owner.execute(sql.SQL("REVOKE SELECT ON tgmate.role_permissions FROM {}").format(sql.Identifier(role)))
                        async with conn.transaction():
                            await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(tenant))
                            with pytest.raises(BridgeUnavailable): await resolve_identity(conn,principal,'relay.read')
                            assert await conn.fetchval("SELECT current_setting('app.tenant_id')")==str(tenant)
                        owner.execute(sql.SQL("GRANT SELECT ON tgmate.role_permissions TO {}").format(sql.Identifier(role)))
                        async with conn.transaction():
                            identity=await resolve_identity(conn,principal,"relay.read")
                            assert identity.tenant_id==btenant and identity.user_id==buser
                            assert await conn.fetchval("SELECT current_setting('app.tenant_id')")==str(tenant)
                            await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(uuid4()))
                            assert await conn.fetchval("SELECT count(*) FROM public.integration_member_map")==0
                        owner.execute("UPDATE public.tenant_memberships SET role='VIEWER' WHERE tenant_id=%s",(tenant,))
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.manage")
                        owner.execute("UPDATE tgmate.tenant_members SET status='removed' WHERE tenant_id=%s",(btenant,))
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.read")
                        owner.execute("UPDATE tgmate.tenant_members SET status='active' WHERE tenant_id=%s",(btenant,))
                        owner.execute("DELETE FROM tgmate.role_permissions WHERE tenant_id=%s",(btenant,))
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.read")
                        owner.execute("DELETE FROM public.tenant_memberships WHERE tenant_id=%s AND user_id=%s",(tenant,user))
                        assert owner.execute("SELECT count(*) FROM public.integration_member_map WHERE tenant_id=%s AND user_id=%s",(tenant,user)).fetchone()[0]==0
                        assert owner.execute("SELECT count(*) FROM tgmate.tenant_members WHERE tenant_id=%s AND user_id=%s",(btenant,buser)).fetchone()[0]==1
                        assert owner.execute("SELECT count(*) FROM tgmate.users WHERE id=%s",(buser,)).fetchone()[0]==1
                        assert owner.execute("SELECT count(*) FROM tgmate.roles WHERE id=%s",(rid,)).fetchone()[0]==1
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.read")
                        owner.execute("INSERT INTO public.tenant_memberships(tenant_id,user_id,role) VALUES(%s,%s,'ADMIN')",(tenant,user))
                        assert link('--apply')==0
                        owner.execute("UPDATE public.auth_refresh_tokens SET revoked_at=now()")
                        with pytest.raises(BridgeDenied): await resolve_identity(conn,principal,"relay.read")
                    finally: await conn.close()
                asyncio.run(exercise())
                owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        finally:
            if old:
                admin.execute(sql.SQL("ALTER ROLE tgs_runtime PASSWORD {}").format(sql.Literal(old[0])))
