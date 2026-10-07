const app = document.querySelector('#app');
const dialog = document.querySelector('#detail');
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = v => typeof v === 'number' ? `$${v.toFixed(2)}` : '—';
let data, phase = 'idle', completed = 0, selected = null, timer = null, fixed = new Set(), fixing = new Set(), fixTimers = new Set();
let entries = [];
let motionCleanup = () => {};
let reportView = false, selectedModel = null;
const inputFor = id => data.inputs.find(x => x.id === id);
const causeFor = id => data.causes.find(x => x.id === id);
const isFixed = r => r.status === 'regressed' && fixed.has(r.cause_id);
const unresolved = () => entries.filter(({r}) => r.status === 'regressed' && !isFixed(r));
function validate(d) {
 if (d?._format === 'ner' && d.ner && d.candidates?.length) return;
 if (!d || !d.repo || !d.baseline || !d.prompt || !Array.isArray(d.inputs) || !Array.isArray(d.candidates) || !Array.isArray(d.causes) || !d.pr) throw Error('The report is missing required fields.');
 if (!d.inputs.length || !d.candidates.length) throw Error('The report must include at least one input and model.');
 const ids = new Set(d.inputs.map(x=>x.id));
 d.candidates.forEach(c=>{if(!c.summary || !Array.isArray(c.results))throw Error('A model is missing its summary or results.');c.results.forEach(r=>{if(!ids.has(r.input_id))throw Error('A result references an unknown input.');if(r.status==='regressed'&&!d.causes.some(x=>x.id===r.cause_id))throw Error('A failed result has no matching cause.');});});
}
function normalizeNER(raw) {
 const pct = n => typeof n === 'number' ? `${(n*100).toFixed(1)}%` : '—';
 const costChange = typeof raw.baseline.cost_per_1k_calls_usd === 'number' && typeof raw.candidate.cost_per_1k_calls_usd === 'number'
   ? (raw.candidate.cost_per_1k_calls_usd / raw.baseline.cost_per_1k_calls_usd - 1) * 100 : null;
 const items = raw.items.map(item => ({
   id:item.input_id, text:item.text, verdict:item.verdict, tricky:item.tricky,
   baseline:item.baseline, candidate:item.candidate, regressions:item.regressions,
   improvements:item.improvements, intermittent_failures:item.intermittent_failures
 }));
 const count = status => items.filter(item => item.verdict === status).length;
 const baselineMs = raw.baseline.latency_p50_s * 1000, candidateMs = raw.candidate.latency_p50_s * 1000;
 const normalized = {
   _format:'ner', _note:`NER comparison · ${raw.baseline_file} → ${raw.candidate_file}`,
   run_id: raw.generated_at || 'Recorded report',
   repo:{name:`${String(raw.task).toUpperCase()} entity extraction`,call_site:'Baseline → candidate comparison'},
   baseline:{model:raw.baseline.model,retires_on:null,runs_per_input:raw.baseline.outputs / Math.max(1,raw.items.length),noise_rate:raw.candidate.unstable_inputs / Math.max(1,raw.items.length),cost_per_1k_calls_usd:raw.baseline.cost_per_1k_calls_usd,latency_p50_ms:baselineMs,latency_p95_ms:raw.baseline.latency_p95_s*1000,accuracy:raw.baseline.accuracy,accuracy_label:pct(raw.baseline.accuracy),accuracy_metric:raw.baseline.accuracy_metric,checks:raw.baseline.checks},
   inputs:items.map(item=>({id:item.id,text:item.text})), causes:[], pr:null, ner:{...raw,items},
   candidates:[{model:raw.candidate.model,provider:'Candidate',summary:{total:items.length,passed:count('SAME')+count('IMPROVED'),passed_after_fix:count('SAME')+count('IMPROVED'),cost_change_pct:costChange,cost_per_1k_calls_usd:raw.candidate.cost_per_1k_calls_usd,latency_p50_ms:candidateMs,latency_p95_ms:raw.candidate.latency_p95_s*1000,speed_vs_baseline:baselineMs/candidateMs,recommended:raw.ready_to_merge,accuracy:raw.candidate.accuracy,accuracy_label:pct(raw.candidate.accuracy),verdict_counts:raw.verdict_counts},results:items.map(item=>({input_id:item.id,status:item.verdict==='REGRESSED'?'regressed':item.verdict==='CHANGED'?'changed':item.verdict==='IMPROVED'?'improved':'pass',score:item.candidate.score_runs.reduce((a,b)=>a+b,0)/Math.max(1,item.candidate.score_runs.length),runs:{passed:item.candidate.score_runs.filter(x=>x===1).length,total:item.candidate.score_runs.length},baseline_output:item.baseline.output.join('\n'),output:item.candidate.output.join('\n'),cause_id:null,after_fix_output:null,ner_item:item}))}]
 };
 return normalized;
}
// ---- Inside the pipeline: the 4 steps, every tested ticket, one ticket's journey, the proof, the line, the fix ----
const decisionPart=v=>String(v??'').split(' — ')[0];
const VERDICT={REGRESSED:['bad','broken'],CHANGED:['chg','changed'],IMPROVED:['ok','improved'],SAME:['ok','as before']};
function pipelineSection(p,ti){
 const t=p.tickets[ti]||p.tickets[0];if(!t)return'';
 const steps=p.steps,cause=new Set(p.causal_steps),planted=new Set(p.planted_steps||[]),lineStep=p.lines?.[0]?.step;
 const broken=p.tickets.filter(x=>x.verdict==='REGRESSED').length;
 const node=(s,i)=>`<div class="pipe-node ${cause.has(s)?'cause':''}"><span class="pipe-num">${i+1}</span><strong>${esc(s)}</strong><small>${esc(p.info[s])}</small><span class="pipe-tags">${planted.has(s)?'<em class="planted">planted here</em>':''}${cause.has(s)?'<em>found here</em>':''}</span></div>`;
 const flow=steps.map((s,i)=>node(s,i)+(i<steps.length-1?'<span class="pipe-arrow" aria-hidden="true">→</span>':'')).join('');
 const map=p.tickets.map((x,i)=>{const [cls]=VERDICT[x.verdict]||['ok'];return `<button class="pipe-tile ${cls} ${i===ti?'on':''}" data-ti="${i}" title="${esc(x.id)}: ${esc(VERDICT[x.verdict]?.[1]||x.verdict)}${x.fixed_verdict?` · with fix: ${esc(VERDICT[x.fixed_verdict]?.[1]||x.fixed_verdict)}`:''}">${esc(x.id)}${x.verdict==='REGRESSED'&&x.fixed_verdict&&x.fixed_verdict!=='REGRESSED'?'<i>fixed</i>':''}</button>`;}).join('');
 const differs=(s,row)=>['classify','decide'].includes(s)&&row[s]!=null&&t.old[s]!=null&&decisionPart(row[s])!==decisionPart(t.old[s]);
 const promptMark=(row,s)=>{const c=t.cells?.[row]?.[s];return c&&(c.added.length||c.removed.length)?'<span class="cell-mark" title="prompt differs from the old pipeline">prompt ±</span>':'';};
 const traceRow=(label,sub,row,out,ok,kind)=>row?`<div class="trace-row ${kind}"><div class="trace-label"><strong>${esc(label)}</strong><small>${esc(sub)}</small></div>${steps.map(s=>`<div class="trace-cell ${cause.has(s)?'cause-col':''} ${kind!=='old'&&differs(s,row)?'changed-cell':''}" tabindex="0" data-row="${kind}" data-step="${esc(s)}" aria-label="${esc(s)}, ${esc(label)}: show prompt, expected and actual answer">${promptMark(kind,s)}${esc(row[s]??'—')}</div>`).join('')}<div class="trace-out ${ok?'ok':'bad'}">${ok?'✓':'✗'} ${esc((out||[]).join?out.join(' · '):out)}</div></div>`:'';
 const head=`<div class="trace-row trace-head"><div></div>${steps.map(s=>`<div class="${cause.has(s)?'cause-col':''}">${esc(s)}</div>`).join('')}<div>final result</div></div>`;
 const verdict=r=>r.kind==='reference'?(r.ok?'✓ correct':'✗ wrong'):r.kind==='swap_back'?(r.ok?'✓ fixed':'✗ still wrong'):(r.ok?'✓ not reproduced':'✗ breaks again');
 const decisive=r=>(r.kind==='swap_back'&&r.ok)||(r.kind==='only_new'&&!r.ok);
 const exps=t.experiments||[];
 const proofHead=`<div class="proof-row proof-head"><span></span><span class="proof-dots">${steps.map(s=>`<b class="${cause.has(s)?'cause':''}">${esc(s)}</b>`).join('')}</span><span></span></div>`;
 const proof=exps.length?proofHead+exps.map(r=>`<div class="proof-row ${r.kind} ${decisive(r)?'decisive':''}"><span class="proof-label">${esc(r.label)}</span><span class="proof-dots">${steps.map(s=>`<i class="${r.models[s]}" title="${esc(s)}: ${esc(r.models[s])}"></i>`).join('')}</span><span class="proof-result ${r.ok?'ok':'bad'}">${verdict(r)} <small>${esc(r.output)}</small></span></div>`).join(''):'';
 const lines=(p.lines||[]).map(l=>`<li class="${l.proven?'proven':''}"><span>${esc(l.text)}</span><b>${l.proven?`removing it fixes ${esc(l.repaired)}`:`${esc(l.repaired)}`}</b></li>`).join('');
 const f=p.fix,bar=(n,cls)=>`<div class="fix-bar ${cls}"><i style="width:${f.total&&n?Math.max(2,100*n/f.total):0}%"></i></div>`;
 const headline=broken?(cause.size?`The new pipeline broke the <span class="pipe-hl">${esc([...cause].join(' + '))}</span> step${cause.size>1?'s':''}.`:`${broken} ticket${broken===1?'':'s'} broke; not yet localized.`):'Nothing broke: every ticket came out as before.';
 const plantedNote=planted.size?`<p class="pipe-planted">Planted in <b>${esc([...planted].join(' + '))}</b> · step-finder found <b>${esc([...cause].join(' + ')||'nothing')}</b>${[...planted].every(s=>cause.has(s))&&[...cause].every(s=>planted.has(s))?' <span class="ok-text">✓ match</span>':''}</p>`:'';
 const noProof=t.verdict==='REGRESSED'?'<p class="pipe-caption">This broken ticket wasn’t among the ones localized (a sample per break), so there are no swap experiments for it.</p>':`<p class="pipe-caption">This ticket ${t.verdict==='SAME'?'came out the same on both pipelines':`is ${esc(VERDICT[t.verdict]?.[1]||t.verdict)}`}, so there was nothing to localize. Hover any cell to see what each step got and said.</p>`;
 return `<section class="viz-card pipe-card" id="pipeline-card" aria-label="Inside the pipeline">
 <div class="viz-title"><div><p class="eyebrow">INSIDE THE PIPELINE</p><h2>${headline}</h2></div><span class="pill">${broken} of ${p.tickets.length} tickets broken</span></div>
 ${plantedNote}
 <div class="pipe-flow">${flow}</div>
 <h3 class="pipe-h">Every ticket tested <small>· click one</small></h3>
 <div class="pipe-map" role="tablist" aria-label="Tickets">${map}</div>
 <p class="pipe-legend"><span class="pipe-tile ok">t01</span> as before <span class="pipe-tile chg">t02</span> changed, not worse <span class="pipe-tile bad">t07</span> broken${p.fix.after!=null?' · <i class="fixed-key">fixed</i> = repaired by the proposed fix':''}</p>
 <p class="pipe-input"><b>${esc(t.id)}</b> “${esc(t.text)}”</p>
 <h3 class="pipe-h">One ticket, step by step <small>· hover a cell for its prompt, expected and actual answer</small></h3>
 <div class="trace">${head}${traceRow('Old pipeline',p.old_label,t.old,t.outputs.old,true,'old')}${traceRow('New pipeline',p.new_label,t.new,t.outputs.new,t.verdict!=='REGRESSED','new')}${t.fixed?traceRow('New + fix',f.change||'',t.fixed,t.outputs.fixed||[],t.fixed_verdict!=='REGRESSED','fixed'):''}</div>
 <p class="pipe-caption">Red cells: decisions that differ from the old pipeline. <span class="cell-mark">prompt ±</span>: that step’s prompt differs from the old pipeline’s. ${t.first_divergence&&cause.size&&!cause.has(t.first_divergence)?`The first difference shows up in <b>${esc(t.first_divergence)}</b>, but the cause is <b>${esc([...cause].join(' + '))}</b>: the error travels downstream.`:''}</p>
 ${proof?`<h3 class="pipe-h">The proof: swap one step at a time</h3><div class="proof-legend"><span><i class="old"></i>old</span><span><i class="new"></i>new</span></div><div class="proof">${proof}</div>
 <p class="pipe-caption">A step is the cause when putting <b>only that step</b> back on the old version fixes the ticket, and running <b>only that step</b> on the new version breaks it again.</p>`:noProof}
 ${lines?`<h3 class="pipe-h">The line: ${esc(lineStep)} prompt</h3><ol class="pipe-lines">${lines}</ol><p class="pipe-caption">Each line was removed on its own and the broken tickets re-run.</p>`:''}
 ${f.after!=null?`<h3 class="pipe-h">The fix</h3><div class="pipe-fix"><div><span>Before</span>${bar(f.before,'before')}<b>${esc(f.before)} of ${esc(f.total)} broken</b></div><div><span>After: ${esc(f.change)}</span>${bar(f.after,'after')}<b>${esc(f.after)} of ${esc(f.total)} broken</b></div><p>${f.decision==='pending'?'Proposed fix, awaiting an engineer’s review.':`Fix ${esc(f.decision)}.`}</p></div>`:''}
 </section>`;
}
// Hover / focus a trace cell: the prompt that step got, what it should have answered, what it answered.
const cellPop=Object.assign(document.createElement('div'),{id:'cell-pop',role:'dialog'});cellPop.setAttribute('aria-label','Step detail');
document.body.append(cellPop);
let popTimer=null,popPinned=false;
function cellVerdict(step,expected,output){
 if(step==='classify'){try{const o=JSON.parse(output.slice(output.indexOf('{'),output.lastIndexOf('}')+1));return expected===`category: ${o.category} · order ${o.order_id||'none'}`;}catch{return null;}}
 if(step==='decide'){try{return expected===`action: ${JSON.parse(output.slice(output.indexOf('{'),output.lastIndexOf('}')+1)).action}`;}catch{return null;}}
 return null;
}
function showCellPop(el,p,ti){
 clearTimeout(popTimer);
 const t=p.tickets[ti],row=el.dataset.row,step=el.dataset.step,c=t.cells?.[row]?.[step];if(!c)return;
 const label={old:'Old pipeline',new:'New pipeline',fixed:'New + fix'}[row];
 const exp=t.expected?.[step]||'—',ok=cellVerdict(step,exp,c.output),old=t.cells.old?.[step];
 const system=p.prompts?.[c.prompt]||c.system||[];
 const sameModel=old&&old.model===c.model;
 const diffNote=row==='old'?'':c.added.length||c.removed.length
  ?`<p class="pop-note">Compared with the old pipeline’s prompt for this step: ${[c.added.length?`<b>${c.added.length} line${c.added.length===1?'':'s'} added</b> (highlighted)`:'',c.removed.length?`<b>${c.removed.length} line${c.removed.length===1?'':'s'} removed</b> (struck through, below)`:''].filter(Boolean).join(', ')}.</p>`
  :`<p class="pop-note same">Same prompt as the old pipeline.${sameModel?'':` Only the model changed: <b>${esc(old?.model)}</b> → <b>${esc(c.model)}</b>.`}</p>`;
 cellPop.innerHTML=`<div class="pop-head"><div><strong>${esc(step)}</strong> · ${esc(label)} · ${esc(t.id)} <small>${esc(c.model)}</small></div>${ok===null?'':`<span class="pop-verdict ${ok?'ok':'bad'}">${ok?'✓ matches expected':'✗ not what was expected'}</span>`}</div>
 <div class="pop-cols"><section><h4>Prompt (system)</h4>${diffNote}<ol class="pop-system">${system.map((l,i)=>`<li class="${c.added.includes(i)?'changed':''}">${esc(l)}</li>`).join('')}</ol>
 ${c.removed.length?`<ul class="pop-removed">${c.removed.map(l=>`<li><s>${esc(l)}</s> <small>removed</small></li>`).join('')}</ul>`:''}
 <details><summary>Input this step received</summary><pre>${esc(c.user)}</pre></details></section>
 <section><h4>Expected</h4><p class="pop-expected">${esc(exp)}</p>
 ${row!=='old'&&old?`<h4>Old pipeline answered</h4><pre>${esc(old.output)}</pre>`:''}
 <h4>Actual</h4><pre class="${ok===false?'bad':ok?'ok':''}">${esc(c.output)}</pre></section></div>
 <p class="pop-hint">${popPinned?'Pinned · Esc or click outside to close':'Click the cell to pin'}</p>`;
 cellPop.classList.add('open');
 const r=el.getBoundingClientRect(),w=Math.min(800,innerWidth-24),h=cellPop.offsetHeight;
 cellPop.style.width=`${w}px`;
 cellPop.style.left=`${Math.max(12,Math.min(r.left+r.width/2-w/2,innerWidth-w-12))}px`;
 cellPop.style.top=`${r.bottom+8+h<innerHeight?r.bottom+8:Math.max(12,r.top-8-h)}px`;
}
function hideCellPop(){if(popPinned)return;popTimer=setTimeout(()=>cellPop.classList.remove('open'),180);}
cellPop.addEventListener('mouseenter',()=>clearTimeout(popTimer));
cellPop.addEventListener('mouseleave',hideCellPop);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){popPinned=false;cellPop.classList.remove('open');}});
document.addEventListener('click',e=>{if(popPinned&&!cellPop.contains(e.target)&&!e.target.closest('.trace-cell')){popPinned=false;cellPop.classList.remove('open');}});
addEventListener('scroll',()=>{if(!popPinned)cellPop.classList.remove('open');},{passive:true});
function installPipeline(p){
 const card=document.querySelector('#pipeline-card');if(!card)return;
 const ti=Number(card.querySelector('.pipe-tile.on')?.dataset.ti||0);
 card.querySelectorAll('.trace-cell[data-row]').forEach(el=>{
  el.addEventListener('mouseenter',()=>showCellPop(el,p,ti));el.addEventListener('mouseleave',hideCellPop);
  el.addEventListener('focus',()=>showCellPop(el,p,ti));el.addEventListener('blur',hideCellPop);
  el.addEventListener('click',()=>{popPinned=!popPinned;showCellPop(el,p,ti);});
 });
 card.addEventListener('click',e=>{const b=e.target.closest('.pipe-tile[data-ti]');if(!b)return;popPinned=false;cellPop.classList.remove('open');
  const fresh=document.createElement('div');fresh.innerHTML=pipelineSection(p,Number(b.dataset.ti));
  const next=fresh.firstElementChild;card.replaceWith(next);installPipeline(p);next.querySelector('.pipe-tile.on')?.focus({preventScroll:true});});
}
function renderVisualDashboard() {
 motionCleanup();
 const m=data.migration, final=m.final.candidate, initial=m.candidates.find(c=>c.model===m.chosen), cause=m.causes?.[0], edit=m.final.prompt_edit;
 selectedModel ||= m.chosen;
 const selected=m.candidates.find(c=>c.model===selectedModel)||initial;
 const total=m.comparison.inputs_compared;
 const stack=(counts)=>['SAME','IMPROVED','CHANGED','REGRESSED'].map(k=>`<span class="segment ${k.toLowerCase()}" style="flex:${counts?.[k]||0}" title="${counts?.[k]||0} ${k.toLowerCase()}"></span>`).join('');
 const rows=[{...m.baseline,isBaseline:true},...m.candidates];
 const numeric=v=>typeof v==='number'&&Number.isFinite(v);
 const maximum=key=>Math.max(0,...rows.map(r=>numeric(r[key])?r[key]:0));
 const cell=(value,format,max,kind)=>`<div class="table-metric ${numeric(value)?'':'missing'}"><strong>${numeric(value)?format(value):'—'}</strong><span class="mini-track ${kind}" aria-hidden="true">${numeric(value)?`<i style="width:${max>0?Math.min(100,Math.max(0,value/max*100)):0}%"></i>`:''}</span>${numeric(value)?'':'<small>Not available</small>'}</div>`;
 app.innerHTML=`<div class="dashboard-heading"><div><p class="eyebrow">MIGRATION OVERVIEW / ${esc(m.pr?.target_repo||m.task)}</p><h1>New model. Same expectations.</h1><p>Find what changed. See what’s ready to ship.</p></div><div class="heading-actions"><span class="ready-badge"><i></i>${m.final.ready_to_merge?'Ready for review':'Needs review'}</span><button class="text-button" id="full-report">Full report ↗</button></div></div>
 <div class="migration-route"><span class="route-caption">MODEL UPGRADE</span><strong>${esc(m.baseline.model)}</strong><span class="route-line"><i></i>→</span><strong>${esc(m.chosen)}</strong><span class="route-tag">Prompt repaired</span><span class="route-note">Recorded evaluation</span></div>
 <div class="visual-grid">${m.pipeline?pipelineSection(m.pipeline,0):''}<section class="viz-card model-chart comparison-table-card"><div class="viz-title"><div><p class="eyebrow">MODEL COMPARISON</p><h2>The trade-offs, side by side.</h2></div><span class="table-stage">Before prompt repair</span></div><div class="comparison-scroll"><table class="comparison-table"><caption class="sr-only">Baseline and candidate metrics before prompt repair. Missing values retain their column positions.</caption><thead><tr><th scope="col">Model<span>Baseline & candidates</span></th><th scope="col">Quality <em>↑</em><span>Entity F1 · higher is better</span></th><th scope="col">Cost <em>↓</em><span>USD / 1,000 calls</span></th><th scope="col">Speed <em>↓</em><span>Median response · seconds</span></th><th scope="col">Tail latency <em>↓</em><span>p95 · seconds</span></th><th scope="col">Regressions <em>↓</em><span>Inputs worse than baseline</span></th></tr></thead><tbody>${rows.map(r=>`<tr class="${r.isBaseline?'baseline-row':r.model===selectedModel?'focused-row':''}"><th scope="row">${r.isBaseline?`<strong>${esc(r.model)}</strong>`:`<button class="model-select" data-model="${esc(r.model)}" aria-pressed="${r.model===selectedModel}">${esc(r.model)}</button>`}<span class="model-tag ${r.model===m.chosen?'chosen-tag':''}">${r.isBaseline?'Current model':r.model===m.chosen?'Selected for migration':'Alternative'}</span></th><td>${cell(r.accuracy,percent,1,'quality-bar')}</td><td>${cell(r.cost_per_1k_calls_usd,money,maximum('cost_per_1k_calls_usd'),'cost-bar')}</td><td>${cell(r.latency_p50_s,v=>v.toFixed(3),maximum('latency_p50_s'),'speed-bar')}</td><td>${cell(r.latency_p95_s,v=>v.toFixed(3),maximum('latency_p95_s'),'tail-bar')}</td><td>${r.isBaseline?'<span class="baseline-reference">Reference</span>':numeric(r.regressed)?`<span class="regression-pill ${r.regressed?'has-regressions':''}">${esc(r.regressed)} ${r.regressed===1?'input':'inputs'}</span>`:'<span class="baseline-reference">—<br>Not available</span>'}</td></tr>`).join('')}</tbody></table></div><div class="comparison-foot"><span>Bars share a scale within each column. Shorter cost and latency bars are better.</span><span>— Not supplied · space reserved</span></div></section>
 <section class="viz-card repair-chart"><div class="viz-title"><div><p class="eyebrow">REPAIR IMPACT</p><h2>${m.final.verdict_counts.REGRESSED===0?(initial?.regressed?'One fix. The regression is gone.':'No regressions to fix.'):m.fix_attempts?.length?'A fix is proposed. Awaiting review.':'Regressions remain.'}</h2></div><span class="repair-symbol">↗</span></div><div class="impact-number"><span>${esc(initial?.regressed??'—')}</span><span class="impact-arrow">→</span><strong>${esc(m.final.verdict_counts.REGRESSED)}</strong><small>regressions</small></div><div class="stack-row"><span>Before</span><div class="stack" aria-label="Before repair: ${esc(initial?.regressed)} regressions">${stack(initial?.verdict_counts)}</div></div><div class="stack-row"><span>After</span><div class="stack" aria-label="After repair: ${esc(m.final.verdict_counts.REGRESSED)} regressions">${stack(m.final.verdict_counts)}</div></div><div class="stack-legend"><span><i class="same"></i>Same</span><span><i class="improved"></i>Improved</span><span><i class="changed"></i>Changed</span><span><i class="regressed"></i>Regressed</span></div></section>
 <section class="viz-card evidence-card"><div class="viz-title"><div><p class="eyebrow">WHAT SWITCHYARD CAUGHT</p><h2>${m.task==='agent'?'One step. One line. A wrong decision.':'A small word. A wrong label.'}</h2></div><span class="pill">${esc(cause?.input_id||'Evidence')}</span></div><p class="evidence-quote">“${esc(cause?.text||'No input evidence supplied.')}”</p><div class="evidence-flow"><div><span class="demo-label">Before repair</span><div class="entity-list">${(cause?.candidate_output||[]).map(v=>`<span class="entity ${cause.baseline_output.includes(v)?'':'entity-bad'}">${esc(v)}</span>`).join('')}</div></div><span class="flow-arrow">→</span><div><span class="demo-label">After repair</span><div class="entity-list">${(m.comparison.items.find(i=>i.input_id===cause?.input_id)?.candidate.output||[]).map(v=>`<span class="entity">${esc(v)}</span>`).join('')}</div></div></div><details class="inline-details"><summary>What changed in the prompt?</summary><p>${esc(edit?.rationale||'No edit recorded.')}</p><div class="diff"><div class="removed">− ${esc(edit?.old_text)}</div><div class="added">+ ${esc(edit?.new_text)}</div></div></details></section>
 <section class="viz-card checks-card"><div class="viz-title"><div><p class="eyebrow">FINAL EVALUATION</p><h2>Every input, at a glance.</h2></div><span class="chart-unit">${total} inputs</span></div><div class="result-mosaic">${data.candidates[0].results.map((r,i)=>`<button class="mosaic-cell ${r.status}" data-result="${i}" aria-label="Input ${i+1}: ${esc(r.ner_item.verdict)}" title="${esc(r.input_id)} · ${esc(r.ner_item.verdict)}">${r.status==='changed'?'≈':r.status==='improved'?'↑':r.status==='regressed'?'!':'·'}</button>`).join('')}</div><p class="mosaic-caption">${esc(m.final.verdict_counts.REGRESSED)} regressed · ${esc(m.final.verdict_counts.IMPROVED)} improved · ${esc(m.final.verdict_counts.CHANGED)} changed</p><p class="chart-caption">Click any square to inspect its output. Same means unchanged, not necessarily error-free.</p><div class="quality-summary"><span>Final extraction quality</span><strong>${percent(final.accuracy)}</strong><span class="quality-delta">${((final.accuracy-m.baseline.accuracy)*100).toFixed(1)} pp above baseline</span></div></section></div>
 <section class="delivery-strip"><div class="delivery-icon">⑂</div><div><h2>${m.final.ready_to_merge?'Migration ready for your review':'Migration needs review'}</h2><p>Model upgrade + prompt repair in one proposed code change.</p></div><button class="button primary" id="view-diff">Review code change ↗</button></section><div class="dashboard-foot"><span>Trade-off: ${final.latency_p50_s>m.baseline.latency_p50_s?'slower':'faster'} median response · ${final.cost_per_1k_calls_usd==null?'candidate cost unavailable':'cost in full report'} · ${final.unstable_inputs} unstable inputs</span><span>Recorded results · no live calls or published PR</span></div>`;
 app.querySelectorAll('[data-model]').forEach(b=>b.onclick=()=>{selectedModel=b.dataset.model;render();});
 app.querySelectorAll('[data-result]').forEach(b=>b.onclick=()=>openDetail(0,Number(b.dataset.result)));
 app.querySelector('#full-report').onclick=()=>{reportView=true;phase='complete';completed=entries.length;render();};
 app.querySelector('#view-diff').onclick=()=>{dialog.innerHTML=`<div class="detail-head"><div><p class="eyebrow">PROPOSED CODE CHANGE</p><h2 id="detail-title">Review the migration</h2></div><button class="close" aria-label="Close details">×</button></div><div class="detail-body"><p>${esc(m.pr?.target_path)}</p><pre class="diff code-diff">${esc(m.pr?.diff||'No diff supplied.')}</pre><p class="provider">Recorded proposal. No published PR link supplied.</p></div>`;dialog.querySelector('.close').onclick=()=>dialog.close();dialog.showModal();};
 if(m.pipeline)installPipeline(m.pipeline);
 installDashboardMotion(m);
}
function installDashboardMotion(m) {
 const reduce=window.matchMedia('(prefers-reduced-motion: reduce)');
 const heading=app.querySelector('.dashboard-heading');
 heading.classList.add('cinema-hero');
 heading.innerHTML=`<div class="hero-copy"><p class="hero-kicker">Switchyard / Model migration</p><h1>Move forward.<br><span>Keep what works.</span></h1><p class="hero-description">A new model. A precise repair.<br>Every change, backed by an evaluation.</p><div class="hero-actions"><button class="button primary" id="jump-comparison">Explore the results ↓</button><button class="text-button" id="full-report">Full report ↗</button></div></div><div class="model-stage" aria-label="Migration from ${esc(m.baseline.model)} to ${esc(m.chosen)}"><div class="metal-word" aria-hidden="true">SHIFT</div><div class="stage-halo" aria-hidden="true"></div><div class="model-object model-old"><span class="object-caption">CURRENT MODEL</span><strong>${esc(m.baseline.model)}</strong><div class="chip-traces" aria-hidden="true"></div><span class="object-foot">Baseline behavior</span></div><div class="model-object model-new"><span class="object-caption">SELECTED UPGRADE</span><strong>${esc(m.chosen)}</strong><div class="chip-traces" aria-hidden="true"></div><span class="object-foot">${m.final.ready_to_merge?'Ready for review':'Needs review'} <i></i></span></div><div class="stage-caption">${esc(m.pr?.target_repo||m.task)} <span>·</span> Recorded migration</div></div>`;
 app.querySelector('.migration-route').remove();
 const comparison=app.querySelector('.comparison-table-card');comparison.id='model-comparison';
 heading.querySelector('#jump-comparison').onclick=()=>comparison.scrollIntoView({behavior:reduce.matches?'instant':'smooth',block:'start'});
 heading.querySelector('#full-report').onclick=()=>{reportView=true;phase='complete';completed=entries.length;render();};
 const cause=m.causes?.[0], edit=m.final.prompt_edit;
 const evidence=app.querySelector('.evidence-card'), impact=app.querySelector('.repair-chart');
 if(cause && edit && evidence && impact){
 const corrected=m.comparison.items.find(i=>i.input_id===cause.input_id)?.candidate.output||[];
 const entityList=(values,bad=false)=>values.map(v=>`<span class="entity ${bad&&!cause.baseline_output.includes(v)?'entity-bad':''}">${esc(v)}</span>`).join('');
 const addition=edit.new_text?.startsWith(edit.old_text)?edit.new_text.slice(edit.old_text.length).replace(/^[.\s]+/,''):edit.new_text;
 const section=document.createElement('section');section.className='scroll-repair';section.setAttribute('aria-label','Recorded repair walkthrough');
 section.innerHTML=`<div class="repair-sticky"><div class="repair-anchor"><p class="eyebrow">FOLLOW THE REPAIR</p><h2>Same sentence.<br>A better interpretation.</h2><p class="anchored-quote">“${esc(cause.text)}”</p><div class="anchor-baseline"><span class="demo-label">Expected extraction</span><div class="entity-list">${entityList(cause.baseline_output)}</div></div><nav class="repair-stages" aria-label="Repair stages">${['Detect','Repair','Verify'].map((name,i)=>`<button data-repair-step="${i}"><span>0${i+1}</span>${name}</button>`).join('')}</nav><p class="scroll-cue">↓ Scroll to follow the recorded repair</p></div><div class="repair-scenes"><article class="repair-scene" data-scene="0"><span class="scene-number">01 / DETECT</span><h3>The upgrade adds a wrong label.</h3><div class="entity-list">${entityList(cause.candidate_output,true)}</div><p>${esc(cause.evidence?.join(' · '))}</p><span class="pill warn">Regression detected</span></article><article class="repair-scene" data-scene="1"><span class="scene-number">02 / REPAIR</span><h3>Clarify the instruction.</h3><p class="scene-component">${esc(edit.component)}</p><blockquote>${esc(addition)}</blockquote><details><summary>Inspect the complete prompt edit</summary><div class="diff"><div class="removed">− ${esc(edit.old_text)}</div><div class="added">+ ${esc(edit.new_text)}</div></div></details></article><article class="repair-scene" data-scene="2"><span class="scene-number">03 / VERIFY</span><h3>Retested with the repaired prompt.</h3><div class="entity-list">${entityList(corrected)}</div><div class="impact-slot"></div></article></div><div class="scroll-track" aria-hidden="true"><i></i></div></div>`;
 evidence.replaceWith(section);section.querySelector('.impact-slot').append(impact);
 impact.querySelector('.viz-title').remove();
 }
 
 const observer=new IntersectionObserver(items=>items.forEach(item=>{if(item.isIntersecting){item.target.classList.add('motion-visible');observer.unobserve(item.target);}}),{threshold:0.12});
 for(const selector of ['.comparison-table-card','.checks-card','.delivery-strip']){const el=app.querySelector(selector);if(el){el.classList.add('motion-ready');observer.observe(el);}}
 app.querySelectorAll('.mosaic-cell').forEach((el,i)=>el.style.setProperty('--reveal-delay',`${i*22}ms`));
 const section=app.querySelector('.scroll-repair');let frame=0,active=-1;
 function setScene(index){if(active===index)return;active=index;section.querySelectorAll('[data-scene]').forEach((el,i)=>{el.classList.toggle('scene-active',i===index);el.inert=!reduce.matches&&i!==index;el.setAttribute('aria-hidden',String(!reduce.matches&&i!==index));});section.querySelectorAll('[data-repair-step]').forEach((el,i)=>{if(i===index)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});}
 function update(){frame=0;const heroProgress=reduce.matches?0:Math.min(1,Math.max(0,-heading.getBoundingClientRect().top/heading.offsetHeight));heading.style.setProperty('--hero-p',heroProgress);if(!section)return;const rect=section.getBoundingClientRect(),sticky=section.firstElementChild;const distance=Math.max(1,section.offsetHeight-sticky.offsetHeight);const progress=Math.min(1,Math.max(0,((parseFloat(getComputedStyle(sticky).top)||0)-rect.top)/distance));setScene(Math.min(2,Math.floor(progress*3)));section.querySelector('.scroll-track i').style.transform=`scaleX(${progress})`;}
 function onScroll(){if(!frame)frame=requestAnimationFrame(update);}
 function preference(){active=-1;update();}
 if(section){section.querySelectorAll('[data-repair-step]').forEach((button,i)=>button.onclick=()=>{if(reduce.matches){section.querySelector(`[data-scene="${i}"]`).scrollIntoView({block:'center'});return;}const distance=Math.max(1,section.offsetHeight-section.firstElementChild.offsetHeight);window.scrollTo({top:window.scrollY+section.getBoundingClientRect().top-(parseFloat(getComputedStyle(section.firstElementChild).top)||0)+distance*(i===0?0:i===1?.5:1),behavior:'smooth'});});window.addEventListener('scroll',onScroll,{passive:true});window.addEventListener('resize',onScroll);reduce.addEventListener('change',preference);update();}
 motionCleanup=()=>{observer.disconnect();cancelAnimationFrame(frame);window.removeEventListener('scroll',onScroll);window.removeEventListener('resize',onScroll);reduce.removeEventListener('change',preference);};
}
function normalizeMigration(raw) {
 if (!raw.final || !raw.comparison?.items?.length) throw Error('Migration report needs final results and a per-input comparison.');
 const result = normalizeNER(raw.comparison);
 result.migration = raw;
 result.run_id = raw.finished || raw.started || 'Recorded report';
 result.repo = {name:raw.pr?.target_repo || `${raw.task} migration`,call_site:raw.pr?.target_path || 'Model migration'};
 result._note = 'Complete migration report · Candidate selection → prompt repair → final evaluation.';
 return result;
}
const percent = v => typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '—';
const seconds = v => typeof v === 'number' ? `${v.toFixed(3)} s` : '—';
function renderMigration() {
 const m=data.migration;
 const counts = v => ['REGRESSED','IMPROVED','CHANGED','SAME'].map(key=>`${v?.[key]??0} ${key.toLowerCase()}`).join(' · ');
 const cards=app.querySelector('.cards');
 cards.insertAdjacentHTML('beforebegin', `<section class="migration-section"><p class="eyebrow">01 / Candidate selection · before repair</p><div class="cards">${m.candidates.map(c=>`<article class="card ${c.model===m.chosen?'recommended':''}"><div class="card-head"><div><h3>${esc(c.model)}</h3><div class="provider">Rank ${esc(c.rank??'—')}</div></div>${c.model===m.chosen?'<span class="recommend">SELECTED</span>':''}</div><div class="quality"><strong>${percent(c.accuracy)}</strong><small>entity F1</small></div><p class="provider">${esc(counts(c.verdict_counts))}</p><div class="metrics"><div class="metric"><label>Median response</label><strong>${seconds(c.latency_p50_s)}</strong><small>${esc(c.unstable_inputs)} unstable inputs</small></div><div class="metric"><label>Cost / 1,000 calls</label><strong>${money(c.cost_per_1k_calls_usd)}</strong><small>${c.cost_per_1k_calls_usd==null?'Not supplied in report':'Recorded cost'}</small></div></div></article>`).join('')}</div><p class="provider">Baseline ${esc(m.baseline.model)} · ${percent(m.baseline.accuracy)} entity F1 · ${seconds(m.baseline.latency_p50_s)} median · ${money(m.baseline.cost_per_1k_calls_usd)} / 1,000 calls. Initial per-input reports are referenced but not included.</p></section>
 <section class="panel migration-section"><div class="panel-heading"><div><p class="eyebrow">02 / Diagnose & repair</p><h2>Recorded prompt repairs</h2><p>Evidence and accepted edits from the migration run</p></div><span class="pill">${m.fix_attempts?.length??0} attempts</span></div><div class="repair-body">${(m.causes||[]).map(c=>`<details class="record" open><summary>${esc(c.input_id)} · ${esc(c.status)} cause</summary><p>${esc(c.text)}</p><div class="outputs"><div class="output"><label>BASELINE ENTITIES</label><p>${esc(c.baseline_output?.join('\n'))}</p></div><div class="output bad"><label>BEFORE REPAIR · ${esc(m.chosen)}</label><p>${esc(c.candidate_output?.join('\n'))}</p></div></div><p>${esc(c.evidence?.join(' · '))}</p><p class="provider">Confirmed component: ${esc(c.confirmed_component||'Not confirmed')}</p>${(c.suspects||[]).map(s=>`<p class="provider">${esc(s.component)} · suspicion score ${esc(s.score)} · ${s.editable?'editable':'not editable'}</p>`).join('')}</details>`).join('')}${(m.fix_attempts||[]).map(f=>`<details class="record" open><summary>Attempt ${esc(f.attempt)} · ${esc(f.outcome)}</summary><p class="provider">${esc(f.edit?.component)}</p><div class="diff"><div class="removed">− ${esc(f.edit?.old_text)}</div><div class="added">+ ${esc(f.edit?.new_text)}</div></div><p>${esc(f.edit?.rationale)}</p><p class="provider">${esc(counts(f.verdict_counts))}</p><span class="pill ${f.accepted?'':'warn'}">${f.accepted?'Accepted in recorded run':'Not accepted'}</span></details>`).join('')||'<p>No repair records supplied.</p>'}</div></section><p class="eyebrow">03 / Final evaluation · after repair</p>`);
 app.querySelector('#tests-title').textContent='Per-input results after repair';
 app.querySelector('.row-label small').textContent=`${data.ner.verdict_counts.REGRESSED??0} regressed after repair`;
 const banner=app.querySelector('.pr-banner');
 banner.querySelector('h2').textContent=m.final.ready_to_merge?'✓ Migration ready for review':'Migration needs review';
 banner.querySelector('.provider').textContent='Readiness reported by the evaluation';
 if(m.pr) banner.insertAdjacentHTML('afterend', `<section class="panel migration-section pr-section"><div class="panel-heading"><div><p class="eyebrow">04 / Proposed code change</p><h2>Pull request preview</h2><p>${esc(m.pr.title)}</p></div></div><div class="repair-body"><p class="provider">${esc(m.pr.target_path)}</p><details class="record"><summary>View proposed diff</summary><pre class="diff code-diff">${esc(m.pr.diff||'No diff supplied.')}</pre></details><p class="provider">${safeURL(m.pr.url)?`<a href="${esc(safeURL(m.pr.url))}" target="_blank" rel="noopener noreferrer">Open pull request ↗</a>`:'No published PR link is included. This is the recorded proposal.'}</p></div></section>`);
}
function render() {
 app.classList.toggle('full-report-view',reportView);
 if(reportView)motionCleanup();
 if(data.migration && !reportView){renderVisualDashboard();return;}
 const total=entries.length, done=phase==='complete', remaining=unresolved().length;
 const end=Date.parse(data.baseline.retires_on), reference=Date.parse(data.run_id);
 const days=Number.isFinite(end)&&Number.isFinite(reference)?Math.max(0,Math.ceil((end-reference)/86400000)):null;
 const retirement=days===null?'':`<span class="pill warn">${days} days to retirement at report time</span>`;
 app.innerHTML=`<section class="intro"><div><p class="eyebrow">Migration report / ${esc(data.repo.call_site)}</p><h1>${esc(data.repo.name)}</h1><div class="subline"><span>Baseline</span><strong>${esc(data.baseline.model)}</strong>${retirement}</div></div><div class="actions"><button class="button" id="reset">↺ Reset</button><button class="button primary" id="run" ${phase==='running'?'disabled':''}>${phase==='running'?'Running replay…':phase==='complete'?'↻ Replay run':'▶ Run comparison'}</button></div></section>
 ${data._note?`<div class="sample"><span>◈</span><span>${esc(data._note)} This dashboard replays recorded results; no live model calls.</span></div>`:''}
 <section class="cards" aria-label="Model comparison">${data.candidates.map((c,ci)=>{
 const repaired=c.results.filter(isFixed).length;
 const passed=done?c.summary.passed+repaired:c.summary.passed;
 const cost=c.summary.cost_change_pct;
 return `<article class="card ${c.summary.recommended?'recommended':''}"><div class="card-head"><div><h3>${esc(c.model)}</h3><div class="provider">${esc(c.provider)}</div></div>${c.summary.recommended?'<span class="recommend">RECOMMENDED</span>':''}</div><div class="quality"><strong>${data._format==='ner'?esc(c.summary.accuracy_label):esc(passed)}</strong><small>${data._format==='ner'?'entity F1':`/ ${esc(c.summary.total)}`}</small><span>${data._format==='ner'?`vs ${data.baseline.accuracy_label} baseline · ${esc(c.summary.verdict_counts.REGRESSED??0)} regressed`:repaired?'passed after fixes':'recorded passes'}</span></div><div class="quality-track"><i style="width:${Math.min(100,Math.max(0,data._format==='ner'?c.summary.accuracy*100:passed/c.summary.total*100))}%"></i></div><div class="metrics"><div class="metric"><label>Cost / 1,000 calls</label><strong>${money(c.summary.cost_per_1k_calls_usd)}</strong>${cost===null?'<small>Candidate cost unavailable</small>':`<span class="change">${cost>0?'+':''}${esc(cost.toFixed(1))}%</span>`}<small>Baseline ${money(data.baseline.cost_per_1k_calls_usd)}</small></div><div class="metric"><label>Median response</label><strong>${esc(Math.round(c.summary.latency_p50_ms))} ms</strong><small>${esc(c.summary.speed_vs_baseline.toFixed(2))}× baseline speed · p95 ${Math.round(c.summary.latency_p95_ms)} ms</small></div></div></article>`;
 }).join('')}</section>
 <section class="panel" aria-labelledby="tests-title"><div class="panel-heading"><div><h2 id="tests-title">Behavior across models</h2><p>${data.inputs.length} inputs · ${data.candidates.length} candidates · ${esc(data.baseline.runs_per_input)} runs per input</p></div><div class="progress-meta" role="status"><strong>${completed} / ${total}</strong><br>${phase==='idle'?'Ready to replay':phase==='running'?'Replaying results':'Comparison complete'}</div></div><div class="progress" role="progressbar" aria-label="Replay progress" aria-valuenow="${completed}" aria-valuemin="0" aria-valuemax="${total}"><i style="width:${completed/total*100}%"></i></div><div class="grid-body">${data.candidates.map((c,ci)=>`<div class="grid-row"><div class="row-label"><strong>${esc(c.model)}</strong><small>${c.results.filter(r=>r.status==='regressed'&&!isFixed(r)).length} flagged · ${c.results.filter(isFixed).length} fixed</small></div><div class="tiles">${c.results.map((r,ri)=>{
 const n=entries.findIndex(e=>e.ci===ci&&e.ri===ri), revealed=n<completed;
 const state=fixing.has(r.cause_id)&&r.status==='regressed'?'running':revealed?(isFixed(r)?'fixed':r.status):phase==='running'&&n===completed?'running':'pending';
 const label=state==='fixed'?'fixed':r.status==='changed'?'changed':r.status==='improved'?'improved':state;
 return `<button class="tile ${state}" data-ci="${ci}" data-ri="${ri}" ${revealed?'':'disabled'} title="${esc(r.input_id)} · ${label}" aria-label="${esc(c.model)}, input ${ri+1}, ${label}">${state==='fixed'?'✓':String(ri+1).padStart(2,'0')}</button>`;
 }).join('')}</div></div>`).join('')}</div><div class="legend">${data._format==='ner'?'<span><i class="dot pass"></i>Same</span><span><i class="dot improved"></i>Improved</span><span><i class="dot changed"></i>Changed</span><span><i class="dot regressed"></i>Regressed</span>':'<span><i class="dot pass"></i>Passed</span><span><i class="dot regressed"></i>Got worse</span><span><i class="dot fixed"></i>Fixed</span><span><i class="dot"></i>Pending</span>'}<span>Click a result to inspect the evidence</span></div></section>
 <div class="statusbar"><span>${done?(data._format==='ner'?`${data.ner.verdict_counts.REGRESSED??0} regressed · ${data.ner.verdict_counts.CHANGED??0} changed · ${data.ner.verdict_counts.IMPROVED??0} improved`:remaining?`${remaining} flagged results to review. Fixes apply to every result with the same cause.`:'All flagged results resolved.'): 'Run the comparison to reveal individual results.'}</span><span>${data._format==='ner'?`Candidate unstable inputs: ${esc(data.ner.candidate.unstable_inputs)} / ${esc(data.ner.inputs_compared)}`:`Baseline variation: ${esc(Math.round(data.baseline.noise_rate*100))}%`}</span></div>
 ${data._format==='ner'?`<section class="pr-banner ${data.ner.ready_to_merge?'':'merge-blocked'}" role="status"><div><h2><span class="pr-icon">${data.ner.ready_to_merge?'✓':'!'}</span>${data.ner.ready_to_merge?'Ready to merge':'Not ready to merge'}</h2><p>${esc(data.ner.verdict_counts.REGRESSED??0)} regression · ${esc(data.ner.verdict_counts.IMPROVED??0)} improved · ${esc(data.ner.verdict_counts.CHANGED??0)} changed · ${esc(data.ner.verdict_counts.SAME??0)} same</p><span class="pill">${esc(data.ner.inputs_compared)} inputs compared · ${esc(data.ner.candidate.unstable_inputs)} unstable candidate inputs</span></div><span class="provider">${data.ner.ready_to_merge?'All merge checks passed':'Review the regressed and changed examples'}</span></section>`:done&&remaining===0?`<section class="pr-banner" role="status"><div><h2><span class="pr-icon">⑂</span>Migration ready for review</h2><p>${esc(data.pr.title)}</p><span class="pill">${esc(data.pr.status)} · ${esc(data.pr.lines_changed)} lines changed</span></div>${safeURL(data.pr.url)?`<a class="button" href="${esc(safeURL(data.pr.url))}" target="_blank" rel="noopener noreferrer">Open pull request ↗</a>`:'<span class="provider">PR link not available in this report</span>'}</section>`:''}
 <footer><span>SWITCHYARD / Inspect. Repair. Migrate.</span><span>Report ${esc(data.run_id)} · Recorded replay</span></footer>`;
 if(data.migration) {renderMigration(); app.querySelector('.actions').insertAdjacentHTML('afterbegin','<button class="button" id="demo-view">← Dashboard</button>'); app.querySelector('#demo-view').onclick=()=>{clearTimers();reportView=false;render();};}
 document.querySelector('#run').onclick=start;
 document.querySelector('#reset').onclick=reset;
 app.querySelectorAll('[data-ci]').forEach(b=>b.onclick=()=>openDetail(+b.dataset.ci,+b.dataset.ri));
}
function safeURL(value){try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}}
function clearTimers(){clearInterval(timer);timer=null;fixTimers.forEach(clearTimeout);fixTimers.clear();}
function reset(){clearTimers();phase='idle';completed=0;fixed.clear();fixing.clear();selected=null;dialog.close();render();}
function start(){reset();phase='running';render();const delay=15000/entries.length;timer=setInterval(()=>{completed++;if(completed>=entries.length){completed=entries.length;phase='complete';clearInterval(timer);timer=null;}render();if(dialog.open)drawDetail();},delay);}
function openDetail(ci,ri){selected={ci,ri};drawDetail();if(!dialog.open)dialog.showModal();}
function drawDetail(){
 if(!selected)return;
 const {ci,ri}=selected,c=data.candidates[ci],r=c.results[ri],input=inputFor(r.input_id),cause=causeFor(r.cause_id),repaired=isFixed(r),busy=fixing.has(r.cause_id);
 if(r.ner_item){const item=r.ner_item;const formatEntities=values=>values.length?values.map(esc).join('<br>'):'(no entities)';const formatErrors=errors=>errors.length?errors.map(error=>`<div class="error-item"><strong>${esc(error.type.replaceAll('_',' '))}</strong>${error.gold?` · expected ${esc(error.gold[0])}/${esc(error.gold[1])}`:''}${error.pred?` · found ${esc(error.pred[0])}/${esc(error.pred[1])}`:''}</div>`).join(''):'No errors recorded.';const itemScore=item.candidate.score_runs.map(value=>`${(value*100).toFixed(1)}%`).join(' · ');dialog.innerHTML=`<div class="detail-head"><div><p class="eyebrow">${esc(item.id)} · ${item.tricky?'Tricky input':'Standard input'}</p><h2 id="detail-title">${esc(item.verdict)}</h2></div><button class="close" aria-label="Close details">×</button></div><div class="detail-body"><h3>Input</h3><div class="input-text">${esc(item.text)}</div><h3>Extracted entities</h3><div class="outputs"><div class="output"><label>BASELINE · ${esc(data.baseline.model)}</label><p>${formatEntities(item.baseline.output)}</p><small>F1 across runs: ${item.baseline.score_runs.map(v=>`${(v*100).toFixed(1)}%`).join(' · ')}</small></div><div class="output ${item.verdict==='REGRESSED'?'bad':item.verdict==='IMPROVED'?'good':''}"><label>CANDIDATE · ${esc(data.candidates[ci].model)}</label><p>${formatEntities(item.candidate.output)}</p><small>F1 across runs: ${itemScore} · ${item.candidate.stable?'Stable':'Unstable'}</small></div></div><h3>Candidate errors</h3><div class="input-text">${formatErrors(item.candidate.errors)}</div>${item.baseline.errors.length?`<h3>Baseline errors</h3><div class="input-text">${formatErrors(item.baseline.errors)}</div>`:''}${item.intermittent_failures.length?`<h3>Intermittent failures</h3><div class="input-text">${formatErrors(item.intermittent_failures)}</div>`:''}<h3>Run verdict</h3><p>${esc(item.verdict)}${item.regressions.length?` · ${esc(item.regressions.length)} regression signal`:''}${item.improvements.length?` · ${esc(item.improvements.length)} improvement signal`:''}</p></div><div class="detail-bottom"><span>Evaluation details from the supplied report</span><button class="button" aria-label="Close details">Close</button></div>`;dialog.querySelectorAll('.close, .detail-bottom button').forEach(button=>button.onclick=()=>dialog.close());return;}
 dialog.innerHTML=`<div class="detail-head"><div><p class="eyebrow">${esc(c.model)} / ${esc(r.input_id)}</p><h2 id="detail-title">${esc(cause?(repaired?cause.fixed_title:cause.title):'Result details')}</h2></div><button class="close" aria-label="Close details">×</button></div><div class="detail-body"><div class="subline"><span class="pill">${esc(r.runs.passed)}/${esc(r.runs.total)} original runs passed</span>${cause?`<span class="pill warn">${esc(cause.check.startsWith('hard_check')?'Hard check · '+cause.check.split(':').slice(1).join(':').trim():'AI judge')}</span>`:''}${repaired?'<span class="pill">Fix replay complete</span>':''}</div><h3>Customer input</h3><div class="input-text">${esc(input.text)}</div><h3>Output comparison</h3><div class="outputs"><div class="output"><label>BASELINE · ${esc(data.baseline.model)}</label><p>${esc(r.baseline_output)}</p></div><div class="output ${repaired?'good':cause?'bad':''}"><label>${repaired?'AFTER FIX':'CANDIDATE'} · ${esc(c.model)}</label><p>${esc(repaired?(r.after_fix_output??'No revised output was supplied for this input. See the recorded retest summary below.'):r.output)}</p></div></div>${cause?`<h3>Why it changed</h3><p>${esc(cause.reason)}</p><h3>Suggested prompt change <span class="provider">· line ${esc(cause.line)}</span></h3><p class="provider">${esc(data.prompt.file)}</p><div class="diff"><div class="removed">− ${esc(cause.fix.old_line)}</div><div class="added">+ ${esc(cause.fix.new_line)}</div></div>${repaired?`<p>Recorded cause-level retest: <strong>${esc(cause.fix.retest.passed)}/${esc(cause.fix.retest.total)} passed</strong>. This is replayed evidence, not a new model run.</p>`:''}`:'<p>This result passed the recorded evaluation. No repair is attached.</p>'}</div><div class="detail-bottom"><span>${cause?'Applies to all flagged results linked to cause '+esc(cause.id):'Recorded score: '+esc(r.score)}</span>${cause?`<button class="button primary" id="fix" ${repaired||busy||phase!=='complete'?'disabled':''}>${repaired?'✓ Fix applied':busy?'Replaying retest…':phase!=='complete'?'Finish replay first':'Apply fix & replay retest'}</button>`:''}</div>`;
 dialog.querySelector('.close').onclick=()=>dialog.close();
 const button=dialog.querySelector('#fix');if(button)button.onclick=()=>applyFix(cause.id);
}
function applyFix(id){if(phase!=='complete'||fixing.has(id)||fixed.has(id))return;fixing.add(id);render();drawDetail();const handle=setTimeout(()=>{fixTimers.delete(handle);fixing.delete(id);fixed.add(id);render();drawDetail();},900);fixTimers.add(handle);}
dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
// ---- Reports from the samegrade backend (server.mjs /api/*); falls back to ./results.json ----------
let reports = [], currentReport = null;
const reportURL = id => `/api/report?id=${encodeURIComponent(id)}`;
function showError(error){app.innerHTML=`<section class="error"><h1>Couldn’t open this report</h1><p>${esc(error.message)}</p><p>Serve this folder with a local web server and include a valid results.json. Opening index.html directly from Finder may prevent the browser from reading the JSON.</p></section>`;}
async function loadReport(url){
 clearTimers();phase='idle';completed=0;fixed.clear();fixing.clear();selected=null;reportView=false;selectedModel=null;if(dialog.open)dialog.close();
 const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw Error(`Could not load the report (${response.status}).`);
 const raw=await response.json();data=raw?.comparison && Array.isArray(raw.candidates)?normalizeMigration(raw):raw?.task==='ner'?normalizeNER(raw):raw;validate(data);entries=data.candidates.flatMap((c,ci)=>c.results.map((r,ri)=>({r,ci,ri})));render();
}
function pickerOptions(){
 const groups=[...new Set(reports.map(r=>r.group||'Reports'))];
 return groups.map(g=>`<optgroup label="${esc(g)}">${reports.filter(r=>(r.group||'Reports')===g).map(r=>`<option value="${esc(r.id)}" ${r.id===currentReport?'selected':''}>${esc(r.label||r.id.replaceAll('_',' '))}</option>`).join('')}</optgroup>`).join('');
}
function toast(text){const el=Object.assign(document.createElement('div'),{className:'toast',textContent:text});document.body.append(el);setTimeout(()=>el.remove(),3500);}
async function onReportsChanged(list){
 const before=reports.find(r=>r.id===currentReport)?.updated,added=list.filter(r=>!reports.some(x=>x.id===r.id));
 reports=list;const select=document.querySelector('.report-picker');if(select)select.innerHTML=pickerOptions();
 const now=reports.find(r=>r.id===currentReport)?.updated;
 if(currentReport&&now&&now!==before){try{await loadReport(reportURL(currentReport));toast('Report updated from the latest run');}catch(e){showError(e);}}
 else if(added.length)toast(`New report: ${added.map(r=>r.label||r.id).join(', ')}`);
}
function installReportPicker(){
 const select=document.createElement('select');select.className='report-picker';select.setAttribute('aria-label','Report');
 select.innerHTML=pickerOptions();
 select.onchange=async()=>{try{await loadReport(reportURL(select.value));currentReport=select.value;}catch(e){showError(e);}};
 document.querySelector('.mast-meta').prepend(select);
}

// ---- Live results: rows streamed from samegrade/results/*.jsonl while runs are going ---------------
const liveDialog=document.querySelector('#live');
const live={rows:[],files:new Map(),connected:false};
const ACTIVE_MS=60000;
function addLiveRow(ev){
 live.rows.push(ev);if(live.rows.length>300)live.rows.shift();
 const f=live.files.get(ev.file)||{file:ev.file,rows:0,passed:0,failed:0,errors:0,last:0};
 f.rows++;if(ev.error)f.errors++;else if(ev.passed===true)f.passed++;else if(ev.passed===false)f.failed++;
 f.tickets=f.tickets||{};if(ev.input_id)f.tickets[ev.input_id]=ev;f.last=ev.at;live.files.set(ev.file,f);
}
function drawLive(){
 const files=[...live.files.values()].sort((a,b)=>b.last-a.last);
 const ago=t=>{const s=Math.round((Date.now()-t)/1000);return s<60?`${s}s ago`:`${Math.round(s/60)} min ago`;};
 const name=f=>esc(f.replace(/\.jsonl$/,''));
 const checks=r=>r.error?'<span class="pill warn">error</span>':r.passed===true?'<span class="pill">pass</span>':r.passed===false?'<span class="pill warn">fail</span>':'—';
 const latest=live.rows.at(-1);
 const [cat,act]=String(latest?.label||'').split(' / ');
 const strip=latest&&latest.task==='agent'?`<div class="live-strip"><span class="live-strip-id">${esc(latest.input_id)}</span>${[['classify',`category: ${cat}`],['decide',`action: ${act}`],['draft',latest.error?'error':'reply written'],['tone',latest.passed===false?'checks failed':latest.error?'—':'checks passed']].map(([s,v],i)=>`<div class="pipe-node mini"><strong>${s}</strong><small>${esc(v)}</small></div>${i<3?'<span class="pipe-arrow">→</span>':''}`).join('')}</div>`:'';
 // Green = the ticket's outcome matches what it should be (gold score 1); red = it doesn't, or checks failed.
 const tileClass=r=>r.error?'err':r.passed===false||(typeof r.score==='number'&&r.score<1)?'bad':'ok';
 const grids=files.slice(0,3).map(f=>{const ts=Object.values(f.tickets||{}).sort((a,b)=>String(a.input_id).localeCompare(String(b.input_id),undefined,{numeric:true}));
  const off=ts.filter(r=>tileClass(r)!=='ok').length;
  return `<div class="live-grid-block"><div class="live-grid-head"><strong>${name(f.file)}</strong><span>${ts.length} tickets · <b class="ok-text">${ts.length-off} as expected</b>${off?` · <b class="bad-text">${off} off</b>`:''}</span></div><div class="live-grid">${ts.map(r=>`<span class="live-tile ${tileClass(r)}" title="${esc(r.input_id)}: ${esc(r.error||r.label||'')}${typeof r.score==='number'?` · score ${r.score.toFixed(2)}`:''}">${esc(r.input_id)}<small>${esc(String(r.label||'').split(' / ')[1]||'')}</small></span>`).join('')}</div></div>`;}).join('');
 liveDialog.innerHTML=`<div class="detail-head"><div><p class="eyebrow">Live results</p><h2 id="live-title">Runs writing to samegrade/results</h2></div><button class="close" aria-label="Close">×</button></div>
 <div class="detail-body">
 ${replayState?`<p class="provider">${replayState.state==='running'?'Replaying':'Replay '+esc(replayState.state)}: ${esc(replayState.file)} · ${esc(replayState.sent)}/${esc(replayState.total)} rows · recorded run, no model calls</p>`:''}
 ${live.connected?'':'<p>Not connected to the live feed. Run <code>npm start</code> from this folder with the samegrade folder next to it (or set <code>SAMEGRADE_DIR</code>).</p>'}
 ${strip}${grids?`<p class="live-legend"><span class="live-tile ok">t01</span> outcome as expected <span class="live-tile bad">t07</span> outcome off (wrong decision or failed check) · latest run per ticket</p>`+grids:''}${files.length?`<details class="live-details"><summary>Show raw rows</summary><table class="live-table"><thead><tr><th>Run</th><th>Rows</th><th>Hard checks passed</th><th>Failed</th><th>Errors</th><th>Last row</th></tr></thead><tbody>${files.map(f=>`<tr class="${Date.now()-f.last<ACTIVE_MS?'live-active':''}"><td>${name(f.file)}</td><td>${f.rows}</td><td>${f.passed}</td><td>${f.failed}</td><td>${f.errors}</td><td>${ago(f.last)}</td></tr>`).join('')}</tbody></table>`
  :'<p>No new rows yet. Use <strong>▶ Replay a run</strong> to watch a recorded run, or start a run in samegrade (for example <code>python demo_e2e.py --break u3 --tag try1</code>); each ticket appears here as soon as it finishes.</p>'}
 ${live.rows.length?`<h3>Latest rows</h3><table class="live-table"><thead><tr><th>Input</th><th>Run</th><th>Result</th><th>Hard checks</th><th>Score</th><th>File</th></tr></thead><tbody>${live.rows.slice(-40).reverse().map(r=>`<tr><td>${esc(r.input_id??'')}</td><td>${esc(r.run??'')}</td><td>${esc(r.error?`error: ${r.error}`:r.label??'')}</td><td>${checks(r)}</td><td>${typeof r.score==='number'?r.score.toFixed(2):'—'}</td><td>${name(r.file)}</td></tr>`).join('')}</tbody></table>`:''}${files.length?'</details>':''}
 ${currentReport?'<p class="provider">Reports change only when a run finishes writing them. <button class="button" id="reload-report">Reload report</button></p>':''}
 </div>`;
 liveDialog.querySelector('.close').onclick=()=>liveDialog.close();
 const reload=liveDialog.querySelector('#reload-report');
 if(reload)reload.onclick=async()=>{liveDialog.close();try{await loadReport(reportURL(currentReport));}catch(e){showError(e);}};
}
// ---- Replay a recorded run through the live view (no model calls) ----------------------------------
const replayDialog=document.querySelector('#replay');
let replayState=null;
async function drawReplay(){
 const info=currentReport?await (await fetch(`/api/replays?report=${encodeURIComponent(currentReport)}`,{cache:'no-store'})).json():{runs:[]};
 replayState=info.active||replayState;
 replayDialog.innerHTML=`<div class="detail-head"><div><p class="eyebrow">Replay a recorded run</p><h2 id="replay-title">Watch a run happen</h2></div><button class="close" aria-label="Close">×</button></div>
 <div class="detail-body"><p>Streams the rows of a run that already happened, in the order and pacing they were recorded, through the live view. <strong>No model calls, no cost</strong>; works offline.</p>
 ${info.runs.length?`<label class="replay-field">Run<select id="replay-file">${info.runs.map(r=>`<option value="${esc(r.file)}">${esc(r.role)} · ${esc(r.file.replace(/\.jsonl$/,''))} · ${r.rows} rows</option>`).join('')}</select></label>
 <label class="replay-field">Speed<select id="replay-speed"><option value="20">Compressed to 20 seconds</option><option value="60">Compressed to 1 minute</option><option value="0">Real time (as recorded)</option></select></label>
 <div class="actions"><button class="button primary" id="replay-start">Start replay</button>${replayState?.state==='running'?'<button class="button" id="replay-stop">Stop</button>':''}</div>`
 :'<p>No recorded runs found for this report.</p>'}
 ${replayState?`<p class="provider">Last replay: ${esc(replayState.file)} · ${esc(replayState.state)} · ${esc(replayState.sent)}/${esc(replayState.total)} rows</p>`:''}</div>`;
 replayDialog.querySelector('.close').onclick=()=>replayDialog.close();
 const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 const start=replayDialog.querySelector('#replay-start');
 if(start)start.onclick=async()=>{
  const r=await post('/api/replay',{report:currentReport,file:replayDialog.querySelector('#replay-file').value,seconds:Number(replayDialog.querySelector('#replay-speed').value)});
  if(!r.ok){replayDialog.querySelector('.detail-body').insertAdjacentHTML('beforeend',`<p class="error-item">Could not start: ${esc((await r.json()).error)}</p>`);return;}
  replayDialog.close();drawLive();if(!liveDialog.open)liveDialog.showModal();
 };
 const stop=replayDialog.querySelector('#replay-stop');if(stop)stop.onclick=async()=>{await post('/api/replay/stop');replayDialog.close();};
}
function installReplay(after){
 const button=document.createElement('button');button.type='button';button.className='live-button replay-button';button.textContent='▶ Replay a run';
 after.after(button);
 button.onclick=async()=>{await drawReplay();if(!replayDialog.open)replayDialog.showModal();};
 replayDialog.addEventListener('click',e=>{if(e.target===replayDialog)replayDialog.close();});
}
function installLive(){
 const label=document.querySelector('.connection');if(!label)return;
 const button=document.createElement('button');button.type='button';button.className='live-button';label.replaceWith(button);
 const paint=()=>{
  const active=[...live.files.values()].filter(f=>!f.file.startsWith('replay: ')&&Date.now()-f.last<ACTIVE_MS).length;  // real runs only
  const replaying=replayState?.state==='running';
  button.innerHTML=live.connected?`<span class="live-dot ${active||replaying?'on':''}"></span>Live · ${replaying?`replaying ${replayState.sent}/${replayState.total}`:active?`${active} run${active>1?'s':''} active`:'idle'}`:'● Local replay';
  if(liveDialog.open)drawLive();
 };
 button.onclick=()=>{drawLive();if(!liveDialog.open)liveDialog.showModal();};
 liveDialog.addEventListener('click',e=>{if(e.target===liveDialog)liveDialog.close();});
 if(window.EventSource){
  const source=new EventSource('/api/live');
  source.addEventListener('hello',e=>{const h=JSON.parse(e.data);live.connected=true;replayState=h.replay||replayState;h.recent.forEach(addLiveRow);paint();});
  source.addEventListener('row',e=>{const ev=JSON.parse(e.data);addLiveRow(ev);if(ev.replay&&replayState?.state==='running'&&ev.file===`replay: ${replayState.file}`)replayState.sent++;paint();});
  source.addEventListener('replay',e=>{replayState=JSON.parse(e.data);paint();});
  source.addEventListener('reports',e=>onReportsChanged(JSON.parse(e.data)));
  source.onerror=()=>{live.connected=false;paint();};
 }
 setInterval(paint,5000);paint();
 installReplay(button);
}

try{const r=await fetch('/api/reports',{cache:'no-store'});if(r.ok)reports=await r.json();}catch{reports=[];}
currentReport=reports[0]?.id??null;
if(reports.length)installReportPicker();
try{await loadReport(reports.length?reportURL(reports[0].id):'./results.json');}catch(error){showError(error);}
installLive();
