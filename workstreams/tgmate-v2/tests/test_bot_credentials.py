import uuid,pytest,psycopg,httpx
from test_service import service
from conftest import DB,seed
from app.bot_credentials import register,resolve_token,RegistrationRejected,TelegramIdentityProbe
from app.db import transaction

class Probe:
 def __init__(self,identifier): self.identifier=identifier
 def verify(self,token): return {'id':self.identifier,'is_bot':True,'username':'fixture_bot','first_name':'Fixture'}

def test_registration_rotation_and_isolation(service):
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('x'*30)
 result=register(a['tenant'],a['user'],token,Probe(external)); bot=result['id']
 assert result['webhook_registered'] is False
 assert resolve_token(a['tenant'],bot)==token
 with transaction(a['tenant']) as db:
  row=db.execute('SELECT token_ciphertext,version FROM tgmate.bot_credentials WHERE bot_id=%s',(bot,)).fetchone()
  assert token not in row['token_ciphertext'] and row['version']==1
 new=f'{external}:'+('y'*30)
 assert register(a['tenant'],a['user'],new,Probe(external))['id']==bot
 assert resolve_token(a['tenant'],bot)==new
 with psycopg.connect(**DB) as db: other=seed(db)
 assert resolve_token(other['tenant'],bot) is None
 with pytest.raises(RegistrationRejected,match='无法登记'): register(other['tenant'],other['user'],new,Probe(external))
 with transaction(other['tenant']) as db:
  assert db.execute('SELECT count(*) AS n FROM tgmate.bot_credentials').fetchone()['n']==0
 assert resolve_token(a['tenant'],bot)==new

def test_invalid_identity_and_actor_do_not_store(service):
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('z'*30)
 with pytest.raises(RegistrationRejected,match='不一致'): register(a['tenant'],a['user'],token,Probe(external+1))
 with pytest.raises(RegistrationRejected,match='操作者'): register(a['tenant'],uuid.uuid4(),token,Probe(external))
 with transaction(a['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.bot_credentials').fetchone()['n']==0

def test_probe_does_not_echo_token_on_network_error():
 token='123456:'+('s'*30)
 def broken(request): raise httpx.ConnectError('secret URL '+str(request.url))
 client=httpx.Client(transport=httpx.MockTransport(broken))
 with pytest.raises(RegistrationRejected) as error: TelegramIdentityProbe(client).verify(token)
 assert token not in str(error.value)

def test_registration_http_does_not_echo_invalid_token(service):
 client,a,_=service
 response=client.post('/api/bots/register',headers={'Authorization':'Bearer test-admin-key'},json={'token':'private-bad-token'})
 assert response.status_code==422 and 'private-bad-token' not in response.text

def test_default_runtime_adapters_use_encrypted_credentials(service):
 from app.telegram_adapters import BotBroadcast,ModerationBot
 from app.worker import Telegram,PermanentFailure
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('r'*30)
 bot=register(a['tenant'],a['user'],token,Probe(external))['id']; calls=[]
 def handle(request):
  assert request.url.path.startswith('/bot'+token+'/')
  method=request.url.path.split('/')[-1]; calls.append(method)
  return httpx.Response(200,json={'ok':True,'result':{'id':external} if method=='getMe' else {'message_id':912}})
 client=httpx.Client(transport=httpx.MockTransport(handle))
 config={'tenant_id':a['tenant'],'telegram_bot_id':bot,'external_bot_id':external}
 assert BotBroadcast(client).send('bot_broadcast',config,{'telegram_peer_id':-1001},{'text':'fixture'})==912
 assert ModerationBot(client).send_private({'tenant_id':a['tenant'],'id':bot,'telegram_bot_id':external},100,'fixture')==912
 assert Telegram(client).send(100,{'source_chat_id':200,'source_message_id':1},config)==912
 assert calls==['sendMessage','getMe','sendMessage','copyMessage']
 with pytest.raises(PermanentFailure): BotBroadcast(client).send('bot_broadcast',{'telegram_bot_id':bot},{'telegram_peer_id':-1001},{'text':'fixture'})
 with psycopg.connect(**DB) as db: other=seed(db)
 with pytest.raises(PermanentFailure): BotBroadcast(client).send('bot_broadcast',dict(config,tenant_id=other['tenant']),{'telegram_peer_id':-1001},{'text':'fixture'})
 assert len(calls)==4

def test_corrupt_or_revoked_credentials_never_reach_network(service):
 from app.telegram_adapters import BotBroadcast
 from app.worker import PermanentFailure
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('q'*30)
 bot=register(a['tenant'],a['user'],token,Probe(external))['id']
 with transaction(a['tenant']) as db: db.execute("UPDATE tgmate.telegram_bots SET status='revoked' WHERE id=%s",(bot,))
 assert resolve_token(a['tenant'],bot) is None
 with transaction(a['tenant']) as db:
  db.execute("UPDATE tgmate.telegram_bots SET status='active' WHERE id=%s",(bot,))
  db.execute("UPDATE tgmate.bot_credentials SET token_ciphertext='corrupted' WHERE bot_id=%s",(bot,))
 client=httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('Unavailable credentials must not send')))
 with pytest.raises(PermanentFailure): BotBroadcast(client).send('bot_broadcast',{'tenant_id':a['tenant'],'telegram_bot_id':bot},{'telegram_peer_id':-1001},{'text':'fixture'})

def test_legacy_migration_preserves_existing_bindings(service):
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('m'*30)
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.telegram_bots SET telegram_bot_id=%s WHERE id=%s',(external,a['bot']))
 with pytest.raises(RegistrationRejected,match='迁移'): register(a['tenant'],a['user'],token,Probe(external))
 result=register(a['tenant'],a['user'],token,Probe(external),existing_bot_id=a['bot'])
 assert result['id']==str(a['bot']) and resolve_token(a['tenant'],a['bot'])==token
 with transaction(a['tenant']) as db:
  assert db.execute('SELECT telegram_bot_id FROM tgmate.relay_configs WHERE id=%s',(a['config'],)).fetchone()['telegram_bot_id']==a['bot']
  assert db.execute("SELECT count(*) AS n FROM tgmate.audit_logs WHERE action='bot.credential_migrated'").fetchone()['n']==1

def test_legacy_migration_rejects_other_tenant_and_mismatched_id(service):
 _,a,_=service; external=uuid.uuid4().int%10**12+10000; token=f'{external}:'+('n'*30)
 with psycopg.connect(**DB) as db:
  other=seed(db)
  db.execute('UPDATE tgmate.telegram_bots SET telegram_bot_id=%s WHERE id=%s',(external,other['bot']))
 with pytest.raises(RegistrationRejected,match='不匹配'): register(a['tenant'],a['user'],token,Probe(external),existing_bot_id=other['bot'])
 with pytest.raises(RegistrationRejected,match='不匹配'): register(a['tenant'],a['user'],token,Probe(external),existing_bot_id=a['bot'])
 assert resolve_token(a['tenant'],a['bot']) is None
