(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const workspace = $('#workspace'), notice = $('#notice');
  const kindNames = {auto:'自动匹配',problem:'问题分析',demand:'需求拆解',voc:'VOC 证据分析',bi:'BI 证据分析'};
  const routeNames = {problem:'问题分析',demand:'需求拆解',voc:'VOC / BI 证据分析 · VOC',bi:'VOC / BI 证据分析 · BI'};
  const stages = {analysis:'分析',definition:'需求定义',metrics:'指标定义'};
  const statuses = {queued:'排队中',running:'真实模型运行中',completed:'分析完成',failed:'运行失败'};
  const products = {deebot:'地宝',deebot_bed:'床宝',winbot:'窗宝',goat:'割草',ultramarine:'泳池',cross_product:'跨产品线'};
  const labels = {details:'分析过程',feature:'功能板块',feature_basis:'定位依据',summary:'分析结论',candidates:'候选粗需求',title:'标题',problem_statement:'问题陈述',evidence_quote:'原文证据',findings:'证据分析',observation:'观察事实',hypothesis:'原因假设',missing_information:'证据缺口',next_step:'后续验证',user_task:'用户任务',user_task_basis:'任务依据',rough_requirement:'粗需求',feature_domain_id:'功能域',feature_module_id:'功能模块',feature_reason:'定位依据',feature_confidence:'定位置信度',scenarios:'使用场景',boundaries:'范围边界',success_criteria:'成功判据',metrics:'指标',primary_metric:'主指标',guardrails:'保护指标',formula:'计算公式',baseline:'基线',target:'目标',evidence:'证据',requirements:'需求定义',specification:'需求定义',name:'名称',description:'说明',reason:'理由'};
  let state={cases:[],runs:[],pool:[]}, view='cases', selected=null, compareId=null, pendingFiles=[], fileReading=false, fileVersion=0, poolInspect=null, poolBusy=false;
  const selectedPool=new Set(),selectedRequirements=new Set();
  const fileTypes={png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',webp:'image/webp',csv:'text/csv',txt:'text/plain',json:'application/json',xlsx:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',xls:'application/vnd.ms-excel',pdf:'application/pdf',pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation',ppt:'application/vnd.ms-powerpoint'};
  const el=(p,t,s,c)=>{const n=document.createElement(t);if(s!==undefined)n.textContent=String(s);if(c)n.className=c;p.append(n);return n;};
  const button=(p,s,fn,c)=>{const b=el(p,'button',s,c);b.type='button';b.onclick=fn;return b;};
  const time=v=>v?new Date(v).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
  async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||d.message||'请求失败');return d.data||d;}
  async function refresh(render=true){try{state=await api('/api/state');for(const key of ['cases','runs','pool'])state[key].sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at)));$('#case-count').textContent=state.cases.length;const defined=definedItems();$('#pool-count').textContent=state.pool.length-defined.length;$('#requirement-count').textContent=defined.length;if(view==='pool'&&poolInspect&&defined.some(x=>x.id===poolInspect)){view='requirements';selected=poolInspect;history.replaceState(null,'','#/requirements/'+encodeURIComponent(poolInspect));notice.textContent='需求定义已完成，已进入「需求分析」板块，可在此评审与定义指标。';render=true;}if(render)draw();}catch(e){notice.textContent=`无法读取试验台：${e.message}`;}}
  function route(next,id=null){view=next;selected=id;poolInspect=['pool','requirements'].includes(next)?id:null;compareId=null;notice.textContent='';const hash='#/'+next+(id?'/'+encodeURIComponent(id):'');if(location.hash!==hash)location.hash=hash;draw();window.scrollTo?.({top:0,behavior:'instant'});}
  function readRoute(render=true){
    const match=location.hash.match(/^#\/?(cases|pool|requirements|evaluation|tracking)(?:\/([^/]+))?$/);
    let next=match?match[1]:'cases',id=null;
    try{id=match?.[2]?decodeURIComponent(match[2]):null;}catch{next='cases';}
    if(next==='evaluation'||next==='tracking')id=null;
    const changed=view!==next||selected!==id;view=next;selected=id;
    poolInspect=['pool','requirements'].includes(view)?id:null;compareId=null;
    if(render&&changed){notice.textContent='';draw();window.scrollTo?.({top:0,behavior:'instant'});}
  }
  function draw(){workspace.dataset.view=view;workspace.replaceChildren();document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('#page-title').textContent={cases:selected?'案例分析工作台':'导入材料，自动匹配分析 Skill。',pool:'需求池',requirements:selected?'需求详情':'需求分析',evaluation:'让每一次迭代都有依据。',tracking:'Tracking · 告警到验证闭环'}[view];if(view==='tracking'){window.TrackingLab.render(workspace,{api,notice,refresh});}else if(['pool','requirements'].includes(view)&&selected)drawRequirementDetail();else if(view==='pool')drawPool();else if(view==='requirements')drawRequirements();else if(view==='evaluation')drawEvaluation();else if(selected)drawCase();else drawCases();}
  function empty(p,title,body){const d=el(p,'div',undefined,'empty');el(d,'strong',title);el(d,'p',body);return d;}
  function latestAnalysis(c){return state.runs.filter(r=>r.case_id===c.id&&r.stage==='analysis'&&r.status==='completed').slice().reverse()[0];}
  function analysisStatus(r){return !r?'待分析':({queued:'分析中 · 排队中',running:'分析中',completed:'分析成功',failed:'分析失败'})[r.status]||r.status;}
  function publishedCount(r){return r.pool_publication?.count??state.pool.filter(item=>item.run_id===r.id).length;}
  function matchedRoute(c){const routing=latestAnalysis(c)?.result?.routing;return routing?.category?routeNames[routing.category]||routing.category:null;}
  function drawCases(){const intro=el(workspace,'section',undefined,'panel intro');const text=el(intro,'div');el(text,'h3','一个入口，自动选择分析方式');el(text,'p','提供原始材料后自动匹配 Skill 并分析；分析生成的粗需求直接进入需求池，无需再次确认导入。','muted');const steps=el(intro,'div',undefined,'steps');['01 统一导入','→','02 自动匹配并分析','→','03 自动进入需求池'].forEach(s=>el(steps,'span',s));el(workspace,'div',`${state.cases.length} 个测试案例 · 数据仅保存在本机`,'section-label');if(!state.cases.length){empty(workspace,'导入你手头的任意材料','支持问题截图、粗需求文字、VOC 文件与 BI 数据；无需预先判断该用哪个 Skill。');return;}const grid=el(workspace,'div',undefined,'case-grid');for(const c of [...state.cases].reverse()){const card=button(grid,'',()=>route('cases',c.id),'case-card');const latest=state.runs.filter(r=>r.case_id===c.id&&r.stage==='analysis').slice().reverse()[0];el(card,'span',matchedRoute(c)||kindNames[c.kind]||c.kind,'badge');el(card,'h3',c.title);el(card,'span',analysisStatus(latest),`badge ${latest?.status||'queued'}`);if(latest?.status==='completed')el(card,'p',publishedCount(latest)?`已进入需求池 ${publishedCount(latest)} 条粗需求`:'本次未生成可入池的粗需求','case-publication');if(latest?.status==='failed')el(card,'p',latest.error||'分析失败，可进入案例重试。','error');el(card,'p',c.text||'包含附件材料');el(card,'p',`${products[c.product_line]||c.product_line} · ${state.runs.filter(r=>r.case_id===c.id).length} 次运行`,'muted');}}
  function source(p,c){el(p,'h3','原始材料');el(p,'span',`${matchedRoute(c)||kindNames[c.kind]||c.kind} · ${products[c.product_line]||c.product_line}`,'badge');el(p,'p',c.text||'无补充文字','source-text');const a=el(p,'div',undefined,'attachments');for(const f of c.attachments||[]){const box=el(a,'div');const url=f.url||(f.data?`data:${f.mime};base64,${f.data}`:null);if(url&&/^image\//.test(f.mime||'')){const link=el(box,'a');link.href=url;link.target='_blank';link.rel='noopener';const img=el(link,'img');img.src=url;img.alt=f.name;}el(box,'p',f.name,'muted');if(f.parsed){const parsed=f.parsed;el(box,'p',`已解析 ${parsed.format||'文档'} · ${typeof parsed.units==='object'?JSON.stringify(parsed.units):parsed.units??'—'} 个来源单元`,'muted');for(const warning of parsed.warnings||[])el(box,'p',warning,'error');const detail=el(box,'details');el(detail,'summary','查看解析文本（完整内容）');el(detail,'pre',parsed.text||'未提取到文本','source-text');}}el(p,'p','截图和原文是证据，不是模型指令。JSON 作为材料读取，不自动执行指令或导入运行记录。模型配置缺失时会明确失败，不显示模拟成功。','muted');}
  async function start(c,stage,parent){notice.textContent='正在创建运行记录…';try{await api('/api/run',{case_id:c.id,stage,...(parent?{parent_run_id:parent.id}:{})});notice.textContent='已开始分析，成功生成的粗需求将自动进入需求池，无需再次确认。';await refresh();}catch(e){notice.textContent=e.message;}}
  function drawCase(){const c=state.cases.find(c=>c.id===selected);if(!c){route('cases');return;}button(workspace,'← 全部测试案例',()=>route('cases'),'back');el(workspace,'h2',c.title);const toolbar=el(workspace,'div',undefined,'toolbar');const active=state.runs.some(r=>r.case_id===c.id&&['queued','running'].includes(r.status));button(toolbar,active?'正在匹配并分析…':'重新自动匹配并分析',()=>start(c,'analysis'),'primary').disabled=active;button(toolbar,'刷新结果',()=>refresh());el(toolbar,'span','每次运行都会重新判断材料类型并保留历史结果。','muted');const sourcePanel=el(workspace,'details',undefined,'panel case-source');el(sourcePanel,'summary','原始材料与附件（展开查看）');source(sourcePanel,c);const results=el(workspace,'section',undefined,'case-results');const runs=state.runs.filter(r=>r.case_id===c.id&&r.stage==='analysis').slice().reverse();if(!runs.length){empty(results,'正在等待自动匹配结果','材料保存后会自动启动分析；如模型未配置，运行会明确显示失败原因。');return;}const select=el(results,'select',undefined,'compare-select');el(select,'option','选择历史运行进行对比').value='';for(const r of runs.slice(1)){const o=el(select,'option',`${stages[r.stage]} · ${time(r.created_at)} · ${r.id.slice(-6)}`);o.value=r.id;}select.value=compareId||'';select.onchange=()=>{compareId=select.value;draw();};const compare=runs.find(r=>r.id===compareId);const container=el(results,'div',undefined,compare?'compare':'');runCard(container,runs[0],c);if(compare)runCard(container,compare,c);if(!compare){const history=el(results,'details');el(history,'summary',`历史运行（${runs.length-1}）`);runs.slice(1).forEach(r=>runCard(history,r,c));}}
  function vocSources(c){
    const records=[];
    for(const file of c.attachments||[]){
      if(file.parsed?.format!=='json')continue;
      let data;try{const text=file.parsed.text;data=JSON.parse(text.slice(text.indexOf('\n')+1));}catch{continue;}
      function walk(value,path){
        if(!value||typeof value!=='object')return;
        if(!Array.isArray(value)){
          const key=['comment_content','review_content','feedback_text','content','comment'].find(k=>typeof value[k]==='string'&&value[k].trim().length>=8);
          if(key)records.push({text:value[key],location:`${file.name} · ${path}.${key}`,id:value.id||value.feedback_id||value.index_id||''});
        }
        for(const [key,item] of Object.entries(value))if(item&&typeof item==='object')walk(item,Array.isArray(value)?`${path}[${key}]`:`${path}.${key}`);
      }
      walk(data,'$');
    }
    return records;
  }
  function renderResult(p,r,c){
    const result=r.result;
    if(r.stage==='analysis'&&result.routing){
      const route=el(p,'section',undefined,'route-decision');
      const heading=el(route,'div',undefined,'route-heading');
      const title=el(heading,'div');el(title,'span','自动匹配结果','route-kicker');el(title,'h3',routeNames[result.routing.category]||result.routing.category);
      el(heading,'strong',`${Math.round(Number(result.routing.confidence||0)*100)}% 匹配`,'route-confidence');
      el(route,'p',result.routing.reason,'route-reason');
      const signals=el(route,'div',undefined,'route-signals');for(const signal of result.routing.signals||[])el(signals,'span',signal);
      const detail=el(route,'details');el(detail,'summary','查看调用的 Skill');el(detail,'p',(result.routing.skill_names||[]).join(' ＋ '),'muted');
    }
    if(r.stage==='analysis'){const overview=el(p,'details',undefined,'analysis-notes');el(overview,'summary','分析概览与处理说明');el(overview,'p',result.summary,'analysis-summary');}
    if(r.stage!=='analysis'){renderDefinition(p,r);return;}
    const candidates=result.candidates||[],sources=vocSources(c);
    const toolbar=el(p,'div',undefined,'candidate-toolbar');
    const search=el(toolbar,'input');search.placeholder='搜索需求、功能或证据';search.setAttribute('aria-label','搜索候选需求');
    const filter=el(toolbar,'select');filter.setAttribute('aria-label','筛选功能板块');el(filter,'option','全部功能板块').value='';
    for(const feature of [...new Set(candidates.map(x=>x.feature||'未提供'))].sort()){const o=el(filter,'option',feature);o.value=feature;}
    const count=el(toolbar,'span',undefined,'muted');
    const wrap=el(p,'div',undefined,'candidate-table-wrap');const table=el(wrap,'table',undefined,'candidate-table');
    const head=el(el(table,'thead'),'tr');['序号','得出的粗需求','功能板块','实际 VOC / 原始证据','分析详情'].forEach((v,i)=>el(head,'th',v,['col-num','col-demand','col-feature','col-evidence','col-detail'][i]));
    const body=el(table,'tbody');
    function rows(){
      body.replaceChildren();const query=search.value.trim().toLocaleLowerCase();
      let shown=0;
      candidates.forEach((candidate,index)=>{
        if(filter.value&&filter.value!==(candidate.feature||'未提供'))return;
        if(query&&!JSON.stringify(candidate).toLocaleLowerCase().includes(query))return;
        shown++;const row=el(body,'tr');el(row,'td',index+1);
        const demand=el(row,'td');el(demand,'strong',candidate.title);el(demand,'p',candidate.rough_requirement||candidate.problem_statement);
        const feature=el(row,'td');el(feature,'span',candidate.feature||'未提供','feature-label');
        const evidence=el(row,'td');const quote=String(candidate.evidence||candidate.evidence_quote||'');
        const compact=quote.replace(/\s/g,'');const matches=sources.filter(x=>compact.includes(x.text.replace(/\s/g,'')));
        if(matches.length){
          el(evidence,'p',`原文匹配 ${matches.length} 条记录（不等同独立用户数）`,'muted');
          function show(target,record){el(target,'blockquote',record.text,'evidence-quote');const location=el(target,'p',record.id?`ID ${record.id}`:`原始位置：${record.location.split(' · ').slice(1).join(' · ')}`,'muted');location.title=record.location;}
          matches.slice(0,1).forEach(record=>show(evidence,record));
          if(matches.length>1){const more=el(evidence,'details');el(more,'summary',`展开其余 ${matches.length-1} 条原文`);matches.slice(1).forEach(record=>show(more,record));}
        }else{
          el(evidence,'p',sources.length?'未匹配到原始反馈；以下仅为模型引用':'模型引用（尚未逐条核对原始材料）','muted');
          el(evidence,'blockquote',quote||'未提供证据','evidence-quote');
        }
        const detail=el(el(row,'td'),'details',undefined,'candidate-detail');el(detail,'summary','展开详情');
        structured(detail,{problem_statement:candidate.problem_statement,user_task:candidate.user_task,feature_basis:candidate.feature_basis||candidate.feature_reason,missing_information:candidate.missing_information});
        if(matches.length){el(detail,'h4','模型证据表述');el(detail,'p',quote);}
      });
      count.textContent=`显示 ${shown} / ${candidates.length} 条粗需求`;
      if(!shown)el(el(body,'tr'),'td',candidates.length?'没有符合筛选条件的需求':'本次未生成候选粗需求').colSpan=5;
    }
    search.oninput=filter.onchange=rows;rows();
    const detail=el(p,'details',undefined,'analysis-notes');el(detail,'summary','完整分析过程与证据缺口');structured(detail,{details:result.details,missing_information:result.missing_information});
  }
  function contentSection(parent,title,items,cls=''){
    const section=el(parent,'section',undefined,`definition-section ${cls}`);el(section,'h4',title);
    if(Array.isArray(items)){if(!items.length)el(section,'p','暂无补充','muted');else{const list=el(section,'ul');items.forEach(text=>el(list,'li',text));}}
    else el(section,'p',items||'未提供');return section;
  }
  function renderDefinition(p,r){
    const result=r.result;
    if(!r.pool_item_id||(!result.definition&&!Array.isArray(result.metrics))){
      el(p,'p','这是旧版未按单条需求绑定的结果，不用于当前需求的定义或指标依据。请从需求池重新生成。','error');
      const legacy=el(p,'details');el(legacy,'summary','查看旧版结果（仅供排查）');structured(legacy,result);return;
    }
    if(result.definition){const def=result.definition;
      const hero=el(p,'section',undefined,'definition-hero');el(hero,'h3',def.title||r.requirement_title);el(hero,'p',def.problem);
      contentSection(p,'用户要完成的任务',def.user_task);
      contentSection(p,'使用场景',def.scenarios);
      const scope=el(p,'div',undefined,'definition-scope');contentSection(scope,'本次解决',def.in_scope,'scope-in');contentSection(scope,'本次不解决',def.out_of_scope,'scope-out');
      contentSection(p,'验收标准',def.acceptance_criteria,'criteria');
      const evidence=el(p,'details',undefined,'definition-evidence');el(evidence,'summary','支持该需求的证据');contentSection(evidence,'原始证据',def.evidence);
    }
    if(Array.isArray(result.metrics)){
      const wrap=el(p,'div',undefined,'candidate-table-wrap');const table=el(wrap,'table',undefined,'metric-table');const head=el(el(table,'thead'),'tr');
      ['指标 / 用途','口径与计算','数据来源','基线与目标','验证方式'].forEach(x=>el(head,'th',x));const body=el(table,'tbody');
      result.metrics.forEach(metric=>{const row=el(body,'tr');const name=el(row,'td');el(name,'strong',metric.name);el(name,'p',metric.role,'muted');const formula=el(row,'td');el(formula,'p',metric.definition);el(formula,'p',metric.formula,'formula');el(row,'td',metric.data_source);const target=el(row,'td');el(target,'p',`当前基线：${metric.baseline}`);el(target,'p',`目标：${metric.target}`);el(row,'td',metric.validation);});
    }
    if(result.missing_information?.length)contentSection(p,'证据与测量待补项',result.missing_information,'definition-gaps');
  }
  function structured(p,value,depth=0){if(value===null||value===undefined){el(p,'p','未提供');return;}if(typeof value!=='object'){el(p,'p',value);return;}if(Array.isArray(value)){if(!value.length){el(p,'p','无','muted');return;}value.forEach((v,i)=>{const box=el(p,'div',undefined,typeof v==='object'?'result-object':'');if(typeof v==='object')el(box,'h4',`条目 ${i+1}`);structured(box,v,depth+1);});return;}for(const [k,v]of Object.entries(value)){const box=el(p,'div',undefined,'result-field');el(box,'strong',labels[k]||k);if(depth>6)el(box,'p',JSON.stringify(v));else structured(box,v,depth+1);}}
  function runCard(p,r,c){
    const card=el(p,'article',undefined,'run-card');const heading=el(card,'div',undefined,'run-title');
    el(heading,'h3',r.requirement_title?`${r.requirement_title} · ${stages[r.stage]}`:stages[r.stage]||r.stage);
    el(heading,'span',r.stage==='analysis'?analysisStatus(r):statuses[r.status]||r.status,`badge ${r.status}`);el(card,'p',time(r.created_at),'muted');
    if(r.stage==='analysis'&&r.status==='completed'){
      const published=el(card,'section',undefined,'analysis-publication');const count=publishedCount(r);
      if(r.pool_publication?.status==='blocked'){el(published,'strong','分析已完成，但粗需求入池受阻','error');el(published,'p',r.pool_publication.error||'结果结构不完整，请重新分析。','error');}
      else if(count){el(published,'strong',`已自动进入需求池 ${count} 条粗需求`);el(published,'p','无需在案例页确认或手动导入，可直接到需求池继续处理。','muted');button(published,'查看需求池',()=>route('pool'),'primary');}
      else el(published,'p',r.review?.decision==='reject'?'历史分析结果已驳回，未自动入池；可重新分析。':'分析成功，本次未生成可入池的粗需求。');
    }
    if(r.audit){const a=el(card,'details',undefined,'technical-record');el(a,'summary','技术记录（排查时查看）');el(a,'pre',JSON.stringify(r.audit,null,2),'audit');}
    if(r.error)el(card,'p',r.error,'error');if(r.result)renderResult(card,r,c);
    if(r.status!=='completed'||(r.stage!=='analysis'&&!r.pool_item_id))return;
    if(r.stage==='analysis'){
      if(r.review){const history=el(card,'details',undefined,'analysis-notes');el(history,'summary','历史评价记录（只读）');el(history,'p',r.review.decision==='accept'?'历史评价：已采纳':'历史评价：已驳回');el(history,'p',r.review.note||'无备注','muted');if(r.review.corrected_text)el(history,'p',`历史人工修正：${r.review.corrected_text}`);}
      return;
    }
    if(r.review){el(card,'p',r.review.decision==='accept'?'已采纳':'已驳回',`badge ${r.review.decision}`);el(card,'p',r.review.note||'无备注','muted');if(r.review.corrected_text)el(card,'p',`人工修正：${r.review.corrected_text}`);if(r.stage==='metrics'&&r.review.decision==='accept'){if(r.tracking_publication?.status==='published'){el(card,'p',`已自动创建 ${r.tracking_publication.count} 条 Tracking 验证草稿。`,'muted');button(card,'进入 Tracking 配置验证计划',()=>{window.TrackingLab.open(r.tracking_publication.tracking_ids[0]);route('tracking');},'primary');}else if(r.tracking_publication?.status==='blocked')el(card,'p',`Tracking 建立失败：${r.tracking_publication.error}`,'error');}}
    else{const form=el(card,'div',undefined,'review-form');el(form,'h4','评价这份结果');const note=el(form,'textarea');note.placeholder='评价与修改建议';const correction=el(form,'textarea');correction.placeholder='可选：人工修正内容（保留模型原文）';
      const actions=el(form,'div',undefined,'toolbar');
      for(const [decision,label] of [['accept','采纳结果'],['reject','驳回结果']])button(actions,label,async()=>{try{await api('/api/review',{run_id:r.id,decision,note:note.value,corrected_text:correction.value});await refresh();}catch(e){notice.textContent=e.message;}},decision==='accept'?'primary':'');
    }
    if(r.review?.decision==='accept'&&r.stage==='definition'&&r.pool_item_id)button(card,'为本条需求生成指标',()=>runPool([r.pool_item_id],'metrics'),'primary');
  }
  function poolRuns(id){return state.runs.filter(r=>r.pool_item_id===id).slice().reverse();}
  function latestDefinition(id){return poolRuns(id).find(r=>r.stage==='definition'&&r.status==='completed'&&r.review?.decision==='accept');}
  async function runPool(ids,stage){
    if(!ids.length){notice.textContent='请先选择需求。';return;}
    if(ids.length>20){notice.textContent='每批最多处理 20 条需求，请减少选择。';return;}
    poolBusy=true;notice.textContent='正在按单条需求提交…';draw();
    try{
      const response=await api('/api/pool/run',{pool_item_ids:ids,stage});
      const runs=response.runs||[],errors=response.errors||[];
      notice.textContent=`已提交 ${runs.length} 条${stages[stage]}。${errors.length?'未提交 '+errors.length+' 条：'+errors.map(e=>e.error||e.message||String(e)).join('；'):''}`;
      await refresh(false);if(ids.length===1)showRequirement(ids[0]);
    }catch(e){notice.textContent=e.message;}
    finally{poolBusy=false;draw();}
  }
  function definedItems(){
    const map=new Map((state.requirements||[]).map(item=>[item.id,item]));
    for(const item of state.pool){
      const definition=poolRuns(item.id).find(r=>r.stage==='definition'&&r.status==='completed'&&r.result?.definition);
      if(definition)map.set(item.id,{...item,definition_run_id:definition.id,definition_review_status:definition.review?.decision||'pending',definition_completed_at:definition.finished_at});
    }
    return [...map.values()].sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at)));
  }
  function drawPool(){drawRequirementList(false);}
  function drawRequirements(){drawRequirementList(true);}
  function drawRequirementList(defined){
    const completed=definedItems(),ids=new Set(completed.map(x=>x.id));
    const items=defined?completed:state.pool.filter(x=>!ids.has(x.id));
    const selection=defined?selectedRequirements:selectedPool;
    el(workspace,'p',defined?'已生成需求定义的需求集中在这里，继续评审定义、定义指标并查看版本。定义生成不代表已采纳。':'待定义的粗需求。定义完成后自动进入「需求分析」，此处保留未处理、运行中或失败的需求。','muted');
    if(!items.length){empty(workspace,defined?'还没有已定义的需求':'当前没有待定义需求',defined?'从需求池选择需求并生成定义，成功后自动进入这里。':'已定义的需求请到左侧「需求分析」查看。');return;}
    const existing=new Set(items.map(x=>x.id));for(const id of selection)if(!existing.has(id))selection.delete(id);
    const wrap=el(workspace,'div',undefined,'candidate-table-wrap pool-table-wrap');const table=el(wrap,'table',undefined,'candidate-table pool-table');const head=el(el(table,'thead'),'tr');
    const check=el(el(head,'th'),'input');check.type='checkbox';check.setAttribute('aria-label',defined?'选择全部已定义需求':'选择全部待定义需求');check.checked=selection.size===items.length;check.indeterminate=selection.size>0&&!check.checked;
    check.onchange=()=>{items.forEach(item=>check.checked?selection.add(item.id):selection.delete(item.id));draw();};
    ['需求','功能板块',defined?'定义评审 / 指标进度':'处理进度','操作'].forEach(title=>el(head,'th',title));const body=el(table,'tbody');
    for(const item of [...items].reverse()){
      const row=el(body,'tr');const cell=el(row,'td');const box=el(cell,'input');box.type='checkbox';box.checked=selection.has(item.id);box.setAttribute('aria-label',`选择需求 ${item.title||item.candidate?.title}`);box.onchange=()=>{box.checked?selection.add(item.id):selection.delete(item.id);draw();};
      const candidate=item.candidate||item,content=el(row,'td');el(content,'strong',candidate.title||item.title);el(content,'p',candidate.rough_requirement||candidate.problem_statement);
      el(row,'td',candidate.feature||'未提供');const progress=el(row,'td'),runs=poolRuns(item.id);
      if(defined){el(progress,'p',`需求定义：${{pending:'已生成 · 待评审',accept:'已采纳',reject:'需修改'}[item.definition_review_status]||'待评审'}`);const metric=runs.find(r=>r.stage==='metrics');el(progress,'p',`指标定义：${metric?statuses[metric.status]+(metric.review?.decision==='accept'?' · 已采纳':''):'未处理'}`);const linked=(state.tracking||[]).filter(t=>t.lineage?.requirement_id===item.id);if(linked.length)el(progress,'p',`Tracking：${linked.map(t=>t.status==='closed'?'已闭环':t.status==='pending_configuration'?'待配置':'进行中').join(' / ')}`);}
      else{const latest=runs.find(r=>r.stage==='definition');el(progress,'p',`需求定义：${latest?statuses[latest.status]:'未处理'}`);}
      const actions=el(row,'td',undefined,'pool-actions');const active=stage=>runs.some(r=>r.stage===stage&&['running','queued'].includes(r.status));
      if(defined)button(actions,'查看定义',()=>showRequirement(item.id),'primary');
      button(actions,defined?'重新定义':'生成需求定义',()=>runPool([item.id],'definition'),defined?'':'primary').disabled=poolBusy||active('definition');
      if(defined){const metric=button(actions,'指标定义',()=>runPool([item.id],'metrics'));metric.disabled=poolBusy||active('metrics')||!latestDefinition(item.id);metric.title=latestDefinition(item.id)?'仅使用本条需求已采纳的定义':'先采纳本条需求定义';}
      else button(actions,runs.length?'查看运行':'查看需求',()=>showRequirement(item.id));
    }
    const footer=el(workspace,'div',undefined,'pool-batch-footer');el(footer,'strong',`已选择 ${selection.size} 条需求`);
    button(footer,defined?'批量重新定义':'批量需求定义',()=>runPool([...selection],'definition'),'primary').disabled=poolBusy||!selection.size;
    if(defined)button(footer,'批量指标定义',()=>runPool([...selection],'metrics')).disabled=poolBusy||!selection.size;
    el(footer,'span',defined?'每批最多 20 条；先采纳对应定义，再生成指标。':'每批最多 20 条；生成成功后自动进入「需求分析」。','muted');

  }
  function showRequirement(id){route(definedItems().some(x=>x.id===id)?'requirements':'pool',id);}
  function drawRequirementDetail(){
    const parent=view==='requirements'?'需求分析':'需求池';
    const breadcrumb=el(workspace,'nav',undefined,'detail-breadcrumb');
    const back=el(breadcrumb,'a','← 返回'+parent);back.href='#/'+view;
    el(breadcrumb,'span',' / 需求详情');
    const item=state.pool.find(x=>x.id===selected);
    if(!item){empty(workspace,'未找到该需求','需求可能不存在，请返回列表重新选择。');return;}
    drawRequirementInspector(item);
  }
  function drawRequirementInspector(item){
    const inspector=el(workspace,'section',undefined,'pool-inspector panel');el(inspector,'h2',item.candidate?.title||item.title);
    const sourceInfo=el(inspector,'details');el(sourceInfo,'summary','查看本条需求及证据');structured(sourceInfo,item.candidate||item);
    const runs=poolRuns(item.id),c=state.cases.find(x=>x.id===item.case_id);
    if(!runs.length)empty(inspector,'本条需求尚未生成定义','使用本行操作或批量选择开始处理。');
    for(const stage of ['definition','metrics']){const relevant=runs.filter(r=>r.stage===stage);if(!relevant.length)continue;el(inspector,'h3',stages[stage]);runCard(inspector,relevant[0],c);
      if(relevant.length>1){const history=el(inspector,'details');el(history,'summary',`本条需求的历史${stages[stage]}（${relevant.length-1}）`);relevant.slice(1).forEach(r=>runCard(history,r,c));}}
  }
  function drawEvaluation(){el(workspace,'p','同一份材料可重复运行。比较 Skill 版本、模型输出与人工评价，识别退步和改善。','muted');if(!state.runs.length){empty(workspace,'还没有可评测的运行','每次运行都会留下独立记录，模型失败同样可追溯。');return;}const panel=el(workspace,'section',undefined,'panel');for(const r of [...state.runs].reverse()){const c=state.cases.find(c=>c.id===r.case_id);const row=el(panel,'div',undefined,'evaluation-row');const d=el(row,'div');el(d,'strong',c?.title||r.case_id);el(d,'p',`${stages[r.stage]||r.stage} · ${time(r.created_at)} · ${r.review?(r.review.decision==='accept'?'已采纳':'已驳回'):'未评价'}`,'muted');el(row,'span',statuses[r.status]||r.status,`badge ${r.status}`);button(row,'查看 / 对比',()=>{if(r.pool_item_id){showRequirement(r.pool_item_id);}else route('cases',r.case_id);});}}
  function openDialog(){fileVersion++;fileReading=false;pendingFiles=[];$('#case-form').reset();$('#case-form [type=submit]').disabled=false;$('#file-preview').replaceChildren();$('#form-error').textContent='';$('#case-dialog').showModal();}
  $('#new-case').onclick=openDialog;$('#close-dialog').onclick=$('#cancel-dialog').onclick=()=>$('#case-dialog').close();document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>route(b.dataset.view));
  $('#files').onchange=async e=>{const files=[...e.target.files],version=++fileVersion,loaded=[];$('#form-error').textContent='';pendingFiles=[];$('#file-preview').replaceChildren();fileReading=true;$('#case-form [type=submit]').disabled=true;try{if(files.length>8)throw Error('最多上传 8 个文件。');if(files.some(f=>f.size>5*1024*1024)||files.reduce((s,f)=>s+f.size,0)>10*1024*1024)throw Error('文件超过大小限制，请压缩后上传。');for(const f of files){const extension=f.name.split('.').pop().toLowerCase(),mime=fileTypes[extension];if(!mime)throw Error('仅支持图片、CSV/TXT、Excel、PDF、PowerPoint 和 JSON。');const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(Error('文件读取失败'));reader.readAsDataURL(f);});if(version!==fileVersion)return;loaded.push({name:f.name,mime,data:data.split(',')[1]});const box=el($('#file-preview'),'div');if(mime.startsWith('image/')){const img=el(box,'img');img.src=data;img.alt=f.name;}el(box,'span',f.name);}pendingFiles=loaded;}catch(err){if(version===fileVersion){pendingFiles=[];$('#file-preview').replaceChildren();$('#form-error').textContent=err.message||'文件读取失败';}}finally{if(version===fileVersion){fileReading=false;$('#case-form [type=submit]').disabled=false;}}};
  $('#case-form').onsubmit=async e=>{e.preventDefault();const f=e.target,submit=f.querySelector('[type=submit]');if(fileReading){$('#form-error').textContent='文件正在读取，请稍候。';return;}if(!f.text.value.trim()&&!pendingFiles.length){$('#form-error').textContent='请提供原文或附件。';return;}submit.disabled=true;try{const c=await api('/api/cases',{title:f.title.value,kind:'auto',product_line:f.product_line.value,text:f.text.value,attachments:pendingFiles});await api('/api/run',{case_id:c.id,stage:'analysis'});$('#case-dialog').close();await refresh(false);route('cases',c.id);notice.textContent='材料已导入，系统正在自动匹配并运行对应 Skill。';}catch(err){$('#form-error').textContent=err.message;}finally{submit.disabled=fileReading;}};
  window.addEventListener('hashchange',()=>readRoute());readRoute(false);
  refresh();setInterval(()=>{if(view!=='tracking'&&!$('#case-dialog').open&&!workspace.querySelector('textarea:focus'))refresh(state.runs.some(r=>['queued','running'].includes(r.status)));},4000);
})();
