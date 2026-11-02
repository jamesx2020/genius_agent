from copy import deepcopy
import time
import pytest
from sqlalchemy import select
from backend.db import db,Task,Record,Proposal,Execution,Event
from backend.workflow import run_task
from backend import agents


def create(client):
    r=client.post('/api/projects/P001/tasks',json={'title':'电缆延期','message':'规格一致，首批100米11月4日到货，余量11月9日到货。','scenario':'delivery_delay'})
    assert r.status_code==202,r.text
    # Existing workflow fixtures explicitly exercise v1 checkpoint compatibility.
    id=r.json()['id']
    with db() as s:
        event=s.scalar(select(Event).where(Event.task_id==id,Event.kind=='created'))
        event.data={**event.data,'workflow_version':1}
    return id


def fake_role(task_id,role,context,depth=0):
    return {'status':'completed','summary':role+' 已查询并分析','findings':['首批100米足够使用'],'missing_inputs':[],'evidence_ids':['PO001','SCHEDULE001'] if role=='project_manager' else ['Q001','Q002','BUDGET001'],'options':[{'title':'分批交货','reason':'满足工期，无新增成本','quote_id':'Q001','deliveries':[{'date':'2026-11-04','quantity':100},{'date':'2026-11-09','quantity':100}]},{'title':'替代采购','reason':'分散交付风险','quote_id':'Q002','deliveries':[{'date':'2026-11-04','quantity':200}]}] if role=='procurement' else []}


def approve(client,detail,selected=0,decision='approved',comment='确认'):
    p=detail['proposal']
    return client.post('/api/approvals/'+p['id']+'/decisions',json={'decision':decision,'version':p['version'],'digest':p['digest'],'selected':selected,'comment':comment})


def test_login_csrf_project_and_logout(client,login):
    assert client.get('/api/tasks').status_code==401
    assert client.post('/api/auth/login',json={'username':'pm','password':'wrong'}).status_code==401
    login()
    assert client.get('/api/projects/OTHER/records').status_code==403
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/projects/P001/tasks',json={'title':'a','message':'b'}).status_code==403
    client.headers['X-CSRF-Token']=token
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/auth/me').status_code==401


def test_durable_approval_role_and_idempotent_execution(client,login,monkeypatch):
    monkeypatch.setattr(agents,'run_role',fake_role)
    login('buyer')
    id=create(client)
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='waiting_approval'
    assert detail['pending']['kind']=='approval'
    assert approve(client,detail).status_code==403
    login('pm')
    assert approve(client,detail,selected=1).status_code==200
    run_task(id) # Loads saved SQLite checkpoint in a new connection.
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='completed'
    assert detail['outcome']['additional_cents']==240000
    assert approve(client,detail).status_code==409
    run_task(id)
    with db() as s:
        assert len(list(s.scalars(select(Execution))))==1
        assert s.get(Record,'BUDGET001').data['available_cents']==2760000
        assert s.get(Record,'PO001').version==2


def test_missing_information_pause_and_resume(client,login,monkeypatch):
    def needs_info(id,role,ctx,depth=0):
        if role=='project_manager' and not ctx['supplements']:
            return {'status':'needs_input','summary':'规格未知','findings':[],'missing_inputs':['请确认首批规格'],'evidence_ids':['PO001'],'options':[]}
        return fake_role(id,role,ctx,depth)
    monkeypatch.setattr(agents,'run_role',needs_info)
    login('buyer')
    id=create(client)
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='waiting_input'
    assert detail['pending']['questions']==['请确认首批规格']
    assert client.post('/api/tasks/'+id+'/inputs',json={'message':'首批规格YJV 4×35'}).status_code==403
    login('pm')
    assert client.post('/api/tasks/'+id+'/inputs',json={'message':'首批规格YJV 4×35'}).status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['status']=='waiting_approval'


def test_return_revision_and_reapproval(client,login,monkeypatch):
    monkeypatch.setattr(agents,'run_role',fake_role)
    login('pm')
    id=create(client)
    run_task(id)
    original=client.get('/api/tasks/'+id).json()
    assert approve(client,original,decision='returned',comment='补充规格证明').status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['status']=='waiting_revision'
    assert client.post('/api/tasks/'+id+'/inputs',json={'message':'已补充'}).status_code==403
    login('buyer')
    assert client.post('/api/tasks/'+id+'/inputs',json={'message':'首批规格一致，已补充证明'}).status_code==200
    run_task(id)
    revised=client.get('/api/tasks/'+id).json()
    assert revised['proposal']['version']==2
    assert revised['proposal']['decision'] is None
    login('pm')
    assert approve(client,original).status_code==409
    assert approve(client,revised).status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['status']=='completed'


def test_stale_record_blocks_execution(client,login,monkeypatch):
    monkeypatch.setattr(agents,'run_role',fake_role)
    login()
    id=create(client)
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert approve(client,detail).status_code==200
    with db() as s:
        r=s.get(Record,'SCHEDULE001')
        r.version+=1
    with pytest.raises(ValueError,match='业务数据已变化'):
        run_task(id)
    with db() as s:
        assert s.get(Record,'PO001').version==1
        assert not s.scalar(select(Execution))


def test_overspend_and_invalid_delivery_blocked(client,login,monkeypatch):
    monkeypatch.setattr(agents,'run_role',fake_role)
    login()
    with db() as s:
        r=s.get(Record,'BUDGET001');r.data={'category':'电气材料','available_cents':1}
    id=create(client);run_task(id)
    d=client.get('/api/tasks/'+id).json()
    assert approve(client,d,selected=1).status_code==409
    def invalid(id,role,ctx,depth=0):
        r=fake_role(id,role,ctx,depth)
        if role=='procurement':
            for o in r['options']:o['deliveries']=[{'date':'2026-11-04','quantity':999}]
        return r
    monkeypatch.setattr(agents,'run_role',invalid)
    id2=create(client)
    with pytest.raises(ValueError,match='未满足项目经理'):
        run_task(id2)


def test_documents_permissions_and_record_edit(client,login):
    login('buyer')
    r=client.post('/api/documents',files={'file':('通知.txt','延期5天，首批100米。'.encode(),'text/plain')})
    assert r.status_code==200
    assert client.get('/api/documents/'+r.json()['id']).json()['text'].startswith('延期')
    assert client.post('/api/documents',files={'file':('file.exe',b'abc','application/octet-stream')}).status_code==422
    data=client.get('/api/projects/P001/records').json()
    schedule=next(r for r in data if r['id']=='SCHEDULE001')
    assert client.put('/api/records/SCHEDULE001',json={'version':1,'data':schedule['data']}).status_code==403
    login('pm')
    assert client.put('/api/records/SCHEDULE001',json={'version':1,'data':schedule['data']}).status_code==200
    assert client.put('/api/records/SCHEDULE001',json={'version':1,'data':schedule['data']}).status_code==409


def test_no_key_is_explicit_failure(client,login):
    login()
    assert client.get('/api/config/status').json()['configured'] is False
    id=create(client)
    with pytest.raises(RuntimeError,match='DEEPSEEK_API_KEY'):
        run_task(id)
    with db() as s:
        assert not s.scalar(select(Proposal))


def test_create_idempotency_and_payload_conflict(client,login):
    login('buyer')
    body={'title':'电缆延期','message':'首批100米','scenario':'delivery_delay'}
    headers={'Idempotency-Key':'request-001'}
    first=client.post('/api/projects/P001/tasks',json=body,headers=headers)
    second=client.post('/api/projects/P001/tasks',json=body,headers=headers)
    assert first.status_code==202 and second.status_code==202
    assert first.json()['id']==second.json()['id']
    body['message']='另一项任务'
    assert client.post('/api/projects/P001/tasks',json=body,headers=headers).status_code==409


def test_worker_recovers_running_task_and_reports_missing_key(client,login):
    from fastapi.testclient import TestClient
    from backend.app import app
    login('buyer')
    id=create(client)
    with db() as s:
        s.get(Task,id).status='running'  # Simulate interruption before process restart.
    with TestClient(app):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with db() as s:
                task=s.get(Task,id)
                if task.status=='failed':
                    assert 'DEEPSEEK_API_KEY' in task.error
                    break
            time.sleep(0.05)
        else:
            pytest.fail('后台执行器未恢复任务')


def test_delay_notice_waits_for_pm_and_passes_transfer_direction(client,login,monkeypatch):
    seen=[]
    def directional(id,role,ctx,depth=0):
        seen.append((role,ctx.get('manager_instruction')))
        result=fake_role(id,role,ctx,depth)
        return result
    monkeypatch.setattr(agents,'run_role',directional)
    with db() as s:
        quote=s.get(Record,'Q002');quote.data={**quote.data,'available_date':'2026-11-04'}
    login('buyer')
    id=client.post('/api/projects/P001/tasks',json={'title':'采购延期通知','message':'首批100米，余量延期，请项目经理确认工程影响'}).json()['id']
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='waiting_manager'
    assert [role for role,_ in seen]==['project_manager']
    assert any(e['kind']=='delay_notice' for e in detail['events'])
    reply={'impact':'at_risk','direction':'transfer','required_date':'2026-11-04','required_quantity':100,'comment':'不能等余量，请先调货100米，新增费用另行报批'}
    assert client.post('/api/tasks/'+id+'/manager-response',json=reply).status_code==403
    login('pm')
    incomplete={**reply,'required_date':None}
    assert client.post('/api/tasks/'+id+'/manager-response',json=incomplete).status_code==422
    assert client.post('/api/tasks/'+id+'/manager-response',json=reply).status_code==200
    assert client.post('/api/tasks/'+id+'/manager-response',json=reply).status_code==409
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['status']=='waiting_approval'
    assert len(detail['proposal']['data']['options'])==1
    assert detail['proposal']['data']['options'][0]['quote_id']=='Q002'
    assert detail['proposal']['data']['options'][0]['additional_cents']==240000
    assert seen[1][1]['direction']=='transfer'
    assert approve(client,detail).status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['outcome']['additional_cents']==240000


def test_register_expedite_quote_requires_procurement(client,login):
    body={'quantity':200,'supplier':'华东电缆','material':'低压电缆 YJV 4×35','unit_price_cents':8000,'additional_cents':150000,'fulfillment_type':'expedite','available_date':'2026-11-04','valid_until':'2099-11-10','terms':'加急200米，新增加急费1500元含运费'}
    login('pm')
    assert client.post('/api/projects/P001/quotes',json=body).status_code==403
    login('buyer')
    response=client.post('/api/projects/P001/quotes',json=body)
    assert response.status_code==201
    with db() as s:
        record=s.get(Record,response.json()['id'])
        assert record.data['additional_cents']==150000
        assert record.data['fulfillment_type']=='expedite'


def test_expedite_uses_registered_fee_not_model_estimate(client,login,monkeypatch):
    login('buyer')
    quote=client.post('/api/projects/P001/quotes',json={'quantity':200,'supplier':'华东电缆','material':'低压电缆 YJV 4×35','unit_price_cents':8000,'additional_cents':150000,'fulfillment_type':'expedite','available_date':'2026-11-04','valid_until':'2099-11-10','terms':'加急200米，费用1500元含运费'}).json()['id']
    def expedited(id,role,ctx,depth=0):
        result=fake_role(id,role,ctx,depth)
        if role=='procurement':
            result['options']=[{'title':'供应商加急交货','reason':'满足项目经理现场要求','quote_id':quote,'deliveries':[{'date':'2026-11-04','quantity':200}]}]
        return result
    monkeypatch.setattr(agents,'run_role',expedited)
    id=client.post('/api/projects/P001/tasks',json={'title':'采购延期通知','message':'请项目经理确认是否要加急'}).json()['id']
    run_task(id)
    login('pm')
    assert client.post('/api/tasks/'+id+'/manager-response',json={'impact':'blocked','direction':'expedite','required_date':'2026-11-04','required_quantity':200,'comment':'全量电缆当天必须到场，请确认供应商加急费用'}).status_code==200
    run_task(id)
    detail=client.get('/api/tasks/'+id).json()
    assert detail['proposal']['data']['options'][0]['additional_cents']==150000
    assert approve(client,detail).status_code==200
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['outcome']['additional_cents']==150000


def test_procurement_form_saves_quote_and_resumes_atomically(client,login,monkeypatch):
    def need_quote(id,role,ctx,depth=0):
        if role=='procurement':
            return {'status':'needs_input','summary':'请确认货源和报价','findings':[],'missing_inputs':['供应商承诺何时到货，新增费用多少？'],'evidence_ids':['Q002'],'options':[]}
        return fake_role(id,role,ctx,depth)
    monkeypatch.setattr(agents,'run_role',need_quote)
    login('buyer')
    id=create(client) # v1 fixture: already runs directly to procurement.
    run_task(id)
    assert client.get('/api/tasks/'+id).json()['pending']['role']=='procurement'
    body={'supplier':'确认后的供应商','material':'低压电缆 YJV 4×35','quantity':100,'unit_price_cents':8000,'additional_cents':180000,'fulfillment_type':'transfer','available_date':'2026-11-05','valid_until':'2099-11-10','terms':'调货100米，调货费1500元，运费300元','note':'待项目经理审批'}
    login('finance')
    assert client.post('/api/tasks/'+id+'/procurement-reply',json=body).status_code==403
    login('buyer')
    response=client.post('/api/tasks/'+id+'/procurement-reply',json=body)
    assert response.status_code==200
    with db() as s:
        task=s.get(Task,id)
        assert task.status=='queued'
        assert response.json()['quote_id'] in task.resume['message']
        assert '不表示费用已获批准' in task.resume['message']
        record=s.get(Record,response.json()['quote_id'])
        assert record.data['additional_cents']==180000
        assert record.data['quantity']==100
    assert client.post('/api/tasks/'+id+'/procurement-reply',json=body).status_code==409
    with db() as s:
        assert len(list(s.scalars(select(Record).where(Record.kind=='quote'))))==3


def test_retry_model_failure_keeps_task_and_quote_checkpoint(client,login):
    login('buyer')
    id=create(client)
    with db() as s:
        task=s.get(Task,id)
        task.status='failed';task.error='执行失败（RuntimeError）。'
        task.resume={'message':'采购已登记报价，保留这个回复'}
    response=client.post('/api/tasks/'+id+'/retry')
    assert response.status_code==200
    assert response.json()['id']==id
    with db() as s:
        task=s.get(Task,id)
        assert task.status=='queued'
        assert task.resume['message']=='采购已登记报价，保留这个回复'


def test_price_difference_is_included_in_total_and_budget(client,login):
    login('buyer')
    response=client.post('/api/projects/P001/quotes',json={'quantity':100,'supplier':'调货供应商','material':'低压电缆 YJV 4×35','unit_price_cents':9000,'additional_cents':20000,'available_date':'2026-11-04','valid_until':'2099-11-10','fulfillment_type':'transfer','terms':'100米，材料90元每米，调货运费200元'})
    assert response.status_code==201
    with db() as s:
        quote=s.get(Record,response.json()['id']).data
        assert quote['service_fee_cents']==20000
        assert quote['price_difference_cents']==100000
        assert quote['additional_cents']==120000
    # The reported 500元 × 200米 + 100元 fee must not be treated as just 100元.
    response=client.post('/api/projects/P001/quotes',json={'quantity':200,'supplier':'高价供应商','material':'低压电缆 YJV 4×35','unit_price_cents':50000,'additional_cents':10000,'available_date':'2026-11-04','valid_until':'2099-11-10','fulfillment_type':'expedite','terms':'全量加急'})
    with db() as s:
        quote=s.get(Record,response.json()['id']).data
        assert quote['additional_cents']==8410000
        assert quote['additional_cents']>s.get(Record,'BUDGET001').data['available_cents']
    from backend.pricing import quote_cost
    legacy={'quantity':200,'available_date':'2026-11-04','unit_price_cents':50000,'additional_cents':10000}
    assert quote_cost(legacy,{'quantity':200,'amount_cents':1600000})['additional_cents']==8410000


def test_model_cannot_invent_missing_vendor_delivery_date(client,login,monkeypatch):
    monkeypatch.setattr(agents,'run_role',fake_role)
    login('buyer')
    id=client.post('/api/projects/P001/tasks',json={'title':'交期校验','message':'调货必须有承诺'}).json()['id']
    run_task(id)
    login('pm')
    client.post('/api/tasks/'+id+'/manager-response',json={'impact':'at_risk','direction':'transfer','required_date':'2026-11-05','required_quantity':100,'comment':'100米在11月5日前到场'})
    with pytest.raises(ValueError,match='未满足项目经理'):
        run_task(id)
    with db() as s:
        assert not s.scalar(select(Proposal).where(Proposal.task_id==id))
