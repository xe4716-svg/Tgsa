-- Apply once after 004b. Global authentication tables are not tenant-runtime tables.
-- Access is restricted to a dedicated auth role through fixed SECURITY DEFINER functions.
BEGIN;
CREATE TABLE tgmate.web_credentials (
 user_id uuid PRIMARY KEY REFERENCES tgmate.users(id),
 password_hash text NOT NULL CHECK(length(password_hash) BETWEEN 20 AND 512),
 credential_version bigint NOT NULL DEFAULT 1 CHECK(credential_version>0),
 disabled boolean NOT NULL DEFAULT false,
 changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE tgmate.web_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES tgmate.users(id),
 tenant_id uuid NOT NULL REFERENCES tgmate.tenants(id),
 token_hash text NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
 csrf_hash text NOT NULL CHECK(csrf_hash ~ '^[0-9a-f]{64}$'),
 credential_version bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 revoked_at timestamptz
);
CREATE INDEX web_sessions_user_idx ON tgmate.web_sessions(user_id,expires_at);
CREATE TABLE tgmate.web_login_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 account_key text NOT NULL CHECK(account_key ~ '^[0-9a-f]{64}$'),
 ip_key text NOT NULL CHECK(ip_key ~ '^[0-9a-f]{64}$'),
 succeeded boolean NOT NULL DEFAULT false,
 attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX web_login_account_idx ON tgmate.web_login_attempts(account_key,attempted_at);
CREATE INDEX web_login_ip_idx ON tgmate.web_login_attempts(ip_key,attempted_at);
REVOKE ALL ON tgmate.web_credentials,tgmate.web_sessions,tgmate.web_login_attempts FROM PUBLIC;
INSERT INTO tgmate.permissions(code,description) VALUES
 ('tenant.admin','租户全部管理权限'),('tenant.read','查看工作区概览'),
 ('relay.read','查看客服会话'),('relay.manage','管理客服接待'),
 ('broadcast.read','查看群发计划'),('broadcast.manage','创建和管理群发'),
 ('moderation.read','查看群管理'),('moderation.manage','管理规则、审核与申诉'),
 ('credentials.manage','管理Bot、账号与授权'),
 ('team.manage','管理成员权限'),('audit.read','查看审计'),
 ('crm.read','查看客户'),('crm.manage','管理客户'),
 ('analytics.read','查看分析'),('content.manage','管理素材'),
 ('workflow.manage','管理工作流'),('api.manage','管理开放接口'),
 ('billing.manage','管理套餐与账单') ON CONFLICT(code) DO NOTHING;

CREATE FUNCTION tgmate.auth_lookup(p_email text,p_slug text)
RETURNS TABLE(user_id uuid,tenant_id uuid,password_hash text,credential_version bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE u uuid; t uuid;
BEGIN
 SELECT x.id,y.id INTO u,t FROM tgmate.users x CROSS JOIN tgmate.tenants y
 WHERE x.email=p_email::public.citext AND x.status='active' AND y.slug=p_slug AND y.status='active';
 IF u IS NULL OR t IS NULL THEN RETURN; END IF;
 PERFORM set_config('app.tenant_id',t::text,true);
 IF NOT EXISTS(SELECT 1 FROM tgmate.tenant_members m WHERE m.tenant_id=t AND m.user_id=u AND m.status='active') THEN RETURN; END IF;
 RETURN QUERY SELECT c.user_id,t,c.password_hash,c.credential_version FROM tgmate.web_credentials c WHERE c.user_id=u AND NOT c.disabled;
END $$;

CREATE FUNCTION tgmate.auth_create_session(p_user uuid,p_tenant uuid,p_token text,p_csrf text,p_expires timestamptz,p_expected_version bigint)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE version bigint; identifier uuid;
BEGIN
 IF p_expires<=now() OR p_expires>now()+interval '1 day' THEN RAISE EXCEPTION 'invalid session expiry'; END IF;
 PERFORM set_config('app.tenant_id',p_tenant::text,true);
 IF NOT EXISTS(SELECT 1 FROM tgmate.tenant_members m JOIN tgmate.tenants t ON t.id=m.tenant_id WHERE m.tenant_id=p_tenant AND m.user_id=p_user AND m.status='active' AND t.status='active') THEN RAISE EXCEPTION 'inactive membership'; END IF;
 SELECT c.credential_version INTO version FROM tgmate.web_credentials c JOIN tgmate.users u ON u.id=c.user_id WHERE c.user_id=p_user AND NOT c.disabled AND u.status='active' AND c.credential_version=p_expected_version;
 IF version IS NULL THEN RAISE EXCEPTION 'inactive credential'; END IF;
 INSERT INTO tgmate.web_sessions(user_id,tenant_id,token_hash,csrf_hash,credential_version,expires_at) VALUES(p_user,p_tenant,p_token,p_csrf,version,p_expires) RETURNING id INTO identifier;
 INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(p_tenant,p_user,'auth.session.created','web_session',identifier);
 RETURN identifier;
END $$;

CREATE FUNCTION tgmate.auth_context(p_token text)
RETURNS TABLE(user_id uuid,tenant_id uuid,email text,permission_codes text[],role_codes text[],csrf_hash text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE s tgmate.web_sessions%ROWTYPE;
BEGIN
 SELECT x.* INTO s FROM tgmate.web_sessions x JOIN tgmate.web_credentials c ON c.user_id=x.user_id AND c.credential_version=x.credential_version JOIN tgmate.users u ON u.id=x.user_id JOIN tgmate.tenants t ON t.id=x.tenant_id WHERE x.token_hash=p_token AND x.revoked_at IS NULL AND x.expires_at>now() AND NOT c.disabled AND u.status='active' AND t.status='active';
 IF s.id IS NULL THEN RETURN; END IF;
 PERFORM set_config('app.tenant_id',s.tenant_id::text,true);
 IF NOT EXISTS(SELECT 1 FROM tgmate.tenant_members m WHERE m.tenant_id=s.tenant_id AND m.user_id=s.user_id AND m.status='active') THEN RETURN; END IF;
 RETURN QUERY SELECT s.user_id,s.tenant_id,u.email::text,
  coalesce((SELECT array_agg(DISTINCT p.permission_code) FROM tgmate.tenant_member_roles mr JOIN tgmate.roles r ON r.tenant_id=mr.tenant_id AND r.id=mr.role_id JOIN tgmate.role_permissions p ON p.tenant_id=r.tenant_id AND p.role_id=r.id WHERE mr.tenant_id=s.tenant_id AND mr.user_id=s.user_id),'{}'::text[]),
  coalesce((SELECT array_agg(r.code) FROM tgmate.tenant_member_roles mr JOIN tgmate.roles r ON r.tenant_id=mr.tenant_id AND r.id=mr.role_id WHERE mr.tenant_id=s.tenant_id AND mr.user_id=s.user_id),'{}'::text[]),s.csrf_hash
 FROM tgmate.users u WHERE u.id=s.user_id;
END $$;

CREATE FUNCTION tgmate.auth_revoke_session(p_token text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE s tgmate.web_sessions%ROWTYPE;
BEGIN
 SELECT * INTO s FROM tgmate.web_sessions WHERE token_hash=p_token AND revoked_at IS NULL FOR UPDATE;
 IF s.id IS NULL THEN RETURN; END IF;
 UPDATE tgmate.web_sessions SET revoked_at=now() WHERE id=s.id;
 PERFORM set_config('app.tenant_id',s.tenant_id::text,true);
 INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(s.tenant_id,s.user_id,'auth.session.revoked','web_session',s.id);
END $$;

CREATE FUNCTION tgmate.auth_take_attempt(p_account text,p_ip text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE identifier uuid; a_count bigint; i_count bigint;
BEGIN
 IF p_account !~ '^[0-9a-f]{64}$' OR p_ip !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid rate keys'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('auth-account:'||p_account,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('auth-ip:'||p_ip,0));
 SELECT count(*) INTO a_count FROM tgmate.web_login_attempts WHERE account_key=p_account AND NOT succeeded AND attempted_at>now()-interval '15 minutes';
 SELECT count(*) INTO i_count FROM tgmate.web_login_attempts WHERE ip_key=p_ip AND NOT succeeded AND attempted_at>now()-interval '15 minutes';
 IF a_count>=10 OR i_count>=50 THEN RETURN NULL; END IF;
 INSERT INTO tgmate.web_login_attempts(account_key,ip_key) VALUES(p_account,p_ip) RETURNING id INTO identifier;
 RETURN identifier;
END $$;
CREATE FUNCTION tgmate.auth_mark_success(p_attempt uuid)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
 UPDATE tgmate.web_login_attempts SET succeeded=true WHERE id=p_attempt
$$;

CREATE FUNCTION tgmate.auth_password_record(p_token text)
RETURNS TABLE(user_id uuid,password_hash text,credential_version bigint)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
 SELECT c.user_id,c.password_hash,c.credential_version FROM tgmate.web_credentials c JOIN tgmate.auth_context(p_token) x ON x.user_id=c.user_id
$$;
CREATE FUNCTION tgmate.auth_change_password(p_token text,p_version bigint,p_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,tgmate AS $$
DECLARE u uuid; t uuid;
BEGIN
 SELECT x.user_id,x.tenant_id INTO u,t FROM tgmate.auth_context(p_token) x;
 IF u IS NULL THEN RAISE EXCEPTION 'invalid session'; END IF;
 UPDATE tgmate.web_credentials SET password_hash=p_hash,credential_version=credential_version+1,changed_at=now() WHERE user_id=u AND credential_version=p_version AND NOT disabled;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential changed'; END IF;
 UPDATE tgmate.web_sessions SET revoked_at=now() WHERE user_id=u AND revoked_at IS NULL;
 PERFORM set_config('app.tenant_id',t::text,true);
 INSERT INTO tgmate.audit_logs(tenant_id,actor_user_id,action,resource_type,resource_id) VALUES(t,u,'auth.password.changed','user',u);
END $$;

REVOKE ALL ON FUNCTION tgmate.auth_lookup(text,text),tgmate.auth_create_session(uuid,uuid,text,text,timestamptz,bigint),tgmate.auth_context(text),tgmate.auth_revoke_session(text),tgmate.auth_take_attempt(text,text),tgmate.auth_mark_success(uuid),tgmate.auth_password_record(text),tgmate.auth_change_password(text,bigint,text) FROM PUBLIC;
COMMIT;
