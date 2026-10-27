"""Redis leases and tenant-scoped working cache; SQL fencing remains authoritative."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry


class LeaseBusy(RuntimeError): pass
class LeaseLost(RuntimeError): pass


RENEW = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"


class Lease:
    def __init__(self,cache,key,ttl):
        self.cache,self.key,self.ttl = cache,key,ttl
        self.owner = secrets.token_hex(24)
        self.on_renew = None
        self.lost = False

    async def renew(self):
        ok = bool(await self.cache.eval(RENEW,1,self.key,self.owner,round(self.ttl*1000)))
        if ok and self.on_renew: ok = bool(await self.on_renew())
        return ok

    async def release(self):
        return bool(await self.cache.eval(RELEASE,1,self.key,self.owner))


class Coordination:
    def __init__(self,url,namespace="resolvedesk"):
        self.namespace = namespace
        self.redis = Redis.from_url(url,decode_responses=True,socket_connect_timeout=2,socket_timeout=2,
            retry=Retry(NoBackoff(),0),retry_on_timeout=False,health_check_interval=20)

    def key(self,kind,scope):
        digest = hashlib.sha256(json.dumps(list(scope),ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
        return self.namespace+":"+kind+":"+digest

    @asynccontextmanager
    async def lease(self,scope,ttl=15):
        if ttl < .15: raise ValueError("Lease TTL is too short")
        lease = Lease(self.redis,self.key("lease",scope),ttl)
        if not await self.redis.set(lease.key,lease.owner,nx=True,px=round(ttl*1000)): raise LeaseBusy("Conversation already owned")
        parent = asyncio.current_task()
        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(ttl/3)
                    if not await lease.renew(): raise LeaseLost()
            except asyncio.CancelledError: raise
            except Exception:
                lease.lost = True
                parent.cancel()
        pulse = asyncio.create_task(heartbeat())
        try:
            yield lease
            if lease.lost: raise LeaseLost("Execution lease was lost")
        except asyncio.CancelledError:
            if lease.lost:
                parent.uncancel()
                raise LeaseLost("Execution lease was lost") from None
            raise
        finally:
            pulse.cancel()
            await asyncio.gather(pulse,return_exceptions=True)
            try: await lease.release()
            except Exception: pass  # Expiry is bounded; never delete another owner.

    async def get_json(self,scope):
        raw = await self.redis.get(self.key("working",scope))
        return json.loads(raw) if raw else None

    async def set_json(self,scope,value,ttl=1800):
        raw = json.dumps(value,ensure_ascii=False,separators=(",",":"))
        if len(raw.encode()) > 128000: raise ValueError("Working memory too large")
        if not 1 <= ttl <= 86400: raise ValueError("Invalid working memory TTL")
        await self.redis.set(self.key("working",scope),raw,ex=ttl)

    async def delete_json(self,scope): await self.redis.delete(self.key("working",scope))
    async def close(self): await self.redis.aclose()
