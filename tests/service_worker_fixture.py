"""Exclusive synthetic multi-process integration fixture. No paid model calls."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from resolvedesk.agent import DeskAgent
from resolvedesk.coordination import Coordination
from resolvedesk.retrieval import HybridKnowledge
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Principal,Store


async def main():
    schema,namespace,run_id=sys.argv[1:]
    if not schema.startswith("test_rd_") or not namespace.startswith("test-rd-"): raise ValueError("Fixture requires isolated test scope")
    settings=ServiceSettings.load()
    store=Store(settings.database_url,schema=schema)
    cache=Coordination(settings.redis_url,namespace)
    user=Principal("demo","alice")
    async def create(**kwargs):
        store.event(user,run_id,"offline_fixture_model_call",{})
        await asyncio.sleep(.2)
        return SimpleNamespace(stop_reason="end_turn",content=[{"type":"text","text":"offline process result"}],usage=SimpleNamespace(input_tokens=0,output_tokens=0))
    async def close(): pass
    agent=DeskAgent(store,HybridKnowledge(store),SimpleNamespace(messages=SimpleNamespace(create=create),close=close),coordination=cache)
    try: await agent.execute(user,run_id)
    finally:
        await agent.close();await cache.close();store.close()


if __name__=="__main__": asyncio.run(main())
