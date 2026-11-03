"""Explicit-account, read-only MTProto gateway process. No automatic restart."""
import argparse
import asyncio
import json
import signal
from uuid import UUID, uuid4

import asyncpg

from app.config import settings
from app.security.session_vault import SessionVault
from app.telegram.gateway_runtime import GatewayRunner, GatewayCloseUncertain


async def run(tenant_id,account_id):
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    installed=[]
    for name in ('SIGTERM','SIGINT'):
        sig=getattr(signal,name,None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig,stop.set)
                installed.append(sig)
            except (NotImplementedError,RuntimeError):
                pass
    try:
        vault=SessionVault.from_base64(settings.session_kek_base64,settings.session_kek_id)
        async with asyncpg.create_pool(settings.database_url,min_size=1,max_size=4,command_timeout=3) as pool:
            runner=GatewayRunner(pool,vault,'mtproto-'+str(uuid4()))
            result=await runner.run(tenant_id,account_id,stop)
        print(json.dumps({'state':'stopped','reason':result.reason,'retry_after':result.retry_after}))
        return 0 if result.reason=='STOPPED' else 2
    except GatewayCloseUncertain:
        print(json.dumps({'state':'stopped','reason':'DISCONNECT_UNCERTAIN'}))
        return 3
    except Exception:
        print(json.dumps({'state':'stopped','reason':'GATEWAY_FAILED'}))
        return 2
    finally:
        for sig in installed:loop.remove_signal_handler(sig)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant',type=UUID,required=True)
    parser.add_argument('--account',type=UUID,required=True)
    args=parser.parse_args(argv)
    return asyncio.run(run(args.tenant,args.account))


if __name__=='__main__':
    raise SystemExit(main())
