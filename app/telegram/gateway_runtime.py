"""One explicitly selected account, read-only MTProto connection lifecycle.

No publish/send API, automatic login, proxy switching or automatic retry exists
here. Database ownership does not cancel an already issued Telegram request.
"""
import asyncio
import time
from dataclasses import dataclass

from telethon.errors import FloodWaitError

from app.telegram.client_factory import create_client
from app.telegram.gateway_leases import GatewayLeases, LeaseLost
from app.telegram.gateway_material import load_material


class GatewayCloseUncertain(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayResult:
    reason: str
    retry_after: int = 0


class GatewayRunner:
    def __init__(self,pool,vault,worker_id,*,ttl=30,heartbeat=5,control_timeout=3,rpc_timeout=10,close_timeout=5,
                 leases=None,loader=None,client_factory=None):
        GatewayLeases._ttl(ttl)
        if not (0<heartbeat<ttl/3 and 0<control_timeout<ttl/3 and 0<rpc_timeout<ttl/2 and close_timeout>0):
            raise ValueError('Gateway timeouts must leave a safe lease margin')
        self.leases=leases if leases is not None else GatewayLeases(pool)
        self.loader=loader if loader is not None else load_material
        self.client_factory=client_factory if client_factory is not None else create_client
        self.vault,self.worker_id=vault,worker_id
        self.ttl,self.heartbeat,self.control_timeout=ttl,heartbeat,control_timeout
        self.rpc_timeout,self.close_timeout=rpc_timeout,close_timeout
        self._running=False

    async def run(self,tenant_id,account_id,stop,*,ready=None):
        if self._running:
            raise RuntimeError('Runner already owns an account lifecycle')
        self._running=True
        lease=None;client=None;monitor=None
        lost=asyncio.Event();deadline=0;failure='OWNERSHIP_LOST';material=None
        release=True

        async def controlled(operation,timeout):
            if stop.is_set():raise asyncio.CancelledError()
            if lost.is_set() or time.monotonic()>=deadline:raise LeaseLost('Lease unavailable')
            action=asyncio.create_task(operation())
            stopped=asyncio.create_task(stop.wait())
            invalid=asyncio.create_task(lost.wait())
            tasks=(action,stopped,invalid)
            try:
                done,_=await asyncio.wait(tasks,timeout=min(timeout,max(0,deadline-time.monotonic())),return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:raise asyncio.CancelledError()
                if invalid in done or time.monotonic()>=deadline:raise LeaseLost('Lease unavailable')
                if action not in done:raise TimeoutError()
                return action.result()
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)

        async def read_material(decrypt):
            async with self.leases.fenced_transaction(lease) as conn:
                return await self.loader(conn,lease,self.vault,decrypt=decrypt)

        async def heartbeat():
            nonlocal deadline,failure
            try:
                while not stop.is_set():
                    await asyncio.sleep(self.heartbeat)
                    started=time.monotonic()
                    if started>=deadline:
                        raise LeaseLost('Lease elapsed')
                    renewed=await asyncio.wait_for(self.leases.renew(lease,self.ttl),self.control_timeout)
                    if not renewed:raise LeaseLost('Renewal refused')
                    deadline=started+self.ttl
                    if material is not None:
                        try:
                            revision=await asyncio.wait_for(read_material(False),self.control_timeout)
                        except Exception:
                            failure='MATERIAL_UNAVAILABLE'
                            raise
                        if revision!=material.revision:
                            failure='MATERIAL_CHANGED'
                            raise LeaseLost('Material changed')
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.set()

        try:
            if stop.is_set():return GatewayResult('STOPPED')
            started=time.monotonic()
            lease=await asyncio.wait_for(self.leases.acquire(tenant_id,account_id,self.worker_id,self.ttl),self.control_timeout)
            if lease is None:return GatewayResult('ACCOUNT_UNAVAILABLE')
            deadline=started+self.ttl
            monitor=asyncio.create_task(heartbeat())
            material=await controlled(lambda:read_material(True),self.control_timeout)
            client=self.client_factory(material.session_string,connection_mode=material.connection_mode,
                                       proxy_profile=material.proxy_profile,managed=True)
            await controlled(client.connect,self.rpc_timeout)
            if not await controlled(client.is_user_authorized,self.rpc_timeout):
                return GatewayResult('AUTHORIZATION_INVALID')
            user=await controlled(client.get_me,self.rpc_timeout)
            if user is None or getattr(user,'id',None)!=material.telegram_user_id:
                return GatewayResult('IDENTITY_MISMATCH')
            if ready is not None:ready.set()
            stopped=asyncio.create_task(stop.wait())
            invalid=asyncio.create_task(lost.wait())
            # Telethon's disconnected future is owned by Telethon: never cancel
            # the underlying future when this runner stops monitoring it.
            disconnected=asyncio.ensure_future(asyncio.shield(client.disconnected))
            try:
                done,_=await asyncio.wait((stopped,invalid,disconnected),return_when=asyncio.FIRST_COMPLETED)
                if invalid in done:return GatewayResult(failure)
                if stopped in done:return GatewayResult('STOPPED')
                return GatewayResult('CONNECTION_CLOSED')
            finally:
                for task in (stopped,invalid,disconnected):
                    if not task.done():task.cancel()
                await asyncio.gather(stopped,invalid,disconnected,return_exceptions=True)
        except asyncio.CancelledError:
            if stop.is_set():return GatewayResult('STOPPED')
            raise
        except FloodWaitError as exc:
            # Keep ownership unavailable through the server-mandated wait. Never
            # release this deadline in finally or automatically reconnect.
            release=False
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor,return_exceptions=True)
                monitor=None
            if lease is not None:
                try:
                    held=await asyncio.wait_for(self.leases.defer(lease,max(1,exc.seconds)),self.control_timeout)
                    if held:return GatewayResult('FLOOD_WAIT',max(1,exc.seconds))
                except Exception:
                    pass
            return GatewayResult('WAIT_PERSIST_FAILED',max(1,exc.seconds))
        except LeaseLost:
            return GatewayResult(failure)
        except Exception:
            return GatewayResult('GATEWAY_FAILED')
        finally:
            lost.set()
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor,return_exceptions=True)
            closed=True
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(),self.close_timeout)
                except Exception:
                    closed=False
            # Release only after confirmed disconnect. Otherwise let ownership
            # expire and require operational inspection; do not retry here.
            if lease is not None and release and closed:
                try:await asyncio.wait_for(self.leases.release(lease),self.control_timeout)
                except Exception:pass
            if not closed:
                # If ownership is still current, quarantine before its deadline
                # expires. A stale worker cannot mutate a new owner's account.
                async def quarantine():
                    async with self.leases.fenced_transaction(lease) as conn:
                        await conn.execute("UPDATE telegram_accounts SET status='PAUSED',security_state='GATEWAY_CLOSE_UNCERTAIN' WHERE tenant_id=$1 AND id=$2",lease.tenant_id,lease.account_id)
                        await conn.execute("INSERT INTO audit_logs(tenant_id,action,resource_type,resource_id,details) VALUES($1,'GATEWAY_DISCONNECT_UNCERTAIN','telegram_account',$2,'{}'::jsonb)",lease.tenant_id,lease.account_id)
                        await conn.execute("INSERT INTO account_security_events(tenant_id,account_id,event_type,severity) VALUES($1,$2,'GATEWAY_CLOSE_UNCERTAIN','CRITICAL')",lease.tenant_id,lease.account_id)
                try:
                    await asyncio.wait_for(quarantine(),self.control_timeout)
                except Exception:
                    pass
                self._running=False
                raise GatewayCloseUncertain('Gateway disconnect could not be confirmed')
            self._running=False
