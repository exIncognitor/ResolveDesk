import asyncio
import os
import uuid

import pytest

from resolvedesk.coordination import Coordination,LeaseBusy,LeaseLost
from resolvedesk.settings import ServiceSettings


@pytest.fixture
def cache():
    if os.getenv("RESOLVEDESK_SERVICE_TESTS") != "true": pytest.skip("Real Redis tests explicitly disabled")
    # Every key in these tests is owned by this unique namespace, never FLUSHDB.
    return Coordination(ServiceSettings.load().redis_url,"test-rd-"+uuid.uuid4().hex)


@pytest.mark.services
@pytest.mark.asyncio
async def test_real_redis_renewal_exclusion_and_working_memory_scope(cache):
    scope = ["tenant","alice","conversation"]
    try:
        async with cache.lease(scope,ttl=.3) as lease:
            await asyncio.sleep(.65)
            assert await cache.redis.get(lease.key) == lease.owner
            with pytest.raises(LeaseBusy):
                async with cache.lease(scope,ttl=.3): pass
        assert not await cache.redis.exists(lease.key)
        await cache.set_json(scope,{"test":"中文"},ttl=2)
        assert await cache.get_json(scope) == {"test":"中文"}
        assert await cache.get_json(["other","alice","conversation"]) is None
        assert await cache.get_json(["tenant","bob","conversation"]) is None
        await cache.delete_json(scope)
    finally: await cache.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_redis_ownership_loss_cancels_worker_without_deleting_successor(cache):
    try:
        with pytest.raises(LeaseLost):
            async with cache.lease(["t","u","run"],ttl=.3) as lease:
                await cache.redis.set(lease.key,"successor",px=2000)
                await asyncio.sleep(2)
        assert await cache.redis.get(lease.key) == "successor"
        await cache.redis.delete(lease.key)
    finally: await cache.close()


@pytest.mark.services
@pytest.mark.asyncio
async def test_sql_renewal_failure_invalidates_redis_owner(cache):
    async def failed_fence(): return False
    try:
        with pytest.raises(LeaseLost):
            async with cache.lease(["t","u","run"],ttl=.3) as lease:
                lease.on_renew = failed_fence
                await asyncio.sleep(2)
        assert not await cache.redis.exists(lease.key)
    finally: await cache.close()
