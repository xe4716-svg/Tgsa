"""Run one sandbox queue step; no Telegram network calls."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import settings
from app.worker import run_once
c=settings()
if c.get('mode')!='sandbox': raise SystemExit('This runner only supports sandbox mode')
from app.private_worker import run_once as private_once
from app.sandbox import SandboxModeration
for tenant in sorted({x['tenant_id'] for x in c['api_keys'].values()}):
 print({'mode':'sandbox','private_reply':private_once(tenant,c['worker_id'],SandboxModeration()),'relay':run_once(tenant,c['worker_id'])})
