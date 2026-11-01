"""Atomic reservations; unknown external outcomes retain their reservation."""
class QuotaExceeded(ValueError): pass

def reserve(db,tenant,task,entitlement='relay.messages',key=None):
 account=db.execute('SELECT * FROM tgmate.quota_accounts WHERE entitlement_code=%s AND period_start<=now() AND period_end>now() ORDER BY period_start DESC LIMIT 1 FOR UPDATE',(entitlement,)).fetchone()
 if not account or (account['limit_value'] is not None and account['reserved_value']+account['consumed_value']>=account['limit_value']): raise QuotaExceeded('额度不足或未开通')
 db.execute('UPDATE tgmate.quota_accounts SET reserved_value=reserved_value+1 WHERE id=%s',(account['id'],))
 reservation=db.execute('INSERT INTO tgmate.quota_reservations(tenant_id,quota_account_id,task_id,entitlement_code,amount) VALUES(%s,%s,%s,%s,1) RETURNING id',(tenant,account['id'],task,entitlement)).fetchone()['id']
 db.execute("INSERT INTO tgmate.quota_ledger(tenant_id,quota_account_id,reservation_id,idempotency_key,event_type,reserved_delta) VALUES(%s,%s,%s,%s,'reserve',1)",(tenant,account['id'],reservation,f'reserve:{key or task}'))

 return reservation

def settle(db,tenant,task,success,reservation_id=None):
 reservations=db.execute("SELECT * FROM tgmate.quota_reservations WHERE task_id=%s AND status='reserved' AND (%s::uuid IS NULL OR id=%s) FOR UPDATE",(task,reservation_id,reservation_id)).fetchall()
 for r in reservations:
  db.execute('UPDATE tgmate.quota_accounts SET reserved_value=reserved_value-%s,consumed_value=consumed_value+%s WHERE id=%s',(r['amount'],r['amount'] if success else 0,r['quota_account_id']))
  db.execute('UPDATE tgmate.quota_reservations SET status=%s WHERE id=%s',('committed' if success else 'released',r['id']))
  db.execute('INSERT INTO tgmate.quota_ledger(tenant_id,quota_account_id,reservation_id,idempotency_key,event_type,reserved_delta,consumed_delta) VALUES(%s,%s,%s,%s,%s,%s,%s)',(tenant,r['quota_account_id'],r['id'],f'settle:{r["id"]}','commit' if success else 'release',-r['amount'],r['amount'] if success else 0))
