"""Shared short-transaction lease primitives. Network requests must happen outside them."""
from datetime import timedelta

def acquire(db,tenant,task,worker,token):
 return bool(db.execute("INSERT INTO tgmate.task_leases(tenant_id,task_id,worker_id,lease_token,lease_until) VALUES(%s,%s,%s,%s,now()+interval '60 seconds') ON CONFLICT(tenant_id,task_id) DO UPDATE SET worker_id=EXCLUDED.worker_id,lease_token=EXCLUDED.lease_token,leased_at=now(),lease_until=EXCLUDED.lease_until WHERE tgmate.task_leases.lease_until<now() RETURNING lease_token",(tenant,task,worker,token)).fetchone())

def valid(db,task,token):
 row=db.execute('SELECT lease_token,lease_until>now() AS valid FROM tgmate.task_leases WHERE task_id=%s FOR UPDATE',(task,)).fetchone()
 return bool(row and row['lease_token']==token and row['valid'])

def release(db,task,token): db.execute('DELETE FROM tgmate.task_leases WHERE task_id=%s AND lease_token=%s',(task,token))
