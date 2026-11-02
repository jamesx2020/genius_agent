"""Exercise the actual Deep Agents tool loop with a local scripted model, no network."""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult,ChatGeneration
from langchain_core.messages import AIMessage,ToolMessage
from backend import agents
from backend.config import settings
from backend.db import db,Task,Event
from sqlalchemy import select


def pm_report(evidence_ids):
    return {
        'conclusion':'当前订单及计划资料已查询，按交付前提评估工期。',
        'schedule_impact':'undetermined',
        'impact_explanation':'结论以已查询资料及用户确认的交付条件为准。',
        'facts':[{'label':'已核实资料','detail':'已读取项目业务记录。','evidence_ids':evidence_ids}],
        'risks':['实际到货仍需现场验收。'],
        'handoffs':[{'role':'procurement','task':'核实交付条件与报价有效性。'}],
        'next_step':'由采购继续核实方案，再由财务核算成本。',
    }

class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self):return 'test-scripted'
    def bind_tools(self,tools,**kwargs):return self
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        queried=any(isinstance(m,ToolMessage) and m.name=='query_business' for m in messages)
        name='submit_analysis' if queried else 'query_business'
        args={'status':'completed','summary':'已查询真实订单，数量200米','findings':['订单数量200米'],'evidence_ids':['PO001'],'missing_inputs':[],'options':[]} if queried else {'kind':'order'}
        if queried:
            args['report']=pm_report(['PO001'])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':'call-final' if queried else 'call-query'}]))])


def test_deepagents_runs_actual_business_tool(monkeypatch):
    monkeypatch.setattr(settings(),'deepseek_api_key','test-only-not-a-real-key')
    monkeypatch.setattr(agents,'ChatOpenAI',lambda **kwargs:ScriptedModel())
    with db() as s:
        s.add(Task(id='runtime-test',project_id='P001',owner_id='pm',scenario='delivery_delay',title='测试',message='查订单'))
    result=agents.run_role('runtime-test','project_manager',{'request':'查订单'})
    assert result['status']=='completed'
    assert result['evidence_ids']==['PO001']
    assert result['report']['facts'][0]['evidence_ids']==['PO001']
    assert result['report']['handoffs'][0]['role']=='procurement'
    with db() as s:
        events=list(s.scalars(select(Event).where(Event.task_id=='runtime-test')))
        tool=next(e for e in events if e.kind=='tool_result')
        assert tool.data['records'][0]['data']['quantity']==200
        assert any(e.kind=='role_result' for e in events)

class BusinessModel(BaseChatModel):
    @property
    def _llm_type(self):return 'business-tool-test'
    def bind_tools(self,tools,**kwargs):return self
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        system=str(messages[0].content)
        if '你是工程项目的项目经理 Agent' in system:
            role='project_manager'; queries=['schedule','order']; evidence=['PO001','SCHEDULE001']
        elif '你是工程项目的采购 Agent' in system:
            role='procurement';queries=['order','quote','budget'];evidence=['PO001','Q001','BUDGET001']
        else:
            role='finance';queries=['budget','order','quote'];evidence=['PO001','Q001','BUDGET001']
        tool_msgs=[m for m in messages if isinstance(m,ToolMessage)]
        count=sum(m.name=='query_business' for m in tool_msgs)
        if count<len(queries):
            name='query_business';args={'kind':queries[count]}
        elif role!='project_manager' and not any(m.name=='calculate_cost' for m in tool_msgs):
            name='calculate_cost';args={'quote_id':'Q001'}
        elif role=='procurement' and not any(m.name=='request_role_task' for m in tool_msgs):
            name='request_role_task';args={'target_role':'finance','question':'核算Q001增量成本及预算'}
        else:
            name='submit_analysis';args={'status':'completed','summary':role+'基于查询完成分析','findings':['首批规格一致，数量足够'],'evidence_ids':evidence,'missing_inputs':[],'options':[{'title':'分批交货','reason':'工具核算无新增成本','quote_id':'Q001','deliveries':[{'date':'2026-11-04','quantity':100},{'date':'2026-11-09','quantity':100}]}] if role=='procurement' else []}
        if name=='submit_analysis' and role=='project_manager':
            args['report']=pm_report(evidence)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':'call-'+str(len(tool_msgs))}]))])


def test_real_graph_and_deepagents_cross_role_loop(client,login,monkeypatch):
    from backend.workflow import run_task
    monkeypatch.setattr(settings(),'deepseek_api_key','test-only-not-a-real-key')
    monkeypatch.setattr(agents,'ChatOpenAI',lambda **kwargs:BusinessModel())
    login('buyer')
    task=client.post('/api/projects/P001/tasks',json={'title':'实际工具循环','message':'订单PO001延期，首批规格一致'}).json()
    run_task(task['id'])
    detail=client.get('/api/tasks/'+task['id']).json()
    assert detail['status']=='waiting_manager'
    assert not any(e['role']=='procurement' and e['kind']=='agent_started' for e in detail['events'])
    login('pm')
    response=client.post('/api/tasks/'+task['id']+'/manager-response',json={'impact':'none','direction':'accept_split','comment':'首批足够，请采购核实分批交货及费用。'})
    assert response.status_code==200
    run_task(task['id'])
    detail=client.get('/api/tasks/'+task['id']).json()
    assert detail['status']=='waiting_approval'
    assert any(e['kind']=='role_request' and e['role']=='procurement' for e in detail['events'])
    assert any(e['kind']=='tool_result' and e['data'].get('tool')=='calculate_cost' for e in detail['events'])
    login('pm')
    p=detail['proposal']
    r=client.post('/api/approvals/'+p['id']+'/decisions',json={'decision':'approved','version':p['version'],'digest':p['digest'],'selected':0})
    assert r.status_code==200
    run_task(task['id'])
    assert client.get('/api/tasks/'+task['id']).json()['outcome']['order_id']=='PO001'

class ResumableModel(BusinessModel):
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        import json
        if '你是工程项目的采购 Agent' not in str(messages[0].content):
            return super()._generate(messages,stop,run_manager,**kwargs)
        tool_msgs=[m for m in messages if isinstance(m,ToolMessage)]
        count=sum(m.name=='query_business' for m in tool_msgs)
        queries=['order','quote','budget']
        quotes=[]
        for m in tool_msgs:
            if m.name=='query_business':
                try:
                    records=json.loads(m.content)
                    quotes.extend(r for r in records if r.get('data',{}).get('fulfillment_type')=='expedite')
                except (ValueError,TypeError):pass
        if count<len(queries):
            name='query_business';args={'kind':queries[count]}
        elif not quotes:
            name='submit_analysis';args={'status':'needs_input','summary':'请采购确认加急报价','missing_inputs':['请确认供应商、日期和加急费用。'],'evidence_ids':['PO001','Q001'],'findings':[],'options':[]}
        elif not any(m.name=='calculate_cost' for m in tool_msgs):
            name='calculate_cost';args={'quote_id':quotes[-1]['id']}
        else:
            q=quotes[-1]
            name='submit_analysis';args={'status':'completed','summary':'已读取新加急报价','findings':['报价满足加急要求'],'missing_inputs':[],'evidence_ids':['PO001',q['id']],'options':[{'title':'加急交货','reason':'使用采购确认的新报价','quote_id':q['id'],'deliveries':[{'date':'2026-11-04','quantity':200}]}]}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':'resume-call-'+str(len(tool_msgs))}]))])


def test_deepagents_resumes_after_new_quote_without_reusing_child_checkpoint(client,login,monkeypatch):
    from backend.workflow import run_task
    monkeypatch.setattr(settings(),'deepseek_api_key','test-only-not-a-real-key')
    monkeypatch.setattr(agents,'ChatOpenAI',lambda **kwargs:ResumableModel())
    login('buyer')
    id=client.post('/api/projects/P001/tasks',json={'title':'恢复回归测试','message':'首批100米，余量延期，请确认'}).json()['id']
    run_task(id)
    login('pm')
    client.post('/api/tasks/'+id+'/manager-response',json={'impact':'blocked','direction':'expedite','required_date':'2026-11-05','required_quantity':200,'comment':'需要全量加急'})
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['status']=='waiting_input'
    login('buyer')
    reply=client.post('/api/tasks/'+id+'/procurement-reply',json={'quantity':200,'supplier':'华东电缆','material':'低压电缆 YJV 4×35','unit_price_cents':8000,'additional_cents':150000,'available_date':'2026-11-04','valid_until':'2099-11-10','fulfillment_type':'expedite','terms':'全量加急200米','note':'保留这条人工说明'})
    assert reply.status_code==200
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='waiting_approval'
    assert reply.json()['quote_id']==detail['proposal']['data']['options'][0]['quote_id']
    assert any('保留这条人工说明' in text for text in detail['proposal']['data']['supplements'])
    login('pm')
    p=detail['proposal']
    assert client.post('/api/approvals/'+p['id']+'/decisions',json={'decision':'approved','version':p['version'],'digest':p['digest']}).status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['status']=='completed'
