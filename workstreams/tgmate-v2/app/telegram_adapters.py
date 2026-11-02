"""Live adapters are opt-in and have not been tested against real Telegram identities."""
import asyncio,uuid
import httpx
from app.db import settings
from app.worker import UnknownSend,RetryLater,PermanentFailure

class BotBroadcast:
 def __init__(self,client=None,token_resolver=None):
  self.client=client or httpx.Client(timeout=15)
  self.token_resolver=token_resolver
 def call(self,executor,method,data):
  if self.token_resolver is not None:
   token=self.token_resolver(executor['telegram_bot_id'])
  else:
   from app.bot_credentials import resolve_token
   if not executor.get('tenant_id'): raise PermanentFailure('Bot tenant context unavailable')
   token=resolve_token(executor['tenant_id'],executor['telegram_bot_id'])
  if not token: raise PermanentFailure('Bot credential unavailable')
  try: response=self.client.post(f'https://api.telegram.org/bot{token}/{method}',json=data)
  except httpx.TransportError: raise UnknownSend() from None
  try: body=response.json()
  except ValueError: raise UnknownSend() from None
  if response.status_code==429: raise RetryLater(body.get('parameters',{}).get('retry_after',30))
  if response.status_code>=500: raise UnknownSend()
  if not isinstance(body,dict) or not body.get('ok'): raise PermanentFailure('Telegram rejected request')
  return body.get('result')
 def preflight(self,module,executor,destination):
  if module!='bot_broadcast': raise PermanentFailure('Wrong channel')
  me=self.call(executor,'getMe',{})
  if not executor.get('external_bot_id') or me.get('id')!=executor['external_bot_id']: raise PermanentFailure('Bot identity mismatch')
  peer=destination['telegram_peer_id']
  chat=self.call(executor,'getChat',{'chat_id':peer})
  if chat.get('type') not in ('group','supergroup'): return {'can_send_messages':False}
  member=self.call(executor,'getChatMember',{'chat_id':peer,'user_id':me['id']})
  status=member.get('status')
  allowed=status in ('creator','administrator')
  if status=='member': allowed=chat.get('permissions',{}).get('can_send_messages') is True
  if status=='restricted': allowed=member.get('is_member') is True and member.get('can_send_messages') is True
  return {'can_send_messages':allowed}
 def send(self,module,executor,destination,content):
  if module!='bot_broadcast': raise PermanentFailure('Wrong channel')
  result=self.call(executor,'sendMessage',{'chat_id':destination['telegram_peer_id'],'text':content['text']})
  if not isinstance(result,dict) or type(result.get('message_id')) is not int: raise UnknownSend()
  return result['message_id']

def account_can_send(permissions,entity):
 if not permissions or getattr(entity,'left',False) or permissions.has_left: return False
 if permissions.is_admin or permissions.is_creator: return True
 personal=getattr(permissions.participant,'banned_rights',None)
 default=getattr(entity,'default_banned_rights',None)
 for rights in (personal,default):
  if rights and any(getattr(rights,key,False) for key in ('view_messages','send_messages','send_plain')): return False
 return True

class AccountBroadcast:
 def __init__(self): self.peers={}
 async def operation(self,executor,destination,content=None):
  from telethon import TelegramClient,errors,utils,types
  from telethon.sessions import StringSession
  from telethon.tl.functions.messages import SendMessageRequest
  credential=settings().get('account_credentials',{}).get(str(executor['telegram_session_id']))
  if not credential: raise PermanentFailure('Account credential unavailable')
  client=TelegramClient(StringSession(credential['string_session']),credential['api_id'],credential['api_hash'],request_retries=0,connection_retries=0,auto_reconnect=False,flood_sleep_threshold=0,timeout=10,receive_updates=False)
  sent=False
  try:
   await client.connect()
   if not await client.is_user_authorized(): raise PermanentFailure('Session is not authorized')
   me=await client.get_me()
   if not executor.get('external_user_id') or me.id!=executor['external_user_id']: raise PermanentFailure('Account identity mismatch')
   key=(str(executor['telegram_session_id']),destination['telegram_peer_id'])
   peer=self.peers.get(key)
   if peer is None:
    async for dialog in client.iter_dialogs(limit=200):
     if utils.get_peer_id(dialog.entity)==destination['telegram_peer_id']:
      if not (isinstance(dialog.entity,types.Chat) or isinstance(dialog.entity,types.Channel) and dialog.entity.megagroup): raise PermanentFailure('Only joined groups are permitted')
      peer=await client.get_input_entity(dialog.entity); self.peers[key]=peer; break
   if peer is None: raise PermanentFailure('Group membership could not be verified')
   permissions=await client.get_permissions(peer,me)
   entity=await client.get_entity(peer)
   allowed=account_can_send(permissions,entity)
   if not allowed or content is None: return {'can_send_messages':allowed}
   random_id=uuid.UUID(executor['operation_key']).int & ((1<<63)-1)
   sent=True
   result=await client(SendMessageRequest(peer=peer,message=content['text'],random_id=random_id))
   if isinstance(result,types.UpdateShortSentMessage): return result.id
   for update in getattr(result,'updates',[]):
    if isinstance(update,types.UpdateMessageID) and update.random_id==random_id: return update.id
   raise UnknownSend()
  except errors.FloodWaitError as e: raise RetryLater(e.seconds) from None
  except (errors.ForbiddenError,errors.UnauthorizedError,errors.BadRequestError): raise PermanentFailure('Telegram rejected account request') from None
  except (OSError,TimeoutError):
   if sent: raise UnknownSend() from None
   raise PermanentFailure('Preflight connection unavailable') from None
  finally: await client.disconnect()
 def preflight(self,module,executor,destination):
  if module!='account_broadcast': raise PermanentFailure('Wrong channel')
  return asyncio.run(self.operation(executor,destination))
 def send(self,module,executor,destination,content):
  if module!='account_broadcast': raise PermanentFailure('Wrong channel')
  result=asyncio.run(self.operation(executor,destination,content))
  if type(result) is not int: raise PermanentFailure('Account send permission denied')
  return result

def restriction_fingerprint(member):
 import hashlib,json
 values={k:v for k,v in member.items() if k.startswith('can_') or k in ('status','is_member','until_date')}
 values['user_id']=member.get('user',{}).get('id')
 return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()

class ModerationBot(BotBroadcast):
 def me(self,config):
  result=self.call(config,'getMe',{})
  if result.get('id')!=config.get('external_bot_id') or not config.get('external_bot_id'): raise PermanentFailure('Bot identity mismatch')
  return result
 def member_role(self,config,user):
  self.me(config)
  member=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':user})
  return member.get('status','unknown')
 def permissions(self,config):
  me=self.me(config)
  chat=self.call(config,'getChat',{'chat_id':config['telegram_peer_id']})
  if not isinstance(chat,dict) or chat.get('id')!=config['telegram_peer_id'] or chat.get('type') not in ('group','supergroup'): return {'membership_status':'unknown'}
  member=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':me['id']})
  if not isinstance(member,dict) or member.get('user',{}).get('id')!=me['id']: return {'membership_status':'unknown'}
  creator=member.get('status')=='creator'; admin=member.get('status')=='administrator'
  joined=member.get('status') in ('creator','administrator','member') or member.get('status')=='restricted' and member.get('is_member') is True
  can_send=creator or admin or joined and (member.get('can_send_messages') is True if member.get('status')=='restricted' else chat.get('permissions',{}).get('can_send_messages') is True)
  return {'membership_status':'member' if joined else 'not_member','can_send_messages':can_send,'can_delete_messages':creator or admin and (chat['type']=='group' or member.get('can_delete_messages') is True),'can_restrict_members':chat['type']=='supergroup' and (creator or admin and member.get('can_restrict_members') is True),'can_ban_members':creator or admin and (chat['type']=='group' or member.get('can_restrict_members') is True)}

 def execute(self,config,action,message):
  from datetime import datetime,timezone
  kind=action['action_type']; data={'chat_id':config['telegram_peer_id']}
  if kind=='delete_message': method='deleteMessage'; data['message_id']=message['message_id']
  elif kind=='warn': method='sendMessage'; data['text']='群管理提示：请遵守本群规则。'
  elif kind=='restrict':
   member=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
   if member.get('status')!='member': raise PermanentFailure('Existing member restrictions must not be overwritten')
   until=action.get('until_date') or int(datetime.now(timezone.utc).timestamp())+max(120,config['verification_timeout_seconds']+60)
   if until<int(datetime.now(timezone.utc).timestamp())+45: raise PermanentFailure('Restriction expiry too close')
   method='restrictChatMember'; data.update(user_id=action['telegram_user_id'],permissions={'can_send_messages':False},until_date=until)
  elif kind=='unrestrict':
   member=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
   if member.get('status') in ('member','creator','administrator','left','kicked'): return {'ok':True,'already_released':True}
   if member.get('status')!='restricted' or member.get('until_date')!=action.get('restore_until_date') or restriction_fingerprint(member)!=action.get('restore_fingerprint'): raise PermanentFailure('Restriction ownership changed')
   chat=self.call(config,'getChat',{'chat_id':config['telegram_peer_id']})
   defaults=chat.get('permissions')
   if not isinstance(defaults,dict) or not defaults: raise PermanentFailure('Group default permissions unavailable')
   # Bot API explicitly requires every permission True to remove the individual
   # restriction. Copying False defaults back here would create new permanent
   # individual bans. Group defaults are not changed by restrictChatMember.
   fields=('can_send_messages','can_send_audios','can_send_documents','can_send_photos','can_send_videos','can_send_video_notes','can_send_voice_notes','can_send_polls','can_send_other_messages','can_add_web_page_previews','can_react_to_messages','can_edit_tag','can_change_info','can_invite_users','can_pin_messages','can_manage_topics')
   permissions={key:True for key in fields}
   method='restrictChatMember'; data.update(user_id=action['telegram_user_id'],permissions=permissions,use_independent_chat_permissions=True,until_date=0)
  elif kind=='ban': method='banChatMember'; data['user_id']=action['telegram_user_id']
  elif kind=='unban':
   member=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
   if member.get('status')!='kicked': return {'ok':True,'already_unbanned':True}
   if not action.get('restore_ban_fingerprint') or restriction_fingerprint(member)!=action['restore_ban_fingerprint']: raise PermanentFailure('Ban ownership changed')
   method='unbanChatMember'; data.update(user_id=action['telegram_user_id'],only_if_banned=True)
  else: raise PermanentFailure('Unsupported moderation action')
  response=self.call(config,method,data)
  if isinstance(response,dict) and type(response.get('message_id')) is int: return {'message_id':response['message_id']}
  if response is True:
   if kind=='ban':
    try: snapshot=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
    except Exception: raise UnknownSend() from None
    if snapshot.get('status')!='kicked': raise UnknownSend()
    return {'ok':True,'ban_fingerprint':restriction_fingerprint(snapshot)}
   if kind=='restrict':
    try: snapshot=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
    except Exception: raise UnknownSend() from None
    if snapshot.get('status')!='restricted' or snapshot.get('until_date')!=until: raise UnknownSend()
    return {'ok':True,'applied_until_date':until,'restriction_fingerprint':restriction_fingerprint(snapshot)}
   if kind=='unrestrict':
    try: snapshot=self.call(config,'getChatMember',{'chat_id':config['telegram_peer_id'],'user_id':action['telegram_user_id']})
    except Exception: raise UnknownSend() from None
    if snapshot.get('status') not in ('member','administrator','creator','left','kicked'): raise UnknownSend()
   return {'ok':True}
  raise UnknownSend()

 def send_notice(self,config,text,markup=None):
  data={'chat_id':config['telegram_peer_id'],'text':text}
  if markup: data['reply_markup']=markup
  result=self.call(config,'sendMessage',data)
  if not isinstance(result,dict) or type(result.get('message_id')) is not int: raise UnknownSend()
  return result['message_id']

 def answer_callback(self,config,query_id,text):
  result=self.call(config,'answerCallbackQuery',{'callback_query_id':query_id,'text':text,'show_alert':False})
  if result is not True: raise UnknownSend()
  return True

 def send_private(self,bot,target,text,markup=None):
  config={'telegram_bot_id':bot['id'],'external_bot_id':bot['telegram_bot_id'],'tenant_id':bot.get('tenant_id')}
  self.me(config)
  data={'chat_id':target,'text':text}
  if markup: data['reply_markup']=markup
  result=self.call(config,'sendMessage',data)
  if not isinstance(result,dict) or type(result.get('message_id')) is not int: raise UnknownSend()
  return result['message_id']
