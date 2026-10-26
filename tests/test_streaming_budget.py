import asyncio
import json
from unittest.mock import patch

import anthropic
import httpx
import pytest

from core.paid_api import BudgetedAnthropic,BudgetPolicy,CostLedger,PaidCallBlocked


def stream_payload():
    events=[('message_start',{'type':'message_start','message':{'id':'msg_test','type':'message','role':'assistant','model':'deepseek-flash','content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':12,'output_tokens':0}}}),
            ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
            ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'你好'}}),
            ('content_block_stop',{'type':'content_block_stop','index':0}),
            ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':3}}),
            ('message_stop',{'type':'message_stop'})]
    return '\n\n'.join('event: '+name+'\ndata: '+json.dumps(data,ensure_ascii=False) for name,data in events)+'\n\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel',[False,True])
async def test_sdk_stream_settles_usage_or_keeps_cancelled_reservation(tmp_path,monkeypatch,cancel):
    monkeypatch.setenv('RESOLVEDESK_PAID_API_ENABLED','true');monkeypatch.setenv('RESOLVEDESK_OWN_KEY_CONFIRMED','true')
    seen=[]
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,text=stream_payload(),headers={'content-type':'text/event-stream'})
    sdk=anthropic.AsyncAnthropic(api_key='offline-test',base_url='https://api.deepseek.com/anthropic',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client=BudgetedAnthropic(api_key='offline-test')
    client._ledger=CostLedger(tmp_path/'budget.sqlite3',BudgetPolicy())
    try:
        with patch('core.paid_api._sdk_client',return_value=sdk):
            async def run():
                async with client.messages.stream(model='deepseek-flash',max_tokens=50,messages=[{'role':'user','content':'hi'}]) as stream:
                    chunks=[]
                    async for delta in stream.text_stream:
                        chunks.append(delta)
                        if cancel:raise asyncio.CancelledError()
                    assert ''.join(chunks)=='你好'
            if cancel:
                with pytest.raises(asyncio.CancelledError):await run()
            else:await run()
        assert len(seen)==1 and seen[0]['stream'] is True
        summary=client.ledger().summary()
        assert summary['calls']==({'uncertain':1} if cancel else {'settled':1})
        if not cancel:assert summary['conservative_commitment_cny']=='0.000048'
    finally:await client.close()
