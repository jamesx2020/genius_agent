import React,{useState} from 'react';
import ManagerResponse from './ManagerResponse.jsx';
import ProcurementReply from './ProcurementReply.jsx';
import RoleAnalysis from './RoleAnalysis.jsx';
const roles={project_manager:'项目经理',procurement:'采购',finance:'财务'};
const statuses={queued:'等待处理',running:'Agent 正在处理',waiting_manager:'待项目经理确认',waiting_input:'待补充',waiting_revision:'待采购修改',waiting_approval:'待审批',completed:'已完成',failed:'处理失败'};
const directions={accept_split:'接受分批交货',expedite:'供应商加急',transfer:'其他来源调货'};
const money=n=>'¥'+((n||0)/100).toLocaleString('zh-CN',{minimumFractionDigits:2});
export default function TaskWorkspace({task,user,busy,records,onInput,onRetry,onDecision,onEvidence,onManagerResponse,onProcurementReply}){
 const [history,setHistory]=useState(false);
 const resultEvents=Object.values(task.events.filter(e=>e.kind==='role_result').reduce((all,e)=>({...all,[e.role]:e}),{}));
 const managerEvent=task.events.filter(e=>e.kind==='manager_response').at(-1);
 const instruction=managerEvent?.data||task.proposal?.data?.manager_instruction;
 const assigned=task.status==='waiting_revision'?'procurement':task.pending?.role;
 const steps=['采购通知','现场判断','落实货源','方案审批','完成'];
 const step=task.status==='completed'?4:task.status==='waiting_manager'?1:task.status==='waiting_approval'?3:instruction||assigned==='procurement'||assigned==='finance'?2:1;

 const latest=task.events.filter(e=>e.kind==='agent_started').at(-1);
 return <div className="task-workspace"><div className="workspace-title"><div><h1>{task.title}</h1><p>PO001 · {records.find(r=>r.id==='PO001')?.data?.material||'项目协同'}</p></div><span className={'badge '+(task.status==='completed'?'green':task.status==='failed'?'purple':'blue')}>{statuses[task.status]}</span></div>{task.scenario==='delivery_delay'&&<ol className="task-steps">{steps.map((label,i)=><li key={label} className={i===step?'current':i<step?'done':''}><span>{i<step?'✓':i+1}</span>{label}</li>)}</ol>}
 {instruction?.direction&&task.status!=='waiting_manager'&&<div className="context-line"><span>项目经理要求</span><b>{directions[instruction.direction]}</b>{instruction.required_quantity&&<> · {instruction.required_quantity}米，{instruction.required_date}前到场</>}</div>}
 {task.status==='waiting_manager'&&(user.role==='project_manager'?<ManagerResponse user={user} busy={busy} onSubmit={onManagerResponse}/>:<Waiting role="项目经理"/>)}
 {task.scenario==='delivery_delay'&&task.status==='waiting_input'&&assigned==='procurement'&&user.role==='procurement'&&<ProcurementReply key={task.id} instruction={instruction} records={records} busy={busy} onSubmit={onProcurementReply}/>}
 {['waiting_input','waiting_revision'].includes(task.status)&&!(task.scenario==='delivery_delay'&&task.status==='waiting_input'&&assigned==='procurement'&&user.role==='procurement')&&(assigned&&assigned!==user.role?<Waiting role={roles[assigned]}/>:<QuestionReply key={task.id+'-'+task.events.filter(e=>e.kind==='human_input').length} task={task} busy={busy} onSubmit={onInput}/>)}
 {task.status==='waiting_approval'&&<Approval task={task} user={user} busy={busy} onDecision={onDecision}/>}
 {['queued','running'].includes(task.status)&&<section className="card current-action processing"><span className="processing-dot"/><div><h2>{latest?`${roles[latest.role]||'岗位'} Agent 正在处理`:'回复已收到，正在安排处理'}</h2><p>页面会自动更新，暂时不需要你操作。</p></div></section>}
 {task.status==='failed'&&<section className="card current-action"><div className="action-heading"><div className="action-eyebrow">需要处理</div><h2>这次处理没有完成</h2><p>{task.error||'请检查配置或资料后重试。'}</p></div><div className="action-content"><button className="primary" disabled={busy} onClick={onRetry}>重试处理</button></div></section>}
 {task.status==='completed'&&<section className="card current-action"><div className="action-heading"><div className="action-eyebrow">已完成</div><h2>{task.outcome?.message||'任务处理完成'}</h2><p>{task.outcome?.order_id?`订单 ${task.outcome.order_id} 已更新至 V${task.outcome.version}。`:task.outcome?.draft_id?`申请草稿：${task.outcome.draft_id}`:'处理结果已保存。'}</p></div></section>}
 <div className="workspace-archive"><details><summary>查看原始通知</summary><p className="analysistext">{task.message}</p></details><details><summary>查看 Agent 分析与依据 <span>{resultEvents.length} 个岗位</span></summary>{resultEvents.map(e=><RoleAnalysis key={e.role} event={e} onEvidence={onEvidence}/>)}</details><details onToggle={e=>setHistory(e.currentTarget.open)}><summary>查看处理记录 <span>{task.events.length} 条</span></summary>{history&&task.events.map(e=><div className="compact-event" key={e.id}><small>{new Date(e.created*1000).toLocaleTimeString()} · {roles[e.role]||'系统'}</small><p>{e.kind==='role_result'?`${roles[e.role]} Agent 已提交${e.data.result.status==='completed'?'分析':'待确认事项'}`:e.message}</p>{Object.keys(e.data).length>0&&<details><summary>详细记录</summary><pre className="jsonpreview">{JSON.stringify(e.data,null,2)}</pre></details>}</div>)}</details></div>
 </div>
}
function Waiting({role}){return <section className="card current-action"><div className="action-heading"><div className="action-eyebrow">等待其他岗位</div><h2>等待{role||'相关人员'}处理</h2><p>这一步不需要你填写。对方处理后，任务会自动继续。</p></div></section>}
function QuestionReply({task,busy,onSubmit}){
 const questions=task.status==='waiting_revision'?[task.proposal?.comment||'请补充修改内容']:task.pending?.questions||['请补充当前需要确认的事实'];
 const [answers,setAnswers]=useState({}),[error,setError]=useState(''),[sending,setSending]=useState(false);
 async function submit(e){e.preventDefault();if(sending||busy)return;if(questions.some((_,i)=>!answers[i]?.trim())){setError('请填写每项已确认的信息；未确认的请填写“待确认”及原因。');return}setSending(true);setError('');try{const ok=await onSubmit(questions.map((q,i)=>q+'\n答：'+answers[i].trim()).join('\n\n'));if(ok===false)throw new Error('提交未成功，请重试。')}catch(e){setError(e.message)}finally{setSending(false)}}
 return <section className="card current-action"><div className="action-heading"><div className="action-eyebrow">轮到你了</div><h2>{task.status==='waiting_revision'?'补充修改内容':'确认以下信息'}</h2><p>逐项回答即可，不需要整理成报告。</p></div><div className="action-content"><form onSubmit={submit}>{questions.map((question,i)=><div className="field" key={i}><label htmlFor={'answer-'+i}>{i+1}. {question}</label><textarea id={'answer-'+i} rows="2" value={answers[i]||''} onChange={e=>{setAnswers({...answers,[i]:e.target.value});setError('')}} placeholder="填写已确认的事实；未确认请说明原因。" required/></div>)}{error&&<div className="submit-error" role="alert">{error}</div>}<div className="action-bottom"><span>只回答本岗位需要确认的信息。</span><button className="primary" disabled={sending||busy}>{sending?'正在提交…':'提交，继续处理'}</button></div></form></div></section>
}
function Approval({task,user,busy,onDecision}){
 const [selected,setSelected]=useState(0),[comment,setComment]=useState(''),[returning,setReturning]=useState(false);
 const role=task.scenario==='payment_review'?'finance':'project_manager';const p=task.proposal;
 if(user.role!==role)return <Waiting role={roles[role]}/>;
 if(!p)return <Waiting role="系统生成方案"/>;
 return <section className="card current-action"><div className="action-heading"><div className="action-eyebrow">轮到你了 · {roles[role]}</div><h2>{task.scenario==='payment_review'?'确认付款申请草稿':task.scenario==='material_purchase'?'确认采购申请草稿':'确认采购处理方案'}</h2><p>选择一个方案，核对新增费用后批准。</p></div><div className="action-content">{p.data.options.map((option,i)=><label className={'compact-option '+(i===selected?'selected':'')} key={i}><input type="radio" name="option" checked={i===selected} onChange={()=>setSelected(i)}/><div><b>{option.title}</b>{option.deliveries&&<p>{option.deliveries.map(d=>`${d.date} 到 ${d.quantity}米`).join('；')}</p>}{option.pricing_mode==='unit_price_plus_fees'&&<p>材料价差 {money(option.price_difference_cents)} ＋ 加急 / 运费 {money(option.service_fee_cents)}</p>}</div><strong>{money(option.additional_cents)}<small>新增费用</small></strong></label>)}<details className="subtle-details"><summary>查看所选方案说明</summary><p className="notice">{p.data.options[selected]?.reason}</p></details>{returning&&<div className="field"><label htmlFor="return-comment">需要采购修改什么</label><textarea id="return-comment" value={comment} onChange={e=>setComment(e.target.value)} rows="2" placeholder="例如：请补充供应商确认的到货日期。" autoFocus/></div>}<div className="action-bottom"><button className="textbtn" onClick={()=>{setReturning(!returning);setComment('')}}>{returning?'取消退回':'退回修改'}</button>{returning?<button className="primary" disabled={busy||!comment.trim()} onClick={()=>onDecision('returned',selected,comment)}>退回采购</button>:<button className="primary" disabled={busy||!p.data.options[selected]?.within_budget} onClick={()=>onDecision('approved',selected,'')}>批准方案</button>}</div>{!p.data.options[selected]?.within_budget&&<p className="field-error">该方案超出预算，需调整后再审批。</p>}</div></section>
}
