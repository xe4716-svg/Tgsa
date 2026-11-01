"""HTTP authentication, CSRF checks and module permissions."""
import hmac,hashlib
from urllib.parse import urlsplit
from fastapi import HTTPException,Request
from app.db import settings
from app.auth import context,digest

SESSION_COOKIE='tgmate_session'; CSRF_COOKIE='tgmate_csrf'
READ_PERMISSIONS=['relay.read','broadcast.read','moderation.read','analytics.read','tenant.read']


def origin_allowed(request):
 cfg=settings(); origin=request.headers.get('origin')
 expected=cfg.get('public_origin')
 if cfg.get('mode')!='sandbox':
  if not expected or not expected.startswith('https://') or request.url.scheme!='https': raise HTTPException(503,'登录服务需要配置HTTPS访问地址')
 else:
  expected=expected or str(request.base_url).rstrip('/')
 if origin and origin.rstrip('/')!=expected.rstrip('/'): raise HTTPException(403,'请求来源不匹配')


def require_permission(who,code):
 codes=who.get('permissions',[])
 if 'tenant.admin' not in codes and code not in codes: raise HTTPException(403,'当前角色没有此操作权限')


def permission_for(path,method):
 write=method not in ('GET','HEAD','OPTIONS')
 if path.startswith('/api/relay/'): return 'relay.manage' if write else 'relay.read'
 if path.startswith('/api/broadcast/'): return 'broadcast.manage' if write else 'broadcast.read'
 if path.startswith('/api/moderation/'): return 'moderation.manage' if write else 'moderation.read'
 if path.startswith('/api/private-replies/'): return None  # Handler checks the resource's actual module.
 if path.startswith('/api/bots/'): return 'credentials.manage'
 if path=='/api/overview': return 'tenant.read'
 if path.startswith('/api/'): return 'tenant.admin'
 return None


def check_csrf(request,who):
 cookie=request.cookies.get(CSRF_COOKIE,''); header=request.headers.get('x-csrf-token','')
 if not cookie or len(cookie)>256 or not hmac.compare_digest(cookie.encode(),header.encode()) or not hmac.compare_digest(digest(cookie),who['_csrf_hash']): raise HTTPException(403,'请求校验已失效，请重新登录')
 origin_allowed(request)


def authenticate(request,authorization=''):
 cfg=settings(); who=None
 if cfg.get('mode')=='sandbox' and authorization.startswith('Bearer '):
  candidate=authorization[7:]
  for key,identity in cfg.get('api_keys',{}).items():
   if hmac.compare_digest(key.encode(),candidate.encode()):
    who=dict(identity,permissions=identity.get('permissions',['tenant.admin'] if identity.get('role')=='admin' else READ_PERMISSIONS),_auth_method='bootstrap')
    break
 if who is None:
  token=request.cookies.get(SESSION_COOKIE)
  if not token: raise HTTPException(401,'请先登录')
  try: who=context(token)
  except RuntimeError: raise HTTPException(503,'登录模块尚未初始化') from None
  if not who: raise HTTPException(401,'登录已过期，请重新登录')
  if cfg.get('mode')!='sandbox': origin_allowed(request)
  if request.method not in ('GET','HEAD','OPTIONS'): check_csrf(request,who)
 code=permission_for(request.url.path,request.method)
 if code: require_permission(who,code)
 return who
