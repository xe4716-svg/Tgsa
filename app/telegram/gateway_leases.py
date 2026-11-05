"""RLS-scoped gateway ownership, reusing account_gateway_leases.

A durable lease fences database state; it does not cancel an in-flight Telegram
request. The future connection runner must disconnect when renewal fails and
reconcile unknown sends before retry. No network call is made by this module.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountLease:
    tenant_id: UUID
    account_id: UUID
    worker_id: str
    fencing_token: int


class GatewayLeases:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def _transaction(self, tenant_id, account_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)",str(tenant_id))
                tenant = await conn.fetchrow('SELECT status FROM tenants WHERE id=$1 FOR SHARE',tenant_id)
                row = await conn.fetchrow('SELECT status FROM telegram_accounts WHERE tenant_id=$1 AND id=$2 FOR UPDATE',tenant_id,account_id)
                yield conn, bool(tenant and tenant['status']=='ACTIVE' and row and row['status'] in ('AUTHORIZED','ACTIVE'))

    @staticmethod
    def _ttl(ttl):
        if type(ttl) is not int or not 5<=ttl<=300:
            raise ValueError('Lease duration must be between 5 and 300 seconds')

    async def acquire(self, tenant_id, account_id, worker_id, ttl=30):
        self._ttl(ttl)
        if not isinstance(worker_id,str) or not 1<=len(worker_id)<=150:
            raise ValueError('Invalid worker identifier')
        async with self._transaction(tenant_id, account_id) as (conn, allowed):
            if not allowed:
                return None
            row = await conn.fetchrow('''
                INSERT INTO account_gateway_leases(account_id,tenant_id,worker_id,fencing_token,lease_expires_at)
                VALUES($1,$2,$3,1,clock_timestamp()+$4*interval '1 second')
                ON CONFLICT(account_id) DO UPDATE SET worker_id=EXCLUDED.worker_id,
                  fencing_token=account_gateway_leases.fencing_token+1,
                  lease_expires_at=clock_timestamp()+$4*interval '1 second',updated_at=clock_timestamp()
                WHERE account_gateway_leases.tenant_id=EXCLUDED.tenant_id
                  AND account_gateway_leases.lease_expires_at<=clock_timestamp()
                RETURNING fencing_token''',account_id,tenant_id,worker_id,ttl)
            return AccountLease(tenant_id,account_id,worker_id,row['fencing_token']) if row else None

    async def renew(self, lease, ttl=30):
        self._ttl(ttl)
        async with self._transaction(lease.tenant_id,lease.account_id) as (conn,allowed):
            if not allowed:
                return False
            return bool(await conn.fetchval('''
                UPDATE account_gateway_leases SET lease_expires_at=clock_timestamp()+$5*interval '1 second',updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND account_id=$2 AND worker_id=$3 AND fencing_token=$4
                  AND lease_expires_at>clock_timestamp() RETURNING true''',
                lease.tenant_id,lease.account_id,lease.worker_id,lease.fencing_token,ttl))

    async def release(self, lease):
        # Preserve the row: deleting it would reset the fencing counter to one.
        async with self._transaction(lease.tenant_id,lease.account_id) as (conn,_):
            return bool(await conn.fetchval('''
                UPDATE account_gateway_leases SET lease_expires_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND account_id=$2 AND worker_id=$3 AND fencing_token=$4
                  AND lease_expires_at>clock_timestamp() RETURNING true''',
                lease.tenant_id,lease.account_id,lease.worker_id,lease.fencing_token))

    async def defer(self,lease,seconds):
        """Persist a server wait in the existing ownership deadline."""
        if type(seconds) is not int or not 1<=seconds<=2147483647:
            raise ValueError('Invalid server wait')
        async with self._transaction(lease.tenant_id,lease.account_id) as (conn,allowed):
            if not allowed:return None
            row=await conn.fetchrow('''UPDATE account_gateway_leases
                SET fencing_token=fencing_token+1,
                    lease_expires_at=GREATEST(lease_expires_at,clock_timestamp()+$5*interval '1 second'),updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND account_id=$2 AND worker_id=$3 AND fencing_token=$4
                  AND lease_expires_at>clock_timestamp() RETURNING fencing_token''',
                lease.tenant_id,lease.account_id,lease.worker_id,lease.fencing_token,seconds)
            if not row:
                return None
            await conn.execute("INSERT INTO account_security_events(tenant_id,account_id,event_type,severity,details) VALUES($1,$2,'MTPROTO_FLOOD_WAIT','WARNING',jsonb_build_object('retry_after',$3::integer))",lease.tenant_id,lease.account_id,seconds)
            await conn.execute("INSERT INTO audit_logs(tenant_id,action,resource_type,resource_id,details) VALUES($1,'GATEWAY_SERVER_WAIT','telegram_account',$2,jsonb_build_object('retry_after',$3::integer))",lease.tenant_id,lease.account_id,seconds)
            return AccountLease(lease.tenant_id,lease.account_id,lease.worker_id,row['fencing_token'])

    @asynccontextmanager
    async def fenced_transaction(self, lease):
        async with self._transaction(lease.tenant_id,lease.account_id) as (conn,allowed):
            row = await conn.fetchrow('''SELECT account_id FROM account_gateway_leases
                WHERE tenant_id=$1 AND account_id=$2 AND worker_id=$3 AND fencing_token=$4
                  AND lease_expires_at>clock_timestamp() FOR UPDATE''',
                lease.tenant_id,lease.account_id,lease.worker_id,lease.fencing_token)
            if not allowed or not row:
                raise LeaseLost('Account gateway ownership lost')
            yield conn
            # Clock time may advance during the caller's database work. Refuse
            # commit if the lease elapsed, even though takeover was row-locked.
            live = await conn.fetchval('''SELECT lease_expires_at>clock_timestamp() FROM account_gateway_leases
                 WHERE tenant_id=$1 AND account_id=$2 AND worker_id=$3 AND fencing_token=$4''',
                 lease.tenant_id,lease.account_id,lease.worker_id,lease.fencing_token)
            if not live:
                raise LeaseLost('Account gateway ownership expired before commit')
