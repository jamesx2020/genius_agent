"""Opt-in live DeepSeek smoke test. Run ONLY with an isolated DATA_DIR.

Uses the existing .env key; writes sample business data and calls the paid model.
"""
import json
import os
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0,str(ROOT))
from backend.config import settings
from backend.app import app
from fastapi.testclient import TestClient

cfg=settings()
if cfg.data_dir==ROOT/'data' or 'e2e' not in str(cfg.data_dir):
    raise SystemExit('Refusing to run against a non-isolated DATA_DIR; use a new /private/tmp/...e2e... directory.')
if not cfg.deepseek_api_key:
    raise SystemExit('DeepSeek key is not configured')

def login(client,username,password):
    response=client.post('/api/auth/login',json={'username':username,'password':password})
    response.raise_for_status()
    client.headers['X-CSRF-Token']=response.json()['csrf']

def wait(client,id,target):
    deadline=time.monotonic()+180
    previous=None
    while time.monotonic()<deadline:
        response=client.get('/api/tasks/'+id)
        response.raise_for_status()
        detail=response.json()
        if detail['status']!=previous:
            print(json.dumps({'task':id,'status':detail['status']},ensure_ascii=False),flush=True)
            previous=detail['status']
        if detail['status']==target:return detail
        if detail['status'] in ('failed','waiting_input','waiting_revision'):
            raise RuntimeError(json.dumps({'unexpected':detail['status'],'error':detail['error'],'pending':detail['pending']},ensure_ascii=False))
        time.sleep(1)
    raise TimeoutError(target)

with TestClient(app) as client:
    login(client,'buyer',cfg.buyer_password)
    response=client.post('/api/projects/P001/tasks',json={'title':'真实闭环验收：接受分批交货','message':'PO001低压电缆200米，11月4日先交100米，11月9日交余量100米，首批规格与订单一致。请项目经理确认工程影响。'})
    response.raise_for_status()
    id=response.json()['id']
    wait(client,id,'waiting_manager')
    login(client,'pm',cfg.pm_password)
    response=client.post('/api/tasks/'+id+'/manager-response',json={'impact':'none','direction':'accept_split','comment':'现场确认首批100米满足前三天80米需求，剩余100米11月9日前到位即可，同意分批方案，请核实交货条件和费用。'})
    response.raise_for_status()
    detail=wait(client,id,'waiting_approval')
    p=detail['proposal']
    assert p['data']['options'][0]['additional_cents']==0
    response=client.post('/api/approvals/'+p['id']+'/decisions',json={'decision':'approved','version':p['version'],'digest':p['digest'],'selected':0})
    response.raise_for_status()
    detail=wait(client,id,'completed')
    records=client.get('/api/projects/P001/records').json()
    budget=next(r for r in records if r['id']=='BUDGET001')
    assert budget['data']['available_cents']==3000000
    print(json.dumps({'result':'PASS','task_id':id,'outcome':detail['outcome'],'budget_cents':budget['data']['available_cents']},ensure_ascii=False),flush=True)
