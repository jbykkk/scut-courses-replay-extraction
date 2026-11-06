(() => {
  "use strict";
  const A = AssistantClient, labels = {read_local_path:"读取资料",process_missing_lessons:"处理课程回放",export_study_pack:"生成学习资料",lookup_reference:"查找外部参考",research_topic:"查文献，对照课堂",reconstruct_class:"对照课堂与题目",set_review_preferences:"调整复习时间"};
  const el = (tag,text,cls) => {const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n;};
  const post = (path,body={}) => A.api("/api/study"+path,{method:"POST",body});
  const button = (text,action) => {const b=el("button",text);b.type="button";b.addEventListener("click",async()=>{b.disabled=true;try{await action();}catch(e){const note=document.getElementById("notice");note.hidden=false;note.textContent=e.message;}finally{b.disabled=false;}});return b;};
  function sourceLink(root,id,title){root.append(button(title,()=>StudyTools.showEvidence(id)));}
  const clock = seconds => new Date(Math.max(0,seconds||0)*1000).toISOString().slice(11,19);
  async function download(job,file){const {token}=await A.connection();const response=await fetch(A.base+`/api/study/exports/${job.id}/${file.format}`,{headers:{Authorization:"Bearer "+token}});if(!response.ok)throw new Error("导出文件还不可用，请重试");const url=URL.createObjectURL(await response.blob());const link=el("a");link.href=url;link.download=file.name;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
  function renderJob(root,job){
    const signature=JSON.stringify(job);if(root.dataset.signature===signature)return;root.dataset.signature=signature;root.replaceChildren();
    root.className="operation-card";root.append(el("span",labels[job.tool]||"任务","fine"),el("p",job.message));
    const active=["queued","running","waiting_browser"].includes(job.status);
    if(job.status==="waiting_browser"){
      root.append(el("p","任务已保存。连接百步梯并打开这门课后，会自动继续。","fine"));
      root.append(button("登录百步梯，继续处理 ↗",()=>document.dispatchEvent(new CustomEvent("study-connect-school",{detail:{course:job.course}}))));
    }
    if(job.items?.length){const list=el("details");list.append(el("summary",`课时 / 文件明细 · ${job.items.length}`));for(const item of job.items){const row=el("div",undefined,"operation-item");row.append(el("strong",item.title||item.file),el("span",({ready:"已读取",pending:"待提交",queued:"后台处理中",done:"已完成",skipped:"已有结果，跳过",failed:"失败"})[item.status]||item.status));if(item.error)row.append(el("p",item.error));list.append(row);}root.append(list);}
    for(const file of job.files||[])root.append(button("下载 "+file.format.toUpperCase(),()=>download(job,file)));
    for(const ref of job.references||[]){const row=el("div",undefined,"operation-item"),a=el("a",ref.title+" ↗");try{const url=new URL(ref.url);if(url.protocol!=="https:"||url.username||url.password)continue;a.href=url.href;}catch{continue;}a.target="_blank";a.rel="noreferrer noopener";row.append(a);if(ref.coverage)row.append(el("small",({metadata_only:"仅书目信息",abstract_only:"仅摘要",bounded_full_text:"已读取部分正文",full_text_available:"正文待读"})[ref.coverage]||"待核对"));root.append(row);}
    for(const warning of job.warnings||[])root.append(el("p",warning,"uncertainty"));
    for(const link of job.links||[]){const row=el("div",undefined,"alignment-card");row.append(el("span","关联候选 · 尚未确认","fine"),el("p",link.reason));sourceLink(row,link.speech.id,`${link.speech.title} · ${clock(link.speech.locator.start)}–${clock(link.speech.locator.end)}`);sourceLink(row,link.material.id,`${link.material.title} · 第 ${link.material.locator.page||link.material.locator.slide||1} 页`);for(const v of link.visuals||[]){const a=el("a",`课堂截图 ${clock(v.seconds)} ↗`);a.href=v.url;a.target="_blank";a.rel="noreferrer noopener";row.append(a);}root.append(row);}
    for(const c of job.result?.claims||[]){root.append(el("p",c.text));for(const ref of c.citations)sourceLink(root,ref.evidence_id,"查看依据");if(c.verification)root.append(el("small",c.verification.label));}
    for(const text of job.result?.uncertainties||[])root.append(el("p",text,"uncertainty"));
    if(job.trace_id)root.append(button("查看任务执行过程 ↗",()=>document.dispatchEvent(new CustomEvent("study-runtime",{detail:job.trace_id}))));
    if(active)root.append(button("取消未完成步骤",()=>post(`/operations/${job.id}/cancel`)));
    if(job.status==="failed"||job.items?.some(i=>i.status==="failed"))root.append(button("重试失败部分",()=>post(`/operations/${job.id}/retry`)));
  }
  function updateJobs(jobs){for(const root of document.querySelectorAll("[data-operation-id]")){const job=jobs.find(x=>x.id===root.dataset.operationId);if(job)renderJob(root,job);}}
  async function sources(){
    const data=await A.api("/api/study/sources"),root=document.getElementById("source-list");root.replaceChildren();
    const filter=el("input");filter.type="search";filter.placeholder="搜索课程或文件";filter.setAttribute("aria-label","搜索资料");root.append(filter);const listing=el("div");root.append(listing);
    const roles={reference:"教材 / 参考书",exercise:"题目 / 作业原件",slides:"课件",transcript:"课堂转写",external:"外部参考",other:"其他资料"};
    const render=()=>{listing.replaceChildren();for(const s of data.items.filter(s=>(s.title+" "+s.course).toLowerCase().includes(filter.value.toLowerCase()))){const row=el("div",undefined,"managed-source"),controls=el("div",undefined,"source-controls");row.append(el("strong",s.title),el("p",`${s.course} · ${s.kind==="asr"||s.kind==="school_subtitle"?"字幕原文，待核对":s.kind==="image_reading"?"图片识读，待核对":"原始资料"}`,"fine"));
      const role=el("select");role.setAttribute("aria-label",s.title+"的资料类型");for(const [value,label]of Object.entries(roles)){const o=el("option",label);o.value=value;role.append(o);}role.value=s.policy.role;
      const trusted=el("input");trusted.type="checkbox";trusted.checked=s.policy.trusted;trusted.disabled=["asr","school_subtitle","image_reading","student_report"].includes(s.kind);const label=el("label","用作核对参考");label.prepend(trusted);
      controls.append(role,label,button("保存分类",async()=>{await post(`/sources/${s.id}`,{role:role.value,trusted:trusted.checked,excluded:s.policy.excluded});await sources();}),button(s.policy.excluded?"恢复使用":"从回答中排除",async()=>{await post(`/sources/${s.id}`,{role:role.value,trusted:trusted.checked,excluded:!s.policy.excluded});await sources();}));
      row.append(controls);if(s.policy.excluded)row.append(el("span","已停用；原文和旧引用保留","fine"));listing.append(row);}
      const memory=el("div",undefined,"memory-list");memory.append(el("h3","核对记录"),el("p","只把你确认的条目用于长期记忆。来源失效后暂停引用；可随时撤回。","fine"));
      for(const m of data.knowledge.filter(m=>m.status!=="dismissed")){const row=el("div",undefined,"managed-source");row.append(el("span",m.status==="confirmed"?"你已确认":"待核对","pill"),el("p",m.text));for(const c of m.citations)sourceLink(row,c.evidence_id,"原文");row.append(button("撤回这条记忆",async()=>{await post(`/knowledge/${m.id}/dismiss`);await sources();}));memory.append(row);}listing.append(memory);};
    filter.addEventListener("input",render);render();
  }
  window.StudyActions={updateJobs,renderJob,sources};
})();
