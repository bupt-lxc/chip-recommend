/* Per-session run cards. No raw database paths are accepted from the browser. */
(() => {
  const state={mode:null,offset:0,limit:10,timer:null,generation:0,restore:null};
  const storageKey='data-agent-run-cards-v1';
  function saveView(){
    const root=document.getElementById('run-history');if(!root||!state.mode)return;
    const cards=[...root.querySelectorAll('.run-history-card')].map(c=>({key:c.dataset.runKey,open:c.open,sections:[...c.querySelectorAll('[data-run-section][open]')].map(x=>x.dataset.runSection),jobs:[...c.querySelectorAll('[data-job-id][open]')].map(x=>x.dataset.jobId)}));
    const openWeb=[...document.querySelectorAll('#open-web-test-history .open-web-run')].map(c=>({session:c.dataset.openWebSession,open:c.open,view:captureBodyView(c.querySelector('.run-card-body'))}));
    try{sessionStorage.setItem(storageKey,JSON.stringify({mode:state.mode,offset:state.offset,cards,openWeb,scrollY:scrollY}))}catch(_){}
  }
  addEventListener('pagehide',saveView);
  const labels={preparing:'准备中',running:'进行中',in_progress:'进行中',queued:'等待',pending:'待处理',retry_wait:'待重试',awaiting_agent:'等待提取',awaiting_publish:'等待保存测试结果',success:'完成',no_findings:'未发现可用信息',succeeded:'可用',partial:'部分完成',failed:'失败',rejected:'拒绝',program_rejected:'程序拒绝',semantic_rejected:'语义拒绝',interrupted:'已中断',not_run:'未执行',ready_to_publish:'已保存到测试区',published:'已保存',new:'新网页'};
  const safeDisplayString=v=>v==null?'未记录':typeof v==='object'?JSON.stringify(v):String(v);
  const txt=v=>esc(safeDisplayString(v)).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const badge=s=>`<span class="run-badge ${txt(s)}">${txt(labels[s]||s)}</span>`;
  const value=v=>v==null?'未记录':v===''?'空值':typeof v==='object'?JSON.stringify(v):String(v);
  function link(url){
    if(url==null||url==='')return '<span>来源未记录</span>';
    if(typeof url!=='string')return txt(url);
    try{const u=new URL(url);if(!['http:','https:'].includes(u.protocol))return txt(url);return `<a href="${txt(redactDataAgentUrl(u.href))}" target="_blank" rel="noopener noreferrer">${txt(redactDataAgentUrl(u.href))}</a>`}catch(_){return txt(url)}
  }
  const duration=r=>{const a=Date.parse(r.started_at),b=r.active?Date.now():Date.parse(r.finished_at);return Number.isFinite(a)&&Number.isFinite(b)?Math.max(0,(b-a)/1000).toFixed(1)+' 秒':'未记录'};
  function summary(r){const s=r.statuses||{};return `<div class="run-card-head"><div><h3>周期 #${Number(r.id)} · ${txt(r.title)}<span class="run-title-hint">展开详情 ↓</span></h3><div class="run-card-meta">${r.session==='formal'?'正式运行':'隔离测试'} · 会话 ${txt(r.session)}<br>开始 ${txt(formatUpdateTime(r.started_at))} · ${r.active?'尚未结束':'结束 '+txt(formatUpdateTime(r.finished_at))} · 耗时 ${duration(r)}</div></div>${badge(r.status)}</div><div class="run-card-stats"><span class="run-card-stat">成功 ${Number(s.succeeded||0)}</span><span class="run-card-stat">失败 ${Number(s.failed||0)}</span><span class="run-card-stat">拒绝 ${Number(s.rejected||0)}</span><span class="run-card-stat">执行/等待 ${Number(s.running||0)+Number(s.queued||0)+Number(s.awaiting_agent||0)}</span><span class="run-card-stat">候选字段 ${Number(r.candidate_count||0)}</span></div>`}
  function fieldTable(rows){
    if(!rows.length)return '<div class="run-note">尚无候选字段。计划检查的字段不代表一定会更新。</div>';
    const basis={candidate_snapshot:'候选生成时快照',publication_record:'发布前记录',current_reference:'当前库参考值 · 历史旧值未记录',unknown:'旧值未记录'};
    return '<p class="run-table-hint">窄屏可左右滑动查看全部五列</p><div class="run-field-scroll" tabindex="0" aria-label="字段比较五列表格"><table class="run-field-table"><colgroup><col><col><col><col><col></colgroup><thead><tr><th>实体 / 字段</th><th>原值与原来源</th><th>候选新值与新来源</th><th>原文证据</th><th>变更与状态</th></tr></thead><tbody>'+rows.map(c=>{
      const v3=Boolean(c.baseline_display),ev=c.candidate?.evidence||{};
      const old=c.baseline_display||{value:value(c.old_value),basis_label:basis[c.old_value_basis]||'旧值未记录',time:null};
      const identity=safeDisplayString(c.entity?.chip_model??c.entity?.model_name??c.entity?.name??'实体未确认');
      return `<tr data-v3-row data-entity="${txt(identity.toLowerCase())}" data-domain="${txt(c.domain_id||'')}" data-value-change="${txt(c.value_change_kind||'unknown')}" data-source-change="${txt(c.source_change_kind||'unknown')}" data-review="${txt(c.review_status||'not_executed')}"><td><b>${txt(identity)}</b><br>${txt(c.target_table)}.<wbr>${txt(c.field_name)}<div class="run-note">${txt(c.entity?.vendor||c.entity?.hint?.vendor||'')}<br>候选 <span title="${txt(c.id)}">${txt(v3?String(c.id).slice(-8):c.id)}</span> · ${txt(v3?(domainNames[c.domain_id]||c.domain_id):c.owner_skill)}</div>${v3?section('field-entity-'+c.id,'完整实体与关系',jsonBlock({entity:c.entity,entity_key:c.candidate?.entity_key,conditions:c.conditions,comparison_key:c.comparison_key})):''}</td><td><div class="run-value">${txt(old.value)}</div><div class="run-note">${txt(old.basis_label)}${old.time?'<br>快照时间 '+txt(old.time):''}</div>${(c.old_sources||[]).map(s=>`<div class="run-links">${link(s.source_url||s.url)}<br>${txt(s.source_type)} · ${txt(s.updated_at||s.verified_evidence?.captured_at)}${v3?'<br>旧证据：'+txt(s.evidence_status||s.verified_evidence?.status||'未记录（质量未知）'):''}</div>`).join('')||'<div class="run-note">原来源未记录</div>'}</td><td><div class="run-value run-new-value">${txt(value(c.proposed_value))}</div><div class="run-links">${link(c.source_url)}</div><div class="run-note">${txt(c.source_type)} · 置信度 ${txt(c.confidence)}</div>${v3?section('field-normalized-'+c.id,'原值、单位与换算',jsonBlock({raw_value:c.candidate?.raw_value,normalized_value:c.proposed_value,unit:c.candidate?.unit,conversion:c.candidate?.conversion})):''}</td><td>${renderEvidence(c)}${v3?`${section('field-proof-'+c.id,'证据与规则记录',jsonBlock({raw_candidate:c.raw_candidate,candidate_source_ref:c.candidate_source_ref,evidence:ev,context_refs:c.candidate?.context_refs,evidence_kind:c.candidate?.evidence_kind,logical_candidate_id:c.id,model_proof:c.model_proof,revision_chain:c.revision_chain,domain_contract_ref:c.domain_contract_ref,evidence_versions:c.evidence_versions,phase_refs:c.phase_refs}))}`:''}</td><td>${v3?'<span class="run-badge">已保存到隔离区，未发布</span>'+fieldOutcomes(c):badge(c.published?'published':c.status)+`<div class="run-note">${txt({added:'填补空值',changed:'值有变化',unchanged:'值相同',unknown:'无法比较'}[c.change_kind])}${c.published?'':' · 尚未写入业务表'}</div>`}${c.rejection_reason?'<div class="run-rejection">'+txt(c.rejection_reason)+'</div>':''}</td></tr>`;
    }).join('')+'</tbody></table></div>';
  }
  const jsonBlock=v=>`<pre class="run-json">${txt(JSON.stringify(v,null,2))}</pre>`;
  const outcomeLabels={unknown:'未知',unchanged:'无变化',added:'新增',changed:'值有变化',new_entity:'新实体',upgraded:'提升',conflict:'冲突',accept:'接受',reject:'拒绝',revise:'修订',unresolved:'待确认',not_executed:'未执行',not_recorded:'未记录',accepted:'接受',verified:'已核验',unverifiable:'无法核验',program_rejected:'程序拒绝',semantic_rejected:'语义拒绝',pending:'待处理',existing:'已有实体',new:'新实体',ambiguous:'有歧义',out_of_scope:'不在范围内',resolved:'已解决',dependency_unresolved:'依赖未满足',ready:'条件齐备',matched:'关系已匹配',new_relation:'新关系',not_applicable:'无需比较',comparison_unresolved:'关系条件未确认',validated:'模型已接受',program_valid:'程序校验通过',partial_failure:'部分失败',completed:'完成',running:'进行中',queued:'等待'};
  const outcome=v=>txt(outcomeLabels[v]||labels[v]||v||'未记录');
  function renderEvidence(c){
    const quote=safeDisplayString(c.evidence_text);
    if(quote.length<=240)return `<div class="run-value run-evidence">${txt(quote)}</div>`;
    return `<div class="run-value run-evidence">${txt(quote.slice(0,240))}…</div>`+section('field-quote-'+c.id,`展开完整引文 · ${quote.length} 字符`,`<div class="run-value run-evidence-full">${txt(quote)}</div>`);
  }
  function fieldOutcomes(c){
    const more=`<div class="run-outcomes"><div>程序校验：${outcome(c.program_status)}</div><div>实体：${outcome(c.entity?.status)}</div><div>必要条件：${outcome(c.relation_readiness?.status)}</div><div>依赖：${outcome(c.dependency_status)}</div><div>历史比较：${outcome(c.comparison?.status)}</div><div>更新资格：${c.update_eligible?'满足隔离候选条件':'尚不满足'}</div><div>执行记录：${outcome(c.status)}</div></div>`+jsonBlock({program_reason:c.program_reason,review_reason:c.review_reason,value:c.value_change_reason,source_membership:c.source_membership_reason,source_quality:c.source_quality_reason,evidence:c.evidence_verification_reason,missing_dependencies:c.missing_dependencies,relation_readiness:c.relation_readiness});
    const rejected=['program_rejected','semantic_rejected','rejected','review_failed','extract_failed'].includes(c.status);
    return `${rejected?badge(c.status):''}<div class="run-outcomes"><div>值：${outcome(c.value_change_kind)}</div><div>来源成员：${outcome(c.source_membership_kind)}</div><div>证据质量：${outcome(c.source_quality_kind)}</div><div>独立复核：${outcome(c.review_status)}</div><div>证据绑定：${outcome(c.evidence_verification_status)}</div></div>${section('field-reasons-'+c.id,'实体、条件与判断依据',more)}`;
  }
  const section=(id,title,body,open=false)=>`<details data-run-section="${txt(id)}" ${open?'open':''}><summary>${title}</summary>${body}</details>`;
  function detail(d){
    const params={...d.parameters}, names={limit:'首轮来源数量上限',force:'强制运行',mode:'运行模式',max_attempts:'抓取重试上限',proxy_enabled:'使用代理',max_crawl_depth:'最大抓取深度',selected_link_ids:'选定来源 ID',parser_version:'解析版本',model:'模型',thinking:'思考模式'};
    for(const k of ['limit','max_attempts','max_crawl_depth','model','thinking'])if(!(k in params))params[k]=null;
    const p=Object.entries(params).map(([k,v])=>`<div><b>${txt(names[k]||k)}</b>${txt(value(v))}</div>`).join('')+`<div><b>已记录模型响应 / Token 用量</b>${Number(d.runtime?.recorded_responses||0)} 次 / ${Number(d.runtime?.total_tokens||0).toLocaleString()} tokens</div>`;
    const phases=d.stages.map(s=>`<div class="run-stage"><b>${txt(s.label)}</b><br>${badge(s.status)}<p>${txt(s.description)}</p><p>${Object.entries(s.counts).map(([k,v])=>`${txt(labels[k]||k)} ${v}`).join(' · ')}</p></div>`).join('');
    const fields=d.planned_fields.map(s=>`<div class="run-source"><b>${txt(s.skill)}</b><div class="run-note">${s.fields.map(txt).join(' · ')}</div></div>`).join('')||'<div class="run-note">尚未规划字段提取。</div>';
    const discoveries=d.discoveries.map(e=>`<div class="run-source"><b>链路 #${Number(e.id)} · 发现任务 #${Number(e.discovery_job_id)}</b> · 深度 ${Number(e.crawl_depth)} · ${badge(e.status)}<div class="run-links">${link(e.parent_url)}<br>→ ${link(e.child_url||e.source_url)}</div><div class="run-note">访问 ${txt(formatUpdateTime(e.fetched_at))} ${txt(e.reason||'')}</div></div>`).join('')||'<div class="run-note">本次运行没有 URL 发现链路。</div>';
    const sources=d.sources.map(s=>`<div class="run-source"><b>来源访问 #${Number(s.id)} · 抓取批次 #${Number(s.run_id)}</b> · HTTP ${txt(s.http_status)} · ${badge(s.outcome)}<div class="run-links">请求 ${link(s.requested_url)}<br>最终 ${link(s.final_url)}</div><div class="run-note">${txt(formatUpdateTime(s.checked_at))} · ${txt(s.duration_ms)} ms · ${txt(s.response_bytes)} bytes</div>${s.error_message?`<div class="error">${txt(s.error_message)}</div>`:''}</div>`).join('');
    const timeline=d.timeline.map(e=>`<div><span class="run-note">${txt(formatUpdateTime(e.created_at))} · ${txt(e.actor)} · ${txt(e.stage)}</span><br>${badge(e.status)} ${txt(e.message)}${e.details&&Object.keys(e.details).length?`<details><summary>事件参数</summary><pre class="run-json">${txt(JSON.stringify(e.details,null,2))}</pre></details>`:''}</div>`).join('');
    const details=`<div class="run-detail-block"><h4>运行阶段</h4><div class="run-stage-grid">${phases}</div></div>`+
      `<div class="run-detail-block"><h4>运行参数</h4><div class="run-parameters">${p}</div></div>`+
      `<div class="run-detail-block"><h4>计划提取字段</h4>${fields}</div>`+
      `<div class="run-detail-block"><h4>URL 发现链路 · ${d.discoveries.length} 条</h4>${discoveries}</div>`+
      `<div class="run-detail-block"><h4>网页访问记录 · ${d.sources.length} 条</h4>${sources||'<div class="run-note">本次运行没有网页访问记录。</div>'}</div>`+
      `<div class="run-detail-block"><h4>Agent 任务 · ${d.jobs.length} 个</h4>${d.jobs.map(renderDataAgentJob).join('')||'<div class="run-note">本次运行没有 Agent 任务。</div>'}</div>`+
      `<div class="run-detail-block"><h4>执行时间线 · ${d.timeline.length} 条</h4><div class="run-timeline">${timeline||'<div class="run-note">本次运行没有时间线记录。</div>'}</div></div>`;
    return `<div class="run-toolbar"><button class="btn secondary" data-download-run>下载本次运行 JSON</button></div>`+
      section('changes',`字段提取与校验 · ${d.field_changes.length} 条`,fieldTable(d.field_changes),true)+
      section('details','详细记录',details);
  }
  async function fill(card,run,refresh=false){
    const body=card.querySelector('.run-card-body');if(body.dataset.loaded&&!refresh)return;
    const d=await fetchJSON(`${API}/data-agent/runs/${encodeURIComponent(run.session)}/${run.id}`);
    if(!card.isConnected)return;
    if(d._error){body.innerHTML=`<div class="error">${txt(d._error)}</div>`;return}
    const serialized=JSON.stringify(d);if(body.dataset.snapshot===serialized)return;
    const open=[...body.querySelectorAll('details[open]')].map(x=>x.dataset.runSection?'s:'+x.dataset.runSection:x.dataset.jobId?'j:'+x.dataset.jobId:null);
    const previous=body.dataset.loaded;
    body.innerHTML=detail(d);body.dataset.loaded='1';body.dataset.snapshot=serialized;
    if(previous)body.querySelectorAll('details').forEach(x=>{if(x.dataset.runSection)x.open=open.includes('s:'+x.dataset.runSection);else if(x.dataset.jobId)x.open=open.includes('j:'+x.dataset.jobId)});
    else {const saved=state.restore?.cards?.find(x=>x.key===run.key);if(saved)body.querySelectorAll('details').forEach(x=>{if(x.dataset.runSection)x.open=saved.sections.includes(x.dataset.runSection);else if(x.dataset.jobId)x.open=saved.jobs.includes(x.dataset.jobId)})}
    body.querySelector('[data-download-run]').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`run-${run.session}-${run.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
  }
  const auditSkillLabels={'chip-identity':'芯片型号','chip-specs':'基础参数','chip-compute':'算力指标','chip-compatibility':'兼容信息','chip-benchmark':'实测数据','chip-deployment':'部署资料'};
  const domainNames={identity:'芯片型号',specs:'基础参数',compute:'算力指标',compatibility:'兼容信息',benchmark:'实测数据',deployment:'部署资料'};
  const stageNames={preparing:'准备隔离区',planning:'生成搜索词',searching:'搜索网页',previewing:'访问与预览',selecting:'选择来源',extracting:'提取字段',reviewing:'独立复核',comparison:'比较冻结基线',comparing:'比较冻结基线',completion:'保存隔离结果'};
  const manualKey='domain-discovery-manual-request-v3';
  let manualRequest=null;
  try{manualRequest=JSON.parse(sessionStorage.getItem(manualKey))}catch(_){}
  function ensureManualForm(root){
    if(document.querySelector('[data-domain-manual-form]'))return;
    const box=document.createElement('section');box.className='run-manual';
    box.innerHTML=`<h3>手动运行开放发现</h3><p class="run-note">选择一类信息，Hermes 将在开放互联网寻找芯片与来源。芯片线索可留空，也可填写已知或未知型号。结果保存到隔离区，未发布。</p><form data-domain-manual-form><label>信息域<select name="domain">${Object.entries(domainNames).map(([id,label])=>`<option value="${id}" ${id==='specs'?'selected':''}>${label}</option>`).join('')}</select></label><label>芯片线索（可选）<input name="chip" maxlength="160" placeholder="留空即开放发现，不从数据库枚举芯片"></label><label>管理员口令<input name="admin_token" type="password" autocomplete="off" required placeholder="仅通过请求头发送"></label><div class="run-manual-actions"><button class="btn" type="submit">开始隔离运行</button><button class="btn secondary" type="button" data-new-manual>新建另一轮</button></div></form><details data-manual-contract><summary>该信息域的全部字段（只读）</summary><div data-domain-contract class="run-note">字段范围由六类信息域规则定义。</div></details><div role="status" aria-live="polite" data-manual-result></div>`;
    root.before(box);
    const form=box.querySelector('form'),result=box.querySelector('[data-manual-result]');
    if(manualRequest?.payload){form.elements.domain.value=manualRequest.payload.domain;form.elements.chip.value=manualRequest.payload.chip||''}
    const clear=()=>{manualRequest=null;try{sessionStorage.removeItem(manualKey)}catch(_){}result.textContent='设置已变更，下次提交将创建新请求。';form.querySelector('button[type="submit"]').textContent='开始隔离运行'};
    box.querySelector('[data-new-manual]').onclick=()=>{clear();result.textContent='下次提交将新建另一轮隔离运行。'};
    let contracts=[];
    const showContract=()=>{
      const contract=contracts.find(x=>x.domain_id===form.elements.domain.value);
      box.querySelector('[data-domain-contract]').innerHTML=contract?Object.entries(contract.fields||{}).map(([table,fields])=>`<div><b>${txt(table)}</b><p>${fields.map(txt).join(' · ')}</p></div>`).join(''):'字段范围由该信息域完整规则定义；本页不接受字段覆盖参数。';
    };
    form.elements.domain.addEventListener('change',()=>{clear();showContract()});form.elements.chip.addEventListener('input',clear);
    fetchJSON(`${API}/data-agent/domains`).then(d=>{contracts=d.domains||[];showContract()});
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      const base={pipeline:'open_web',mode:'test',domain:form.elements.domain.value,chip:form.elements.chip.value.trim()||null};
      const fingerprint=JSON.stringify(base);
      if(!manualRequest||manualRequest.fingerprint!==fingerprint){
        const request_id=crypto.randomUUID();manualRequest={fingerprint,payload:{...base,request_id}};
        try{sessionStorage.setItem(manualKey,JSON.stringify(manualRequest))}catch(_){}
      }
      // Capture the exact request before awaiting; edits cannot mutate a retry.
      const payload={...manualRequest.payload},button=form.querySelector('button[type="submit"]');
      button.disabled=true;result.textContent='正在提交隔离运行请求…';
      try{
        const response=await fetch(`${API}/data-agent/manual-run`,{method:'POST',headers:{'Content-Type':'application/json','X-Data-Agent-Admin':form.elements.admin_token.value,'Idempotency-Key':payload.request_id},body:JSON.stringify(payload)});
        const data=await response.json();
        if(!response.ok){result.textContent=`提交失败（${response.status}）：${data.detail||'未记录原因'}。可用同一请求重试。`;button.textContent='同一请求重试';return}
        result.textContent=`已加入隔离运行队列 · ${labels[data.status]||data.status} · 运行 ${data.run_id}。结果未发布。`;
        button.textContent='查看 / 重试同一请求';await loadRunHistory({silent:true});
      }catch(_){result.textContent='响应未收到；请使用同一请求重试，持久队列会返回原运行。';button.textContent='同一请求重试'}
      finally{button.disabled=false}
    });
  }
  const auditCategoryOrder=['芯片型号','基础参数','算力指标','兼容信息','实测数据','部署资料'];
  const auditFieldLabels={
    vendor:'厂商',chip_series:'芯片系列',chip_model:'芯片型号',release_date:'发布时间',production_status:'发布状态',
    vram_gb:'显存容量',vram_type:'显存类型',vram_bw_gb_s:'显存带宽',tdp_w:'功耗',process_node_nm:'制程',form_factor:'产品形态',bus_interface:'接口类型',architecture:'架构',interconnect_bw_gb_s:'互联带宽',interconnect_tech:'互联技术',
    precision_support:'支持精度',precision_perf:'理论峰值',compute_units:'计算单元',tensor_cores:'张量计算单元',sm_count:'流处理器组',
    model_id:'模型',compat_status:'兼容状态',framework:'框架',precision:'精度',notes:'说明',
    suite_name:'测试套件',workload_type:'训练或推理',hardware_config:'硬件配置',chip_count:'芯片数量',batch_size:'批大小',input_seq_length:'输入长度',output_seq_length:'输出长度',concurrency:'并发数',throughput_tok_s:'总吞吐',prefill_throughput:'Prefill 吞吐',decode_throughput:'Decode 吞吐',time_to_first_token_ms:'首字时延',inter_token_latency_ms:'输出时延',tpot_ms:'每 Token 时延',memory_peak_mb:'峰值显存',mfu_pct:'MFU',test_date:'测试日期',
    backend:'部署后端',title:'资料标题',source_type:'资料类型'
  };
  const num=v=>Number.isFinite(Number(v))?Number(v):0;
  const isReachableStatus=s=>['new','changed','unchanged'].includes(String(s||''));
  const auditSkillName=skill=>auditSkillLabels[skill]||skill||'未记录';
  const renderFlowSteps=steps=>`<div class="audit-steps">${steps.map(([title,note,result],index)=>`<div class="audit-step"><span class="audit-step-no">${index+1}</span><div><b>${txt(title)}</b><p>${txt(note)}</p></div><em>${txt(result)}</em></div>`).join('')}</div>`;
  const auditSkillBadges=skills=>{
    const values=[...new Set((Array.isArray(skills)?skills:[skills]).filter(Boolean))];
    return values.length
      ?`<span class="audit-origin-skills">${values.map(skill=>`<span class="audit-skill-badge" title="${txt(skill)}">${txt(auditSkillName(skill))}</span>`).join('')}</span>`
      :'<span class="audit-origin-empty">未记录</span>';
  };
  const auditUrlKey=value=>String(value||'').trim().replace(/#.*$/,'').replace(/\/$/,'');
  function openWebSummary(run){
    const c=run.counts||{};
    if(run.schema_version==='hermes-open-web-v3'){
      const s=run.statistics||{};
      return `<div class="run-card-head"><div><h3>Hermes 开放发现 · ${txt(run.skill_label||domainNames[run.domain]||run.domain)}</h3><div class="run-card-meta">${run.chip?'定向线索：'+txt(run.chip):'开放发现 · 未指定芯片'} · 隔离区，未发布<br>运行 ${txt(run.run_id)}<br>开始 ${txt(formatUpdateTime(run.started_at))} · ${run.active?'尚未结束':'结束 '+txt(formatUpdateTime(run.finished_at))}</div></div>${badge(run.status)}</div><div class="run-card-stats"><span class="run-card-stat">主域候选 ${num(s.main?.validated_candidates)}</span><span class="run-card-stat">关联域候选 ${num(s.linked?.validated_candidates)}</span><span class="run-card-stat">新实体 ${num(s.new_entities)}</span><span class="run-card-stat">独立字段 ${num(s.independent_fields)}</span><span class="run-card-stat">来源证据 ${num(s.source_candidates)}</span><span class="run-card-stat">URL ${num(s.urls)}</span><span class="run-card-stat">拒绝 ${num(s.rejected_candidates)}</span></div>`;
    }
    if(run.skill==='all-skills'&&run.audit_summary){
      const a=run.audit_summary,existing=a.existing||{},fresh=a.new||{};
      return `<div class="run-card-head"><div><h3>全部链接检查<span class="run-title-hint">展开流程 ↓</span></h3><div class="run-card-meta">已有链接库 + 6 类信息的开放互联网搜索<br>开始 ${txt(formatUpdateTime(run.started_at))} · 结束 ${txt(formatUpdateTime(run.finished_at))}</div></div>${badge(run.status)}</div><div class="run-card-stats"><span class="run-card-stat strong">搜索 ${num(fresh.queries)} 次 · ${num(fresh.search_mentions)} 条结果</span><span class="run-card-stat strong">已有链接可用 ${num(existing.reachable)} / ${num(existing.safe_unique_urls)}</span><span class="run-card-stat strong">新网址可用 ${num(fresh.reachable)} / ${num(fresh.visited_unique_urls)}</span></div>`;
    }
    const hermes=String(run.schema_version||'').startsWith('hermes-open-web-');
    const full=hermes&&run.scope==='all';
    const target=full?`全部芯片 · ${num((run.chips||[]).length)} 颗`:(run.chips||[]).join('、')||'开放发现';
    return `<div class="run-card-head"><div><h3>${hermes?'Hermes 开放网络更新':'开放互联网测试'} · ${txt(run.skill_label||'基础参数')}</h3><div class="run-card-meta">${txt(target)}<br>开始 ${txt(formatUpdateTime(run.started_at))} · 结束 ${txt(formatUpdateTime(run.finished_at))}</div></div>${badge(run.status)}</div><div class="run-card-stats">${hermes?`<span class="run-card-stat">单元 ${num(c.units_completed)} / ${num(c.units_total)}</span><span class="run-card-stat">搜索词 ${num(c.queries)}</span><span class="run-card-stat">去重候选 ${num(c.unique_candidates)}</span><span class="run-card-stat">可访问 ${num(c.reachable)} / ${num(c.previewed)}</span>`:`<span class="run-card-stat">网页访问 ${num(c.visit_succeeded)} / ${num(c.visited||c.coarse_passed)}</span>`}<span class="run-card-stat">有效字段 ${num(c.validated)}</span></div>`;
  }
  function openWebCard(run){
    const audit=run.skill==='all-skills'&&run.audit_summary;
    return `<details class="run-history-card open-web-run${audit?' audit-run':''}" data-open-web-session="${txt(run.session_id)}"><summary>${openWebSummary(run)}</summary><div class="run-card-body">${audit?'展开查看检查覆盖、搜索结果与问题分布…':'展开查看测试结果…'}</div></details>`;
  }
  function auditFailureGroups(urls){
    const groups=new Map();
    for(const u of urls.filter(x=>!isReachableStatus(x.fetch_status))){
      const reason=String(u.precise_reason||'');let label='其他访问问题';
      if(/403|forbidden/i.test(reason))label='HTTP 403';
      else if(/404|not found/i.test(reason))label='HTTP 404';
      else if(/超过|too large|max.bytes|response.*large|2\s*MB/i.test(reason))label='响应内容过大';
      else if(/empty|空内容|无正文|没有.*正文/i.test(reason))label='无可用正文';
      else if(/timed out|timeout|超时/i.test(reason))label='访问超时';
      else if(/certificate|ssl|tls/i.test(reason))label='证书问题';
      else if(/network|connection|连接|unreachable/i.test(reason))label='网络连接失败';
      groups.set(label,(groups.get(label)||0)+1);
    }
    return [...groups.entries()].sort((a,b)=>b[1]-a[1]);
  }
  function auditFieldsByCategory(row){
    const result={};
    for(const label of auditCategoryOrder)result[label]=[];
    const existing=row._fieldsByCategory||row.fields_by_category||{};
    for(const label of auditCategoryOrder){
      if(Array.isArray(existing[label]))result[label]=[...new Set(existing[label].filter(Boolean).map(String))];
    }
    const activeLabel=auditSkillLabels[row.skill]||row.information_category||'';
    if(activeLabel&&result[activeLabel]&&Array.isArray(row.extracted_fields)){
      result[activeLabel]=[...new Set([...result[activeLabel],...row.extracted_fields.filter(Boolean).map(String)])];
    }
    return result;
  }
  function mergeAuditLinkAssets(rows){
    const merged=new Map(),rank={failed:0,retry_wait:1,unchanged:2,changed:3,new:4};
    for(const source of rows){
      const key=source.url||source.canonical_url||`missing-${merged.size}`;
      const fields=auditFieldsByCategory(source);
      if(!merged.has(key)){
        merged.set(key,{...source,information_categories:[...new Set(source.information_categories||[])],extracted_fields:[...new Set(source.extracted_fields||[])],discovery_skills:[...new Set([...(source.discovery_skills||[]),source.skill].filter(Boolean))],_fieldsByCategory:fields});
        continue;
      }
      const row=merged.get(key);
      row.information_categories=[...new Set([...(row.information_categories||[]),...(source.information_categories||[])])];
      row.extracted_fields=[...new Set([...(row.extracted_fields||[]),...(source.extracted_fields||[])])];
      row.discovery_skills=[...new Set([...(row.discovery_skills||[]),...(source.discovery_skills||[]),source.skill].filter(Boolean))];
      for(const label of auditCategoryOrder)row._fieldsByCategory[label]=[...new Set([...(row._fieldsByCategory[label]||[]),...(fields[label]||[])])];
      if((rank[source.fetch_status]??-1)>(rank[row.fetch_status]??-1)){
        for(const keyName of ['fetch_status','http_status','final_url','asset_status','decision_reason'])if(source[keyName]!=null&&source[keyName]!=='')row[keyName]=source[keyName];
      }
      if(!row.title&&source.title)row.title=source.title;
    }
    return [...merged.values()];
  }
  function auditCategoryCell(row,label){
    const categories=new Set(row.information_categories||[]);
    if(row.information_category)categories.add(row.information_category);
    const fields=(row._fieldsByCategory||auditFieldsByCategory(row))[label]||[];
    const fieldNames=fields.map(field=>auditFieldLabels[field]||field);
    if(fields.length)return `<span class="audit-category-state extracted" title="${txt(label)}：已提取并校验 ${fields.length} 个字段（${txt(fieldNames.join('、'))}）">${fields.length}</span>`;
    if(categories.has(label))return `<span class="audit-category-state matched" title="${txt(label)}：识别到该类信息，尚无通过校验的字段">✓</span>`;
    return `<span class="audit-category-state empty" title="${txt(label)}：未识别">—</span>`;
  }
  const auditCategoryHeader=()=>auditCategoryOrder.map(label=>`<div class="audit-link-category">${txt(label)}</div>`).join('');
  const auditCategoryCells=row=>auditCategoryOrder.map(label=>`<div class="audit-link-category">${auditCategoryCell(row,label)}</div>`).join('');
  function auditLinkRows(rows,group){
    if(!rows.length)return '<div class="run-note">本组没有链接。</div>';
    return `<div class="audit-link-list" data-audit-link-list="${txt(group)}"><div class="audit-link-header"><div>状态</div><div>链接与发现来源</div>${auditCategoryHeader()}<div>链接信息</div></div>${rows.map((row,index)=>{
      const url=row.url||row.canonical_url||'';
      const finalUrl=row.final_url&&row.final_url!==url?row.final_url:'';
      const title=row.title||row.description||url||`链接 ${index+1}`;
      const categories=(row.information_categories||[]).join('、')||row.information_category||row.category||'类别未记录';
      const owner=row.chip_model||row.vendor||row.source_domain||'';
      const http=row.http_status==null?'HTTP 未记录':`HTTP ${row.http_status}`;
      const reason=row.error||row.decision_reason||row.precise_reason||'';
      const origin=group==='existing'
        ?'<span class="audit-library-badge">已有链接库</span>'
        :auditSkillBadges(row.discovery_skills||row.skill);
      const strategy=row.query_strategy||'';
      const discoveryQuery=row.discovery_query||'';
      const reachable=isReachableStatus(row.fetch_status);
      const status=row.audit_excluded?'not_run':reachable?'succeeded':row.fetch_status==='retry_wait'?'retry_wait':'failed';
      const discoveryLabels=(row.discovery_skills||[]).map(auditSkillName).join(' ');
      const search=[title,url,finalUrl,categories,discoveryLabels,owner,row.source_domain,row.discovery_query,reason].filter(Boolean).join(' ').toLowerCase();
      return `<article class="audit-link-row" data-audit-link-row data-audit-link-search="${txt(search)}"><div class="audit-link-status"><span class="audit-link-index">${index+1}</span>${badge(status)}</div><div class="audit-link-main"><b>${txt(title)}</b><div class="run-links">${link(url)}${finalUrl?`<br><span>跳转至：</span>${link(finalUrl)}`:''}</div><div class="audit-discovery-line"><span class="audit-origin-label">发现来源</span>${origin}</div>${discoveryQuery?`<div class="audit-discovery-query"><span>搜索词</span>${txt(discoveryQuery)}${strategy?` · ${txt(strategy)}`:''}</div>`:''}${reason?`<div class="audit-link-reason">${txt(reason)}</div>`:''}</div>${auditCategoryCells(row)}<div class="audit-link-meta">${owner?`<span>${txt(owner)}</span>`:''}<span>${txt(http)}</span></div></article>`;
    }).join('')}</div>`;
  }
  function auditLinkGroup(group,label,rows){
    return `<div class="audit-link-tools"><label class="audit-link-filter-label">筛选${txt(label)}<input type="search" class="audit-link-filter" data-audit-link-filter="${txt(group)}" placeholder="输入标题、域名、Skill 或 URL"></label><span class="run-note">显示 <b data-audit-link-count="${txt(group)}">${rows.length}</b> / ${rows.length} 条</span></div><div class="audit-category-legend"><span><b>发现来源</b> 表示哪个 Skill 找到了该链接</span><span><b>右侧六列</b> 表示访问网页后复核出的信息类别</span><span><b>数字</b> 已提取并校验的字段数</span><span><b>✓</b> 识别到该类信息</span><span><b>—</b> 未识别</span></div>${auditLinkRows(rows,group)}`;
  }
  function bindAuditLinkFilters(root){
    root.querySelectorAll('[data-audit-link-filter]').forEach(input=>{
      input.addEventListener('input',()=>{
        const group=input.dataset.auditLinkFilter;
        const needle=input.value.trim().toLowerCase();
        const list=root.querySelector(`[data-audit-link-list="${CSS.escape(group)}"]`);
        if(!list)return;
        let visible=0;
        list.querySelectorAll('[data-audit-link-row]').forEach(row=>{
          const matched=!needle||row.dataset.auditLinkSearch.includes(needle);
          row.hidden=!matched;if(matched)visible+=1;
        });
        const count=root.querySelector(`[data-audit-link-count="${CSS.escape(group)}"]`);
        if(count)count.textContent=String(visible);
      });
    });
  }
  function auditSearchDetails(d){
    const results=Array.isArray(d.search_results)?d.search_results:[];
    const plan=Array.isArray(d.search_plan)&&d.search_plan.length?d.search_plan:(d.queries||[]).map(query=>({query}));
    const byQuery=new Map();
    for(const result of results){
      const query=String(result.query||'').trim();
      if(!byQuery.has(query))byQuery.set(query,[]);
      byQuery.get(query).push(result);
    }
    const known=new Set(plan.map(item=>String(item.query||'').trim()));
    for(const query of byQuery.keys())if(query&&!known.has(query))plan.push({query});
    if(!plan.length)return '<div class="run-note">本轮没有保存搜索词明细。</div>';
    const statusText={selected:'进入候选',filtered:'未入选',unsafe:'安全过滤'};
    return `<div class="audit-search-summary">每条搜索词都标明生成它的目标 Skill、目标芯片和构词策略；展开后，每条 URL 也会保留这组发现来源。</div><div class="audit-search-queries">${plan.map((item,index)=>{
      const query=String(item.query||'').trim(),rows=(byQuery.get(query)||[]).sort((a,b)=>num(a.rank)-num(b.rank));
      const skill=item.skill||rows[0]?.skill||'',chip=item.chip||rows[0]?.chip||'',strategy=item.strategy||item.query_strategy||rows[0]?.strategy||rows[0]?.query_strategy||'';
      return `<details class="audit-search-query"><summary><span class="audit-search-index">${index+1}</span><span class="audit-search-query-main"><b>${txt(query||'未记录搜索词')}</b><span class="audit-query-context"><span><em>目标 Skill</em>${auditSkillBadges(skill)}</span>${chip?`<span><em>目标芯片</em><b>${txt(chip)}</b></span>`:''}${strategy?`<span><em>构词策略</em><b>${txt(strategy)}</b></span>`:''}</span></span><strong>${rows.length} 条结果</strong></summary><div class="audit-search-results">${rows.length?rows.map((row,resultIndex)=>{
        const reasons=Array.isArray(row.coarse_reasons)?row.coarse_reasons.join('；'):row.coarse_reason||'';
        const state=statusText[row.status]||row.status||'未判定';
        const resultSkill=row.skill||skill;
        const provider=row.provider||'搜索来源未记录';
        const providerRank=num(row.rank)?` · 原始排名 #${num(row.rank)}`:'';
        return `<article class="audit-search-result"><span class="audit-search-rank">#${resultIndex+1}</span><div><b>${txt(row.title||row.url||'未命名结果')}</b><div class="run-links">${link(row.url)}</div><div class="audit-search-result-origin"><span>发现来源</span>${auditSkillBadges(resultSkill)}</div>${row.snippet?`<p>${txt(row.snippet)}</p>`:''}<small>${txt(provider)}${txt(providerRank)} · ${txt(state)} · 粗筛 ${num(row.coarse_score)} 分${reasons?' · '+txt(reasons):''}</small></div></article>`;
      }).join(''):'<div class="run-note">本次搜索没有返回结果，或旧记录未保存结果明细。</div>'}</div></details>`;
    }).join('')}</div>`;
  }
  function legacyPhaseState(d,phase,artifacts=[]){
    // Historical run-level success/finish markers cannot prove each phase.
    // Qualify successful calls/results by their recorded evidence, and keep
    // absence distinct from an explicit not-executed decision.
    const stages={plan:['plan','planning','search_plan'],search:['search','searching','coarse_filter','search_provider'],
      visit:['visit','preview','previewing','fetch'],select:['select','selecting','selection','url_selection'],
      extract:['extract','extracting','model_extract'],record:['record_url','record_urls','save_urls','asset_saved','linked_tasks']};
    const tools={search:['open_web_search'],visit:['open_web_preview'],select:['open_web_submit_selection']};
    const list=value=>Array.isArray(value)?value.filter(x=>x&&typeof x==='object'):[];
    const events=list(d.events).filter(x=>(stages[phase]||[]).includes(x.stage));
    const calls=list(d.model_calls||d.calls).filter(x=>x.phase===phase||x.phase==='extract'&&phase==='extract');
    const traces=list(d.tool_trace).filter(x=>(tools[phase]||[]).includes(x.tool));
    const records=[...events,...calls,...traces],status=x=>String(x.status||x.outcome||'');
    if(records.some(x=>['failed','error','partial','interrupted','transport_failed','parser_rejected'].includes(status(x))))return '已记录失败';
    if(records.some(x=>status(x)==='rejected'))return '已记录拒绝';
    if(traces.some(x=>['success','completed','succeeded'].includes(status(x))))return '已记录工具调用完成';
    if(calls.some(x=>['success','completed','succeeded'].includes(status(x))))return '已记录模型调用完成';
    if(events.some(x=>['success','completed','succeeded'].includes(status(x))))return '已记录成功事件';
    if(records.some(x=>['running','in_progress','pending','queued'].includes(status(x))))return '已记录进行中状态';
    if(records.some(x=>status(x)==='not_run'||status(x)==='not_executed'))return '已明确记录未执行';
    return records.length||Array.isArray(artifacts)&&artifacts.length?'已保存记录 · 完成状态未记录':'未记录 / 无法核验';
  }
  function renderAuditRun(d){
    const report=d.audit_report||{},existing=report.existing||{},fresh=report.new||{},outcomes=report.outcomes||{};
    const urls=d.urls||[],assets=d.url_assets||[];
    const existingAssets=mergeAuditLinkAssets(assets.filter(a=>a.search_provider==='link_library'||(a.query_strategy||'')==='已有资产复查'));
    const discoverySkillsByUrl=new Map();
    for(const result of (d.search_results||[])){
      if(result.status!=='selected'||!result.skill)continue;
      const key=auditUrlKey(result.url);
      if(!key)continue;
      if(!discoverySkillsByUrl.has(key))discoverySkillsByUrl.set(key,new Set());
      discoverySkillsByUrl.get(key).add(result.skill);
    }
    const newAssets=assets.filter(a=>a.search_provider!=='link_library'&&(a.query_strategy||'')!=='已有资产复查').map(asset=>({...asset,discovery_skills:[...new Set([...(asset.discovery_skills||[]),...Array.from(discoverySkillsByUrl.get(auditUrlKey(asset.url))||[]),asset.skill].filter(Boolean))]}));
    const uniqueNew=mergeAuditLinkAssets(newAssets);
    const stages=[
      ['生成搜索词',`6 类 Skill 共生成 ${num(fresh.queries)} 组搜索词`,legacyPhaseState(d,'plan',d.search_plan)],
      ['搜索并去重',`返回 ${num(fresh.search_mentions)} 条结果，形成 ${num(fresh.selected_unique_skill_urls)} 条 Skill 候选`,legacyPhaseState(d,'search',d.search_results)],
      ['网页访问',`检查已有链接 ${num(existing.safe_unique_urls)} 个，并访问新增网址 ${num(fresh.visited_unique_urls)} 个`,legacyPhaseState(d,'visit',urls.filter(x=>x.fetch_status&&x.fetch_status!=='not_run'))],
      ['URL 选择判断',`已有链接可用 ${num(existing.reachable)} 个，新增网址可用 ${num(fresh.reachable)} 个`,legacyPhaseState(d,'select',d.url_decisions)],
      ['字段提取',`提取 ${num(d.counts?.extracted)} 个候选字段，校验通过 ${num(d.counts?.validated)} 个`,legacyPhaseState(d,'extract',d.facts)],
      ['记录 URL',`形成 ${existingAssets.length} 条已有链接记录和 ${uniqueNew.length} 条新增链接记录`,legacyPhaseState(d,'record',assets)],
    ];
    return `<div class="audit-overview">${renderFlowSteps(stages)}</div>`+
      section('audit-search-results',`搜索新网址明细 · ${num(fresh.queries)} 次 / ${num(fresh.search_mentions)} 条结果`,auditSearchDetails(d))+
      section('audit-existing',`已有链接明细 · ${existingAssets.length} 条（${num(existing.reachable)} 条可用）`,auditLinkGroup('existing','已有链接',existingAssets))+
      section('audit-new',`开放网络新增链接 · ${uniqueNew.length} 条（${num(fresh.reachable)} 条可用）`,auditLinkGroup('new','新增链接',uniqueNew),true)+
      `<div class="run-note">抓取结果：可用 ${num(outcomes.new)} · 等待重试 ${num(outcomes.retry_wait)} · 失败 ${num(outcomes.failed)}</div>`;
  }
  function renderHermesRun(d){
    const plan=Array.isArray(d.search_plan)?d.search_plan:[],results=Array.isArray(d.search_results)?d.search_results:[];
    const previews=new Map((d.url_previews||[]).map(row=>[row.candidate_id,row]));
    const decisions=new Map((d.url_decisions||[]).map(row=>[row.candidate_id,row]));
    const byQuery=new Map(plan.map(item=>[String(item.query||''),[]]));
    for(const candidate of results){
      if(candidate.status!=='candidate')continue;
      for(const mention of candidate.mentions||[]){
        const query=String(mention.query||'');if(!byQuery.has(query))byQuery.set(query,[]);
        byQuery.get(query).push({...candidate,mention});
      }
    }
    const searches=`<div class="audit-search-queries">${plan.map((item,index)=>{
      const rows=byQuery.get(String(item.query||''))||[];
      return `<details class="audit-search-query"><summary><span class="audit-search-index">${index+1}</span><span class="audit-search-query-main"><b>${txt(item.query)}</b><small>${txt(item.reason||'未记录构词理由')}</small></span><strong>${rows.length} 条结果</strong></summary><div class="audit-search-results">${rows.map(row=>`<article class="audit-search-result"><span class="audit-search-rank">#${num(row.mention?.rank)||'-'}</span><div><b>${txt(row.title||row.url)}</b><div class="run-links">${link(row.url)}</div>${row.snippet?`<p>${txt(row.snippet)}</p>`:''}<small>${txt(row.mention?.provider||'搜索来源未记录')} · 候选 ${txt(row.candidate_id)}</small></div></article>`).join('')||'<div class="run-note">该搜索词没有可用结果。</div>'}</div></details>`;
    }).join('')}</div>`;
    const candidateRows=results.filter(row=>row.status==='candidate').map(row=>{
      const preview=previews.get(row.candidate_id)||{},decision=decisions.get(row.candidate_id)||{};
      const categories=(decision.matched_categories||[]).join('、')||'未标记';
      return `<div class="run-source"><div class="run-source-title"><b>${txt(row.title||row.url)}</b>${badge(preview.access_status==='reachable'?'succeeded':'failed')}</div><div class="run-links">${link(row.url)}</div><div class="run-note">候选 ${txt(row.candidate_id)} · HTTP ${txt(preview.http_status)} · ${decision.selected?'已选择':'未选择'} · 类别 ${txt(categories)}</div>${preview.core_text?`<div class="run-value">核心预览：${txt(preview.core_text)}</div>`:''}<div class="run-note">判断理由：${txt(decision.reason||preview.error||'未记录')}</div></div>`;
    }).join('')||'<div class="run-note">本轮没有候选网页。</div>';
    const facts=fieldTable((d.facts||[]).map(f=>({...f,entity:{chip_model:f.chip_model||'未确认芯片'},target_table:f.target_table||'chips',status:f.validation_status==='validated'?'独立复核未记录':f.validation_status||'未记录',old_value_basis:'unknown',published:false,change_kind:'unknown'})));
    const linkedSkills=[...new Set((d.linked_tasks||[]).map(row=>row.skill).filter(Boolean))];
    const linked=linkedSkills.length?`<div class="run-source">${auditSkillBadges(linkedSkills)}</div>`:'<div class="run-note">本轮没有生成系统关联 Skill。</div>';
    const traces=(d.tool_trace||[]).map(row=>`<div><span class="run-note">${txt(formatUpdateTime(row.started_at))} · ${txt(row.tool)}</span><br>${badge(row.status)} ${txt(JSON.stringify(row.output_summary||{}))}${row.error?`<div class="error">${txt(row.error)}</div>`:''}</div>`).join('')||'<div class="run-note">本轮没有工具调用记录。</div>';
    const target=`<div class="run-parameters"><div><b>目标 Skill</b>${txt(auditSkillName(d.skill))}</div><div><b>目标芯片</b>${txt(d.target_chip||(d.chips||[]).join('、'))}</div><div><b>目标字段</b>${txt((d.target_fields||[]).map(field=>auditFieldLabels[field]||field).join('、'))}</div></div>`;
    const c=d.counts||{};
    const flow=[
      ['生成搜索词',`${num(c.queries||plan.length)} 组搜索词`,legacyPhaseState(d,'plan',plan)],
      ['搜索并去重',`${num(c.search_results)} 条搜索结果，去重后 ${num(c.unique_candidates)} 个候选 URL`,legacyPhaseState(d,'search',results)],
      ['网页访问',`访问 ${num(c.previewed)} 个候选 URL，其中 ${num(c.reachable)} 个可访问`,legacyPhaseState(d,'visit',d.url_previews)],
      ['Hermes 选择 URL',`选择 ${num(c.selected)} 个，排除 ${num(c.rejected_urls)} 个`,legacyPhaseState(d,'select',d.url_decisions)],
      ['字段提取',`提取 ${num(c.extracted)} 个候选字段，校验通过 ${num(c.validated)} 个`,legacyPhaseState(d,'extract',d.facts)],
      ['记录 URL 与关联 Skill',`记录选择结果，并生成 ${num(c.linked_tasks)} 个关联任务`,legacyPhaseState(d,'record',[...(d.url_assets||[]),...(d.url_decisions||[]),...(d.linked_tasks||[])])],
    ];
    const units=Array.isArray(d.unit_details)?d.unit_details:[];
    const unitList=units.length?`<div class="run-toolbar"><input type="search" data-hermes-unit-filter placeholder="按芯片名称筛选"></div><div data-hermes-unit-list>${units.map(unit=>`<div class="run-source" data-hermes-unit-row data-hermes-unit-search="${txt(String(unit.target_chip||'开放发现').toLowerCase())}"><div class="run-source-title"><b>${txt(unit.target_chip||'开放发现')}</b>${badge(unit.status)}</div><div class="run-note">${txt(unit.scope_type==='discovery'?'新芯片与新来源':'已有芯片')} · 尝试 ${num(unit.attempts)} 次 · 搜索词 ${num(unit.counts?.queries)} · 候选 ${num(unit.counts?.unique_candidates)} · 可访问 ${num(unit.counts?.reachable)} · 有效字段 ${num(unit.counts?.validated)}</div>${unit.error?`<div class="error">${txt(unit.error)}</div>`:''}</div>`).join('')}</div>`:'';
    const newChips=(d.new_chip_candidates||[]).map(row=>`<div class="run-source"><b>${txt(row.title||'新芯片候选')}</b><div class="run-links">${link(row.url)}</div><div class="run-note">${txt(row.reason||'')}</div></div>`).join('')||'<div class="run-note">本轮没有发现新芯片候选。</div>';
    return `<div class="run-detail-block"><h4>本轮目标</h4>${target}</div><div class="audit-overview">${renderFlowSteps(flow)}</div>`+
      (units.length?section('hermes-units',`芯片处理结果 · ${units.length} 个单元`,unitList,true):'')+
      section('hermes-search',`Hermes 搜索词 · ${plan.length} 条`,searches,true)+
      section('hermes-candidates',`URL 访问与 Kimi 选择 · ${results.filter(row=>row.status==='candidate').length} 条`,candidateRows,true)+
      section('hermes-facts',`字段提取与校验 · ${num(d.counts?.validated)} 条`,facts,true)+
      (units.length?section('hermes-new-chips',`新芯片候选 · ${(d.new_chip_candidates||[]).length} 条`,newChips):'')+
      section('hermes-linked',`系统关联 Skill · ${linkedSkills.length} 类`,linked)+
      section('hermes-tools','Hermes 工具调用记录',`<div class="run-timeline">${traces}</div>`);
  }
  function renderV3Run(d){
    const rows=d.field_changes||[],stats=d.statistics||{};
    const choose=(name,label,values)=>`<label>${txt(label)}<select data-v3-filter="${name}"><option value="">全部</option>${[...new Set(values.filter(Boolean))].map(v=>`<option value="${txt(v)}">${name==='domain'?txt(domainNames[v]||v):outcome(v)}</option>`).join('')}</select></label>`;
    const filters=`<div class="run-field-filters"><label>实体线索<input type="search" data-v3-filter="entity" placeholder="型号或芯片名称"></label>${choose('domain','信息域',rows.map(x=>x.domain_id))}${choose('value_change','值变化',rows.map(x=>x.value_change_kind))}${choose('source_change','来源变化',rows.map(x=>x.source_change_kind))}${choose('review','独立复核',rows.map(x=>x.review_status))}</div><div class="run-note" data-v3-visible-count></div>`;
    const known=(d.verified_entities||[]).map(e=>`<div class="run-identity-card"><b>${txt(e.hint?.full_sku||e.chip_model)}</b><span>${txt(e.hint?.vendor||e.vendor)} · ${outcome(e.status)}</span><small>已核验身份 · ${txt(e.entity_kind||'')} · ${txt(e.reason)}</small></div>`).join('');
    const phases=Object.entries(d.stages||{}).map(([name,scopes])=>Object.entries(scopes).map(([scope,s])=>`<div class="run-stage"><b>${txt(stageNames[name]||name)} · ${scope==='main'?'主域':'关联域'}</b><br>${outcome(s.status)}<p>${txt(s.reason)}</p></div>`).join('')).join('');
    const candidates=Object.values(d.search?.candidates||{});
    const visits=Object.values(d.pages?.visits||{});
    const previews=visits.map(v=>`<article class="run-source"><b>${txt(v.candidate_id||'网页访问')}</b><div class="run-links">${link(v.requested_url||v.url||v.final_url)}</div><div class="run-note">${outcome(v.status)} · HTTP ${txt(v.http_status)}</div>${jsonBlock(v.preview||v)}</article>`).join('');
    return `<div class="run-note">本轮结果已保存到隔离区，未发布。字段与来源的候选变化供查看；正式业务表和字段溯源均写入 0 次。</div>`+
      section('v3-fields',`字段提取与校验 · ${rows.length} 条`,filters+fieldTable(rows),true)+
      (known?section('v3-entities',`已核验芯片身份 · ${(d.verified_entities||[]).length} 个`,`<div class="run-identity-grid">${known}</div>`):'')+
      section('v3-stages','实际执行阶段',`<div class="run-stage-grid">${phases||'<div class="run-note">尚无阶段结果；未执行的阶段不会标为完成。</div>'}</div>`)+
      section('v3-search',`搜索与搜索服务诊断 · ${candidates.length} 个 URL 候选`,jsonBlock(d.search))+
      section('v3-previews',`网页访问与 500 字符预览 · ${visits.length} 条`,previews||'<div class="run-note">尚无网页访问记录。</div>')+
      section('v3-selection','Hermes 选择判断',jsonBlock(d.selection))+
      section('v3-models',`提取与独立复核调用 · ${(d.model_calls||[]).length} 次`,jsonBlock(d.model_calls))+
      section('v3-linked',`关联任务 · ${(d.linked_tasks||[]).length} 个`,jsonBlock(d.linked_tasks))+
      section('v3-audit','基线、规则与执行器记录',jsonBlock({baseline:d.baseline,executor_proof:d.executor_proof,statistics:stats,raw_records:d.raw_records}));
  }
  function captureBodyView(body){
    if(!body)return {};
    return {open:[...body.querySelectorAll('[data-run-section][open]')].map(x=>x.dataset.runSection),
      filters:Object.fromEntries([...body.querySelectorAll('[data-v3-filter]')].map(x=>[x.dataset.v3Filter,x.value])),
      tables:[...body.querySelectorAll('.run-field-scroll')].map(x=>x.scrollLeft)};
  }
  function restoreBodyView(body,view){
    if(!view)return;
    body.querySelectorAll('[data-run-section]').forEach(x=>x.open=(view.open||[]).includes(x.dataset.runSection));
    body.querySelectorAll('[data-v3-filter]').forEach(x=>{x.value=view.filters?.[x.dataset.v3Filter]||''});
    body.querySelectorAll('.run-field-scroll').forEach((x,i)=>x.scrollLeft=view.tables?.[i]||0);
  }
  function bindV3Filters(body){
    const apply=()=>{
      const vals=Object.fromEntries([...body.querySelectorAll('[data-v3-filter]')].map(x=>[x.dataset.v3Filter,x.value.trim().toLowerCase()]));
      const rows=[...body.querySelectorAll('[data-v3-row]')];
      rows.forEach(row=>row.hidden=(Boolean(vals.entity)&&!row.dataset.entity.includes(vals.entity))||
        (Boolean(vals.domain)&&row.dataset.domain!==vals.domain)||(Boolean(vals.value_change)&&row.dataset.valueChange!==vals.value_change)||
        (Boolean(vals.source_change)&&row.dataset.sourceChange!==vals.source_change)||(Boolean(vals.review)&&row.dataset.review!==vals.review));
      const count=body.querySelector('[data-v3-visible-count]');if(count)count.textContent=`显示 ${rows.filter(x=>!x.hidden).length} / ${rows.length} 条候选字段`;
    };
    body.querySelectorAll('[data-v3-filter]').forEach(x=>{x.addEventListener('input',apply);x.addEventListener('change',apply)});apply();
  }
  async function fillOpenWeb(card,refresh=false){
    const body=card.querySelector('.run-card-body');if(body.dataset.loaded&&!refresh)return;
    const d=await fetchJSON(`${API}/data-agent/open-web-runs/${encodeURIComponent(card.dataset.openWebSession)}`);
    if(!card.isConnected)return;
    if(d._error){body.innerHTML=`<div class="error">${txt(d._error)}</div>`;return}
    if(d.schema_version==='hermes-open-web-v3'){
      const serialized=JSON.stringify(d);if(body.dataset.snapshot===serialized)return;
      const y=scrollY,view=body.dataset.loaded?captureBodyView(body):state.restore?.openWeb?.find(x=>x.session===card.dataset.openWebSession)?.view;
      body.innerHTML=renderV3Run(d);body.dataset.loaded='1';body.dataset.snapshot=serialized;
      restoreBodyView(body,view);bindV3Filters(body);if(view)window.scrollTo({top:y,behavior:'instant'});return;
    }
    if(d.skill==='all-skills'&&d.audit_report){body.innerHTML=renderAuditRun(d);bindAuditLinkFilters(body);body.dataset.loaded='1';return}
    if(String(d.schema_version||'').startsWith('hermes-open-web-')){
      body.innerHTML=renderHermesRun(d);body.dataset.loaded='1';
      const filter=body.querySelector('[data-hermes-unit-filter]');
      if(filter)filter.addEventListener('input',()=>{const needle=filter.value.trim().toLowerCase();body.querySelectorAll('[data-hermes-unit-row]').forEach(row=>{row.hidden=Boolean(needle)&&!row.dataset.hermesUnitSearch.includes(needle)})});
      return
    }
    const assetByUrl=new Map((d.url_assets||[]).map(a=>[a.url,a]));
    const urls=(d.urls||[]).map(u=>{const a=assetByUrl.get(u.canonical_url)||{};const categories=(a.information_categories||[]).join('、')||'未确认';const fields=(a.extracted_fields||[]).join('、')||'无';return `<div class="run-source"><b>${txt(u.title||u.canonical_url)}</b> · ${badge(u.fetch_status||'not_run')}<div class="run-links">${link(u.canonical_url)}</div><div class="run-note">${txt(a.asset_status||'尚未形成资产记录')} · 信息类别：${txt(categories)} · 提取字段：${txt(fields)}<br>发现方式：${txt(a.query_strategy||'未记录')} · 粗筛 ${Number(u.coarse_score||0)} 分 · ${txt(u.coarse_reason||'')}${u.precise_reason?'<br>'+txt(u.precise_reason):''}</div></div>`}).join('')||'<div class="run-note">没有通过粗筛的网页。</div>';
    const facts=fieldTable((d.facts||[]).map(f=>({...f,entity:{chip_model:f.chip_model||'未确认芯片'},status:'独立复核未记录',old_value_basis:'unknown',published:false,change_kind:'unknown'})));
    const events=(d.events||[]).map(e=>`<div><span class="run-note">${txt(formatUpdateTime(e.time))} · ${txt(e.stage)}</span><br>${badge(e.status)} ${txt(e.message)}</div>`).join('');
    const details=`<div class="run-detail-block"><h4>URL 资产与访问记录 · ${d.urls?.length||0} 条</h4>${urls}</div>`+
      `<div class="run-detail-block"><h4>执行时间线 · ${d.events?.length||0} 条</h4><div class="run-timeline">${events||'<div class="run-note">本次运行没有时间线记录。</div>'}</div></div>`+
      `<div class="run-detail-block"><h4>技术参数</h4><pre class="run-json">${txt(JSON.stringify({search_provider:d.search_provider,extractor_model:d.extractor_model,target_fields:d.target_fields,queries:d.queries,schema_version:d.schema_version},null,2))}</pre></div>`;
    body.innerHTML=`${section('ow-facts',`字段提取与校验 · ${Number(d.counts?.validated||0)} 条`,facts,true)}${section('ow-details','详细记录',details)}`;
    body.dataset.loaded='1';
  }
  async function loadOpenWebHistory(){
    let box=$('open-web-test-history');
    if(!box){box=document.createElement('div');box.id='open-web-test-history';$('run-history')?.before(box)}
    const data=await fetchJSON(`${API}/data-agent/open-web-runs?final_only=false`);
    if(data._error){box.innerHTML='';return}
    const runs=data.runs||[];if(!runs.length){box.innerHTML='';return}
    if(!box.querySelector('.open-web-history-list'))box.innerHTML='<div class="run-history-header"><div><h3 style="margin:0">最新检查结果</h3></div></div><div class="open-web-history-list"></div>';
    const list=box.querySelector('.open-web-history-list'),sessions=new Set(runs.map(r=>r.session_id));
    [...list.children].forEach(card=>{if(!sessions.has(card.dataset.openWebSession))card.remove()});
    for(const [i,run] of runs.entries()){
      let card=[...list.children].find(c=>c.dataset.openWebSession===run.session_id);
      if(!card){
        const wrap=document.createElement('div');wrap.innerHTML=openWebCard(run);card=wrap.firstElementChild;
        const saved=state.restore?.openWeb?.find(x=>x.session===run.session_id);if(saved)card.open=saved.open;
        card.addEventListener('toggle',()=>{if(card.open)fillOpenWeb(card)});list.append(card);
      }
      if(list.children[i]!==card)list.insertBefore(card,list.children[i]||null);
      card.classList.toggle('audit-run',run.skill==='all-skills'&&Boolean(run.audit_summary));
      card.querySelector(':scope > summary').innerHTML=openWebSummary(run);
      if(card.open)await fillOpenWeb(card,true);
    }
  }
  window.loadRunHistory=async function({silent=false}={}){
    clearTimeout(state.timer);const mode=dataAgentMode();const generation=++state.generation;
    let root=$('run-history');if(!root){root=document.createElement('div');root.id='run-history';$('source-updates-list').before(root)}
    ensureManualForm(root);
    if(mode!==state.mode){const initial=state.mode===null;state.mode=mode;state.offset=0;root.innerHTML='';if(initial){try{const saved=JSON.parse(sessionStorage.getItem(storageKey));if(saved?.mode===mode){state.restore=saved;state.offset=saved.offset||0}}catch(_){}}}
    await loadOpenWebHistory();
    const data=await fetchJSON(`${API}/data-agent/runs?mode=${mode}&limit=${state.limit}&offset=${state.offset}`);
    if(generation!==state.generation)return;
    if(data._error){if(!silent)root.innerHTML=`<div class="error">${txt(data._error)}</div>`;return}
    if(!data.runs.length){root.innerHTML='';if(state.restore){window.scrollTo({top:state.restore.scrollY||0,behavior:'instant'});state.restore=null}if(generation===state.generation)state.timer=setTimeout(()=>{if($('tab-status').classList.contains('active'))loadRunHistory({silent:true})},15000);return}
    if(!root.querySelector('.run-history-list'))root.innerHTML='<details class="run-history-archive"><summary class="run-history-archive-summary"></summary><div class="run-history-list"></div><div class="run-history-pages run-toolbar"></div></details>';
    root.querySelector('.run-history-archive-summary').innerHTML=`历史运行 <span class="run-note">${Number(data.total)} 次</span>`;
    const list=root.querySelector('.run-history-list');const keys=new Set(data.runs.map(r=>r.key));
    [...list.children].forEach(c=>{if(!keys.has(c.dataset.runKey))c.remove()});
    const promises=[];
    for(const [i,r] of data.runs.entries()){
      let card=[...list.children].find(c=>c.dataset.runKey===r.key);
      if(!card){card=document.createElement('details');card.className='run-history-card';card.dataset.runKey=r.key;card.innerHTML='<summary></summary><div class="run-card-body">加载详情…</div>';const saved=state.restore?.cards?.find(x=>x.key===r.key);card.open=saved?saved.open:i===0;list.append(card);card.addEventListener('toggle',()=>{if(card.open)fill(card,r)})}
      if(list.children[i]!==card)list.insertBefore(card,list.children[i]||null);
      card.querySelector(':scope > summary').innerHTML=summary(r);
      if(card.open)promises.push(fill(card,r,true));
    }
    const pages=root.querySelector('.run-history-pages');pages.innerHTML=`<button class="btn secondary" data-prev ${state.offset===0?'disabled':''}>上一页</button><span class="run-note">${data.total?state.offset+1:0}–${Math.min(state.offset+state.limit,data.total)} / ${data.total}</span><button class="btn secondary" data-next ${state.offset+state.limit>=data.total?'disabled':''}>下一页</button>${data.warnings.length?`<span class="run-note">${data.warnings.length} 个旧会话不可读取</span>`:''}`;
    pages.querySelector('[data-prev]').onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);list.innerHTML='';loadRunHistory()};pages.querySelector('[data-next]').onclick=()=>{state.offset+=state.limit;list.innerHTML='';loadRunHistory()};
    await Promise.all(promises);
    if(state.restore){const y=state.restore.scrollY;state.restore=null;window.scrollTo(0,y||0)}
    if(generation===state.generation)state.timer=setTimeout(()=>{if($('tab-status').classList.contains('active'))loadRunHistory({silent:true})},15000);
  };
})();
