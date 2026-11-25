import asyncio
import httpx
import pytest
from llm_gateway.harness_client import HarnessClient,SCHEMA


def test_versioned_transport_and_unknown_version_refusal():
    def upstream(request):
        assert request.headers['Authorization']=='Bearer fixture'
        assert request.url.path=='/v1/runs'
        return httpx.Response(200,json={'schema':SCHEMA,'run_id':'one','state':'queued'})
    code,result=asyncio.run(HarnessClient('http://harness','fixture').request('POST','/v1/runs',{'schema':SCHEMA},transport=httpx.MockTransport(upstream)))
    assert code==200 and result['run_id']=='one'
    with pytest.raises(ValueError,match='schema'):
        asyncio.run(HarnessClient('http://harness','fixture').request('GET','/v1/runs',transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'schema':'future'}))))


def test_reconnect_duplicate_gap_and_version():
    events=[{'schema_version':'harness-event/v1','sequence':i} for i in range(3)]
    rows,cursor=HarnessClient.validate_events({'schema':SCHEMA,'events':events},1)
    assert [r['sequence'] for r in rows]==[1,2] and cursor==3
    with pytest.raises(ValueError,match='gap'):
        HarnessClient.validate_events({'schema':SCHEMA,'events':[events[2]]},0)
    with pytest.raises(ValueError,match='schema'):
        HarnessClient.validate_events({'schema':SCHEMA,'events':[{'schema_version':'future','sequence':0}]},0)
