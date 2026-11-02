import React from 'react';
import { splitLegacyText } from './reportUtils.js';

const roleNames={project_manager:'项目经理',procurement:'采购',finance:'财务'};
const impactLabels={no_delay:'预计不影响工期',at_risk:'存在工期风险',delayed:'预计影响工期',undetermined:'工期影响待确认'};

function Sources({ids=[],onEvidence}){
  return <div className="report-sources">{[...new Set(ids)].map(id=><button key={id} className="evidence" onClick={()=>onEvidence(id)}>↗ {id}</button>)}</div>;
}
function Section({number,title,children}){
  return <section className="report-section"><h3><span>{number}</span>{title}</h3>{children}</section>;
}
export default function RoleAnalysis({event,onEvidence}){
  let section=0;
  const result=event.data.result;
  const report=event.role==='project_manager'?result.report:null;
  return <article className="card space role-report"><div className="sectionhead"><div><h2>{roleNames[event.role]} Agent</h2><p className="report-caption">{event.role==='project_manager'?'施工与工期评估':'岗位分析结果'}</p></div><span className={'badge '+(result.status==='completed'?'green':'amber')}>{result.status==='completed'?'本岗位分析完成':'需要确认业务事实'}</span></div>
    <div className="panelbody">{report?<>
      <section className={'report-conclusion '+(report.schedule_impact==='no_delay'?'positive':'attention')}><div className="report-kicker">分析结论</div><p>{report.conclusion}</p><span className="report-impact">{impactLabels[report.schedule_impact]||'工期影响待确认'}</span></section>
      <Section number={String(++section).padStart(2,'0')} title="对工期有什么影响"><p className="report-body">{report.impact_explanation}</p></Section>
      <Section number={String(++section).padStart(2,'0')} title="判断依据">{report.facts?.length?<div className="report-facts">{report.facts.map((fact,i)=><div className="report-fact" key={i}><b>{fact.label}</b><div><p>{fact.detail}</p><Sources ids={fact.evidence_ids} onEvidence={onEvidence}/></div></div>)}</div>:<p className="report-muted">本次未单独列出事实明细，请查看下方引用资料。</p>}</Section>
      {!!report.risks?.length&&<Section number={String(++section).padStart(2,'0')} title="风险与未确认事项"><ul className="report-list report-risk">{report.risks.map((risk,i)=><li key={i}>{risk}</li>)}</ul></Section>}
      {!!result.missing_inputs?.length&&<Section number={String(++section).padStart(2,'0')} title="需要你确认什么"><ol className="report-list">{result.missing_inputs.map((question,i)=><li key={i}>{question}</li>)}</ol><p className="report-muted">请在下方“补充资料”填写这些问题的答案。</p></Section>}
      {!!report.handoffs?.length&&<Section number={String(++section).padStart(2,'0')} title="后续岗位需要处理"><div className="report-handoffs">{report.handoffs.map((handoff,i)=><div key={i}><span className={'badge '+(handoff.role==='finance'?'purple':'blue')}>{roleNames[handoff.role]}</span><p>{handoff.task}</p></div>)}</div><p className="report-muted">以上为后续处理建议，实际派发和完成状态以执行轨迹为准。</p></Section>}
      <div className="report-next"><b>下一步</b><p>{report.next_step}</p></div>
    </>:<>
      {event.role==='project_manager'&&<p className="report-muted">这是一条旧版回复，以下按原文分段展示；未补写新的分析结论。</p>}
      <Section number={String(++section).padStart(2,'0')} title={event.role==='project_manager'?'原回复说明':'分析结论'}><div className="report-body">{splitLegacyText(result.summary).map((paragraph,i)=><p key={i}>{paragraph}</p>)}</div></Section>
      {!!result.findings?.length&&<Section number={String(++section).padStart(2,'0')} title="关键发现"><ul className="report-list">{result.findings.map((finding,i)=><li key={i}>{finding}</li>)}</ul></Section>}
      {!!result.missing_inputs?.length&&<Section number={String(++section).padStart(2,'0')} title="待确认事项"><ol className="report-list">{result.missing_inputs.map((question,i)=><li key={i}>{question}</li>)}</ol></Section>}
    </>}
      {!!result.evidence_ids?.length&&<details className="report-references"><summary>引用资料 · {[...new Set(result.evidence_ids)].length} 项</summary><Sources ids={result.evidence_ids} onEvidence={onEvidence}/></details>}
    </div></article>;
}
