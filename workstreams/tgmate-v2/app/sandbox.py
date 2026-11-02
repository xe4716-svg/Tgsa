"""Explicit sandbox adapter; all results are simulated and no network is used."""
import uuid
from app.db import transaction
class SandboxModeration:
 def member_role(self,config,user): return 'member'
 def permissions(self,config): return dict(membership_status='member',can_send_messages=True,can_delete_messages=True,can_restrict_members=True,can_ban_members=True)
 def execute(self,config,action,message):
  result={'ok':True,'sandbox':True}
  if action['action_type']=='restrict': result.update(applied_until_date=action.get('until_date',0),restriction_fingerprint='sandbox-owned')
  return result
 def send_notice(self,config,text,markup=None): return uuid.uuid4().int%2_000_000_000+1

 def answer_callback(self,config,query_id,text): return True

 def send_private(self,bot,user,text,markup=None): return uuid.uuid4().int%2_000_000_000+1
