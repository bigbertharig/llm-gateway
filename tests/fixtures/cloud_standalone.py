import asyncio,json,os,time
from pathlib import Path
from fastapi.testclient import TestClient
import llm_gateway.app as module
from llm_gateway.settings import Settings
class Fake:
    def __init__(self,**kwargs): self.workspaces=[Path.cwd()]
    def workspace(self,value): return Path.cwd()
    def research_workspace(self): return Path.cwd()
    async def account(self): return {'connected':True}
    async def close(self): pass
    async def models(self): return []
    def status(self,thread): return {'events':[],'approvals':[]}
    async def turn(self,thread,prompt,cwd,on_thread,**kwargs):
        await on_thread('fixture-thread')
        return 'fixture-thread','Completed with fake cloud provider'
module.CodexBridge=Fake
os.environ['GATEWAY_MODE']='cloud'
os.environ['PROFILES_PATH']=str(Path.cwd()/'missing-profiles.json')
os.environ['CHAT_DATA_ROOT']=str(Path.cwd()/'cloud-chat')
settings=Settings.from_env()
base='/dashboard/api/chat/conversations'
with TestClient(module.create_app(settings)) as client:
    page=client.get('/dashboard')
    assert page.status_code==200 and 'data-runtime-enabled="false"' in page.text
    assert client.get('/v1/capabilities').json()['runtime'] is False
    response=client.post(base,json={'model':'Codex','mode':'coding','cwd':str(Path.cwd())})
    assert response.status_code==201,response.text
    cid=response.json()['id']
    response=client.post(base+'/'+cid+'/turn',json={'model':'Codex','message':'finish'})
    assert response.status_code==200,response.text
    for _ in range(100):
        saved=client.get(base+'/'+cid).json()
        if saved.get('last_turn',{}).get('state')!='running':break
        time.sleep(.02)
    assert saved['messages'][-1]['content']=='Completed with fake cloud provider',saved
with TestClient(module.create_app(settings)) as client:
    assert client.get(base+'/'+cid).json()['messages'][-1]['content']=='Completed with fake cloud provider'
print(json.dumps({'cloud_dashboard':True,'coding_turn':True,'restart_history':True,'runtime_profiles_absent':True}))
