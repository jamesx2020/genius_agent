"""Durable, single-worker business workflow with version-bound human approvals."""
import json
import sqlite3
import time
from datetime import date
from typing import TypedDict
from sqlalchemy import select, update
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from . import agents
from .pricing import quote_cost
from .proposals import delivery_option_error
from .config import settings
from .db import db, Task, Record, Proposal, Execution, Event, digest, log, uid

class WorkflowError(ValueError):
    """Safe-to-display business validation error."""


class FlowState(TypedDict, total=False):
    task_id: str
    workflow_version: int
    manager_instruction: dict
    message: str
    scenario: str
    supplements: list[str]
    results: dict
    proposal_id: str
    approval: dict
    outcome: dict


def set_status(task_id,status):
    with db() as s:
        t=s.get(Task,task_id)
        t.status=status
        t.updated=int(time.time())


def role_node(role):
    def node(state):
        task_id=state['task_id']
        supplements=list(state.get('supplements',[]))
        # A paused node is replayed from its start. Carry the authenticated human
        # input into the very first fresh model attempt, even if it can now finish
        # without reaching the old interrupt again (e.g. a new quote was saved).
        with db() as s:
            pending=s.get(Task,task_id).resume
        if pending and pending.get('message') and pending['message'] not in supplements:
            supplements.append(pending['message'])
        while True:
            set_status(task_id,'running')
            log(task_id,'role_request','场景协调器请求 '+agents.ROLES[role]+' 执行专项任务',data={'target_role':role})
            result=agents.run_role(task_id,role,{'scenario':state['scenario'],'request':state['message'],'supplements':supplements,'other_role_results':state.get('results',{}),'manager_instruction':state.get('manager_instruction',{})})
            if result['status']=='completed':
                results={**state.get('results',{}),role:result}
                return {'results':results,'supplements':supplements}
            reply=interrupt({'kind':'input','role':role,'questions':result['missing_inputs'],'summary':result['summary']})
            if str(reply['message']) not in supplements:
                supplements.append(str(reply['message']))
    return node


def notify_manager(state):
    log(state['task_id'],'delay_notice','采购已发送延期通知，请项目经理判断工程影响及处理方向。',role='procurement',data={'recipient_role':'project_manager','notice':state['message']})
    return {}


def manager_response_node(state):
    response=interrupt({'kind':'manager_response','role':'project_manager','questions':['请结合现场情况判断：是否影响工程？接受分批交货，还是要求采购加急或调货？'],'analysis':state.get('results',{}).get('project_manager',{})})
    return {'manager_instruction':response,'supplements':state.get('supplements',[])+['项目经理现场回复：'+json.dumps(response,ensure_ascii=False)]}


def prepare_proposal(state):
    task_id=state['task_id']
    with db() as s:
        t=s.get(Task,task_id)
        records={r.id:r for r in s.scalars(select(Record).where(Record.project_id==t.project_id))}
        procurement=state['results']['procurement']
        options=[]
        if state['scenario']=='delivery_delay':
            order=records['PO001']
            for option in procurement['options']:
                quote=records.get(option['quote_id'])
                if not quote or quote.kind!='quote' or quote.data['valid_until']<date.today().isoformat():
                    continue
                if delivery_option_error(option,quote.data,order.data,state.get('manager_instruction',{})):
                    continue
                cost=quote_cost(quote.data,order.data)
                amount=cost['additional_cents']
                options.append({**option,**cost,'within_budget':amount<=records['BUDGET001'].data['available_cents'],'supplier':quote.data['supplier']})
            if not options:
                raise WorkflowError('方案未满足项目经理的处理方向、到货数量或时间，或缺少有效报价。请采购核实货源与费用后重新分析。')
        else:
            options=[{'title':'确认分析并生成'+('采购申请草稿' if state['scenario']=='material_purchase' else '付款申请草稿'),'reason':'仅创建草稿，后续下单或付款需独立审批。','additional_cents':0,'within_budget':True}]
        data={'scenario':state['scenario'],'summary':procurement['summary'],'options':options,'results':state['results'],'record_versions':{r.id:r.version for r in records.values()},'supplements':state.get('supplements',[]),'manager_instruction':state.get('manager_instruction',{})}
        proposal=s.scalar(select(Proposal).where(Proposal.task_id==task_id))
        fingerprint=digest(data)
        if not proposal:
            proposal=Proposal(id=uid('PROP-'),task_id=task_id,data=data,digest=fingerprint)
            s.add(proposal)
        elif proposal.digest!=fingerprint:
            proposal.version+=1
            proposal.data=data
            proposal.digest=fingerprint
            proposal.decision=None
            proposal.decision_by=None
            proposal.selected=None
            proposal.comment=''
        pid=proposal.id
    log(task_id,'proposal_created','三岗位分析已汇总为待审批方案')
    return {'proposal_id':pid}


def approval_node(state):
    # The API persists an authenticated decision; the graph only carries its ID.
    decision=interrupt({'kind':'approval','proposal_id':state['proposal_id']})
    return {'approval':decision}


def revision_node(state):
    reply=interrupt({'kind':'revision','questions':['请按审批意见补充或修改方案后重新提交。']})
    return {'supplements':state.get('supplements',[])+[reply['message']], 'results':{},'manager_instruction':{}}


def execute_node(state):
    task_id=state['task_id']
    with db() as s:
        # Acquire SQLite writer lock BEFORE version/budget checks.
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        done=s.scalar(select(Execution).where(Execution.task_id==task_id))
        if done:
            return {'outcome':done.result}
        t=s.get(Task,task_id)
        p=s.get(Proposal,state['proposal_id'])
        if not p or p.decision!='approved' or state['approval'].get('proposal_id')!=p.id:
            raise WorkflowError('缺少有效的已批准方案')
        if digest(p.data)!=p.digest or state['approval'].get('digest')!=p.digest:
            raise WorkflowError('批准后方案已变化，必须重新审批')
        for id,version in p.data['record_versions'].items():
            r=s.get(Record,id)
            if not r or r.version!=version:
                raise WorkflowError('业务数据已变化，原审批失效，请重新分析审批')
        option=p.data['options'][p.selected]
        if t.scenario=='delivery_delay':
            order=s.get(Record,'PO001')
            budget=s.get(Record,'BUDGET001')
            quote=s.get(Record,option['quote_id'])
            if quote.data['valid_until']<date.today().isoformat():
                raise WorkflowError('报价已过期，请重新询价')
            cost=quote_cost(quote.data,order.data)['additional_cents']
            if budget.data['available_cents']<cost:
                raise WorkflowError('预算不足，禁止执行')
            order.data={**order.data,'delivery_plan':option['deliveries'],'change_note':option['title'],'additional_cents':cost,'alternative_supplier':option['supplier']}
            order.version+=1
            budget.data={**budget.data,'available_cents':budget.data['available_cents']-cost}
            budget.version+=1
            outcome={'message':'订单交付计划已更新','order_id':order.id,'version':order.version,'additional_cents':cost}
        else:
            rid=uid('DRAFT-')
            kind='purchase_request' if t.scenario=='material_purchase' else 'payment_request'
            s.add(Record(id=rid,project_id=t.project_id,kind=kind,data={'status':'draft','task_id':task_id,'analysis':p.data['results'],'request':t.message}))
            outcome={'message':'已创建待人工核实的申请草稿，未下单、未付款','draft_id':rid}
        s.add(Execution(id='execute-'+task_id,task_id=task_id,result=outcome))
        s.add(Event(task_id=task_id,kind='executed',role='system',message=outcome['message'],data=outcome))
        t.status='completed'
    return {'outcome':outcome}


def build_graph(checkpointer):
    graph=StateGraph(FlowState)
    for role in ['project_manager','procurement','finance']:
        graph.add_node(role,role_node(role))
    graph.add_node('notify_manager',notify_manager)
    graph.add_node('manager_response',manager_response_node)
    graph.add_node('proposal',prepare_proposal)
    graph.add_node('approval',approval_node)
    graph.add_node('execute',execute_node)
    graph.add_node('revision',revision_node)
    is_delay_v2=lambda state: state.get('workflow_version',1)>=2 and state['scenario']=='delivery_delay'
    graph.add_conditional_edges(START,lambda state: 'notify_manager' if is_delay_v2(state) else 'project_manager')
    graph.add_edge('notify_manager','project_manager')
    graph.add_conditional_edges('project_manager',lambda state: 'manager_response' if is_delay_v2(state) else 'procurement')
    graph.add_edge('manager_response','procurement')
    graph.add_edge('procurement','finance')
    graph.add_edge('finance','proposal')
    graph.add_edge('proposal','approval')
    graph.add_conditional_edges('approval',lambda state: 'revision' if state['approval'].get('decision')=='returned' else 'execute')
    graph.add_edge('revision','project_manager')
    graph.add_edge('execute',END)
    return graph.compile(checkpointer=checkpointer)


def run_task(task_id):
    cfg=settings()
    agents.DEADLINE.set(time.monotonic()+cfg.agent_task_timeout_seconds)
    conn=sqlite3.connect(cfg.data_dir/'checkpoints.db',check_same_thread=False,timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        graph=build_graph(SqliteSaver(conn))
        config={'configurable':{'thread_id':task_id},'recursion_limit':40}
        snap=graph.get_state(config)
        with db() as s:
            t=s.get(Task,task_id)
            resume=t.resume
            created=s.scalar(select(Event).where(Event.task_id==task_id,Event.kind=='created').order_by(Event.id))
            version=created.data.get('workflow_version',1) if created else 1
            initial={'workflow_version':version,'manager_instruction':{},'task_id':t.id,'message':t.message,'scenario':t.scenario,'results':{},'supplements':[]}
        if snap.values:
            payload=Command(resume=resume) if resume else None
        else:
            payload=initial
        graph.invoke(payload,config)
        snap=graph.get_state(config)
        with db() as s:
            t=s.get(Task,task_id)
            t.resume=None
            t.error=None
            if not snap.next:
                t.status='completed'
            else:
                pending=[i.value for item in snap.tasks for i in item.interrupts]
                t.status={'approval':'waiting_approval','revision':'waiting_revision','manager_response':'waiting_manager'}.get(pending[0].get('kind'),'waiting_input') if pending else 'failed'
            t.updated=int(time.time())
    finally:
        conn.close()


def pending_interrupt(task_id):
    path=settings().data_dir/'checkpoints.db'
    if not path.exists():
        return None
    conn=sqlite3.connect(path,check_same_thread=False,timeout=10)
    try:
        snap=build_graph(SqliteSaver(conn)).get_state({'configurable':{'thread_id':task_id}})
        for task in snap.tasks:
            for item in task.interrupts:
                return item.value
    finally:
        conn.close()
    return None
