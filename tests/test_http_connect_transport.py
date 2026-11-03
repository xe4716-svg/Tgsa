"""Actual installed Telethon/python-socks transport over loopback, no Telegram."""
import asyncio
import base64
from collections import defaultdict
import logging

import pytest
from telethon.network.connection.tcpfull import ConnectionTcpFull

from app.telegram.client_factory import build_proxy


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('authenticated',[False,True])
async def test_http_connect_handshake_and_tunnel_with_installed_telethon(authenticated):
    headers=[];finished=asyncio.Event()
    async def proxy_client(reader,writer):
        try:
            headers.append(await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),1))
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n');await writer.drain()
            value=await asyncio.wait_for(reader.readexactly(5),1)
            writer.write(value.upper());await writer.drain()
        finally:
            writer.close();await writer.wait_closed();finished.set()
    server=await asyncio.start_server(proxy_client,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    config={'proxy_type':'HTTP_CONNECT','host':'127.0.0.1','port':port,'verification_status':'VERIFIED'}
    if authenticated:config.update(username='test-user',password='test-password')
    proxy,_=build_proxy(config)
    conn=ConnectionTcpFull('does-not-resolve.invalid',443,2,
                           loggers=defaultdict(lambda:logging.getLogger('transport-test')),proxy=proxy)
    try:
        await conn._connect(timeout=1)
        conn._writer.write(b'probe');await conn._writer.drain()
        assert await asyncio.wait_for(conn._reader.readexactly(5),1)==b'PROBE'
        assert b'CONNECT does-not-resolve.invalid:443 HTTP/1.1' in headers[0]
        if authenticated:
            assert b'Proxy-Authorization: Basic '+base64.b64encode(b'test-user:test-password') in headers[0]
        else:assert b'Proxy-Authorization' not in headers[0]
        await asyncio.wait_for(finished.wait(),1)
    finally:
        if conn._writer:
            conn._writer.close();await conn._writer.wait_closed()
        server.close();await server.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize('failure',['407','502','timeout','closed'])
async def test_proxy_failure_never_falls_back_to_direct(failure):
    target_hits=[];finished=asyncio.Event();writers=[]
    async def target_client(reader,writer):
        target_hits.append(True);writer.close();await writer.wait_closed()
    async def proxy_client(reader,writer):
        writers.append(writer)
        try:
            await reader.readuntil(b'\r\n\r\n')
            if failure=='timeout':await reader.read()
            elif failure!='closed':
                writer.write(f'HTTP/1.1 {failure} Rejected\r\n\r\n'.encode());await writer.drain()
        finally:
            writer.close();await writer.wait_closed();finished.set()
    target=await asyncio.start_server(target_client,'127.0.0.1',0)
    server=await asyncio.start_server(proxy_client,'127.0.0.1',0)
    proxy,_=build_proxy({'proxy_type':'HTTP_CONNECT','host':'127.0.0.1',
                        'port':server.sockets[0].getsockname()[1],'verification_status':'VERIFIED'})
    conn=ConnectionTcpFull('127.0.0.1',target.sockets[0].getsockname()[1],2,
                           loggers=defaultdict(lambda:logging.getLogger('transport-test')),proxy=proxy)
    try:
        with pytest.raises((ConnectionError,TimeoutError)):
            await conn._connect(timeout=.1)
        assert not target_hits
    finally:
        for writer in writers:writer.close()
        await asyncio.wait_for(finished.wait(),1)
        server.close();target.close()
        await server.wait_closed();await target.wait_closed()
