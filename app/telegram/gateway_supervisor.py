"""Bounded account routing for an explicit tenant allowlist.

No tenant discovery privilege, new business queue or automatic failure reconnect.
Each connection remains owned by the existing database lease/fencing mechanism.
"""
import asyncio
import time
from dataclasses import dataclass

from app.telegram.gateway_runtime import GatewayCloseUncertain


@dataclass
class RunningAccount:
    route: object
    stop: asyncio.Event
    task: asyncio.Task


class GatewaySupervisor:
    def __init__(self, routes, runner_factory, tenants, *, capacity=20, per_tenant=5,
                 poll_interval=5, retry_interval=30, clock=time.monotonic):
        self.tenants=tuple(dict.fromkeys(tenants))
        if not self.tenants or len(self.tenants)>100:
            raise ValueError('An explicit allowlist of 1 to 100 tenants is required')
        if (type(capacity) is not int or not 1<=capacity<=1000 or type(per_tenant) is not int
                or not 1<=per_tenant<=capacity or poll_interval<=0 or retry_interval<poll_interval):
            raise ValueError('Invalid gateway capacity or polling limits')
        self.routes,self.runner_factory=routes,runner_factory
        self.capacity,self.per_tenant=capacity,per_tenant
        self.poll_interval,self.retry_interval=poll_interval,retry_interval
        self.clock=clock
        self.running={}
        self.retry_at={}
        self.cursor=0

    async def step(self):
        # Complete the whole read before starting new clients. Database failure
        # propagates to serve(), whose finally stops every current lifecycle.
        desired={}
        for tenant in self.tenants:
            desired[tenant]=await self.routes.enabled(tenant)
        snapshot={(r.tenant_id,r.account_id):r for rows in desired.values() for r in rows}
        for key, entry in list(self.running.items()):
            wanted=snapshot.get(key)
            if wanted!=entry.route:
                entry.stop.set()
            if not entry.task.done():
                continue
            del self.running[key]
            try:
                result=entry.task.result()
            except GatewayCloseUncertain as exc:
                try:
                    await self.routes.pause(entry.route,'DISCONNECT_UNCERTAIN')
                except Exception:
                    pass
                # Stop the process: an unconfirmed old socket must be inspected.
                raise exc
            except asyncio.CancelledError:
                await self.routes.pause(entry.route,'GATEWAY_FAILED')
                raise RuntimeError('Account lifecycle was unexpectedly cancelled') from None
            except Exception:
                await self.routes.pause(entry.route,'GATEWAY_FAILED')
                raise RuntimeError('Account lifecycle failed') from None
            if result.reason=='ACCOUNT_UNAVAILABLE':
                # Another owner or a persisted wait: bounded DB polling only.
                self.retry_at[key]=self.clock()+self.retry_interval
            elif not (result.reason=='STOPPED' and entry.stop.is_set()):
                await self.routes.pause(entry.route,
                    'UNEXPECTED_STOP' if result.reason=='STOPPED' else result.reason)
                snapshot.pop(key,None)

        # Round-robin tenants, then accounts, rather than filling capacity from
        # the first tenant. Draining lifecycles still consume a capacity slot.
        ordered=self.tenants[self.cursor:]+self.tenants[:self.cursor]
        self.cursor=(self.cursor+1)%len(self.tenants)
        candidates={t:[r for r in desired[t] if (r.tenant_id,r.account_id) in snapshot
                         and (r.tenant_id,r.account_id) not in self.running
                         and self.retry_at.get((r.tenant_id,r.account_id),0)<=self.clock()]
                    for t in ordered}
        while len(self.running)<self.capacity:
            started=False
            for tenant in ordered:
                if len(self.running)>=self.capacity:
                    break
                count=sum(key[0]==tenant for key in self.running)
                if count>=self.per_tenant or not candidates[tenant]:
                    continue
                route=candidates[tenant].pop(0)
                stop=asyncio.Event()
                runner=self.runner_factory()
                task=asyncio.create_task(runner.run(
                    route.tenant_id, route.account_id, stop,
                    route_generation=route.generation,
                    traceparent=route.traceparent,
                ))
                self.running[(route.tenant_id,route.account_id)]=RunningAccount(route,stop,task)
                started=True
            if not started:
                break
        # Prevent unbounded history growth as accounts/routes are removed.
        self.retry_at={key:until for key,until in self.retry_at.items() if key in snapshot and until>self.clock()}

    async def close(self):
        entries=list(self.running.values())
        for entry in entries:
            entry.stop.set()
        results=await asyncio.gather(*(entry.task for entry in entries),return_exceptions=True)
        self.running.clear()
        uncertain=None;failed=False
        for entry,result in zip(entries,results):
            if isinstance(result,GatewayCloseUncertain):
                uncertain=result;reason='DISCONNECT_UNCERTAIN'
            elif isinstance(result,BaseException):
                failed=True;reason='GATEWAY_FAILED'
            elif result.reason not in ('STOPPED','ACCOUNT_UNAVAILABLE'):
                reason=result.reason
            else:
                continue
            # Attempt every pause even if one write fails. A storage error may
            # not hide the fatal "old socket could still be open" classification.
            try:
                await self.routes.pause(entry.route,reason)
            except Exception:
                failed=True
        if uncertain is not None:raise uncertain
        if failed:raise RuntimeError('Account cleanup failed') from None

    async def serve(self, stop):
        try:
            while not stop.is_set():
                await self.step()
                try:
                    await asyncio.wait_for(stop.wait(),self.poll_interval)
                except TimeoutError:
                    pass
        finally:
            await self.close()
