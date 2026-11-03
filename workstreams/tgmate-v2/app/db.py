"""Tenant-scoped database access and local encrypted payload vault."""
import json,os
from pathlib import Path
from uuid import UUID
from contextlib import contextmanager
from contextvars import ContextVar
import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
_current_transaction=ContextVar("tgmate_transaction",default=None)
_runtime_settings=ContextVar("tgmate_runtime_settings",default=None)

def settings():
 current=_runtime_settings.get()
 if current is not None: return current
 return json.loads(Path(os.environ.get('TGMATE_CONFIG',ROOT/'.local/app.json')).read_text())

@contextmanager
def use_settings(config):
 """Root worker injects its own private configuration per execution context."""
 token=_runtime_settings.set(config)
 try: yield
 finally: _runtime_settings.reset(token)

@contextmanager
def transaction(tenant):
 tenant=str(UUID(str(tenant)))
 existing=_current_transaction.get()
 if existing:
  if existing[0]!=str(tenant): raise ValueError('Nested tenant changes are forbidden')
  with existing[1].transaction(): yield existing[1]
  return
 c=settings()
 if c.get('allowed_tenants') is not None and tenant not in c['allowed_tenants']:
  raise PermissionError('Tenant is not assigned to this process')
 with psycopg.connect(**c['database'],row_factory=dict_row) as db:
  db.execute("SELECT set_config('app.tenant_id',%s,true)",(str(tenant),))
  if c.get('require_root_tenant_guard'):
   if not db.execute('SELECT public.lock_business_worker_tenant(%s) AS allowed',(tenant,)).fetchone()['allowed']:
    raise PermissionError('Business tenant is unavailable')
  token=_current_transaction.set((str(tenant),db))
  try: yield db
  finally: _current_transaction.reset(token)

def seal(payload): return Fernet(settings()['vault_key'].encode()).encrypt(json.dumps(payload).encode()).decode()
def unseal(ciphertext): return json.loads(Fernet(settings()['vault_key'].encode()).decrypt(ciphertext.encode()))
