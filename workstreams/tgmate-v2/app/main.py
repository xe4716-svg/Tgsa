import secrets
from datetime import datetime
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI,Header,HTTPException,Depends,Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.db import settings,transaction
from app.relay import receive,Rejected
from app.errors import DeferredIngress
from app.auth_http import require_permission
from psycopg.types.json import Jsonb

app=FastAPI(title='TGMate V2.0',docs_url=None,redoc_url=None,openapi_url=None)

def authorized(request:Request,authorization: str=Header(default='')):
 from app.auth_http import authenticate
 return authenticate(request,authorization)

@app.middleware('http')
async def security_headers(request,call_next):
 from urllib.parse import urlsplit
 cfg=settings(); hosts=cfg.get('allowed_hosts',['127.0.0.1','localhost','::1'])
 if not isinstance(hosts,list): hosts=['127.0.0.1','localhost','::1']
 public_host=urlsplit(cfg.get('public_origin','')).hostname
 if public_host: hosts=[*hosts,public_host]
 if request.url.hostname not in hosts: return JSONResponse(status_code=400,content={'detail':'访问地址未授权'})
 response=await call_next(request)
 response.headers['Cache-Control']='no-store'
 response.headers['X-Content-Type-Options']='nosniff'
 response.headers['X-Frame-Options']='DENY'
 return response

@app.get('/',response_class=HTMLResponse)
def home(): return (Path(__file__).parent/'index.html').read_text(encoding='utf-8')

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/api/overview')
def overview(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db:
  counts={}
  for name in ['relay_conversations','relay_operations','task_registry']:
   from psycopg import sql
   counts[name]=db.execute(sql.SQL('SELECT count(*) AS n FROM tgmate.{}').format(sql.Identifier(name))).fetchone()['n']
  return {'mode':settings().get('mode','sandbox'),'counts':counts,'products':['群管理','双向','频道群组克隆','贴纸克隆','机器人群发'],'independent_channel':'TG账号群发'}

@app.get('/api/relay/configs')
def configs(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db: return db.execute('SELECT id,status,generation FROM tgmate.relay_configs ORDER BY created_at DESC').fetchall()

@app.get('/api/relay/conversations')
def conversations(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db: return db.execute('SELECT id,status,created_at FROM tgmate.relay_conversations ORDER BY created_at DESC LIMIT 100').fetchall()

@app.get('/api/relay/operations')
def operations(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db: return db.execute('SELECT id,conversation_id,direction,status,created_at FROM tgmate.relay_operations ORDER BY created_at DESC LIMIT 100').fetchall()

class StateChange(BaseModel):
 status: str

@app.post('/api/relay/configs/{identifier}/state')
def config_state(identifier:UUID,body:StateChange,who=Depends(authorized)):
 require_permission(who,'relay.manage')
 if body.status not in ['active','paused','revoked']: raise HTTPException(422,'状态无效')
 with transaction(who['tenant_id']) as db:
  old=db.execute('SELECT status FROM tgmate.relay_configs WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
  if old and old['status']=='revoked' and body.status!='revoked': raise HTTPException(409,'已撤销的入口不能重新启用')
  row=db.execute('UPDATE tgmate.relay_configs SET status=%s WHERE id=%s RETURNING id,status,generation',(body.status,identifier)).fetchone()
  if not row: raise HTTPException(404,'未找到配置')
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id,metadata) VALUES(%s,'relay.config.state','relay_config',%s,%s)",(who['tenant_id'],identifier,Jsonb({'status':body.status})))
  return row

@app.post('/api/relay/conversations/{identifier}/state')
def conversation_state(identifier:UUID,body:StateChange,who=Depends(authorized)):
 require_permission(who,'relay.manage')
 if body.status not in ['open','paused','blocked','closed']: raise HTTPException(422,'状态无效')
 with transaction(who['tenant_id']) as db:
  row=db.execute('UPDATE tgmate.relay_conversations SET status=%s WHERE id=%s RETURNING id,status,generation',(body.status,identifier)).fetchone()
  if not row: raise HTTPException(404,'未找到会话')
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id,metadata) VALUES(%s,'relay.conversation.state','relay_conversation',%s,%s)",(who['tenant_id'],identifier,Jsonb({'status':body.status})))
  return row

@app.post('/webhooks/telegram/{config_id}')
async def telegram_webhook(config_id:UUID,request:Request,x_telegram_bot_api_secret_token:str=Header(default='')):
 if settings().get('mode')!='sandbox': raise HTTPException(404,'入口已停用，请使用统一Bot入口')
 entry=settings()['webhooks'].get(str(config_id))
 if not entry or not secrets.compare_digest(x_telegram_bot_api_secret_token,entry['secret']): raise HTTPException(403,'入口认证失败')
 raw=b''
 async for chunk in request.stream():
  raw+=chunk
  if len(raw)>1024*1024: raise HTTPException(413,'消息过大')
 try:
  import json
  update=json.loads(raw)
  if not isinstance(update,dict): raise ValueError()
  return receive(entry['tenant_id'],config_id,update)
 except (Rejected,ValueError,TypeError) as e: raise HTTPException(422,str(e) if isinstance(e,Rejected) else '消息格式错误') from None

class Resolution(BaseModel):
 status: str
 evidence: str
 target_message_id: int | None = None

@app.post('/api/relay/operations/{identifier}/resolve')
def resolve(identifier:UUID,body:Resolution,who=Depends(authorized)):
 require_permission(who,'relay.manage')
 if body.status not in ['succeeded','failed','cancelled'] or len(body.evidence.strip())<10 or len(body.evidence)>1000:
  raise HTTPException(422,'请提供核对结果与证据说明')
 if body.status=='succeeded' and (type(body.target_message_id) is not int or body.target_message_id<=0): raise HTTPException(422,'成功结果需要远端消息编号')
 if body.status!='succeeded' and body.target_message_id is not None: raise HTTPException(422,'失败或取消不能填写成功编号')
 with transaction(who['tenant_id']) as db:
  op=db.execute('SELECT * FROM tgmate.relay_operations WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
  if not op: raise HTTPException(404,'未找到消息')
  if op['status']!='uncertain': raise HTTPException(409,'仅可核对结果不明的消息')
  link=db.execute('SELECT * FROM tgmate.relay_runtime_links WHERE operation_id=%s',(identifier,)).fetchone()
  if not link: raise HTTPException(409,'缺少运行记录')
  db.execute('UPDATE tgmate.relay_operations SET status=%s,target_message_id=%s WHERE id=%s',(body.status,body.target_message_id,identifier))
  from app.quota import settle
  settle(db,who['tenant_id'],link['task_id'],body.status=='succeeded')
  db.execute('UPDATE tgmate.task_registry SET status=%s WHERE id=%s',({'succeeded':'completed','failed':'failed','cancelled':'cancelled'}[body.status],link['task_id']))
  db.execute("UPDATE tgmate.task_deliveries SET status=%s,remote_message_ref=%s WHERE task_id=%s",({'succeeded':'succeeded','failed':'failed','cancelled':'skipped'}[body.status],str(body.target_message_id) if body.target_message_id else None,link['task_id']))
  db.execute("INSERT INTO tgmate.audit_logs(tenant_id,action,resource_type,resource_id,metadata) VALUES(%s,'relay.manual_resolution','relay_operation',%s,%s)",(who['tenant_id'],identifier,Jsonb({'status':body.status,'evidence':body.evidence,'actor_role':who['role']})))
  return {'id':str(identifier),'status':body.status}

class BroadcastPlan(BaseModel):
 request_key: str
 module: str
 text: str
 targets: list[UUID]
 robot: UUID | None = None
 binding: UUID | None = None
 session: UUID | None = None
 scheduled_at: datetime | None = None

@app.post('/api/broadcast/plans')
def broadcast_plan(body:BroadcastPlan,who=Depends(authorized)):
 require_permission(who,'broadcast.manage')
 from app.broadcast import create_plan
 try: return create_plan(who['tenant_id'],**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.get('/api/broadcast/plans/{identifier}/preview')
def broadcast_preview(identifier:UUID,who=Depends(authorized)):
 from app.broadcast import preview
 try: return preview(who['tenant_id'],identifier)
 except Rejected as e: raise HTTPException(404,str(e)) from None

@app.post('/api/broadcast/plans/{identifier}/start')
def broadcast_start(identifier:UUID,who=Depends(authorized)):
 require_permission(who,'broadcast.manage')
 from app.broadcast import start_run
 try: return start_run(who['tenant_id'],identifier)
 except Rejected as e: raise HTTPException(422,str(e)) from None

class RunAction(BaseModel):
 action: str

@app.post('/api/broadcast/runs/{identifier}/state')
def broadcast_state(identifier:UUID,body:RunAction,who=Depends(authorized)):
 require_permission(who,'broadcast.manage')
 from app.broadcast import change_run
 try: return change_run(who['tenant_id'],identifier,body.action)
 except Rejected as e: raise HTTPException(422,str(e)) from None

class BroadcastReview(BaseModel):
 destination_id: UUID
 action: str
 evidence: str
 remote_message_id: int | None = None

@app.post('/api/broadcast/runs/{identifier}/review')
def broadcast_review(identifier:UUID,body:BroadcastReview,who=Depends(authorized)):
 require_permission(who,'broadcast.manage')
 from app.broadcast import review_target
 try: return review_target(who['tenant_id'],identifier,**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

class ModerationConfig(BaseModel):
 robot: UUID
 binding: UUID
 destination: UUID

@app.post('/api/moderation/configs')
def moderation_config(body:ModerationConfig,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.moderation import create_config
 try: return create_config(who['tenant_id'],**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

class ModerationRule(BaseModel):
 code: str
 name: str
 kind: str
 match_config: dict
 action: str = 'review'
 severity: str = 'medium'
 priority: int = 100

@app.post('/api/moderation/configs/{identifier}/rules')
def moderation_rule(identifier:UUID,body:ModerationRule,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.moderation import add_rule
 try: return add_rule(who['tenant_id'],identifier,**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.get('/api/moderation/reviews')
def moderation_reviews(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db: return db.execute('SELECT id,group_config_id,review_type,status,created_at FROM tgmate.moderation_review_queue ORDER BY created_at DESC LIMIT 100').fetchall()

class ModerationReview(BaseModel):
 decision: str
 note: str

@app.post('/api/moderation/reviews/{identifier}/resolve')
def moderation_review(identifier:UUID,body:ModerationReview,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.moderation import review
 try: return review(who['tenant_id'],identifier,who.get('user_id'),body.decision,body.note)
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.post('/webhooks/moderation/{config_id}')
async def moderation_webhook(config_id:UUID,request:Request,x_telegram_bot_api_secret_token:str=Header(default='')):
 if settings().get('mode')!='sandbox': raise HTTPException(404,'入口已停用，请使用统一Bot入口')
 entry=settings().get('moderation_webhooks',{}).get(str(config_id))
 if not entry or not secrets.compare_digest(x_telegram_bot_api_secret_token,entry['secret']): raise HTTPException(403,'入口认证失败')
 raw=b''
 async for chunk in request.stream():
  raw+=chunk
  if len(raw)>1024*1024: raise HTTPException(413,'消息过大')
 try:
  import json
  from app.moderation import receive
  update=json.loads(raw)
  if isinstance(update,dict) and 'callback_query' in update:
   from app.verification import callback
   return callback(entry['tenant_id'],config_id,update)
  return receive(entry['tenant_id'],config_id,update)
 except (Rejected,ValueError,TypeError) as e: raise HTTPException(422,str(e) if isinstance(e,Rejected) else '消息格式错误') from None

class ModerationResolution(BaseModel):
 decision: str
 evidence: str

@app.post('/api/moderation/actions/{identifier}/resolve')
def moderation_action_resolution(identifier:UUID,body:ModerationResolution,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.moderation import resolve_action
 try: return resolve_action(who['tenant_id'],identifier,who.get('user_id'),body.decision,body.evidence)
 except Rejected as e: raise HTTPException(422,str(e)) from None

class VerificationReview(BaseModel):
 decision: str
 evidence: str

@app.post('/api/moderation/verification/{identifier}/review')
def verification_review(identifier:UUID,body:VerificationReview,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.verification import review_session
 try: return review_session(who['tenant_id'],identifier,who.get('user_id'),body.decision,body.evidence)
 except Rejected as e: raise HTTPException(422,str(e)) from None

class NoticeResolution(BaseModel):
 decision: str
 evidence: str
 remote_message_id: int | None = None

@app.post('/api/moderation/notices/{identifier}/resolve')
def notice_resolution(identifier:UUID,body:NoticeResolution,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.verification import resolve_notice
 try: return resolve_notice(who['tenant_id'],identifier,who.get('user_id'),**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.post('/webhooks/bots/{bot_id}')
async def bot_gateway(bot_id:UUID,request:Request,x_telegram_bot_api_secret_token:str=Header(default='')):
 from app.bot_webhooks import ingress
 from fastapi.concurrency import run_in_threadpool
 entry=await run_in_threadpool(ingress,bot_id,x_telegram_bot_api_secret_token)
 if not entry and settings().get('mode')=='sandbox':
  legacy=settings().get('bot_webhooks',{}).get(str(bot_id))
  if legacy and secrets.compare_digest(x_telegram_bot_api_secret_token,legacy['secret']): entry=legacy
 if not entry: raise HTTPException(403,'入口认证失败')
 raw=b''
 async for chunk in request.stream():
  raw+=chunk
  if len(raw)>1024*1024: raise HTTPException(413,'消息过大')
 try:
  import json
  from fastapi.concurrency import run_in_threadpool
  from app.bot_gateway import dispatch
  return await run_in_threadpool(dispatch,entry['tenant_id'],bot_id,json.loads(raw))
 except DeferredIngress: raise HTTPException(503,'消息映射仍在确认，请稍后重试') from None
 except (Rejected,ValueError,TypeError) as e: raise HTTPException(422,str(e) if isinstance(e,Rejected) else '事件格式错误') from None

class AppealReview(BaseModel):
 decision: str
 note: str

@app.post('/api/moderation/appeals/{identifier}/review')
def appeal_review(identifier:UUID,body:AppealReview,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.appeals import review
 try: return review(who['tenant_id'],identifier,who.get('user_id'),body.decision,body.note)
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.get('/api/moderation/appeals')
def appeals_list(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db: return db.execute('SELECT id,group_config_id,action_id,status,submitted_at,reviewed_at FROM tgmate.moderation_appeals ORDER BY submitted_at DESC LIMIT 100').fetchall()

@app.get('/api/moderation/appeals/{identifier}')
def appeal_detail(identifier:UUID,who=Depends(authorized)):
 require_permission(who,'moderation.manage')
 from app.db import unseal
 with transaction(who['tenant_id']) as db:
  row=db.execute('SELECT id,action_id,appeal_text,status,reviewer_note,submitted_at FROM tgmate.moderation_appeals WHERE id=%s',(identifier,)).fetchone()
  if not row: raise HTTPException(404,'未找到申诉')
  ref=row.pop('appeal_text'); row['reason']=None; row['reason_expired']=False
  if ref and ref.startswith('vault:'):
   stored=db.execute('SELECT ciphertext FROM tgmate.relay_payloads WHERE id=%s AND expires_at>now()',(ref[6:],)).fetchone()
   if stored: row['reason']=unseal(stored['ciphertext'])['reason']
   else: row['reason_expired']=True
  else: row['reason']=ref
  return row

@app.post('/api/private-replies/{identifier}/resolve')
def private_reply_resolution(identifier:UUID,body:NoticeResolution,who=Depends(authorized)):
 with transaction(who['tenant_id']) as db:
  resource=db.execute("SELECT payload->>'kind' AS kind FROM tgmate.outbox_events WHERE id=%s AND event_type='bot.private_reply'",(identifier,)).fetchone()
 if not resource: raise HTTPException(404,'未找到私聊回复')
 require_permission(who,'relay.manage' if (resource['kind'] or '').startswith('relay_') else 'moderation.manage')
 from app.private_replies import review_reply
 try: return review_reply(who['tenant_id'],identifier,who.get('user_id'),**body.model_dump())
 except ValueError as e: raise HTTPException(422,str(e)) from None

# Login bodies do not echo submitted passwords through validation errors.
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse,Response
from pydantic import Field

@app.exception_handler(RequestValidationError)
async def invalid_input(request,exc):
 return JSONResponse(status_code=422,content={'detail':'输入格式有误','fields':['.'.join(map(str,e['loc'])) for e in exc.errors()]})

class WebLogin(BaseModel):
 email: str
 workspace: str
 password: str = Field(repr=False)

@app.post('/auth/login')
def web_login(body:WebLogin,request:Request,response:Response):
 from app.auth import login,context,LoginRejected,LoginLimited
 from app.auth_http import origin_allowed,SESSION_COOKIE,CSRF_COOKIE
 origin_allowed(request)
 try: result=login(body.email,body.workspace,body.password,request.client.host if request.client else 'unknown')
 except LoginLimited as e: raise HTTPException(429,str(e)) from None
 except LoginRejected as e: raise HTTPException(401,str(e)) from None
 except RuntimeError: raise HTTPException(503,'登录模块尚未初始化') from None
 who=context(result['token'])
 if not who:
  from app.auth import logout
  logout(result['token'])
  raise HTTPException(401,'账号状态已变更，请重新登录')
 secure=settings().get('mode')!='sandbox'
 response.set_cookie(SESSION_COOKIE,result['token'],max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
 response.set_cookie(CSRF_COOKIE,result['csrf'],max_age=43200,httponly=False,secure=secure,samesite='strict',path='/')
 return {'user_id':who['user_id'],'tenant_id':who['tenant_id'],'role':who['role'],'permissions':who['permissions'],'expires_at':result['expires_at']}

@app.get('/auth/me')
def web_me(who=Depends(authorized)):
 return {key:who.get(key) for key in ['user_id','tenant_id','email','role','permissions']}

@app.post('/auth/logout')
def web_logout(request:Request,response:Response):
 from app.auth import context,logout
 from app.auth_http import origin_allowed,check_csrf,SESSION_COOKIE,CSRF_COOKIE
 origin_allowed(request)
 token=request.cookies.get(SESSION_COOKIE)
 if token:
  who=context(token)
  if who: check_csrf(request,who)
  logout(token)
 response.delete_cookie(SESSION_COOKIE,path='/'); response.delete_cookie(CSRF_COOKIE,path='/')
 return {'status':'logged_out'}

class PasswordChange(BaseModel):
 old_password: str = Field(repr=False)
 new_password: str = Field(repr=False)

@app.post('/auth/password')
def web_password(body:PasswordChange,request:Request,response:Response,who=Depends(authorized)):
 from app.auth import change_password,LoginRejected,LoginLimited
 from app.auth_http import SESSION_COOKIE,CSRF_COOKIE
 try: change_password(request.cookies.get(SESSION_COOKIE),body.old_password,body.new_password,request.client.host if request.client else 'unknown')
 except LoginLimited as e: raise HTTPException(429,str(e)) from None
 except LoginRejected as e: raise HTTPException(401,str(e)) from None
 response.delete_cookie(SESSION_COOKIE,path='/'); response.delete_cookie(CSRF_COOKIE,path='/')
 return {'status':'password_changed','login_required':True}

class BotRegistration(BaseModel):
 token: str = Field(repr=False, min_length=24, max_length=224)

@app.post('/api/bots/register')
def register_bot(body:BotRegistration,who=Depends(authorized)):
 from app.bot_credentials import register,RegistrationRejected
 require_permission(who,'credentials.manage')
 try: return register(who['tenant_id'],who['user_id'],body.token)
 except RegistrationRejected as e: raise HTTPException(422,str(e)) from None


@app.post('/api/bots/{bot_id}/credentials')
def migrate_bot_credentials(bot_id:UUID,body:BotRegistration,who=Depends(authorized)):
 from app.bot_credentials import register,RegistrationRejected
 require_permission(who,'credentials.manage')
 try: return register(who['tenant_id'],who['user_id'],body.token,existing_bot_id=bot_id)
 except RegistrationRejected as e: raise HTTPException(422,str(e)) from None


@app.post('/api/bots/{bot_id}/webhook/prepare')
def prepare_bot_webhook(bot_id:UUID,who=Depends(authorized)):
 from app.bot_webhooks import prepare,WebhookRejected
 try: return prepare(who['tenant_id'],who['user_id'],bot_id)
 except WebhookRejected as e: raise HTTPException(422,str(e)) from None

class WebhookApply(BaseModel):
 allow_replace: bool = False
 retry_uncertain: bool = False

@app.post('/api/bots/{bot_id}/webhook/apply')
def apply_bot_webhook(bot_id:UUID,body:WebhookApply,who=Depends(authorized)):
 from app.bot_webhooks import apply,WebhookRejected
 try: return apply(who['tenant_id'],who['user_id'],bot_id,allow_replace=body.allow_replace,retry_uncertain=body.retry_uncertain)
 except WebhookRejected as e: raise HTTPException(422,str(e)) from None


@app.post('/api/bots/{bot_id}/webhook/inspect')
def inspect_bot_webhook(bot_id:UUID,who=Depends(authorized)):
 from app.bot_webhooks import reconcile,WebhookRejected
 try: return reconcile(who['tenant_id'],bot_id)
 except WebhookRejected as e: raise HTTPException(422,str(e)) from None


class ModerationState(BaseModel):
 enabled: bool
 sandbox: bool = False

@app.post('/api/moderation/configs/{identifier}/state')
def moderation_state(identifier:UUID,body:ModerationState,who=Depends(authorized)):
 from app.moderation_control import set_enabled
 try: return set_enabled(who['tenant_id'],identifier,who['user_id'],**body.model_dump())
 except Rejected as e: raise HTTPException(422,str(e)) from None

@app.get('/api/moderation/configs')
def moderation_config_list(who=Depends(authorized)):
 with transaction(who['tenant_id']) as db:
  return db.execute('SELECT id,destination_id,enabled,cleanup_mode,welcome_enabled,verification_enabled,updated_at FROM tgmate.moderation_group_configs ORDER BY created_at DESC LIMIT 200').fetchall()


@app.post('/api/relay/configs/{identifier}/operator/challenge')
def relay_operator_challenge(identifier:UUID,who=Depends(authorized)):
 from app.relay_operator import challenge
 try: return challenge(who['tenant_id'],identifier,who['user_id'])
 except Rejected as e: raise HTTPException(422,str(e)) from None


@app.get('/api/relay/conversations/{identifier}/privacy')
def relay_privacy_status(identifier:UUID,who=Depends(authorized)):
 with transaction(who['tenant_id']) as db:
  rows=db.execute("SELECT n.version,n.outbox_id,n.confirmed_at,n.remote_message_id,t.status AS notice_status FROM tgmate.relay_privacy_notices n LEFT JOIN tgmate.outbox_events o ON o.tenant_id=n.tenant_id AND o.id=n.outbox_id LEFT JOIN tgmate.task_registry t ON t.tenant_id=o.tenant_id AND t.id=(o.payload->>'task_id')::uuid WHERE n.conversation_id=%s",(identifier,)).fetchall()
  return rows

@app.post('/api/relay/conversations/{identifier}/privacy/review')
def relay_privacy_review(identifier:UUID,body:NoticeResolution,who=Depends(authorized)):
 from app.relay_privacy import VERSION
 from app.private_replies import review_reply
 with transaction(who['tenant_id']) as db:
  row=db.execute('SELECT outbox_id FROM tgmate.relay_privacy_notices WHERE conversation_id=%s AND version=%s FOR UPDATE',(identifier,VERSION)).fetchone()
  if not row or not row['outbox_id']: raise HTTPException(404,'未找到待处理告知')
  try: return review_reply(who['tenant_id'],row['outbox_id'],who['user_id'],**body.model_dump())
  except (Rejected,ValueError) as e: raise HTTPException(422,str(e)) from None

class PrivacyRetry(BaseModel):
 evidence: str

@app.post('/api/relay/conversations/{identifier}/privacy/retry')
def relay_privacy_retry(identifier:UUID,body:PrivacyRetry,who=Depends(authorized)):
 from app.relay_privacy import retry
 try: return retry(who['tenant_id'],identifier,who['user_id'],body.evidence)
 except Rejected as e: raise HTTPException(422,str(e)) from None
