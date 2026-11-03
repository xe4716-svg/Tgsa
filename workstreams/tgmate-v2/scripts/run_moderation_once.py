"""One sandbox moderation cycle: expiry cleanup, rules/actions, and notices; no network."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import settings
from app.moderation_worker import run_once
from app.verification_worker import run_once as notice_once
from app.verification import expire_sessions
from app.verification_ack import run_once as ack_once
from app.private_worker import run_once as private_once
from app.sandbox import SandboxModeration
c=settings()
if c.get('mode')!='sandbox': raise SystemExit('Sandbox only')
for identity in c['api_keys'].values():
 tenant=identity['tenant_id']; adapter=SandboxModeration()
 print({'mode':'sandbox','expired':expire_sessions(tenant),'action':run_once(tenant,c['worker_id'],adapter),'notice':notice_once(tenant,c['worker_id'],adapter),'callback_ack':ack_once(tenant,c['worker_id'],adapter),'private_reply':private_once(tenant,c['worker_id'],adapter)})
