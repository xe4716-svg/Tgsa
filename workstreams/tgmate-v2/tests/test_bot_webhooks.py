import json,os,uuid,pytest
from pathlib import Path
from test_service import service,message
from test_bot_credentials import Probe
from app.bot_credentials import register
from app.bot_webhooks import prepare,apply,ingress,WebhookRejected
from app.db import transaction,unseal
from app.worker import UnknownSend

@pytest.fixture
def webhook(service):
 client,a,_=service; external=uuid.uuid4().int%10**12+10000
 # Migrate the fixture's existing Bot to preserve its relay binding.
 with transaction(a['tenant']) as db: db.execute('UPDATE tgmate.telegram_bots SET telegram_bot_id=%s WHERE id=%s',(external,a['bot']))
 register(a['tenant'],a['user'],f'{external}:'+('w'*30),Probe(external),existing_bot_id=a['bot'])
 path=Path(os.environ['TGMATE_CONFIG']);cfg=json.loads(path.read_text());cfg['public_origin']='https://tgmate.example';path.write_text(json.dumps(cfg))
 return client,a

class Remote:
 def __init__(self,url='',unknown=False): self.url=url;self.unknown=unknown;self.calls=[];self.secret=None
 def call(self,config,method,data):
  self.calls.append(method)
  if method=='getWebhookInfo': return {'url':self.url}
  assert method=='setWebhook' and data['drop_pending_updates'] is False
  assert len(data['secret_token'])<=256 and '=' not in data['secret_token']
  self.secret=data['secret_token']
  if self.unknown: raise UnknownSend()
  self.url=data['url'];return True

def stored(a):
 with transaction(a['tenant']) as db: return db.execute('SELECT * FROM tgmate.bot_webhook_registrations WHERE bot_id=%s',(a['bot'],)).fetchone()

def test_persistent_secret_prepared_apply_and_ingress(webhook):
 client,a=webhook; result=prepare(a['tenant'],a['user'],a['bot']);first=stored(a)
 assert result['status']=='prepared' and 'secret' not in json.dumps(result,default=str)
 assert prepare(a['tenant'],a['user'],a['bot'])==result
 secret=unseal(first['secret_ciphertext'])['secret']
 assert ingress(a['bot'],secret) is None
 remote=Remote(); assert apply(a['tenant'],a['user'],a['bot'],adapter=remote)['status']=='active'
 assert ingress(uuid.uuid4(),secret) is None and ingress(a['bot'],'bad') is None
 assert ingress(a['bot'],secret)['tenant_id']==str(a['tenant'])
 response=client.post(f"/webhooks/bots/{a['bot']}",json=message(),headers={'X-Telegram-Bot-Api-Secret-Token':secret})
 assert response.status_code==200 and response.json()['status']=='queued'
 with pytest.raises(WebhookRejected,match='禁止重复'): apply(a['tenant'],a['user'],a['bot'],adapter=remote)
 assert remote.calls==['getWebhookInfo','setWebhook']

def test_existing_remote_requires_explicit_replacement(webhook):
 _,a=webhook;prepare(a['tenant'],a['user'],a['bot']);remote=Remote('https://other.example/hook')
 result=apply(a['tenant'],a['user'],a['bot'],adapter=remote)
 assert result['status']=='prepared' and result['last_result']=='replacement_requires_confirmation'
 assert remote.calls==['getWebhookInfo']
 assert apply(a['tenant'],a['user'],a['bot'],allow_replace=True,adapter=remote)['status']=='active'

def test_unknown_registration_does_not_auto_repeat(webhook):
 _,a=webhook;prepare(a['tenant'],a['user'],a['bot']);remote=Remote(unknown=True)
 assert apply(a['tenant'],a['user'],a['bot'],adapter=remote)['status']=='uncertain'
 with pytest.raises(WebhookRejected): apply(a['tenant'],a['user'],a['bot'],adapter=remote)
 assert remote.calls==['getWebhookInfo','setWebhook']
 # Successful authentication can prove Telegram installed our secret despite timeout.
 assert ingress(a['bot'],remote.secret)
 assert stored(a)['status']=='active'

def test_prepare_requires_valid_public_origin_and_membership(service):
 _,a,_=service
 with pytest.raises(WebhookRejected,match='公开域名'): prepare(a['tenant'],a['user'],a['bot'])
 path=Path(os.environ['TGMATE_CONFIG']);cfg=json.loads(path.read_text());cfg['public_origin']='https://tgmate.example';path.write_text(json.dumps(cfg))
 with pytest.raises(WebhookRejected,match='操作者'): prepare(a['tenant'],uuid.uuid4(),a['bot'])
 with pytest.raises(WebhookRejected,match='登记'): prepare(a['tenant'],a['user'],a['bot'])

def test_reconcile_does_not_promote_matching_url_to_secret_verified(webhook):
 from app.bot_webhooks import reconcile
 _,a=webhook;preview=prepare(a['tenant'],a['user'],a['bot']);remote=Remote(unknown=True)
 apply(a['tenant'],a['user'],a['bot'],adapter=remote)
 remote.url=preview['url']
 result=reconcile(a['tenant'],a['bot'],remote)
 assert result['remote_url_matches'] is True and result['status']=='uncertain'
 remote.unknown=False
 assert apply(a['tenant'],a['user'],a['bot'],adapter=remote,retry_uncertain=True)['status']=='active'
 assert remote.calls.count('setWebhook')==2

def test_interrupted_registration_reconciles_after_lease_without_reapplying(webhook):
 from app.bot_webhooks import reconcile
 _,a=webhook;prepare(a['tenant'],a['user'],a['bot'])
 with transaction(a['tenant']) as db:
  db.execute("UPDATE tgmate.bot_webhook_registrations SET status='registering',operation_token=%s,updated_at=now()-interval '100 seconds' WHERE bot_id=%s",(uuid.uuid4(),a['bot']))
 remote=Remote()
 assert reconcile(a['tenant'],a['bot'],remote)['status']=='uncertain'
 assert remote.calls==['getWebhookInfo']

def test_forged_envelope_and_cross_tenant_config_are_rejected(webhook):
 import psycopg
 from conftest import DB,seed
 _,a=webhook;prepare(a['tenant'],a['user'],a['bot']);remote=Remote()
 apply(a['tenant'],a['user'],a['bot'],adapter=remote)
 assert ingress(a['bot'],remote.secret[:-3]+'abc') is None
 with psycopg.connect(**DB) as db: other=seed(db)
 with transaction(other['tenant']) as db: assert db.execute('SELECT count(*) AS n FROM tgmate.bot_webhook_registrations').fetchone()['n']==0
 with pytest.raises(WebhookRejected): prepare(other['tenant'],other['user'],a['bot'])
 assert ingress(a['bot'],remote.secret)['tenant_id']==str(a['tenant'])
