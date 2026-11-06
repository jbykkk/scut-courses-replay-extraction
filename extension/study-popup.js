(() => {
const A=AssistantClient,V=SessionView,$=id=>document.getElementById(id);
let tab,inspection,connected=false,reading=false,editing=false,configured=false,activeSession=null,importSession=null,preferences={analysisDefault:true},acting=new Set();
let lastConnection=0,lastInspection=0,inspecting=false,inspectionError="",recorderStatus={unknown:true},connectionFailures=0,launching=false,autoLaunchTried=false,importPending=false;
function keyState(cfg){const openai=cfg.llm_provider==="openai";configured=cfg.llm_configured!==undefined?cfg.llm_configured:cfg.deepseek_configured;$("key-indicator").textContent=configured?("已识别本机 "+(cfg.llm_label||"AI")+" Key"):(openai?cfg.openai_key_status:cfg.deepseek_key_status)==="unreadable"?"Key 无法解密":"尚未配置 Key";$("key-indicator").parentElement.dataset.ready=String(configured);$("test-key").disabled=!configured;$("key-check-result").hidden=!cfg.deepseek_check;if(cfg.deepseek_check){$("key-check-result").hidden=false;$("key-check-result").textContent=cfg.deepseek_check.message+" · "+new Date(cfg.deepseek_check.checked_at*1000).toLocaleTimeString();}}
async function refreshConnection(){
  try{const cfg=await A.api("/api/settings",{timeout:6000});connected=true;connectionFailures=0;keyState(cfg);$("connection").textContent="本地已连接";$("connection").className="chip";$("launch-panel").hidden=true;}
  catch(e){connectionFailures++;if(e.kind==="timeout"&&connected&&connectionFailures<2){$("connection").textContent="正在复核连接";return;}connected=false;configured=false;const pairing=e.status===401||e.message.includes("口令");$("connection").textContent=launching?"正在启动":pairing?"需要配对":"待连接";$("connection").className="chip warn";$("key-indicator").textContent=pairing?"请连接本机启动器以恢复扩展配对":"服务连接后读取 Key";$("key-check-result").hidden=true;$("key-indicator").parentElement.dataset.ready="false";$("test-key").disabled=true;$("launch-panel").hidden=false;}
}
async function inspectTab(){
  if(inspecting||!tab?.id)return;inspecting=true;lastInspection=Date.now();const id=tab.id;
  try{const value=await A.send("INSPECT",{tabId:id,includeCatalogue:false});if(tab?.id!==id)return;inspection=value;inspectionError="";paintScene(recorderStatus);}
  catch(e){if(tab?.id!==id)return;inspectionError=e.message;if(!inspection){$("course").textContent="正在读取课时";$("source").textContent="自动重试中";$("lesson").textContent=e.message;}}
  finally{inspecting=false;}
}
function show(text,error=false){$("message").textContent=text;$("message").className="notice"+(error?" error":"");}
function paintScene(state={}){
  const view=PopupState.scene(inspection,state);document.body.dataset.scene=view.name;
  document.body.classList.toggle("is-recording",view.recording);
  $("scene-label").textContent=view.title;$("start-label").textContent=view.startLabel;
  $("action-hint").textContent=view.hint;$("action-hint").hidden=view.recording;
  const available=PopupState.availability(inspection,state,{connected});
  $("start").disabled=acting.has("start")||!available.canStart;
  if(available.reason&&!view.recording){$("action-hint").textContent=inspectionError&&!inspection?inspectionError+"；会自动重试。":available.reason;$("action-hint").hidden=false;}
  $("start").classList.toggle("primary",!view.replay);$("process").classList.toggle("primary",view.replay);
  $("process").disabled=!connected||!view.importable||acting.has("process")||importPending;
  $("lifecycle-note").hidden=!view.recording;
  $("recorder").parentElement.hidden=!view.recording&&!state.pending&&!state.error;
  $("start").hidden=view.recording;$("stop").hidden=!view.recording;
  $("expand-room").hidden=!view.recording;
  if(inspection&&!view.recording){$("course").textContent=inspection.lesson.course_title;$("lesson").textContent=inspection.lesson.title;$("source").textContent=view.replay?"回放已就绪":String(inspection.status)==="1"?"直播中":"等待回放";}
}
if(!globalThis.chrome?.runtime?.id||new URLSearchParams(location.search).get("preview")==="1"){
  if(new URLSearchParams(location.search).get("frame")!=="popup"){document.documentElement.classList.add("popup-preview");document.body.classList.add("demo");}document.title="扩展外观预览 · SCUT 课堂助手";
  const sample=new URLSearchParams(location.search).get("scene")||"replay";
  inspection={status:sample==="replay"?"6":"1",lesson:{course_title:"神经科学",title:"2026-09-02 · 第 5–6 节"}};connected=true;
  paintScene({recording:sample==="recording"});$("connection").textContent="外观预览";
  keyState({deepseek_configured:true});$("test-key").disabled=true;$("key-settings").onclick=()=>location.href="options.html#intelligence";
  if(sample==="recording"){$("course").textContent=inspection.lesson.course_title;$("lesson").textContent=inspection.lesson.title;$("source").textContent="正在录音";$("live-context").hidden=false;V.paint($("live-sentences"),[{id:"demo1",start:4,end:9,text:"这个视频介绍了不同脑区的功能。"},{id:"demo2",start:9,end:14,text:"看完以后，把记住的名词和解释写下来。"}],true);}
  for(const id of ["start","stop","process","recover","analysis","legacy"])$(id).disabled=true;
  $("analysis").checked=true;$("dashboard").onclick=()=>location.href="dashboard.html";$("options").onclick=()=>location.href="options.html";
  $("expand-room").onclick=()=>location.href="classroom.html";$("recorder").textContent="示例字幕 · 未启动录音";return;
}
async function action(id,fn){acting.add(id);$(id).disabled=true;try{await fn();}catch(e){show(e.message,true);}finally{acting.delete(id);$(id).disabled=false;await recorder();}}
async function recorder(){
  if(reading)return;reading=true;
  try{if(Date.now()-lastConnection>5000){lastConnection=Date.now();await refreshConnection();if(tab?.id){const current=await chrome.tabs.get(tab.id);if(inspection?.context?.url&&current.url!==inspection.context.url){inspection=null;inspectionError="";importPending=false;}tab=current;}}if(!inspection&&Date.now()-lastInspection>5000)void inspectTab();const id=tab?.id;let state;try{state=await A.send("RECORDER_STATE",{tabId:id});}catch(e){state={...recorderStatus,unknown:true,error:e.message};}if(tab?.id!==id)return;recorderStatus=state||{};const running=!!state?.recording;
    paintScene(state||{});
    $("expand-room").textContent=running?"返回课堂实况 ↗":"查看上次课堂 ↗";
    $("expand-room").classList.toggle("primary",running);
    $("lifecycle-note").textContent="关掉面板后，录音继续。再次点击图标即可回来。";
    $("start").hidden=running;$("stop").hidden=!running;
    $("expand-room").hidden=!running;
    if(state?.sid){activeSession=await A.api("/api/sessions/"+state.sid);if(!editing)$("analysis").checked=V.analysisEnabled(preferences,running?activeSession:null);
      $("live-context").hidden=!running||!activeSession.segments.length;V.paint($("live-sentences"),activeSession.segments.slice(-4),true);
      if(running){$("course").textContent=activeSession.course_title;$("lesson").textContent=activeSession.title;$("source").textContent="正在录音";}
    }else {activeSession=null;$("live-context").hidden=true;if(!editing)$("analysis").checked=V.analysisEnabled(preferences,null);}
    $("recorder").textContent=state?.error||(running?"正在听课 · 待上传 "+(state.pending||0)+" 段":state?.pending?"正在补传 "+state.pending+" 段":"当前没有录音 · 历史字幕在课程笔记中");
    if(!acting.has("stop"))$("stop").disabled=!running;
    $("analysis").disabled=!configured||editing;if(!configured)$("analysis").checked=false;
    if(connected&&inspection){
      const entries=await A.api("/api/imports");
      const entry=entries.filter(i=>i.course_id===inspection.lesson.course_id&&i.sub_id===inspection.lesson.sub_id).sort((a,b)=>b.created_at-a.created_at)[0];
      importPending=entry?.status==="pending";
      $("import-progress").hidden=!entry;
      if(entry){importSession=entry.sid;
        $("import-state").textContent={pending:"正在读取学校数据",failed:"本节课导入未完成",queued:"本节课已交给本地处理",cancelled:"已停止导入"}[entry.status];
        $("import-description").textContent=entry.error||(entry.status==="pending"?"请求已保存。请暂时保持学校课时标签页打开。":entry.status==="queued"?"进度、字幕和笔记都在课程笔记中。没有学校字幕时，会自动下载音轨并转写。":"可再次点击整理本节回放。");
        if(entry.status==="pending")$("process").disabled=true;
      }
    }
  }catch(e){$("recorder").textContent=e.message;}finally{paintScene(recorderStatus);reading=false;}
}
async function launchService(){
  if(launching)return;launching=true;$("launch-service").setAttribute("aria-busy","true");$("connection").textContent="正在启动";show("正在启动并连接本地服务，请等待结果，无需重复点击。");
  try{await A.startLocalService();lastConnection=0;await refreshConnection();show("本地服务已连接，扩展配对已自动恢复。");}
  catch(e){show(e.message,true);$("launch-compat").hidden=false;}
  finally{launching=false;$("launch-service").removeAttribute("aria-busy");await recorder();}
}
$("options").onclick=()=>chrome.runtime.openOptionsPage();
$("key-settings").onclick=()=>chrome.tabs.create({url:chrome.runtime.getURL("options.html#intelligence")});
$("test-key").onclick=()=>action("test-key",async()=>{$("key-check-result").hidden=false;$("key-check-result").textContent="正在验证 Key 与模型…";const result=await A.api("/api/deepseek/check",{method:"POST",timeout:15000});$("key-check-result").textContent=result.message;lastConnection=0;});
$("launch-service").onclick=e=>{e.preventDefault();void launchService();};
$("course-tab").onchange=async()=>{tab=$("course-tab").value?await chrome.tabs.get(Number($("course-tab").value)):null;inspection=null;inspectionError="";activeSession=null;importPending=false;recorderStatus={unknown:true};$("course").textContent="正在读取课时…";$("lesson").textContent="";paintScene(recorderStatus);await inspectTab();await recorder();};
$("legacy").onclick=()=>location.href="popup.html";
$("dashboard").onclick=()=>A.openDashboard(null,tab?.id);
$("session-detail").onclick=()=>A.openDashboard(activeSession?.id,tab?.id);
$("import-detail").onclick=()=>A.openDashboard(importSession,tab?.id);
$("expand-room").onclick=()=>action("expand-room",()=>A.openClassroom(tab?.id));
$("analysis").onchange=async()=>{editing=true;const wanted=$("analysis").checked;
  try{if(activeSession&&!activeSession.stopped)await A.api("/api/sessions/"+activeSession.id+"/analysis-preference",{method:"POST",body:{enabled:wanted}});
    preferences.analysisDefault=wanted;await chrome.storage.local.set({analysisDefault:wanted});
    show(wanted?"实时提醒已开启。字幕保存后会自动生成笔记。":"实时提醒已关闭，保存后仍会自动生成笔记。");
  }catch(e){$("analysis").checked=!wanted;show(e.message,true);}finally{editing=false;await recorder();}
};
$("start").onclick=()=>action("start",async()=>{await A.send("START_CAPTURE",{tabId:tab.id,analysis:configured&&$("analysis").checked});show("已开始录音。点“返回正在录音的课堂”查看连续字幕。");});
$("stop").onclick=()=>action("stop",async()=>{const s=await A.send("STOP_CAPTURE",{tabId:tab.id});show(s.pending?"正在补传 "+s.pending+" 段音频，请保持本地服务运行。":"录音已结束，剩余字幕和总结会继续保存。");});
$("recover").onclick=()=>action("recover",async()=>{const s=await A.send("RECOVER_UPLOADS");show(s.error||"待上传音频："+s.pending+" 段",!!s.error);});
$("auto-open").onchange=async e=>{await chrome.storage.local.set({autoOpenAssistant:e.target.checked});};
$("process").onclick=()=>action("process",async()=>{if(!inspection)throw new Error("未找到课时信息");await A.send("START_BATCH",{tabId:tab.id,subIds:[inspection.lesson.sub_id],lessons:[inspection.lesson],analysis:false,forceAsr:false});show("导入请求已保存，正在读取学校数据。");});
(async()=>{preferences=await chrome.storage.local.get({analysisDefault:true,autoOpenAssistant:true});$("analysis").checked=V.analysisEnabled(preferences,null);$("auto-open").checked=preferences.autoOpenAssistant;
  const sourceTab=Number(new URLSearchParams(location.search).get("tab"));
  if(sourceTab>0)tab=await chrome.tabs.get(sourceTab);else [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  const courses=await chrome.tabs.query({url:["https://video.jw.scut.edu.cn/livingroom*","https://video-jw-443.webvpn.scut.edu.cn/livingroom*"]});
  if(courses.length){$("course-picker").hidden=false;$("course-tab").replaceChildren(new Option("选择正在学习的课时",""),...courses.map(t=>new Option(t.title,t.id)));if(courses.some(t=>t.id===tab?.id))$("course-tab").value=String(tab.id);else if(courses.length===1){tab=courses[0];$("course-tab").value=String(tab.id);}}
  await Promise.all([inspectTab(),refreshConnection()]);lastConnection=Date.now();
  $("process").disabled=!connected||!inspection||["1","2","3","5","9"].includes(String(inspection.status));
  await recorder();setInterval(()=>void recorder(),1500);if(!connected&&!autoLaunchTried){autoLaunchTried=true;void launchService();}
})();
})();
