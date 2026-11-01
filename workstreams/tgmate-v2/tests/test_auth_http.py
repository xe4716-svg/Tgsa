from fastapi.testclient import TestClient
from app.main import app
from test_auth import web_user,PASSWORD
from test_service import service
from app.db import settings
from conftest import DB
import json,os,uuid,psycopg,pytest
from pathlib import Path


def client_for(web_user): return TestClient(app,base_url='http://127.0.0.1:8769',client=(web_user[3],50000))

def signin(client,web_user):
 a,email,slug,ip=web_user
 return client.post('/auth/login',json={'email':email,'workspace':slug,'password':PASSWORD})


def csrf(client): return {'X-CSRF-Token':client.cookies['tgmate_csrf']}


def test_login_cookie_and_logout(web_user):
 a,*_=web_user; client=client_for(web_user); response=signin(client,web_user)
 assert response.status_code==200
 assert 'token' not in response.json() and 'csrf' not in response.json()
 assert 'HttpOnly' in response.headers.get_list('set-cookie')[0]
 assert client.get('/auth/me').json()['user_id']==str(a['user'])
 assert client.post('/auth/logout',headers=csrf(client)).status_code==200
 assert client.get('/auth/me').status_code==401


def test_cookie_writes_need_csrf_and_origin(web_user):
 a,*_=web_user; client=client_for(web_user); signin(client,web_user)
 url=f"/api/relay/configs/{a['config']}/state"
 assert client.post(url,json={'status':'paused'}).status_code==403
 assert client.post(url,json={'status':'paused'},headers=dict(csrf(client),Origin='https://evil.example')).status_code==403
 assert client.post(url,json={'status':'paused'},headers=csrf(client)).status_code==200


def test_read_only_role_cannot_write(web_user):
 a,*_=web_user
 with psycopg.connect(**DB) as db:
  db.execute("DELETE FROM tgmate.role_permissions WHERE tenant_id=%s",(a['tenant'],))
  role=db.execute('SELECT role_id FROM tgmate.tenant_member_roles WHERE tenant_id=%s AND user_id=%s',(a['tenant'],a['user'])).fetchone()[0]
  db.execute("INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) VALUES(%s,%s,'relay.read')",(a['tenant'],role))
 client=client_for(web_user); signin(client,web_user)
 assert client.get('/api/relay/configs').status_code==200
 assert client.post(f"/api/relay/configs/{a['config']}/state",json={'status':'paused'},headers=csrf(client)).status_code==403
 assert client.get('/api/broadcast/plans/'+str(uuid.uuid4())+'/preview').status_code==403


def test_module_operator_can_manage_only_its_module(web_user):
 a,*_=web_user
 with psycopg.connect(**DB) as db:
  db.execute('DELETE FROM tgmate.role_permissions WHERE tenant_id=%s',(a['tenant'],))
  role=db.execute('SELECT role_id FROM tgmate.tenant_member_roles WHERE tenant_id=%s AND user_id=%s',(a['tenant'],a['user'])).fetchone()[0]
  for code in ['relay.read','relay.manage']: db.execute('INSERT INTO tgmate.role_permissions(tenant_id,role_id,permission_code) VALUES(%s,%s,%s)',(a['tenant'],role,code))
 client=client_for(web_user); response=signin(client,web_user)
 assert response.json()['role']=='operator'
 assert client.post(f"/api/relay/configs/{a['config']}/state",json={'status':'paused'},headers=csrf(client)).status_code==200
 assert client.get('/api/moderation/appeals').status_code==403


def test_cross_origin_login_and_untrusted_host_denied(web_user):
 a,email,slug,ip=web_user; client=client_for(web_user)
 assert client.post('/auth/login',json={'email':email,'workspace':slug,'password':PASSWORD},headers={'Origin':'https://evil.example'}).status_code==403
 assert client.get('/health',headers={'Host':'evil.example'}).status_code==400


def test_validation_never_echoes_password(web_user):
 client=client_for(web_user); secret='synthetic-input-password'
 response=client.post('/auth/login',json={'email':{},'workspace':'test','password':secret})
 assert response.status_code==422 and secret not in response.text


def test_static_bootstrap_key_disabled_outside_sandbox(web_user):
 p=Path(os.environ['TGMATE_CONFIG']); c=json.loads(p.read_text()); c['mode']='production'; c['public_origin']='https://testserver';p.write_text(json.dumps(c))
 client=TestClient(app,base_url='https://testserver')
 assert client.get('/api/relay/configs',headers={'Authorization':'Bearer test-admin-key'}).status_code==401


def test_production_login_cookie_requires_secure_transport(web_user):
 p=Path(os.environ['TGMATE_CONFIG']); c=json.loads(p.read_text()); c['mode']='production'; c['public_origin']='https://testserver';p.write_text(json.dumps(c))
 client=TestClient(app,base_url='https://testserver',client=(web_user[3],50000))
 response=signin(client,web_user)
 assert response.status_code==200 and 'Secure' in response.headers.get_list('set-cookie')[0]
