import asyncio
import hashlib
import io
import json
import secrets
import time
import zipfile
from datetime import date
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, Request, Response, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, update, delete
from .pricing import quote_cost
from .config import ROOT, settings
from .db import db, init_db, User, LoginSession, Membership, Record, Task, Proposal, Execution, Event, Document, uid, digest, verify_password, log
from .workflow import run_task, pending_interrupt, WorkflowError

ROLE_NAMES={'project_manager':'项目经理','procurement':'采购','finance':'财务'}
login_failures=defaultdict(list)

async def worker(stop):
    while not stop.is_set():
        task_id=None
        with db() as s:
            task=s.scalar(select(Task).where(Task.status=='queued').order_by(Task.created))
            if task:
                claimed=s.execute(update(Task).where(Task.id==task.id,Task.status=='queued').values(status='running',updated=int(time.time())))
                if claimed.rowcount:
                    task_id=task.id
        if task_id:
            try:
                await asyncio.to_thread(run_task,task_id)
            except Exception as exc:
                # Provider exception text may contain credentials or payloads. Never persist it.
                if not settings().deepseek_api_key:
                    error='未配置 DEEPSEEK_API_KEY，请填写 .env 后重启，再点击重试。'
                elif isinstance(exc,WorkflowError):
                    error=str(exc)[:500]
                else:
                    error=f'执行失败（{type(exc).__name__}）。请检查模型配置、网络或补充资料后重试。'
                with db() as s:
                    t=s.get(Task,task_id)
                    t.status='failed'
                    t.error=error
                    t.updated=int(time.time())
                detail=str(exc)
                if settings().deepseek_api_key:
                    detail=detail.replace(settings().deepseek_api_key,'[redacted]')
                log(task_id,'error',error,data={'error_type':type(exc).__name__,'reason':detail[:1200]})
        else:
            try:
                await asyncio.wait_for(stop.wait(),timeout=0.7)
            except asyncio.TimeoutError:
                pass

@asynccontextmanager
async def lifespan(app):
    init_db()
    with db() as s:
        s.execute(update(Task).where(Task.status=='running').values(status='queued'))
    stop=asyncio.Event()
    job=asyncio.create_task(worker(stop))
    yield
    stop.set()
    await job

app=FastAPI(title='筑序工程协同 Agent',lifespan=lifespan)

@app.middleware('http')
async def origin_guard(request,call_next):
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and origin!=str(request.base_url).rstrip('/'):
        return Response('跨域写入已拒绝',status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api/') else 'no-cache'
    return response


def auth(request:Request):
    token=request.cookies.get(settings().session_cookie_name,'')
    with db() as s:
        session=s.get(LoginSession,hashlib.sha256(token.encode()).hexdigest())
        if not session or session.expires<int(time.time()):
            raise HTTPException(401,'请先登录')
        user=s.get(User,session.user_id)
        if request.method not in ('GET','HEAD','OPTIONS') and not secrets.compare_digest(request.headers.get('x-csrf-token',''),session.csrf):
            raise HTTPException(403,'CSRF 校验失败，请重新登录')
        return {'id':user.id,'name':user.name,'role':user.role,'role_name':ROLE_NAMES[user.role],'csrf':session.csrf}


def require_project(user,project_id):
    with db() as s:
        found=s.scalar(select(Membership).where(Membership.user_id==user['id'],Membership.project_id==project_id))
        if not found:
            raise HTTPException(403,'没有此项目的权限')


def get_task(id,user):
    with db() as s:
        t=s.get(Task,id)
        if not t:
            raise HTTPException(404,'任务不存在')
    require_project(user,t.project_id)
    return t

class LoginBody(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)

@app.post('/api/auth/login')
def login(body:LoginBody,request:Request,response:Response):
    ip=request.client.host
    login_failures[ip]=[t for t in login_failures[ip] if t>time.time()-300]
    if len(login_failures[ip])>=10:
        raise HTTPException(429,'登录尝试过多，请五分钟后重试')
    with db() as s:
        user=s.get(User,body.username)
        if not user or not verify_password(body.password,user.password_hash):
            login_failures[ip].append(time.time())
            raise HTTPException(401,'账号或密码不正确')
        token=secrets.token_urlsafe(32)
        csrf=secrets.token_urlsafe(24)
        s.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=user.id,csrf=csrf,expires=int(time.time())+settings().session_hours*3600))
        response.set_cookie(settings().session_cookie_name,token,httponly=True,samesite='strict',secure=settings().cookie_secure,max_age=settings().session_hours*3600)
        return {'id':user.id,'name':user.name,'role':user.role,'role_name':ROLE_NAMES[user.role],'csrf':csrf}

@app.post('/api/auth/logout')
def logout(request:Request,response:Response,user=Depends(auth)):
    with db() as s:
        s.execute(delete(LoginSession).where(LoginSession.token_hash==hashlib.sha256(request.cookies[settings().session_cookie_name].encode()).hexdigest()))
    response.delete_cookie(settings().session_cookie_name)
    return {'ok':True}

@app.get('/api/auth/me')
def me(user=Depends(auth)):
    return user

@app.get('/api/config/status')
def config_status(user=Depends(auth)):
    cfg=settings()
    return {'configured':bool(cfg.deepseek_api_key),'model':cfg.deepseek_model,'provider':'DeepSeek','database':'SQLite'}

@app.get('/api/projects')
def projects(user=Depends(auth)):
    with db() as s:
        ids=list(s.scalars(select(Membership.project_id).where(Membership.user_id==user['id'])))
        return [{'id':r.id,**r.data} for r in s.scalars(select(Record).where(Record.kind=='project',Record.project_id.in_(ids)))]

@app.get('/api/projects/{project_id}/records')
def records(project_id:str,user=Depends(auth)):
    require_project(user,project_id)
    with db() as s:
        return [{'id':r.id,'kind':r.kind,'version':r.version,'data':r.data} for r in s.scalars(select(Record).where(Record.project_id==project_id))]

class RecordEdit(BaseModel):
    version: int
    data: dict

@app.put('/api/records/{id}')
def edit_record(id:str,body:RecordEdit,user=Depends(auth)):
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        r=s.get(Record,id)
        if not r:
            raise HTTPException(404,'记录不存在')
        require_project(user,r.project_id)
        if {'schedule':'project_manager','quote':'procurement','budget':'finance'}.get(r.kind)!=user['role']:
            raise HTTPException(403,'此岗位不能直接修改该类记录')
        if r.version!=body.version:
            raise HTTPException(409,'数据版本已变化，请刷新')
        # Keep MVP record schemas intact and types stable.
        if set(body.data)!=set(r.data) or any(type(body.data[k]) is not type(v) for k,v in r.data.items()):
            raise HTTPException(422,'字段和类型必须与原记录一致')
        if any(isinstance(v,int) and v<0 for v in body.data.values()):
            raise HTTPException(422,'金额和数量不能为负数')
        data=body.data
        if r.kind=='quote' and data.get('pricing_mode')=='unit_price_plus_fees':
            order=s.scalar(select(Record).where(Record.project_id==r.project_id,Record.kind=='order'))
            try:
                data={**data,**quote_cost(data,order.data)}
            except (ValueError,KeyError):
                raise HTTPException(422,'报价数量或金额无效')
        r.data=data
        r.version+=1
        s.add(Event(task_id=None,kind='record_updated',role=user['role'],message=f'{user["name"]} 更新 {id}',data={'id':id,'version':r.version}))
        return {'id':r.id,'version':r.version}

class CreateTask(BaseModel):
    title: str = Field(min_length=1,max_length=120)
    message: str = Field(min_length=1,max_length=12000)
    scenario: Literal['delivery_delay','material_purchase','payment_review']='delivery_delay'
    document_ids: list[str] = Field(default_factory=list,max_length=10)

@app.post('/api/projects/{project_id}/tasks',status_code=202)
def create_task(project_id:str,body:CreateTask,request:Request,user=Depends(auth)):
    require_project(user,project_id)
    key=request.headers.get('idempotency-key')
    if key and len(key)>200:
        raise HTTPException(422,'幂等键过长')
    task_id='WF-'+digest({'user':user['id'],'project':project_id,'key':key})[:24] if key else uid('WF-')
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        existing=s.get(Task,task_id)
        if existing:
            if (existing.title,existing.message,existing.scenario,existing.document_ids)!=(body.title.strip(),body.message.strip(),body.scenario,body.document_ids):
                raise HTTPException(409,'相同幂等键不能用于不同任务')
            return {'id':existing.id,'status':existing.status}
        for id in body.document_ids:
            doc=s.get(Document,id)
            if not doc or doc.project_id!=project_id:
                raise HTTPException(422,'附件不存在或不属于此项目')
        t=Task(id=task_id,project_id=project_id,owner_id=user['id'],scenario=body.scenario,title=body.title.strip(),message=body.message.strip(),document_ids=body.document_ids)
        if not t.title or not t.message:
            raise HTTPException(422,'标题和描述不能为空')
        s.add(t)
        s.flush()
        s.add(Event(task_id=t.id,kind='created',role=user['role'],message=user['name']+' 发起协同任务',data={'scenario':t.scenario,'workflow_version':2}))
        return {'id':t.id,'status':'queued'}

@app.get('/api/tasks')
def list_tasks(user=Depends(auth)):
    with db() as s:
        ids=list(s.scalars(select(Membership.project_id).where(Membership.user_id==user['id'])))
        return [{'id':t.id,'title':t.title,'scenario':t.scenario,'status':t.status,'created':t.created,'owner_id':t.owner_id} for t in s.scalars(select(Task).where(Task.project_id.in_(ids)).order_by(Task.created.desc()))]

@app.get('/api/tasks/{id}')
def task_detail(id:str,user=Depends(auth)):
    t=get_task(id,user)
    with db() as s:
        p=s.scalar(select(Proposal).where(Proposal.task_id==id))
        ev=list(s.scalars(select(Event).where(Event.task_id==id).order_by(Event.id)))
        result=s.scalar(select(Execution).where(Execution.task_id==id))
        return {'id':t.id,'title':t.title,'message':t.message,'scenario':t.scenario,'status':t.status,'error':t.error,'owner_id':t.owner_id,'events':[event_dict(e) for e in ev],'proposal':{'id':p.id,'version':p.version,'digest':p.digest,'data':p.data,'decision':p.decision,'comment':p.comment,'selected':p.selected} if p else None,'outcome':result.result if result else None,'pending':pending_interrupt(id) if t.status in ('waiting_input','waiting_approval','waiting_revision','waiting_manager') else None}


def event_dict(e):
    return {'id':e.id,'kind':e.kind,'role':e.role,'message':e.message,'data':e.data,'created':e.created}

@app.get('/api/tasks/{id}/events')
async def task_events(id:str,request:Request,user=Depends(auth)):
    get_task(id,user)
    async def stream():
        last=int(request.headers.get('last-event-id','0') or 0)
        for _ in range(300):
            if await request.is_disconnected():
                break
            with db() as s:
                events=list(s.scalars(select(Event).where(Event.task_id==id,Event.id>last).order_by(Event.id)))
                t=s.get(Task,id)
                status=t.status
            for ev in events:
                last=ev.id
                yield f'id: {ev.id}\nevent: activity\ndata: {json.dumps(event_dict(ev),ensure_ascii=False)}\n\n'
            yield f'event: status\ndata: {json.dumps({"status":status})}\n\n'
            if status in ('completed','failed','waiting_input','waiting_approval','waiting_revision','waiting_manager'):
                break
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream')

class ManagerResponse(BaseModel):
    impact: Literal['none','at_risk','blocked']
    direction: Literal['accept_split','expedite','transfer']
    required_date: date | None = None
    required_quantity: int | None = Field(default=None,gt=0)
    comment: str = Field(min_length=1,max_length=2000)

@app.post('/api/tasks/{id}/manager-response')
def manager_response(id:str,body:ManagerResponse,user=Depends(auth)):
    get_task(id,user)
    if user['role']!='project_manager':
        raise HTTPException(403,'请由项目经理确认工程影响和处理方向')
    if not body.comment.strip():
        raise HTTPException(422,'请填写现场判断及处理要求')
    if bool(body.required_date)!=bool(body.required_quantity):
        raise HTTPException(422,'数量和最晚到货日期请同时填写')
    if body.direction!='accept_split' and (not body.required_date or not body.required_quantity):
        raise HTTPException(422,'加急或调货请明确所需数量和最晚到货日期')
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        t=s.get(Task,id)
        if t.status!='waiting_manager':
            raise HTTPException(409,'当前不处于等待项目经理回复的阶段')
        order=s.get(Record,'PO001')
        if body.required_quantity and body.required_quantity>order.data['quantity']:
            raise HTTPException(422,'需求数量不能超过当前订单总量')
        reply={**body.model_dump(mode='json'),'responded_by':user['id']}
        t.resume=reply
        t.status='queued'
        s.add(Event(task_id=id,kind='manager_response',role=user['role'],message=user['name']+' 回复了工程影响与采购处理要求',data=reply))
    return {'status':'queued'}

class QuoteBody(BaseModel):
    quantity: int = Field(gt=0,le=200)
    supplier: str = Field(min_length=1,max_length=100)
    material: str = Field(min_length=1,max_length=200)
    unit_price_cents: int = Field(ge=0)
    additional_cents: int = Field(ge=0)
    valid_until: date
    available_date: date
    fulfillment_type: Literal['split','expedite','transfer']
    terms: str = Field(min_length=1,max_length=2000)

@app.post('/api/projects/{project_id}/quotes',status_code=201)
def add_quote(project_id:str,body:QuoteBody,user=Depends(auth)):
    require_project(user,project_id)
    if user['role']!='procurement':
        raise HTTPException(403,'只有采购可以登记报价')
    with db() as s:
        data=body.model_dump(mode='json')
        data.update(pricing_mode='unit_price_plus_fees',service_fee_cents=body.additional_cents)
        order=s.scalar(select(Record).where(Record.project_id==project_id,Record.kind=='order'))
        try:
            data.update(quote_cost(data,order.data))
        except ValueError:
            raise HTTPException(422,'报价数量超出订单范围')
        record=Record(id=uid('Q-'),project_id=project_id,kind='quote',data=data)
        s.add(record)
        s.flush()
        s.add(Event(task_id=None,kind='quote_created',role=user['role'],message=user['name']+' 登记有效报价',data={'id':record.id}))
        return {'id':record.id,'version':1}


class ProcurementReply(QuoteBody):
    quantity: int = Field(gt=0,le=200)
    note: str = Field(default='',max_length=2000)

@app.post('/api/tasks/{id}/procurement-reply')
def procurement_reply(id:str,body:ProcurementReply,user=Depends(auth)):
    task=get_task(id,user)
    if user['role']!='procurement':
        raise HTTPException(403,'请由采购确认货源和报价')
    pending=pending_interrupt(id)
    if task.scenario!='delivery_delay' or task.status!='waiting_input' or not pending or pending.get('role')!='procurement':
        raise HTTPException(409,'当前不是等待采购补充的阶段，请刷新任务')
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        task=s.get(Task,id)
        if task.status!='waiting_input':
            raise HTTPException(409,'任务已被处理，请刷新')
        quote_id=uid('Q-')
        quote=body.model_dump(mode='json',exclude={'note'})
        quote.update(pricing_mode='unit_price_plus_fees',service_fee_cents=body.additional_cents)
        order=s.scalar(select(Record).where(Record.project_id==task.project_id,Record.kind=='order'))
        try:
            quote.update(quote_cost(quote,order.data))
        except ValueError:
            raise HTTPException(422,'报价数量超出订单范围')
        s.add(Record(id=quote_id,project_id=task.project_id,kind='quote',data=quote))
        message=(f'采购已登记供应商确认的报价 {quote_id}：{body.supplier}，{body.quantity}米，'
                 f'承诺{body.available_date}到货，单价{body.unit_price_cents}分/米，'
                 f'加急/调货及运费{body.additional_cents}分，材料价差{quote["price_difference_cents"]}分，增量合计{quote["additional_cents"]}分，报价有效至{body.valid_until}。'
                 f'交付条款：{body.terms}。补充说明：{body.note or "无"}。'
                 '本回复仅确认货源与报价，不表示费用已获批准、货物已验收或原订单已变更。')
        task.resume={'message':message}
        task.status='queued'
        s.add(Event(task_id=id,kind='human_input',role=user['role'],message=user['name']+' 已确认货源和报价',data={'quote_id':quote_id,'quantity':body.quantity,'available_date':str(body.available_date),'additional_cents':quote['additional_cents']}))
    return {'status':'queued','quote_id':quote_id}


class InputBody(BaseModel):
    message: str = Field(min_length=1,max_length=12000)

@app.post('/api/tasks/{id}/inputs')
def task_input(id:str,body:InputBody,user=Depends(auth)):
    task=get_task(id,user)
    pending=pending_interrupt(id) if task.status=='waiting_input' else None
    assigned_role=pending.get('role') if pending else None
    if assigned_role and assigned_role!=user['role']:
        raise HTTPException(403,'这项补充需要由'+ROLE_NAMES.get(assigned_role,assigned_role)+'处理')
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        t=s.get(Task,id)
        if t.status not in ('waiting_input','waiting_revision'):
            raise HTTPException(409,'任务当前不接受补充资料')
        if t.status=='waiting_revision' and user['role']!='procurement':
            raise HTTPException(403,'退回方案须由采购补充后重新提交')
        t.resume={'message':body.message}
        t.status='queued'
        s.add(Event(task_id=id,kind='human_input',role=user['role'],message=user['name']+' 补充资料',data={'message':body.message}))
    return {'status':'queued'}

@app.post('/api/tasks/{id}/retry')
def retry_task(id:str,user=Depends(auth)):
    get_task(id,user)
    with db() as s:
        t=s.get(Task,id)
        if t.status!='failed':
            raise HTTPException(409,'只允许重试失败的任务')
        last_error=s.scalar(select(Event).where(Event.task_id==id,Event.kind=='error').order_by(Event.id.desc()))
        business_refresh=(last_error and last_error.data.get('error_type')=='WorkflowError') or any(word in (t.error or '') for word in ['已变化','未满足','报价已过期','预算不足'])
        if not business_refresh:
            t.status='queued'
            t.error=None
            s.add(Event(task_id=id,kind='retry',role=user['role'],message=user['name']+' 重试当前步骤',data={}))
            return {'id':t.id,'status':'queued'}
        # Invalidated business facts require a fresh analysis and approval.
        new=Task(id=uid('WF-'),project_id=t.project_id,owner_id=user['id'],scenario=t.scenario,title=t.title+' · 重试',message=t.message,document_ids=t.document_ids)
        s.add(new)
        s.flush()
        s.add(Event(task_id=new.id,kind='created',role=user['role'],message=user['name']+' 重新发起协同任务',data={'scenario':new.scenario,'workflow_version':2}))
        return {'id':new.id,'status':'queued'}

class DecisionBody(BaseModel):
    decision: Literal['approved','returned']
    version: int
    digest: str
    selected: int = Field(default=0,ge=0)
    comment: str = Field(default='',max_length=2000)

@app.post('/api/approvals/{id}/decisions')
def decide(id:str,body:DecisionBody,user=Depends(auth)):
    with db() as s:
        s.connection().exec_driver_sql('BEGIN IMMEDIATE')
        p=s.get(Proposal,id)
        if not p:
            raise HTTPException(404,'方案不存在')
        t=s.get(Task,p.task_id)
        require_project(user,t.project_id)
        required='finance' if t.scenario=='payment_review' else 'project_manager'
        if user['role']!=required:
            raise HTTPException(403,'当前岗位没有本方案审批权')
        if p.version!=body.version or p.digest!=body.digest or digest(p.data)!=p.digest:
            raise HTTPException(409,'方案版本已变化，请重新查看')
        if p.decision or t.status!='waiting_approval':
            raise HTTPException(409,'方案已经处理或暂不可审批')
        if body.selected>=len(p.data['options']):
            raise HTTPException(422,'所选方案不存在')
        if body.decision=='returned' and not body.comment.strip():
            raise HTTPException(422,'退回须填写原因')
        if body.decision=='approved':
            for rid,version in p.data['record_versions'].items():
                record=s.get(Record,rid)
                if not record or record.version!=version:
                    raise HTTPException(409,'业务资料已变化，请退回并重新分析')
            if not p.data['options'][body.selected].get('within_budget',False):
                raise HTTPException(409,'方案超预算，不能批准')
        p.decision=body.decision
        p.decision_by=user['id']
        p.comment=body.comment
        p.selected=body.selected
        t.resume={'proposal_id':p.id,'digest':p.digest,'decision':body.decision}
        t.status='queued'
        s.add(Event(task_id=t.id,kind='human_decision',role=user['role'],message=user['name']+(' 批准方案' if body.decision=='approved' else ' 退回方案'),data={'comment':body.comment,'version':p.version,'selected':body.selected}))
    return {'status':'queued'}

@app.post('/api/documents')
async def upload(project_id:str='P001',file:UploadFile=File(...),user=Depends(auth)):
    require_project(user,project_id)
    raw=await file.read(settings().max_upload_mb*1024*1024+1)
    if len(raw)>settings().max_upload_mb*1024*1024:
        raise HTTPException(413,'文件超过上传限制')
    name=Path(file.filename or 'document').name
    ext=Path(name).suffix.lower()
    try:
        if ext in ('.txt','.md','.csv'):
            text=raw.decode('utf-8-sig')[:100000]
        elif ext=='.pdf':
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(raw))
            if len(reader.pages)>80:
                raise ValueError('页数超过80')
            text='\n'.join(f'[第 {i+1} 页]\n{page.extract_text() or ""}' for i,page in enumerate(reader.pages))[:100000]
        elif ext=='.xlsx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(info.file_size for info in archive.infolist())>30*1024*1024:
                    raise ValueError('解压后文件过大')
            from openpyxl import load_workbook
            wb=load_workbook(io.BytesIO(raw),read_only=True,data_only=True)
            chunks=[]
            for sheet in wb.worksheets[:10]:
                chunks.append('[工作表 '+sheet.title+']')
                for row in sheet.iter_rows(max_row=min(sheet.max_row or 1000,1000),max_col=30,values_only=True):
                    chunks.append('\t'.join('' if v is None else str(v) for v in row))
            wb.close()
            text='\n'.join(chunks)[:100000]
        else:
            raise HTTPException(422,'支持文本 PDF、XLSX、TXT、MD、CSV；扫描件暂不支持')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422,'文件无法解析，请检查格式或改用文本文件')
    if not text.strip():
        raise HTTPException(422,'未提取到文字，扫描件请先转为文本')
    id=uid('DOC-')
    path=settings().data_dir/'uploads'/(id+ext)
    path.write_bytes(raw)
    with db() as s:
        s.add(Document(id=id,project_id=project_id,name=name,path=str(path),text=text,digest=hashlib.sha256(raw).hexdigest()))
    return {'id':id,'name':name,'characters':len(text)}

@app.get('/api/documents/{id}')
def document(id:str,user=Depends(auth)):
    with db() as s:
        doc=s.get(Document,id)
        if not doc:
            raise HTTPException(404,'附件不存在')
        require_project(user,doc.project_id)
        return {'id':doc.id,'name':doc.name,'text':doc.text}

@app.get('/')
def index():
    return FileResponse(ROOT/'web'/'index.html')

app.mount('/assets',StaticFiles(directory=ROOT/'web'/'assets',check_dir=False),name='assets')
