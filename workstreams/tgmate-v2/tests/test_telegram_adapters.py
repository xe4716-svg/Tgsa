import httpx,pytest
from app.telegram_adapters import BotBroadcast
from app.worker import RetryLater,UnknownSend,PermanentFailure

def adapter(responses):
 calls=[]
 def handle(request):
  calls.append(request.url.path.split('/')[-1])
  status,body=responses.pop(0)
  return httpx.Response(status,json=body)
 client=httpx.Client(transport=httpx.MockTransport(handle))
 return BotBroadcast(client,lambda identifier:'test-token'),calls

E={'telegram_bot_id':'internal','external_bot_id':44}
D={'telegram_peer_id':-10011}

def test_bot_live_adapter_contract_only_with_mock_http():
 a,calls=adapter([(200,{'ok':True,'result':{'id':44}}),(200,{'ok':True,'result':{'type':'supergroup','permissions':{'can_send_messages':True}}}),(200,{'ok':True,'result':{'status':'member'}}),(200,{'ok':True,'result':{'message_id':55}})])
 assert a.preflight('bot_broadcast',E,D)=={'can_send_messages':True}
 assert a.send('bot_broadcast',E,D,{'text':'hello'})==55
 assert calls==['getMe','getChat','getChatMember','sendMessage']

def test_bot_identity_mismatch_denied():
 a,calls=adapter([(200,{'ok':True,'result':{'id':45}})])
 with pytest.raises(PermanentFailure): a.preflight('bot_broadcast',E,D)

def test_channel_target_is_denied():
 a,calls=adapter([(200,{'ok':True,'result':{'id':44}}),(200,{'ok':True,'result':{'type':'channel'}})])
 assert not a.preflight('bot_broadcast',E,D)['can_send_messages']

def test_floodwait_persisted_by_caller():
 a,calls=adapter([(429,{'ok':False,'parameters':{'retry_after':31}})])
 with pytest.raises(RetryLater) as result: a.send('bot_broadcast',E,D,{'text':'hello'})
 assert result.value.seconds==31

def test_server_error_has_unknown_send_outcome():
 a,calls=adapter([(500,{'ok':False})])
 with pytest.raises(UnknownSend): a.send('bot_broadcast',E,D,{'text':'hello'})

def test_account_permissions_use_banned_rights_not_nonexistent_property():
 from types import SimpleNamespace as S
 from app.telegram_adapters import account_can_send
 p=S(has_left=False,is_admin=False,is_creator=False,participant=S(banned_rights=None))
 assert account_can_send(p,S(left=False,default_banned_rights=None))
 assert not account_can_send(p,S(left=False,default_banned_rights=S(send_messages=True)))
 p.participant.banned_rights=S(send_plain=True)
 assert not account_can_send(p,S(left=False,default_banned_rights=None))

def test_moderation_restore_lifts_individual_restriction_without_changing_group_defaults():
 from app.telegram_adapters import ModerationBot,restriction_fingerprint
 import json,time
 snapshot={'status':'restricted','user':{'id':300},'is_member':True,'until_date':int(time.time())+120,'can_send_messages':False,'can_send_photos':False}
 responses=[{'ok':True,'result':snapshot},{'ok':True,'result':{'type':'supergroup','permissions':{'can_send_messages':True,'can_send_photos':False,'can_invite_users':False}}},{'ok':True,'result':True},{'ok':True,'result':{'status':'member','user':{'id':300}}}]; requests=[]
 def handle(request): requests.append((request.url.path.split('/')[-1],json.loads(request.content))); return httpx.Response(200,json=responses.pop(0))
 a=ModerationBot(httpx.Client(transport=httpx.MockTransport(handle)),lambda identifier:'test-token')
 action={'action_type':'unrestrict','telegram_user_id':300,'restore_until_date':snapshot['until_date'],'restore_fingerprint':restriction_fingerprint(snapshot)}
 assert a.execute(dict(E,telegram_peer_id=-10011),action,{})=={'ok':True}
 request=next(data for method,data in requests if method=='restrictChatMember')
 assert all(request['permissions'].values())
 assert 'setChatPermissions' not in [method for method,data in requests]
 assert requests[-1][0]=='getChatMember'


def test_moderation_restore_refuses_admin_changes():
 from app.telegram_adapters import ModerationBot,restriction_fingerprint
 snapshot={'status':'restricted','user':{'id':300},'until_date':100000,'can_send_messages':False}
 changed=dict(snapshot,can_send_photos=True)
 a,calls=adapter([(200,{'ok':True,'result':changed})]); bot=ModerationBot(a.client,a.token_resolver)
 with pytest.raises(PermanentFailure): bot.execute(dict(E,telegram_peer_id=-10011),{'action_type':'unrestrict','telegram_user_id':300,'restore_until_date':100000,'restore_fingerprint':restriction_fingerprint(snapshot)},{})
 assert calls==['getChatMember']


def test_restrict_snapshot_failure_is_unknown_not_retryable():
 from app.telegram_adapters import ModerationBot
 import time
 a,calls=adapter([(200,{'ok':True,'result':{'status':'member'}}),(200,{'ok':True,'result':True}),(429,{'ok':False,'parameters':{'retry_after':60}})])
 bot=ModerationBot(a.client,a.token_resolver)
 with pytest.raises(UnknownSend): bot.execute(dict(E,telegram_peer_id=-10011,verification_timeout_seconds=60),{'action_type':'restrict','telegram_user_id':300,'until_date':int(time.time())+120},{})
 assert calls==['getChatMember','restrictChatMember','getChatMember']


def test_restrict_near_expiry_cannot_accidentally_become_permanent():
 from app.telegram_adapters import ModerationBot
 import time
 a,calls=adapter([(200,{'ok':True,'result':{'status':'member'}})])
 bot=ModerationBot(a.client,a.token_resolver)
 with pytest.raises(PermanentFailure): bot.execute(dict(E,telegram_peer_id=-10011,verification_timeout_seconds=60),{'action_type':'restrict','telegram_user_id':300,'until_date':int(time.time())+10},{})
 assert calls==['getChatMember']

def test_appeal_unban_checks_ownership_and_never_removes_current_members():
 from app.telegram_adapters import ModerationBot,restriction_fingerprint
 import json
 member={'status':'kicked','user':{'id':300},'until_date':0}; requests=[]
 responses=[{'ok':True,'result':member},{'ok':True,'result':True}]
 def handle(request): requests.append((request.url.path.split('/')[-1],json.loads(request.content))); return httpx.Response(200,json=responses.pop(0))
 bot=ModerationBot(httpx.Client(transport=httpx.MockTransport(handle)),lambda identifier:'test-token')
 assert bot.execute(dict(E,telegram_peer_id=-10011),{'action_type':'unban','telegram_user_id':300,'restore_ban_fingerprint':restriction_fingerprint(member)},{})=={'ok':True}
 assert requests[-1][1]['only_if_banned'] is True


def test_appeal_unban_refuses_changed_ban():
 from app.telegram_adapters import ModerationBot,restriction_fingerprint
 original={'status':'kicked','user':{'id':300},'until_date':1000}
 a,calls=adapter([(200,{'ok':True,'result':dict(original,until_date=2000)})]); bot=ModerationBot(a.client,a.token_resolver)
 with pytest.raises(PermanentFailure): bot.execute(dict(E,telegram_peer_id=-10011),{'action_type':'unban','telegram_user_id':300,'restore_ban_fingerprint':restriction_fingerprint(original)},{})
 assert calls==['getChatMember']

@pytest.mark.parametrize('member,joined,can_send',[
 ({'status':'restricted','is_member':False,'can_send_messages':True},False,False),
 ({'status':'restricted','is_member':True,'can_send_messages':False},True,False),
 ({'status':'restricted','is_member':True,'can_send_messages':True},True,True),
 ({'status':'left'},False,False),
])
def test_moderation_preflight_respects_personal_membership_and_permissions(member,joined,can_send):
 from app.telegram_adapters import ModerationBot
 member=dict(member,user={'id':44})
 a,_=adapter([(200,{'ok':True,'result':{'id':44}}),(200,{'ok':True,'result':{'id':-10011,'type':'supergroup','permissions':{'can_send_messages':True}}}),(200,{'ok':True,'result':member})])
 facts=ModerationBot(a.client,a.token_resolver).permissions(dict(E,telegram_peer_id=-10011))
 assert (facts['membership_status']=='member') is joined
 assert facts['can_send_messages'] is can_send

def test_basic_group_admin_delete_does_not_invent_supergroup_restriction():
 from app.telegram_adapters import ModerationBot
 a,_=adapter([(200,{'ok':True,'result':{'id':44}}),(200,{'ok':True,'result':{'id':-10011,'type':'group'}}),(200,{'ok':True,'result':{'status':'administrator','user':{'id':44}}})])
 facts=ModerationBot(a.client,a.token_resolver).permissions(dict(E,telegram_peer_id=-10011))
 assert facts['can_delete_messages'] is True and facts['can_restrict_members'] is False
