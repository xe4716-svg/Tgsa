"""Supervise explicitly routed accounts for an explicit tenant allowlist."""
import argparse
import asyncio
import json
import os
from uuid import UUID, uuid4

import asyncpg

from app.config import settings
from app.security.session_vault import SessionVault
from app.telemetry import configure_worker_tracing
from app.structured_logging import configure_structured_logging
from app.gateway_metrics import GatewayCommandMetrics, configure_worker_metrics
from app.telegram.gateway_routes import GatewayRoutes
from app.telegram.gateway_runtime import GatewayRunner, GatewayCloseUncertain
from app.telegram.gateway_supervisor import GatewaySupervisor
from app.workers.billing import install_signal_handlers


async def run(tenants, capacity=20, per_tenant=5):
    stop=asyncio.Event()
    restorers=[]
    tracing_provider=None
    metrics_provider=None
    try:
        configure_structured_logging()
        tracing_provider=configure_worker_tracing(
            service_name="tgmate-gateway-worker",
            environment=settings.app_env,
        )
        metrics_provider=configure_worker_metrics(
            service_name="tgmate-gateway-worker",
            environment=settings.app_env,
        )
        command_tracer=tracing_provider.get_tracer("tgmate.gateway.commands","1.0.0") if tracing_provider else None
        command_metrics=GatewayCommandMetrics(metrics_provider.get_meter("tgmate.gateway.commands","1.0.0")) if metrics_provider else None
        restorers=install_signal_handlers(stop)
        vault=SessionVault.from_base64(settings.session_kek_base64,settings.session_kek_id)
        async with asyncpg.create_pool(settings.database_url,min_size=1,max_size=10,command_timeout=3) as pool:
            supervisor=GatewaySupervisor(GatewayRoutes(pool),
                lambda:GatewayRunner(pool,vault,'gateway-'+str(uuid4()),command_tracer=command_tracer,command_metrics=command_metrics),tenants,
                capacity=capacity,per_tenant=per_tenant)
            await supervisor.serve(stop)
        print(json.dumps({'state':'stopped','reason':'STOPPED'}))
        return 0
    except GatewayCloseUncertain:
        print(json.dumps({'state':'stopped','reason':'DISCONNECT_UNCERTAIN'}))
        return 3
    except Exception:
        print(json.dumps({'state':'stopped','reason':'SUPERVISOR_FAILED'}))
        return 2
    finally:
        try:
            if tracing_provider is not None:
                tracing_provider.shutdown()
            if metrics_provider is not None:
                metrics_provider.shutdown()
        finally:
            for restore in reversed(restorers):
                restore()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tenant',type=UUID,action='append',
                        help='Assigned tenant UUID; repeat for multiple tenants')
    selection.add_argument('--from-env',action='store_true',help='Read explicit GATEWAY_TENANT_IDS allowlist')
    parser.add_argument('--capacity',type=int,default=20)
    parser.add_argument('--per-tenant',type=int,default=5)
    args=parser.parse_args(argv)
    if args.from_env:
        try:args.tenant=[UUID(value.strip()) for value in os.environ.get('GATEWAY_TENANT_IDS','').split(',') if value.strip()]
        except ValueError:parser.error('Invalid gateway tenant configuration')
    if not (1<=len(set(args.tenant))<=100 and 1<=args.per_tenant<=args.capacity<=1000):
        parser.error('Require 1-100 tenants and 1 <= per-tenant <= capacity <= 1000')
    return asyncio.run(run(args.tenant,args.capacity,args.per_tenant))


if __name__=='__main__':
    raise SystemExit(main())
