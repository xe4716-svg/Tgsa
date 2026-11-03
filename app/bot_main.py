"""Bot process: authenticated durable ingress and existing business queue runner."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from uuid import UUID

import httpx
import psycopg
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.business import db, bot_webhooks, bot_gateway
from app.business.errors import DeferredIngress
from app.business.relay import Rejected
from app.telegram.bot_rate_limits import BotRateLimits
from app.telegram.router import ResourceRoute
from app.workers.bot import runtime_settings, verify_runtime, serve, build_jobs


@asynccontextmanager
async def lifespan(app):
    try:
        tenants=tuple(dict.fromkeys(UUID(value.strip()) for value in os.environ.get('BOT_TENANT_IDS','').split(',') if value.strip()))
        if not 1<=len(tenants)<=100:raise ValueError()
        identifier=UUID(os.environ['BOT_WORKER_ID'])
        config=runtime_settings(identifier)
        config['allowed_tenants']=frozenset(str(t) for t in tenants)
        await asyncio.to_thread(verify_runtime,config,identifier)
    except Exception:
        raise RuntimeError('Bot process requires valid private settings and worker registration') from None
    stop=asyncio.Event();limiter=BotRateLimits()
    config['safety_check']=limiter.assert_healthy
    app.state.business_config=config
    app.state.ingress_slots=asyncio.Semaphore(8)
    with httpx.Client(timeout=httpx.Timeout(5,connect=2),trust_env=False,follow_redirects=False) as client:
        task=asyncio.create_task(serve(tenants,identifier,stop,config=config,jobs=build_jobs(client,limiter)))
        app.state.worker_task=task
        try:
            yield
        finally:
            stop.set()
            result=await asyncio.gather(task,return_exceptions=True)
            if isinstance(result[0],BaseException):
                raise RuntimeError('Bot worker stopped after a failure; inspect health and audit records') from None


app=FastAPI(title='TGMate Bot gateway',lifespan=lifespan)
app.router.route_class=ResourceRoute


@app.get('/health/ready')
async def readiness(request: Request):
    task=getattr(request.app.state,'worker_task',None)
    if task is None or task.done():raise HTTPException(503,'Bot worker unavailable')
    return {'status':'ready'}


@app.post('/webhooks/bots/{bot_id}')
async def receive(bot_id: UUID, request: Request,
                  x_telegram_bot_api_secret_token: str=Header(default='')):
    if not 100<=len(x_telegram_bot_api_secret_token)<=256:
        raise HTTPException(403,'Webhook authentication failed')
    task=getattr(request.app.state,'worker_task',None)
    if task is None or task.done():raise HTTPException(503,'Bot worker unavailable')
    config=request.app.state.business_config
    with db.use_settings(config):
        async with request.app.state.ingress_slots:
            try:
                # Authenticated encrypted envelope chooses tenant before any
                # tenant lookup; never trust a URL/body tenant identifier.
                entry=await run_in_threadpool(bot_webhooks.ingress,bot_id,x_telegram_bot_api_secret_token)
                if not entry:raise HTTPException(403,'Webhook authentication failed')
                raw=bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw)>1024*1024:raise HTTPException(413,'Webhook body too large')
                update=json.loads(raw)
                return await run_in_threadpool(bot_gateway.dispatch,entry['tenant_id'],bot_id,update)
            except PermissionError:
                raise HTTPException(403,'Webhook tenant unavailable') from None
            except DeferredIngress:
                raise HTTPException(503,'Event mapping requires retry') from None
            except (Rejected,ValueError,TypeError):
                raise HTTPException(422,'Invalid webhook event') from None
            except (psycopg.Error,OSError,TimeoutError):
                raise HTTPException(503,'Webhook storage unavailable') from None
