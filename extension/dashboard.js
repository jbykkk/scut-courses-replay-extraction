const A=AssistantClient,L=CourseLibrary,$=id=>document.getElementById(id),isExtension=!!globalThis.chrome?.runtime?.id;
let sessions=[],imports=[],trashed=[],selected=null,detail=null,catalogue=[],catalogueCourse="",loading=false,detailStamp=0,listStamp="",course="",filter="lessons",eventCategory="all",focusedEventId=null,configured=false,llmLabel="AI";
const eventCache=new Map();
let eventHydration=null,noticeSource="";
let catalogueContext=null,catalogueRequest=0,sourceTabs=new Map(),sourceRefresh=0;
function initCourseRail(){
  const toggle=$("toggle-courses"),rail=$("course-rail"),layout=$("library-layout");
  if(!toggle||!rail||!layout)return;
  const key="scut.library.coursesHidden";let hidden=false;
  try{hidden=localStorage.getItem(key)==="1";}catch{ /* Layout still works when browser storage is unavailable. */ }
  function apply(){rail.hidden=hidden;layout.classList.toggle("courses-hidden",hidden);toggle.textContent=hidden?"展开我的课程":"收起我的课程";toggle.setAttribute("aria-expanded",String(!hidden));}
  toggle.onclick=()=>{hidden=!hidden;apply();try{localStorage.setItem(key,hidden?"1":"0");}catch{ /* Keep this page's selection. */ }};
  apply();
}
initCourseRail();
function sourceCourse(url){try{const u=new URL(url);return u.origin+"/"+u.searchParams.get("course_id")+"/"+(u.searchParams.get("tenant_code")||"21");}catch{return "";}}
async function refreshSourceTabs(){
  if(!isExtension)return;
  const stamp=++sourceRefresh,select=$("source-tab"),previous=sourceTabs.get(Number(select.value)),expected=catalogueContext?.url||previous?.url;
  const tabs=await chrome.tabs.query({url:["https://video.jw.scut.edu.cn/livingroom*","https://video-jw-443.webvpn.scut.edu.cn/livingroom*"]});
  if(stamp!==sourceRefresh)return;
  const initial=new URLSearchParams(location.search).get("tab");
  const chosen=expected?tabs.find(t=>String(t.id)===select.value&&sourceCourse(t.url)===sourceCourse(expected))||tabs.find(t=>sourceCourse(t.url)===sourceCourse(expected)):tabs.find(t=>String(t.id)===initial)||tabs[0];
  sourceTabs=new Map(tabs.map(t=>[t.id,t]));
  select.replaceChildren(new Option(tabs.length?"选择学校播放页":"请先打开学校课程播放页",""),...tabs.map(t=>new Option(t.title,t.id)));
  select.value=chosen?String(chosen.id):"";$("load-catalogue").disabled=!chosen;
  if(!chosen)$("batch-hint").textContent=expected?"这门课的播放页已关闭或切换；打开同一门课后可继续。":"请先打开要导入的课程播放页。";
}
function clearCatalogue(){catalogueRequest++;catalogue=[];catalogueContext=null;catalogueCourse="";$("catalogue").replaceChildren();$("start-batch").disabled=true;$("batch-hint").textContent="点击读取课时目录，选择要导入的课时。";}
const params=new URLSearchParams(location.search);
if(/^[a-f0-9]{32}$/.test(params.get("session")||""))selected=params.get("session");
if(/^\d+$/.test(params.get("course")||""))course=params.get("course");
const labels={queued:"等待本地处理",recording:"正在录音",stopping:"正在保存字幕",downloading:"下载音轨",transcribing:"本地转写",processing:"处理字幕",analyzing:"整理笔记",complete:"字幕已保存",failed:"字幕处理失败",cancelled:"已停止任务",interrupted:"录制中断"};
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function clock(sec){const n=Math.max(0,Math.floor(sec||0));return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(x=>String(x).padStart(2,"0")).join(":");}
function versionLabel(s){return (s.source_label||"字幕")+" · "+(s.coverage?.length?s.coverage.map(([a,b])=>clock(a)+"–"+clock(b)).join(" / "):labels[s.status]||s.status)+(s.time_basis==="capture"?" · "+new Date(s.created_at*1000).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"}):"");}
function transcriptionProgress(s){return s.transcription_total?`已处理 ${s.transcription_done||0} / ${s.transcription_total} 段音频 · 当前 ${s.segment_count||0} 条字幕`:s.progress||"等待课程音频";}
function eventPosition(e){const start=clock(e.start),end=Number(e.end)||0;return `课内 ${start}`+(end>e.start+1&&end-e.start<=120?`–${clock(end)}`:end-e.start>120?` · 补充至 ${clock(end)}`:"")+(e.time_basis==="capture"?" · 录音时间":"");}
function eventDate(e){return e.lesson_at?new Date(e.lesson_at*1000).toLocaleDateString("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit"}):"课时日期未记录";}
function categoryLabel(id){return L.eventCategories.find(c=>c.id===id)?.label||"其他事项";}
function renderEventCategories(target,events,onChange){
  if(eventCategory!=="all"&&!events.some(e=>e.category===eventCategory))eventCategory="all";
  target.replaceChildren();const choices=[{id:"all",label:"全部"},...L.eventCategories.filter(c=>events.some(e=>e.category===c.id))];
  for(const c of choices){const count=c.id==="all"?events.length:events.filter(e=>e.category===c.id).length,b=node("button",`${c.label} ${count}`,"event-category"+(eventCategory===c.id?" active":""));b.setAttribute("aria-pressed",String(eventCategory===c.id));b.onclick=()=>{eventCategory=c.id;onChange();};target.append(b);}
}
function completeSavedEvents(rows){
  const missing=[];
  for(const s of rows){if(s.important_events_complete||!s.event_count)continue;const cached=eventCache.get(s.id);if(cached?.updated_at===s.updated_at)s.important_events=cached.events;else missing.push(s);}
  if(eventHydration||!missing.length)return;
  eventHydration=(async()=>{
    for(let i=0;i<missing.length;i+=6){await Promise.all(missing.slice(i,i+6).map(async s=>{
      try{const record=await A.api(`/api/sessions/${s.id}`),events=record.events.filter(e=>e.source==="deepseek");eventCache.set(s.id,{updated_at:s.updated_at,events});}catch{ /* The compact list remains usable if a detail cannot be read. */ }
    }));}
    eventHydration=null;let changed=false;
    for(const s of sessions){const cached=eventCache.get(s.id);if(!s.important_events_complete&&cached?.updated_at===s.updated_at){s.important_events=cached.events;changed=true;}}
    if(changed){listStamp="";if(!selected)renderLibrary();}
  })();
}
function status(text,error=false,source="action"){noticeSource=source==="action"&&/本地服务未启动|暂时无法连接本地服务|本地服务响应超时/.test(text)?"connection":source;$("status").textContent=text;$("status").className="notice"+(error?" error":"");AssistantUI.toast(text);if($("import-dialog").open)$("batch-status").textContent=text;}
function clearConnectionNotice(){if(noticeSource==="connection"){noticeSource="";$("status").textContent="";$("status").className="notice hidden";}}
function seek(sec){if(detail.time_basis==="capture")return node("span",clock(sec),"muted");const url=new URL(detail.page_url);url.searchParams.set("note_play",Math.floor(sec));const a=node("a",clock(sec)+" ↗");a.href=url.href;a.target="_blank";a.rel="noopener noreferrer";a.title="从此处打开学校回放";return a;}
async function act(button,fn){button.disabled=true;try{await fn();}catch(e){status(e.message,true);}finally{button.disabled=button.id==="start-batch"&&(!catalogueContext||!catalogue.length);if(detail)renderAction();}}
function matches(value){return !$("search").value.trim()||JSON.stringify(value).toLowerCase().includes($("search").value.trim().toLowerCase());}
function navigate(id=null){
  if(globalThis.LectureMindmap&&!LectureMindmap.canLeave())return;
  globalThis.LectureMindmap?.reset();
  globalThis.Courseware?.reset();
  if(!id)focusedEventId=null;
  selected=id;detail=null;detailStamp=0;
  const url=new URL(location.href);
  if(id)url.searchParams.set("session",id);else url.searchParams.delete("session");
  if(course)url.searchParams.set("course",course);else url.searchParams.delete("course");
  history.replaceState(null,"",url);
  $("browse").hidden=!!id;$("detail").hidden=!id;
  if(id){
    const lesson=sessions.find(s=>s.id===id),name=lesson?.title||"这节课";
    $("detail-course").textContent=lesson?lesson.course_title+" · "+name:"";
    $("detail-title").textContent="正在读取「"+name+"」…";
    $("detail-meta").textContent="连接恢复后会自动显示这节课的笔记";
    $("detail-warning").textContent="";$("detail-warning").hidden=true;
    $("analysis-progress").hidden=true;$("lesson-versions").replaceChildren();
    globalThis.LectureCompare?.reset();
    $("view-summary").replaceChildren();$("view-events").replaceChildren();$("view-transcript").replaceChildren();
    document.querySelector(".version-panel").hidden=true;document.querySelector(".detail-actions").hidden=true;document.querySelector(".tabs").hidden=true;
    void refreshDetail();
  }else renderLibrary();
  window.scrollTo({top:0,behavior:"instant"});
}
function showView(view){for(const item of document.querySelectorAll("[data-view]")){const on=item.dataset.view===view;item.classList.toggle("active",on);item.setAttribute("aria-selected",String(on));$("view-"+item.dataset.view).hidden=!on;}if(view==="mindmap")globalThis.LectureMindmap?.activate();}
function openLesson(id,view="summary",eventId=null){focusedEventId=eventId;navigate(id);showView(view);}
function courseButton(id,title,count,notes){const b=node("button",undefined,"course-choice"+(course===id?" active":""));b.append(node("strong",title),node("small",count+" 节课 · "+notes+" 份笔记"));b.setAttribute("aria-pressed",String(course===id));b.onclick=()=>{course=id;navigate();};return b;}
function renderLibrary(){
  const records=L.lessons([...sessions,...L.importRows(imports)]);
  const groups=L.groups(records),chosen=groups.find(g=>g.id===course),all=chosen?.lessons||records;
  const rail=$("course-list");rail.replaceChildren(courseButton("","全部课程",records.length,records.filter(L.hasNotes).length));
  for(const g of groups)rail.append(courseButton(g.id,g.title,g.lessons.length,g.ready));
  $("course-title").textContent=chosen?.title||"全部课程";
  const pending=all.filter(s=>s.lesson_pending).length;
  $("course-meta").textContent=all.length+" 节课 · "+all.filter(L.hasNotes).length+" 份完整笔记"+(pending?" · "+pending+" 份待整理":"");
  document.querySelector('[data-filter="pending"]').textContent="待整理"+(pending?" "+pending:"");
  const cloud=$("topic-cloud");cloud.replaceChildren();
  if(filter==="lessons"&&chosen){for(const t of chosen.topics.slice(0,12)){const b=node("button",t,"topic-chip");b.onclick=()=>{$("search").value=t;renderLibrary();};cloud.append(b);}}
  const eventFilters=$("event-categories");eventFilters.hidden=filter!=="events";
  const target=$("lesson-grid");target.replaceChildren();
  if(filter==="trash"){
    for(const s of trashed.filter(s=>(!course||s.course_id===course)&&matches(s.title))){const card=node("article",undefined,"note-card import-card");card.append(node("div",s.course_title,"note-meta"),node("h3",s.title),node("p","已移出课程。恢复后会重新检查时间覆盖关系。"));const b=node("button","恢复这份字幕");b.onclick=()=>act(b,async()=>{await A.api(`/api/sessions/${s.id}/restore`,{method:"POST"});await refresh();});card.append(b);target.append(card);}
  }else if(filter==="events"){
    const timeline=L.eventTimeline(chosen?[chosen]:groups).filter(matches);
    renderEventCategories(eventFilters,timeline,renderLibrary);
    const events=timeline.filter(e=>eventCategory==="all"||e.category===eventCategory);
    for(const e of events){const card=node("article",undefined,"library-event");
      card.dataset.category=e.category;
      const head=node("div",undefined,"library-event-head");head.append(node("span",categoryLabel(e.category),"event-badge"),node("strong",eventDate(e),"event-date"));
      card.append(head,node("small",e.course+" · "+e.lesson,"event-lesson"),node("h3",e.message),node("p",eventPosition(e),"event-time"));
      const facts=node("div",undefined,"event-brief");for(const [key,label] of [["deadline","截止／时间"],["action","要做什么"],["submission","提交方式"],["requirements","具体要求"],["grading","评分"]]){if(e.details?.[key])facts.append(node("span",label+" · "+e.details[key]));}if(facts.children.length)card.append(facts);
      const foot=node("div",undefined,"library-event-foot"),b=node("button","查看本课原文与上下文 ↗");b.onclick=()=>openLesson(e.sid,"events",e.id);foot.append(b);
      if(e.time_basis!=="capture"&&e.page_url){const url=new URL(e.page_url);url.searchParams.set("note_play",Math.floor(e.start||0));const a=node("a","从此处播放回放 ↗");a.href=url.href;a.target="_blank";a.rel="noopener noreferrer";foot.append(a);}
      card.append(foot);target.append(card);
    }
  }else{
    const rows=all.filter(s=>(filter!=="pending"||s.lesson_pending)&&matches([s.course_title,s.title,s.summary,s.important_events])).sort(L.compareLessons);
    for(const s of rows){
      if(s.isImport){const card=node("article",undefined,"note-card import-card");card.append(node("div",s.course_title,"note-meta"),node("h3",s.title),node("span",s.status==="failed"?"导入未完成":"正在读取学校数据","note-state pending"),node("p",s.error||"尚未交给本地处理。请保持浏览器、学校课时标签页和本地服务运行。"));
        const link=node("a",s.status==="failed"?"返回本节课重新导入 ↗":"打开学校课时 ↗","text-button");link.href=s.page_url;link.target="_blank";link.rel="noopener noreferrer";card.append(link);target.append(card);continue;}
      const card=node("button",undefined,"note-card"),titles=L.titles(s);
      const state=L.busy(s)?"整理中 "+(s.analysis_progress||""):s.analysis_status==="failed"?"笔记待续":L.hasNotes(s)?"笔记就绪":labels[s.status]||s.status;
      card.append(node("div",s.course_title,"note-meta"),node("div",s.title,"lesson-date"),node("h3",s.summary?.headline||titles.slice(0,2).join(" · ")||"本课字幕"));
      const assignments=(s.important_events||[]).filter(e=>e.category==="assignment");if(assignments.length)card.append(node("span",`${assignments.length} 项作业要求 · 查看原文`,"note-assignment-tag"));
      const unfinished=s.versions.find(v=>v.isImport);if(unfinished)card.append(node("p",unfinished.error?"另一次导入未完成："+unfinished.error:"正在读取本课的新字幕来源，请保持学校课时页面打开。","import-summary"));
      card.append(node("p",(s.summary?.abstract||s.summary?.overview||(s.analysis_status==="failed"?(s.notes_retry_at>Date.now()/1000&&s.notes_auto_attempts<3?"字幕已保存，稍后自动重试，已完成的笔记会复用。":"字幕已保存。自动重试未完成，请查看错误原因。"):["queued","downloading","transcribing","processing"].includes(s.status)?transcriptionProgress(s):s.segment_count?"字幕已保存，将自动整理本课知识点与重要事项。":s.error||"尚未识别出可用字幕。")).slice(0,180)));
      const footer=node("footer");footer.append(node("span",state,"note-state"+(s.analysis_status==="failed"?" failed":L.hasNotes(s)?"":" pending")),node("span",s.record_count+" 份字幕 · 查看课时 →"));card.append(footer);card.onclick=()=>openLesson(s.id);target.append(card);
    }
  }
  if(!target.children.length){const empty=node("div",undefined,"library-empty");empty.append(node("h3",sessions.length?(filter==="events"?"暂无匹配的重要事项":filter==="pending"?"没有待整理的记录":"没有匹配的内容"):"还没有课堂记录"),node("p",sessions.length?"可以切换课程或清除搜索条件。":"在学校页面开始课堂字幕，或导入已结束的课程回放。"));target.append(empty);}
  $("browse").hidden=!!selected;$("detail").hidden=!selected;
}
function renderAction(){
  const action=L.action({...detail,segment_count:detail.segments.length},configured);
  $("reanalyze").textContent=action.label;$("reanalyze").hidden=!!action.hidden;$("reanalyze").disabled=!!action.disabled;
  const audioBusy=detail.jobs.some(j=>["asr","media"].includes(j.lane)&&["pending","running"].includes(j.state));
  $("regenerate").hidden=!detail.summary;$("regenerate").disabled=L.busy(detail)||audioBusy||!detail.stopped;
  $("repair").hidden=detail.mode==="subtitle"||detail.status==="cancelled";$("repair").disabled=audioBusy||!detail.stopped;
  $("retry").hidden=!detail.jobs.some(j=>j.lane!=="analysis"&&j.state==="failed")||detail.status==="cancelled";
  $("attach").hidden=!isExtension||detail.time_basis==="capture";$("cancel").hidden=!audioBusy&&detail.status!=="interrupted";$("delete-record").disabled=!detail.stopped||detail.jobs.some(j=>["pending","running"].includes(j.state));$("delete-record").title=$("delete-record").disabled?"请先结束录音或停止任务，再移到回收站":"所选字幕及其笔记可恢复";
}
function renderDetail(){
  if(!detail)return;
  document.querySelector(".version-panel").hidden=false;document.querySelector(".detail-actions").hidden=false;document.querySelector(".tabs").hidden=false;
  const versions=$("lesson-versions");versions.replaceChildren();
  for(const s of detail.versions||[]){const b=node("button",versionLabel(s),"version-choice"+(s.id===detail.id?" active":""));b.setAttribute("aria-pressed",String(s.id===detail.id));b.onclick=()=>navigate(s.id);versions.append(b);}
  $("detail").hidden=false;$("browse").hidden=true;
  $("detail-course").textContent=detail.course_title+" · "+detail.title;$("detail-title").textContent=detail.summary?.headline||detail.title;
  const audio=detail.jobs.filter(j=>["chunk","repair"].includes(j.kind)),done=audio.filter(j=>j.state==="done").length;
  $("detail-meta").textContent=(labels[detail.status]||detail.status)+(audio.some(j=>["pending","running"].includes(j.state))?` · 已处理 ${done}/${audio.length} 段音频 · 当前 ${detail.segments.length} 条字幕`:` · ${detail.segments.length} 条字幕`)+" · "+(detail.time_basis==="capture"?"时间从本次录音开始":"时间对应原视频");
  const warning=[detail.error,detail.analysis_status==="failed"?detail.warning:"",detail.status==="interrupted"?detail.warning:""].filter(Boolean);
  $("detail-warning").textContent=[...new Set(warning)].join("\n");$("detail-warning").hidden=!warning.length;
  const busy=L.busy(detail),partial=!!detail.summary?.partial;
  $("analysis-progress").hidden=!busy&&!partial;
  $("progress-text").textContent=detail.analysis_stage==="events"&&busy?"通读全文，合并同一事项的任务、时间与要求":(busy?(detail.analysis_stage==="outline"?"生成整课概览":"正在整理"):"已保存部分笔记")+" · "+(detail.analysis_done||0)+" / "+(detail.analysis_total||"?")+" 段";
  $("progress-model").textContent=detail.analysis_model||"";
  $("progress-bar").max=detail.analysis_total||1;$("progress-bar").value=detail.analysis_done||0;
  const summary=$("view-summary");summary.replaceChildren();
  const overviewEvents=detail.events.filter(e=>e.source==="deepseek"),assignments=overviewEvents.filter(e=>e.category==="assignment");
  if(overviewEvents.length){const strip=node("section",undefined,"lesson-alerts"),heading=node("div",undefined,"lesson-alerts-heading"),open=node("button","查看全部事项 ↗");open.onclick=()=>showView("events");heading.append(node("strong",`本课重要事项 · ${overviewEvents.length} 项`),open);strip.append(heading);
    const highlights=[...assignments,...overviewEvents.filter(e=>e.category!=="assignment"&&e.category!=="question").sort((a,b)=>(b.start||0)-(a.start||0))].slice(0,5);
    for(const e of highlights){const line=node("div",undefined,"lesson-alert-line");line.append(node("span",categoryLabel(e.category),"event-badge"),node("span",e.message),seek(e.start));strip.append(line);}
    if(assignments.length>highlights.length)strip.append(node("small","还有作业要求，点击“查看全部事项”阅读完整列表。"));summary.append(strip);}
  if(detail.summary){
    const topics=detail.summary.topics||[];
    if(detail.summary.abstract)summary.append(node("p",detail.summary.abstract,"lesson-abstract"));
    summary.append(node("p",topics.length+" 条分段笔记"+(partial?" · 后续内容仍在整理":"")+" · 点击时间定位回放","overview-intro"));
    const list=node("div",undefined,"topic-notes");
    for(const [index,t] of topics.entries()){const card=node("details",undefined,"topic-note"),top=node("header");card.id="topic-"+index;
      const compare=node("button","课件 · 原话 · 笔记 ↗");compare.onclick=()=>{showView("compare");globalThis.LectureCompare?.select(index);};
      top.append(seek(t.start),compare);if(t.chapter)top.append(node("span",t.chapter,"chapter-tag"));
      card.append(node("summary",t.title),top,LectureCompare.markdown(t.detail));list.append(card);
    }
    if(!topics.length)list.append(node("p",detail.summary.overview||"本段未提取到独立知识点。","overview-intro"));
    const all=node("details",undefined,"all-notes");all.open=!detail.summary.groups?.length;all.append(node("summary","全部分段笔记 · "+topics.length),list);
    if(detail.summary.groups?.length){const groups=node("div",undefined,"theme-grid");for(const g of detail.summary.groups){const card=node("article",undefined,"theme-card");card.append(node("h3",g.title),LectureCompare.markdown(g.detail));const refs=node("div",undefined,"theme-refs");for(const index of g.topic_indices||[]){if(!topics[index])continue;const link=node("button",topics[index].title);link.onclick=()=>{all.open=true;const target=list.children[index];target.open=true;target.scrollIntoView({block:"center",behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"});};refs.append(link);}card.append(refs);groups.append(card);}summary.append(groups);}
    summary.append(all);
  }else{
    const empty=node("div",undefined,"library-empty"),action=L.action({...detail,segment_count:detail.segments.length},configured);
    empty.append(node("h3",busy?"正在阅读本课字幕":detail.analysis_status==="failed"?"笔记生成中断":"本课笔记尚未生成"),
      node("p",busy?"第一段完成后会自动显示，页面可以关闭。":!configured?`在设置中保存一次 ${llmLabel} Key，现有和今后保存的字幕都会自动生成笔记。`:detail.analysis_status==="failed"?"上方显示了具体原因。点击“继续生成笔记”会复用已经完成的部分。":"本课已交给后台自动整理，无需点击生成。可以关闭页面，稍后到课程笔记查看。"));
    if(action.disabled&&!busy)empty.append(node("small",action.label));
    summary.append(empty);
  }
  const events=$("view-events");events.replaceChildren();
  const allConfirmed=detail.events.filter(e=>e.source==="deepseek").sort((a,b)=>(b.start||0)-(a.start||0));
  events.append(node("p",detail.events_version?"已结合全文整理。同一事项的补充说明合并显示，点击出处可核对原话。":"这是旧版或课堂中的即时分析。点击“整理完整事项”，结合全文重新核对作业与要求。","overview-intro"));
  const detailFilters=node("div",undefined,"event-categories");detailFilters.setAttribute("role","group");detailFilters.setAttribute("aria-label","按本课事项类型筛选");renderEventCategories(detailFilters,allConfirmed,renderDetail);events.append(detailFilters);
  const confirmed=allConfirmed.filter(e=>eventCategory==="all"||e.category===eventCategory);
  for(const e of confirmed){const card=node("article",undefined,"topic-note event-note"),top=node("header");card.dataset.category=e.category;top.append(node("strong",categoryLabel(e.category),"event-badge"),node("span",eventPosition(e),"event-time"),seek(e.start));card.append(top,node("h3",e.message));
    const facts=node("dl",undefined,"event-facts");
    for(const [key,label] of [["action","任务"],["deadline","时间"],["submission","提交方式"],["requirements","具体要求"],["grading","评分"]]){
      const value=e.details?.[key];if(value){facts.append(node("dt",label),node("dd",value));}
    }card.append(facts);
    const source=node("details",undefined,"event-source");source.append(node("summary","查看原文与上下文"));
    const ids=new Set(e.segment_ids),positions=detail.segments.map((s,i)=>ids.has(s.id)?i:-1).filter(i=>i>=0),included=new Set(positions.flatMap(i=>Array.from({length:7},(_,n)=>i+n-3).filter(n=>n>=0&&n<detail.segments.length)));
    if(!positions.length)for(const q of e.evidence_quotes||[e.evidence])source.append(node("blockquote",q));
    else source.append(node("small","深色为引用句，浅色为前后文。"));
    for(const i of [...included].sort((a,b)=>a-b)){const s=detail.segments[i],line=node("div",undefined,"event-context"+(ids.has(s.id)?" cited":""));line.append(seek(s.start),node("span",SessionView.clean(s)));source.append(line);}
    card.append(source);events.append(card);
    if(focusedEventId&&focusedEventId===e.id){card.classList.add("focused");focusedEventId=null;requestAnimationFrame(()=>card.scrollIntoView({block:"center",behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"}));}
  }
  if(!confirmed.length)events.append(node("p",allConfirmed.length?"本课没有这一类事项，请选择其他分类。":detail.events_version?"通读本课字幕后，未提取到明确的课堂事项。":"尚未整理出明确事项。关键词初筛记录可在下方展开查看。","library-empty"));
  const candidates=detail.rule_candidates||detail.events.filter(e=>e.source==="local_rule");
  if(candidates.length){const initial=node("details",undefined,"rule-candidates");initial.append(node("summary","关键词初筛 · "+candidates.length+" 处（不代表有任务）"));for(const e of candidates){const p=node("p");p.append(seek(e.start),document.createTextNode(" "+e.evidence));initial.append(p);}events.append(initial);}
  const transcript=$("view-transcript"),scroll=transcript.scrollTop;transcript.replaceChildren();
  for(const s of detail.segments){const el=node("div",undefined,"segment"),jump=node("button","查看课件","courseware-jump");jump.onclick=()=>{showView("compare");globalThis.Courseware?.selectSegment(s.id);};el.append(seek(s.start),node("span",SessionView.clean(s)),jump);transcript.append(el);}transcript.scrollTop=scroll;
  globalThis.LectureCompare?.mount($("view-compare"),detail,seek,{
    notice:text=>status(text),
    saved:async()=>{detailStamp=0;await refreshDetail();}
  });
  let alignment=$("courseware-alignment");if(!alignment){alignment=node("section",undefined,"courseware-panel");alignment.id="courseware-alignment";$("view-compare").prepend(alignment);}
  globalThis.Courseware?.mount(alignment,detail,seek,{notice:text=>status(text),saved:async()=>{detailStamp=0;await refreshDetail();}});
  if(params.get("segment")){globalThis.Courseware?.selectSegment(params.get("segment"));params.delete("segment");}
  globalThis.LectureMindmap?.mount($("view-mindmap"),detail,{
    topic:index=>{showView("compare");globalThis.LectureCompare?.select(index);}
  });
  renderAction();
}
async function refreshDetail(){if(!selected)return;const id=selected;try{const value=await A.api("/api/sessions/"+id);if(id!==selected)return;if(value.deleted_at){navigate();status("这份字幕已移到回收站，可恢复后查看。");return;}if(value.superseded_by){navigate(value.superseded_by);return;}const stamp=JSON.stringify([value.updated_at,value.versions]);if(stamp!==detailStamp){detail=value;detailStamp=stamp;renderDetail();}}catch(e){if(id!==selected)return;if(!detail)$("detail-meta").textContent="这节课暂时无法读取，连接恢复后会自动重试。";status(e.message,true);}}
async function refresh(){
  if(loading)return;loading=true;
  try{
    const result=await Promise.all([A.api("/api/sessions"),A.api("/api/settings"),A.api("/api/imports"),A.api("/api/sessions?trash=true")]);
    completeSavedEvents(result[0]);
    sessions=result[0];configured=result[1].llm_configured??result[1].deepseek_configured;llmLabel=result[1].llm_label||"AI";imports=result[2];trashed=result[3];
    $("health").textContent="本地已连接";$("health").className="chip";
    clearConnectionNotice();
    const live=sessions.filter(s=>s.mode==="live"&&!s.stopped&&s.status==="recording");
    $("current-class").hidden=!live.length;if(live.length)$("current-class-title").textContent=live.length===1?live[0].course_title+" · 正在后台录音":live.length+" 节课正在后台录音 · "+live.slice(0,3).map(s=>s.course_title).join("、");
    const stamp=JSON.stringify([sessions,imports,trashed]);
    if(stamp!==listStamp){listStamp=stamp;renderLibrary();}
    await refreshDetail();if(detail)renderAction();
    if(isExtension){try{const batch=await A.send("BATCH_STATE");$("stop-batch").hidden=!batch||batch.cancelled||!batch.items.some(i=>i.status==="pending");if(batch)$("batch-status").textContent=(batch.cancelled?"已停止继续导入 · ":"")+batch.items.filter(i=>i.status==="queued").length+"/"+batch.items.length+" 节已导入\n"+batch.items.filter(i=>i.status==="failed").map(i=>i.sub_id+"："+i.error).join("\n");}catch(e){status(e.message,true);}}
  }catch(e){$("health").textContent="服务未连接";$("health").className="chip warn";status(e.message,true,"connection");}finally{loading=false;}
}
$("refresh").onclick=()=>{detailStamp=0;void refresh();};
$("search").oninput=()=>{if(selected)navigate();else renderLibrary();};
$("back-library").onclick=()=>{if(detail)course=detail.course_id;navigate();};
document.querySelectorAll("[data-filter]").forEach(b=>b.onclick=()=>{filter=b.dataset.filter;document.querySelectorAll("[data-filter]").forEach(x=>{x.classList.toggle("active",x===b);x.setAttribute("aria-pressed",String(x===b));});renderLibrary();});
document.querySelectorAll("[data-view]").forEach(b=>b.onclick=()=>showView(b.dataset.view));
document.querySelector(".tabs").addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;const buttons=[...document.querySelectorAll("[data-view]")],i=buttons.indexOf(document.activeElement);if(i<0)return;e.preventDefault();const n=e.key==="Home"?0:e.key==="End"?buttons.length-1:(i+(e.key==="ArrowRight"?1:-1)+buttons.length)%buttons.length;buttons[n].click();buttons[n].focus();});
$("return-classroom").onclick=()=>{if(isExtension)void A.openClassroom(Number($("source-tab").value)||null).catch(e=>status(e.message,true));else location.href="classroom.html";};
$("open-import").onclick=()=>{$("import-dialog").showModal();void refreshSourceTabs().catch(e=>status(e.message,true));};
$("close-import").onclick=()=>$("import-dialog").close();
$("repair").onclick=()=>act($("repair"),async()=>{const result=await A.api("/api/sessions/"+selected+"/repair",{method:"POST"});status("已安排 "+result.repair_count+" 段音频重新转写，旧字幕已备份。");detailStamp=0;await refresh();});
$("export").onclick=()=>act($("export"),()=>A.download(selected));
$("delete-record").onclick=()=>act($("delete-record"),async()=>{await A.api(`/api/sessions/${selected}/delete`,{method:"POST"});course=detail.course_id;navigate();await refresh();status("已将这份字幕及其笔记移到回收站，可以恢复。其他来源保留。");});
async function analyze(restart=false){if(!configured){location.href="options.html#intelligence";return;}await A.api("/api/sessions/"+selected+"/analyze"+(restart?"?restart=true":""),{method:"POST"});detailStamp=0;await refresh();}
$("reanalyze").onclick=()=>act($("reanalyze"),()=>analyze());
$("regenerate").onclick=()=>act($("regenerate"),()=>analyze(true));
for(const name of ["retry","cancel"])$(name).onclick=()=>act($(name),async()=>{await A.api("/api/sessions/"+selected+"/"+name,{method:"POST"});detailStamp=0;await refresh();});
$("attach").onclick=()=>act($("attach"),async()=>{const tabs=await chrome.tabs.query({url:["https://video.jw.scut.edu.cn/livingroom*","https://video-jw-443.webvpn.scut.edu.cn/livingroom*"]});const tab=tabs.find(t=>{const u=new URL(t.url);return u.searchParams.get("course_id")===detail.course_id&&u.searchParams.get("sub_id")===detail.sub_id;});if(!tab)throw new Error("请先在浏览器中打开这节课的播放页");await A.send("ATTACH_REPLAY",{tabId:tab.id,sid:selected});status("字幕已显示，将跟随原视频播放位置。");});
$("load-catalogue").onclick=()=>act($("load-catalogue"),async()=>{
  await refreshSourceTabs();const tabId=Number($("source-tab").value);if(!tabId)throw new Error("请先打开并选择这门课的学校播放页");
  const request=++catalogueRequest;const data=await A.send("INSPECT",{tabId});if(request!==catalogueRequest)return;
  catalogue=data.catalogue;catalogueContext=data.context;catalogueCourse=data.lesson.course_title;$("batch-hint").textContent=catalogueCourse;$("catalogue").replaceChildren();
  for(const item of catalogue){const label=node("label",undefined,"lesson-check"),box=document.createElement("input");box.type="checkbox";box.value=item.sub_id;box.checked=!["1","2","3","5","9"].includes(item.status);box.disabled=!box.checked;label.append(box,node("span",item.title),node("small",box.disabled?"未就绪":"可处理"));$("catalogue").append(label);}
  if(!catalogue.length)throw new Error("未取得课程目录；请检查学校接口或重新打开课程页面");
  $("start-batch").disabled=false;
});
$("source-tab").onchange=()=>{clearCatalogue();$("load-catalogue").disabled=!$("source-tab").value;};
$("start-batch").disabled=true;
$("select-ready").onclick=()=>$("catalogue").querySelectorAll("input:not(:disabled)").forEach(x=>x.checked=true);$("select-none").onclick=()=>$("catalogue").querySelectorAll("input").forEach(x=>x.checked=false);
$("start-batch").onclick=()=>act($("start-batch"),async()=>{if(!catalogueContext||!catalogue.length)throw new Error("请先读取所选课程的课时目录");const subIds=[...$("catalogue").querySelectorAll("input:checked")].map(i=>i.value);await A.send("START_BATCH",{tabId:catalogueContext.tabId,context:catalogueContext,subIds,lessons:catalogue.map(l=>({...l,course_title:catalogueCourse})),analysis:false,forceAsr:$("force-asr").checked});status("导入请求已保存，正在读取学校数据。进度和失败原因会保留在待整理中。");await refresh();});
$("stop-batch").onclick=()=>act($("stop-batch"),async()=>{await A.send("STOP_BATCH");status("已停止继续导入。已入队的本地课时可逐个取消。");});
(async()=>{
  const token=new URLSearchParams(location.hash.slice(1)).get("token");if(token){await A.setConnection(token);history.replaceState(null,"",location.pathname+location.search);}
  if(isExtension){await refreshSourceTabs();const changed=()=>{if($("import-dialog").open)void refreshSourceTabs().catch(e=>status(e.message,true));};chrome.tabs.onRemoved.addListener(changed);chrome.tabs.onUpdated.addListener((_id,change)=>{if(change.url||change.title||change.status==="complete")changed();});chrome.tabs.onCreated.addListener(changed);window.addEventListener("focus",changed);}
  else {$("batch-controls").hidden=true;$("batch-unavailable").hidden=false;}
  await refresh();if(params.get("import")==="1")$("import-dialog").showModal();setInterval(()=>void refresh(),3000);
})();

$("open-courseware").onclick=()=>Courseware.open(detail?.course_title||L.groups(L.lessons(sessions)).find(g=>g.id===course)?.title||"");
if(params.get("view")==="compare")showView("compare");
