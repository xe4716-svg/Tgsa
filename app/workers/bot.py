"""Root Bot worker: schedule the existing durable business queues fairly.

No account-broadcast adapter or standalone legacy HTTP/auth server is loaded.
"""
import argparse
import asyncio
import json
import os
from uuid import UUID

import httpx
import psycopg
from cryptography.fernet import Fernet

from app.business import db, worker, broadcast_worker, private_worker, moderation_worker
from app.business import verification_worker, verification_ack, verification
from app.business.telegram_adapters import BotBroadcast, ModerationBot
from app.telegram.bot_rate_limits import BotRateLimits
from app.workers.billing import install_signal_handlers


def runtime_settings(worker_id):
    url=os.environ.get('BOT_WORKER_DATABASE_URL')
    key=os.environ.get('BUSINESS_VAULT_KEY')
    if not url or not key:
        raise ValueError('Private worker database and existing business vault key are required')
    Fernet(key.encode())
    return {'database':{'conninfo':url,'connect_timeout':3,
                        'options':'-c statement_timeout=5000 -c lock_timeout=3000'},
            'vault_key':key,'worker_id':str(worker_id),'mode':'live',
            'require_root_tenant_guard':True}


def verify_runtime(config, worker_id):
    with psycopg.connect(**config['database']) as conn:
        flags=conn.execute('''SELECT current_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole
            FROM pg_roles WHERE rolname=current_user''').fetchone()
        if flags!=('tgs_bot_worker',False,False,False,False):
            raise ValueError('A dedicated restricted Bot worker role is required')
        if conn.execute('SELECT worker_type,status FROM tgmate.worker_nodes WHERE id=%s',(worker_id,)).fetchone()!=('bot','healthy'):
            raise ValueError('Worker is not registered and healthy')


def build_jobs(client, rate_limiter=None):
    moderation=ModerationBot(client,rate_limiter=rate_limiter)
    return (
        ('private_reply',lambda tenant,identifier:private_worker.run_once(tenant,identifier,moderation)),
        ('relay',lambda tenant,identifier:worker.run_once(tenant,identifier,worker.Telegram(client,rate_limiter=rate_limiter))),
        ('bot_broadcast',lambda tenant,identifier:broadcast_worker.run_once(tenant,identifier,BotBroadcast(client,rate_limiter=rate_limiter),business_module='bot_broadcast')),
        ('moderation',lambda tenant,identifier:moderation_worker.run_once(tenant,identifier,moderation)),
        ('verification',lambda tenant,identifier:verification_worker.run_once(tenant,identifier,moderation)),
        ('callback_ack',lambda tenant,identifier:verification_ack.run_once(tenant,identifier,moderation)),
        ('verification_expiry',lambda tenant,identifier:{'status':'checked','count':verification.expire_sessions(tenant)}),
    )


async def serve(tenants, worker_id, stop, *, config, jobs, poll_interval=2):
    tenants=tuple(dict.fromkeys(tenants))
    if not tenants or len(tenants)>100 or poll_interval<=0:
        raise ValueError('An explicit tenant allowlist and positive polling interval are required')
    cursor=0
    config['allowed_tenants']=frozenset(str(t) for t in tenants)
    with db.use_settings(config):
        while not stop.is_set():
            ordered=tenants[cursor:]+tenants[:cursor]
            cursor=(cursor+1)%len(tenants)
            for name,job in jobs:
                for tenant in ordered:
                    if stop.is_set():return
                    # One operation completes before shutdown. Network calls
                    # happen outside the existing short database transactions.
                    try:
                        await asyncio.to_thread(job,tenant,worker_id)
                        if config.get('safety_check'):
                            config['safety_check']()
                    except PermissionError:
                        # A suspended/unmapped tenant cannot block other tenants.
                        continue
            try:
                await asyncio.wait_for(stop.wait(),poll_interval)
            except TimeoutError:
                pass


async def run(tenants, worker_id):
    stop=asyncio.Event();restorers=[]
    try:
        config=runtime_settings(worker_id)
        await asyncio.to_thread(verify_runtime,config,worker_id)
        restorers=install_signal_handlers(stop)
        limiter=BotRateLimits()
        config['safety_check']=limiter.assert_healthy
        with httpx.Client(timeout=httpx.Timeout(5,connect=2),follow_redirects=False,trust_env=False) as client:
            await serve(tenants,worker_id,stop,config=config,jobs=build_jobs(client,limiter))
        print(json.dumps({'state':'stopped','reason':'STOPPED'}));return 0
    except Exception:
        print(json.dumps({'state':'stopped','reason':'BOT_WORKER_FAILED'}));return 2
    finally:
        for restore in reversed(restorers):restore()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant',type=UUID,action='append',required=True,help='Assigned business tenant UUID')
    parser.add_argument('--worker',type=UUID,required=True)
    parser.add_argument('--live',action='store_true',required=True,help='Explicitly enable saved Bot API jobs')
    args=parser.parse_args(argv)
    if len(set(args.tenant))>100:parser.error('At most 100 tenants may be assigned')
    return asyncio.run(run(args.tenant,args.worker))


if __name__=='__main__':
    raise SystemExit(main())
