"""Shared Bot API server waits. A failed persistence gate stops the root worker."""
import psycopg

from app.business.db import transaction
from app.business.worker import RetryLater


class BotRateLimits:
    def __init__(self):
        self.failed=False

    def assert_healthy(self):
        if self.failed:
            raise RuntimeError('Bot API wait gate is unavailable')

    def fail(self):
        self.failed=True
        self.assert_healthy()

    def check(self, tenant, bot_id):
        self.assert_healthy()
        try:
            with transaction(tenant) as conn:
                row=conn.execute('''SELECT ceil(extract(epoch FROM wait_until-clock_timestamp()))::bigint AS remaining
                    FROM tgmate.bot_runtime_limits WHERE tenant_id=%s AND bot_id=%s
                    AND wait_until>clock_timestamp()''',(tenant,bot_id)).fetchone()
        except psycopg.Error:
            self.fail()
        if row and row['remaining']>0:
            raise RetryLater(row['remaining'])

    def defer(self, tenant, bot_id, seconds):
        self.assert_healthy()
        if type(seconds) is not int or not 1<=seconds<=2147483647:
            self.fail()
        try:
            with transaction(tenant) as conn:
                conn.execute('''INSERT INTO tgmate.bot_runtime_limits(tenant_id,bot_id,wait_until)
                    VALUES(%s,%s,clock_timestamp()+%s*interval '1 second')
                    ON CONFLICT(tenant_id,bot_id) DO UPDATE
                    SET wait_until=GREATEST(bot_runtime_limits.wait_until,EXCLUDED.wait_until),updated_at=clock_timestamp()''',
                    (tenant,bot_id,seconds))
        except psycopg.Error:
            self.fail()
