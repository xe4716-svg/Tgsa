"""One sandbox queue step for both broadcast channels. Never contacts Telegram."""
import sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import settings
from app.broadcast_worker import run_once
class Sandbox:
 def preflight(self,module,executor,destination): return {'can_send_messages':True}
 def send(self,module,executor,destination,content): return uuid.uuid4().int%2_000_000_000+1
c=settings()
if c.get('mode')!='sandbox': raise SystemExit('Sandbox only')
for identity in c['api_keys'].values(): print(run_once(identity['tenant_id'],c['worker_id'],Sandbox()))
