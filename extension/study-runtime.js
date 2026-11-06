/* Local execution inspector. Read-only; all model/source content is plain text. */
(() => {
  "use strict";
  const A=globalThis.AssistantClient, $=id=>document.getElementById(id);
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const btn=(label,fn,cls)=>{const b=el("button",label,cls);b.type="button";b.addEventListener("click",fn);return b;};
  const status={running:"正在执行",completed:"已完成",failed:"未完成",cancelled:"已取消",interrupted:"服务重启，记录中断"};
  const phases={answer:"组织回答",audit:"核对出处",route:"选择工具",vision:"读取图片",feedback:"练习反馈",classroom_events:"提取课堂事项",classroom_notes:"整理课堂笔记",live_alerts:"检查实时提醒"};
  const kindLabel={chat:"对话",study:"学习问答",operation:"课程操作",model:"模型请求",feedback:"练习反馈",classroom:"课堂分析",classroom_job:"课堂分析"};
  const number=n=>Number(n||0).toLocaleString("zh-CN");
  const date=n=>new Date(n*1000).toLocaleString("zh-CN",{month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
  let selected="",cursor=0,events=[],timer=null,generation=0,busy=false,mode="runs",listKey="",catalogLoaded=false;
  const dialog=el("dialog",undefined,"runtime-dialog");dialog.id="runtime-dialog";dialog.setAttribute("aria-labelledby","runtime-title");
  const head=el("header",undefined,"runtime-heading"),heading=el("div");
  heading.append(el("span","AGENT / LOCAL","runtime-kicker"));const title=el("h2","运行详情");title.id="runtime-title";heading.append(title);
  const right=el("div",undefined,"runtime-heading-actions"),live=el("span","等待读取","runtime-live");live.id="runtime-live";
  const close=btn("关闭 ×",()=>dialog.close());close.setAttribute("aria-label","关闭运行详情");right.append(live,close);head.append(heading,right);
  const nav=el("nav",undefined,"runtime-nav");nav.setAttribute("aria-label","运行查看方式");
  const runTab=btn("执行记录",()=>switchMode("runs")),catalogTab=btn("提示词与工具",()=>switchMode("catalog"));nav.append(runTab,catalogTab);
  const body=el("div",undefined,"runtime-body"),aside=el("aside",undefined,"runtime-sidebar"),list=el("div");list.id="runtime-list";
  aside.append(el("p","最近 30 天 · 最多 300 次","runtime-caption"),list);
  const main=el("section",undefined,"runtime-main");main.id="runtime-main";
  main.append(el("p","发送一个问题或任务后，这里会显示实际执行步骤。","runtime-empty"));body.append(aside,main);
  const catalog=el("section",undefined,"runtime-catalog");catalog.hidden=true;
  const error=el("p",undefined,"runtime-error");error.hidden=true;error.setAttribute("role","status");
  const foot=el("footer","记录留在本机，包含实际发送的提示词、资料摘录和工具结果；Key 已脱敏。这里展示执行事件，不展示模型隐藏思维。","runtime-footer");
  dialog.append(head,nav,error,body,catalog,foot);document.body.append(dialog);

  function detail(label,value,open=false){const d=el("details",undefined,"runtime-detail");d.open=open;d.append(el("summary",label),el("pre",typeof value==="string"?value:JSON.stringify(value,null,2)));return d;}
  function switchMode(next){mode=next;body.hidden=mode!=="runs";catalog.hidden=mode!=="catalog";runTab.setAttribute("aria-pressed",String(mode==="runs"));catalogTab.setAttribute("aria-pressed",String(mode==="catalog"));if(mode==="catalog")loadCatalog();else schedule(0);}
  async function loadCatalog(){
    if(catalogLoaded)return;
    try{const data=await A.api("/api/study/runtime/catalog");catalog.replaceChildren(el("p","这是当前版本的模板。某次请求实际使用的完整内容，请在“执行记录”中展开模型请求。","runtime-caption"));
      for(const p of data.prompts){const item=el("article",undefined,"runtime-prompt");item.append(el("h3",p.title),el("p",p.file+" · "+p.version,"runtime-caption"),detail("查看提示词",p.text));catalog.append(item);}
      catalog.append(detail("按任务补充的规则",data.purpose_rules),detail("可调用的工具及参数",[...data.tools,...data.retrieval_tools]),detail("上下文预算与存储",{...data.budgets,...data.storage}));catalogLoaded=true;
    }catch(e){error.textContent=e.message;error.hidden=false;}
  }
  function choose(id){if(selected===id)return;selected=id;cursor=0;events=[];++generation;listKey="";main.replaceChildren(el("p","读取执行记录…","runtime-empty"));schedule(0);}
  function renderList(items){
    const signature=JSON.stringify(items.map(r=>[r.id,r.status,r.updated]))+selected;if(signature===listKey)return;listKey=signature;
    const scroll=list.scrollTop,focus=document.activeElement?.dataset?.runId;list.replaceChildren();
    if(!items.length)list.append(el("p","还没有新版本的执行记录。原有对话仍保留。","runtime-caption"));
    for(const r of items){const b=btn("",()=>choose(r.id),"runtime-run"+(r.id===selected?" selected":""));b.dataset.runId=r.id;b.setAttribute("aria-pressed",String(r.id===selected));b.append(el("span",date(r.started)+" · "+(kindLabel[r.kind]||r.kind),"runtime-caption"),el("strong",r.label),el("small",[r.course,status[r.status]||r.status].filter(Boolean).join(" · ")));list.append(b);if(focus===r.id)b.focus({preventScroll:true});}list.scrollTop=scroll;
  }
  function metrics(run){
    const usage=events.filter(e=>["model.finish","model.error"].includes(e.kind)&&e.data.usage).map(e=>e.data.usage);
    const calls=events.filter(e=>e.kind==="model.start").length,tools=events.filter(e=>e.kind==="tool.start").length;
    const total=key=>usage.reduce((s,u)=>s+(u[key]||0),0),seconds=((run.ended||Date.now()/1000)-run.started).toFixed(1);
    const box=el("div",undefined,"runtime-metrics");
    const input=usage.length?number(total("input_tokens")):calls?(run.status==="running"?"待返回":"未提供"):"—";
    for(const [label,value] of [["耗时",seconds+" s"],["模型 / 工具",calls+" / "+tools],["已报告输入 tokens",input],["已报告输出 tokens",usage.length?number(total("output_tokens")):"—"],["缓存命中 tokens",usage.some(u=>u.cached_input_tokens!==null)?number(total("cached_input_tokens")):"未提供"]]){const item=el("div");item.append(el("small",label),el("strong",value));box.append(item);}return box;
  }
  function eventCard(event,started){
    const data=event.data,card=el("article",undefined,"runtime-event"+(event.kind.endsWith("error")?" event-error":""));card.dataset.seq=event.seq;
    const row=el("div",undefined,"runtime-event-heading");row.append(el("time","+"+(event.at-started).toFixed(1)+" s"),el("h3",event.label),el("span",event.kind,"runtime-event-kind"));card.append(row);
    if(event.kind==="model.start"){
      card.append(el("p",`${phases[data.phase]||data.phase} · ${data.model} · 输入 ${number(data.input_characters)} 字符 · 输出上限 ${number(data.max_output_tokens)} tokens`,"runtime-caption"));
      for(const m of data.request?.messages||[])card.append(detail(m.role==="system"?"系统提示词":m.role==="user"?"用户输入 / 资料与上下文":m.role==="assistant"?"上一步模型输出":m.role, m.content));
      if(data.request?.tools)card.append(detail("本次可用工具",data.request.tools));
      const config={...data.request};delete config.messages;delete config.tools;if(Object.keys(config).length)card.append(detail("请求参数",config));
    }else if(["model.finish","model.error"].includes(event.kind)){
      card.append(el("p",`${(data.duration_ms/1000).toFixed(1)} s · `+(data.usage?`输入 ${number(data.usage.input_tokens)} / 输出 ${number(data.usage.output_tokens)} tokens；缓存命中 ${data.usage.cached_input_tokens==null?"未提供":number(data.usage.cached_input_tokens)}`:"服务未返回 token 用量"),"runtime-caption"));
      if(data.error)card.append(el("p",data.error,"runtime-error"));
      if(data.response?.content)card.append(detail("模型返回",data.response.content));
      if(data.response?.tool_calls)card.append(detail("模型请求的工具调用",data.response.tool_calls,true));
    }else if(event.kind==="context.ready"){
      card.append(el("p",`${number(data.characters)} 字符 · ${data.source_count} 个原文片段 · ${data.history_turns} 轮对话线索`,"runtime-caption"),detail("本轮选入的记忆",data.memory));
    }else if(event.kind==="context.sources"){
      card.append(el("p",`本次新增 ${data.selected?.length||0} 段 · 原文预算 ${number(data.used_characters)} / ${number(data.total_budget)} 字符`,"runtime-caption"));
      if(data.deferred?.length)card.append(detail("未装入的片段",data.deferred));
    }else if(event.kind==="tool.finish"&&Array.isArray(data.result)){
      for(const item of data.result){const b=btn(item.title+" ↗",()=>StudyTools.showEvidence(item.id),"runtime-source");card.append(b);}card.append(detail("工具结果",data));
    }else if(event.kind.startsWith("tool.")){card.append(detail(data.arguments?"工具与参数":"执行结果",data,event.kind==="tool.start"||event.kind==="tool.selected"));}
    else if(event.kind==="validation.finish")card.append(detail("每条结论的核对结果",data.checks,true));
    else if(event.kind==="cache.hit")card.append(el("p","本步的相同输入已有完成记录，复用本地结果，没有再次请求模型。","runtime-caption"));
    else card.append(detail("记录",data));
    return card;
  }
  function renderRun(run){
    let summary=$("runtime-summary"),timeline=$("runtime-timeline");
    if(!timeline){main.replaceChildren();summary=el("div",undefined,"runtime-summary");summary.id="runtime-summary";timeline=el("div",undefined,"runtime-timeline");timeline.id="runtime-timeline";main.append(summary,timeline);}
    const snapshot=el("div",undefined,"runtime-summary-title");snapshot.append(el("span",status[run.status]||run.status,"runtime-state "+run.status),el("h3",run.label));
    summary.replaceChildren(snapshot,el("p",[run.course,date(run.started)].filter(Boolean).join(" · "),"runtime-caption"),metrics(run));
    if(run.parent_id)summary.append(btn("查看发起这项任务的对话 ↗",()=>choose(run.parent_id),"runtime-parent"));
    const known=new Set([...timeline.children].map(n=>Number(n.dataset.seq)));for(const e of events)if(!known.has(e.seq))timeline.append(eventCard(e,run.started));
    live.textContent=run.status==="running"?"● 实时更新":"已同步";live.classList.toggle("active",run.status==="running");
  }
  function schedule(delay=1700){clearTimeout(timer);if(dialog.open&&!document.hidden&&mode==="runs")timer=setTimeout(refresh,delay);}
  async function refresh(){
    if(busy){schedule();return;}if(!dialog.open||document.hidden||mode!=="runs")return;
    busy=true;const version=generation;
    try{
      const data=await A.api("/api/study/runtime?limit=60",{timeout:8000});if(version!==generation||!dialog.open)return;
      if(!selected&&data.items.length){selected=data.items[0].id;cursor=0;events=[];}renderList(data.items);
      if(selected){const id=selected,run=await A.api("/api/study/runtime/"+encodeURIComponent(id)+"?after="+cursor,{timeout:8000});if(version!==generation||id!==selected||!dialog.open)return;
        const seen=new Set(events.map(e=>e.seq));events.push(...run.events.filter(e=>!seen.has(e.seq)));cursor=events.at(-1)?.seq||0;renderRun(run);}
      else live.textContent="等待新的请求";error.hidden=true;
    }catch(e){if(version===generation&&dialog.open){error.textContent=e.message;error.hidden=false;live.textContent="等待连接恢复";}}
    finally{busy=false;schedule();}
  }
  function open(id=""){if(id&&id!==selected)choose(id);else if(!id){selected="";cursor=0;events=[];++generation;main.replaceChildren();}if(!dialog.open)dialog.showModal();switchMode("runs");}
  dialog.addEventListener("close",()=>{++generation;clearTimeout(timer);});
  document.addEventListener("visibilitychange",()=>schedule(0));
  $("open-runtime")?.addEventListener("click",()=>open());
  document.addEventListener("study-runtime",e=>open(e.detail||""));
  window.StudyRuntime={open};
})();
