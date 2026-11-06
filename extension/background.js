importScripts("core.js","study-core.js","client.js","school-api.js","visual-evidence.js","study-bridge.js");
const A=AssistantClient;
const ready=chrome.storage.local.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"});
chrome.runtime.onInstalled?.addListener(({reason})=>{
  if(reason==="install")void chrome.tabs.create({url:chrome.runtime.getURL("welcome.html")});
});
let creatingOffscreen, batchRunning=false, startingBatch=false;
let launchingService=null;
async function startLocalService(){
  if(launchingService)return launchingService;
  launchingService=(async()=>{
    if(!chrome.runtime.sendNativeMessage)throw new Error("当前扩展尚未启用本地启动通道，请在扩展管理中重新加载一次");
    let result;
    try{result=await chrome.runtime.sendNativeMessage("com.poesis.scut_classroom",{action:"start"});}
    catch{throw new Error("本地启动通道尚未安装。请运行一次项目启动器，再重新加载扩展；之后可直接在浏览器启动。");}
    if(!result?.ok)throw new Error(result?.error||"本地启动器没有返回状态");
    if(typeof result.token!=="string"||!result.token)throw new Error("本地启动器未返回连接信息");
    await A.setConnection(result.token);
    await A.api("/api/health?brief=true",{timeout:10000});
    return {connected:true,version:result.version};
  })();
  try{return await launchingService;}finally{launchingService=null;}
}
const startingCaptures=new Set();
let captureMutation=Promise.resolve();

async function offscreen() {
  if(!chrome.offscreen||!chrome.tabCapture?.getMediaStreamId) throw new Error("请升级到 Edge / Chrome 116 或以上版本");
  if(creatingOffscreen) return creatingOffscreen;
  const contexts=await chrome.runtime.getContexts({contextTypes:["OFFSCREEN_DOCUMENT"],documentUrls:[chrome.runtime.getURL("offscreen.html")]});
  if(contexts.length) return;
  creatingOffscreen=chrome.offscreen.createDocument({url:"offscreen.html",reasons:["USER_MEDIA"],justification:"持续捕获用户选择的课程标签页声音，并送到本机语音引擎"});
  try {await creatingOffscreen;} finally {creatingOffscreen=null;}
}

async function tellRecorder(type,data={}) {
  await offscreen();
  const result=await chrome.runtime.sendMessage({target:"offscreen",type,...data});
  if(!result?.ok) throw new Error(result?.error||"音频后台没有响应");
  return result.data;
}

async function activeCaptures(){
  const saved=await chrome.storage.session.get(["activeCaptures","activeCapture"]);
  const captures=saved.activeCaptures||{};
  if(saved.activeCapture?.tabId!=null){captures[String(saved.activeCapture.tabId)]=saved.activeCapture;await chrome.storage.session.set({activeCaptures:captures});await chrome.storage.session.remove("activeCapture");}
  return captures;
}
async function captureFor({tabId,sid}={}){
  const captures=await activeCaptures(),values=Object.values(captures);
  return (tabId!=null?captures[String(tabId)]:sid?values.find(c=>c.sid===sid):values[0])||null;
}
async function mutateCaptures(change){
  let result;const previous=captureMutation;let release;captureMutation=new Promise(resolve=>{release=resolve;});await previous;
  try{const captures=await activeCaptures();change(captures);await chrome.storage.session.set({activeCaptures:captures});result=captures;}finally{release();}
  return result;
}
async function saveCapture(capture){await mutateCaptures(captures=>{captures[String(capture.tabId)]=capture;});}
async function removeCapture(capture){if(!capture)return;const captures=await mutateCaptures(value=>{delete value[String(capture.tabId)];});await updateBadge(captures);}
async function updateBadge(captures){
  captures=captures||await activeCaptures();
  const count=Object.keys(captures).length;await chrome.action.setBadgeText({text:count?count===1?"REC":String(count):""});
  if(count)await chrome.action.setBadgeBackgroundColor({color:"#dc5538"});
}
async function recorderState(selector={}){
  const capture=await captureFor(selector);
  const contexts=await chrome.runtime.getContexts({contextTypes:["OFFSCREEN_DOCUMENT"],documentUrls:[chrome.runtime.getURL("offscreen.html")]});
  if(!contexts.length){if(capture&&!startingCaptures.has(capture.tabId))await removeCapture(capture);return {recording:false,pending:0,sid:null,tabId:selector.tabId||null};}
  const result=await chrome.runtime.sendMessage({target:"offscreen",type:"STATE",sid:capture?.sid||selector.sid,tabId:capture?.tabId||selector.tabId});
  if(!result?.ok)throw new Error(result?.error||"暂时无法读取录音状态");
  if(capture&&!result.data?.recording&&!result.data?.pending&&!startingCaptures.has(capture.tabId)){await removeCapture(capture);return {...result.data,sid:null};}
  return result.data;
}
async function installOverlay(tabId){await chrome.scripting.executeScript({target:{tabId},files:["session-view.js","overlay.js"]});}

function schoolPage(url){try{return ["https://video.jw.scut.edu.cn","https://video-jw-443.webvpn.scut.edu.cn"].includes(new URL(url).origin);}catch{return false;}}
async function openAssistant(tab){
  // Current Chromium supports opening the action; older builds use the page entry.
  if(chrome.action.openPopup){try{await chrome.action.openPopup({windowId:tab.windowId});return true;}catch{ /* Keep the in-page fallback accessible. */ }}
  const url=chrome.runtime.getURL("study-popup.html?tab="+tab.id);
  const saved=await chrome.storage.session.get("controlWindow");
  if(saved.controlWindow){try{const window=await chrome.windows.get(saved.controlWindow,{populate:true});
    const page=window.tabs?.find(t=>t.url===url);if(page){await chrome.windows.update(window.id,{focused:true,state:"normal"});return true;}
  }catch{ /* Previous control window was closed. */ }}
  const window=await chrome.windows.create({url,type:"popup",width:430,height:790,focused:true});
  await chrome.storage.session.set({controlWindow:window.id});return true;
}

async function startCapture(message) {
  if(startingCaptures.has(message.tabId)||await captureFor({tabId:message.tabId})) throw new Error("这节课已经在录制或等待上传");
  startingCaptures.add(message.tabId);
  let session;
  try {
    const context=await SchoolAPI.context(message.tabId);
    const info=(await SchoolAPI.get(context,"info")).data;
    const metadata=ScutStudy.lesson(info,context);
    const state=(await chrome.scripting.executeScript({target:{tabId:message.tabId},func:()=>{
      const v=document.querySelector("video");return v?{currentTime:v.currentTime,paused:v.paused,rate:v.playbackRate}:null;
    }}))[0]?.result;
    if(!state||state.paused) throw new Error("请先在学校播放器开始播放声音，再开启实时字幕");
    if(metadata.time_basis==="video"&&state.rate!==1) throw new Error("边播边识别请使用 1 倍速；批量回放转写不受播放速度影响");
    await A.api("/api/health?brief=true");
    await offscreen();
    let streamId;
    try{streamId=await chrome.tabCapture.getMediaStreamId({targetTabId:message.tabId});}
    catch(error){
      if(/invoked|activeTab|permission/i.test(error.message||""))throw new Error("浏览器需要一次手动打开：请点击工具栏中的 SCUT 课堂助手图标，再点击“开始实时字幕”。自动打开面板不会代替录音授权。");
      throw error;
    }
    session=await A.api("/api/sessions",{method:"POST",body:{...metadata,mode:"live",analysis:!!message.analysis}});
    await installOverlay(message.tabId);
    const capture={sid:session.id,tabId:message.tabId,url:context.url,time_basis:metadata.time_basis};
    await saveCapture(capture);
    await tellRecorder("START",{streamId,session,capture,offset:metadata.time_basis==="video"?state.currentTime:0,
      connection:await A.connection()});
    await updateBadge();
    return session;
  } catch(error) {
    if(session) await A.api(`/api/sessions/${session.id}/cancel`,{method:"POST"}).catch(()=>{});
    await removeCapture(await captureFor({tabId:message.tabId}));
    throw error;
  } finally {startingCaptures.delete(message.tabId);}
}

async function processLesson(context, subId, analysis, forceAsr, requestId, onMetadata) {
  const info=(await SchoolAPI.get(context,"info",subId)).data||{};
  if(!ScutStudy.replayEligible({status:info.sub_status})) throw new Error("课时仍在直播、未开始或回放尚未就绪");
  const pageUrl=new URL(context.url);pageUrl.searchParams.set("sub_id",subId);
  const metadata=ScutStudy.lesson(info,{...context,subId,url:pageUrl.href});
  await onMetadata(metadata);
  let items=[];
  if(!forceAsr) items=ScutSubtitleCore.extractSubtitleItems(await SchoolAPI.get(context,"subtitle",subId)).filter(s=>typeof s.Text==="string"&&Number.isFinite(Number(s.BeginSec)));
  const sources=ScutStudy.mediaSources(info,context.origin).filter(s=>s.kind==="replay");
  if(!items.length&&!sources.length) throw new Error("此课时尚无字幕和可用回放源，可在播放页尝试实时识别");
  const session=await A.api("/api/sessions",{method:"POST",body:{...metadata,request_id:requestId,mode:items.length?"subtitle":"replay",analysis,force_new:forceAsr,
    ...(items.length?{}:{source_url:sources[0].url})}});
  if(items.length&&!session.stopped) {
    try {await A.api(`/api/sessions/${session.id}/subtitles`,{method:"POST",body:{items:items.map(s=>({BeginSec:Number(s.BeginSec),EndSec:s.EndSec==null?Number(s.BeginSec)+5:Number(s.EndSec),Text:s.Text}))}});}
    catch(error){await A.api(`/api/sessions/${session.id}/cancel`,{method:"POST"}).catch(()=>{});throw error;}
  }
  if(session.reused&&analysis&&session.stopped&&session.analysis_status==="idle")await A.api(`/api/sessions/${session.id}/analyze`,{method:"POST"});
  return session;
}

function importRecord(batch,item){
  const url=new URL(batch.context.url);url.searchParams.set("sub_id",item.sub_id);
  const lesson=item.lesson||{};
  return {request_id:`${batch.id}-${item.sub_id}`,course_id:batch.context.courseId,sub_id:item.sub_id,
    course_title:lesson.course_title||"课程 "+batch.context.courseId,title:lesson.title||"课时 "+item.sub_id,
    page_url:url.href,start_at:lesson.start_at||0,status:item.status,error:(item.error||"").slice(0,1000),sid:item.sid||null};
}
async function syncBatch(batch){
  if(!batch?.items.length)return;
  await A.api("/api/imports",{method:"POST",body:{items:batch.items.map(item=>importRecord(batch,item))}});
}
async function runBatch() {
  if(batchRunning) return;
  batchRunning=true;
  try {
    for(;;) {
      const {batch}=await chrome.storage.local.get("batch");
      await syncBatch(batch); // Also flush terminal results after a temporary local-service outage.
      if(!batch||batch.cancelled) break;
      const item=batch.items.find(i=>i.status==="pending");
      if(!item) break;
      try {
        const session=await processLesson(batch.context,item.sub_id,batch.analysis,batch.forceAsr,`${batch.id}-${item.sub_id}`,async metadata=>{item.lesson=metadata;await syncBatch(batch);});
        item.status="queued";item.sid=session.id;
      } catch(e){item.status="failed";item.error=e.message;}
      // Re-read cancellation before committing so a stop click is never overwritten.
      const latest=(await chrome.storage.local.get("batch")).batch;
      if(latest?.id!==batch.id) break;
      batch.cancelled=!!latest.cancelled;
      if(batch.cancelled)for(const entry of batch.items)if(entry.status==="pending")entry.status="cancelled";
      await chrome.storage.local.set({batch});
      await syncBatch(batch);
    }
  } catch { /* The persisted batch is retried by the alarm when the local service returns. */ }
  finally {batchRunning=false;}
}

async function broadcastUpdate(message) {
  const capture=await captureFor({sid:message.session?.id});
  const session=message.session;
  if(capture&&capture.sid===session.id) {
    await chrome.tabs.sendMessage(capture.tabId,{type:"SCUT_UPDATE",session,recorder:message.recorder}).catch(()=>{});
    const {desktopAlerts}=await A.connection();
    if(desktopAlerts&&chrome.notifications&&await chrome.permissions.contains({permissions:["notifications"]})) {
      const {notified={}}=await chrome.storage.session.get("notified");
      for(const e of session.events.slice(-20)) {
        const eventKey=`${session.id}:${e.id}`;
        if(notified[eventKey]||e.priority!=="urgent") continue;
        notified[eventKey]=true;
        await chrome.notifications.create(eventKey,{type:"basic",iconUrl:"icons/assistant-128.png",title:`${e.label} · ${session.course_title}`,message:e.evidence.slice(0,150),priority:2});
      }
      await chrome.storage.session.set({notified});
    }
  }
}

chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(message.target==="offscreen") return false;
  if(sender.id!==chrome.runtime.id) return false;
  const trusted=sender.url?.startsWith(chrome.runtime.getURL(""));
  const localPage=/^http:\/\/(127\.0\.0\.1|localhost):8765\//.test(sender.url||"");
  const contentAllowed=["OVERLAY_READY","PLAYER_CHANGED","SET_OVERLAY_PREFS","OPEN_CLASSROOM","PAGE_ASSISTANT_READY","SET_SITE_ENTRY_PREF","OPEN_ASSISTANT","OPEN_NOTEBOOK"];
  if(!trusted&&!(localPage&&message.type==="LOCAL_APP_READY")&&(!schoolPage(sender.url)||!contentAllowed.includes(message.type))) return false;
  (async()=>{
    await ready;
    switch(message.type) {
      case "LOCAL_APP_READY": {
        if(!localPage||!sender.tab?.id)return false;
        // Read via the same-origin local page; never accept a token posted by a school page.
        const values=await chrome.scripting.executeScript({target:{tabId:sender.tab.id},world:"MAIN",func:async()=>{
          if(!/^http:\/\/(127\.0\.0\.1|localhost):8765$/.test(location.origin))return null;
          const r=await fetch("/api/pairing-token");return r.ok?(await r.json()).token:null;
        }});
        const token=values?.[0]?.result;
        if(typeof token!=="string"||token.length<20)return false;
        await A.fetchWithToken(token,"/api/health?brief=true",{timeout:5000});
        await A.setConnection(token);void pollStudyJobs();return true;
      }
      case "PAGE_ASSISTANT_READY": {
        const {autoOpenAssistant=true}=await chrome.storage.local.get("autoOpenAssistant");
        const capture=await captureFor({tabId:sender.tab?.id});
        let connected=false,course="";
        try{const sessions=await A.api("/api/sessions");connected=true;const id=new URL(sender.tab.url).searchParams.get("course_id");course=sessions.find(s=>s.course_id===id)?.course_title||"";}catch{ /* Entry remains usable before pairing. */ }
        // Automatic entry stays inside the page. Never reopen a browser action window
        // on navigation, hash changes, or a late content-script response.
        const tab=await chrome.tabs.get(sender.tab.id);
        const window=await chrome.windows.get(tab.windowId);
        return {autoOpen:autoOpenAssistant,expandOnLoad:autoOpenAssistant&&tab.active&&window.focused,
                popupOpened:false,connected,course,recording:!!capture};
      }
      case "SET_SITE_ENTRY_PREF":
        if(typeof message.enabled!=="boolean")throw new Error("无效显示偏好");
        await chrome.storage.local.set({autoOpenAssistant:message.enabled});return true;
      case "OPEN_ASSISTANT": return openAssistant(await chrome.tabs.get(sender.tab.id));
      case "OPEN_NOTEBOOK": {
        const params=new URLSearchParams({tab:String(sender.tab.id)}),id=new URL(sender.tab.url).searchParams.get("course_id");
        if(/^\d+$/.test(id||""))params.set("course",id);
        await chrome.tabs.create({url:chrome.runtime.getURL("dashboard.html?"+params)});return true;
      }
      case "INSPECT": {
        const result=await SchoolAPI.inspect(message.tabId,{includeCatalogue:message.includeCatalogue!==false});
        const key=`media-${message.tabId}`;const observed=(await chrome.storage.session.get(key))[key]||[];
        for(const url of observed)if(!result.sources.some(s=>s.url===url))result.sources.push({url,label:"播放器实际请求",kind:result.status==="1"?"live":"replay",format:new URL(url).pathname.split(".").pop()});
        return result;
      }
      case "START_LOCAL_SERVICE": return startLocalService();
      case "COLLECT_VISUAL_EVIDENCE": {
        let tab;try{tab=await chrome.tabs.get(message.tabId);}catch{throw new Error("课程页面已关闭，请重新选择");}
        const context=await SchoolAPI.context(tab.id);
        if(!context.subId)throw new Error("请选择具体课时");
        const result=(await chrome.scripting.executeScript({target:{tabId:tab.id},func:collectScutVisuals,args:[tab.url]}))[0]?.result;
        if(!result||result.error)throw new Error(result?.error||"无法读取截图索引");
        return result;
      }
      case "START_CAPTURE": return startCapture(message);
      case "STOP_CAPTURE": {
        const capture=await captureFor({tabId:message.tabId,sid:message.sid});
        if(!capture)throw new Error("这节课当前没有录音");
        return tellRecorder("STOP",{sid:capture.sid,tabId:capture.tabId,warning:message.warning||""});
      }
      case "RECORDER_STATE": return recorderState({tabId:message.tabId,sid:message.sid});
      case "RECOVER_UPLOADS": return tellRecorder("RECOVER",{connection:await A.connection()});
      case "RECORDER_UPDATE": await broadcastUpdate(message); return true;
      case "RECORDER_FINISHED":
        {const capture=await captureFor({sid:message.session?.id});if(capture&&message.session)await chrome.tabs.sendMessage(capture.tabId,{type:"SCUT_UPDATE",session:message.session,finished:true}).catch(()=>{});await removeCapture(capture);}
        return true;
      case "GET_CONNECTION": return A.connection();
      case "PLAYER_CLOCK": {
        await SchoolAPI.context(message.tabId);
        return (await chrome.scripting.executeScript({target:{tabId:message.tabId},func:()=>{
          const v=document.querySelector("video");return v?{time:v.currentTime,paused:v.paused,rate:v.playbackRate}:null;
        }}))[0]?.result;
      }
      case "OVERLAY_READY": {
        const capture=await captureFor({tabId:sender.tab?.id});
        const {overlayPrefs:prefs={mode:"compact",fontSize:22}}=await chrome.storage.local.get("overlayPrefs");
        return {prefs,...(capture?.tabId===sender.tab?.id?{capture}:{})};
      }
      case "SET_OVERLAY_PREFS": {
        if(!["compact","context","minimized"].includes(message.mode)||!Number.isFinite(message.fontSize)||message.fontSize<16||message.fontSize>36)throw new Error("无效字幕显示设置");
        await chrome.storage.local.set({overlayPrefs:{mode:message.mode,fontSize:message.fontSize}});return true;
      }
      case "OPEN_CLASSROOM": await A.openClassroom(sender.tab?.id||message.tabId);return true;
      case "PLAYER_CHANGED": {
        const capture=await captureFor({tabId:sender.tab?.id});
        if(capture?.tabId===sender.tab?.id&&capture.time_basis==="video")
          await tellRecorder("STOP",{sid:capture.sid,tabId:capture.tabId,warning:"回放暂停、跳转或变速，录制已结束以保持字幕时间轴准确；继续播放后可重新开启"});
        return true;
      }
      case "START_BATCH": {
        if(startingBatch||batchRunning)throw new Error("正在导入课程，请稍候再试");
        startingBatch=true;
        try{
        const previous=(await chrome.storage.local.get("batch")).batch;
        if(previous&&!previous.cancelled&&previous.items.some(i=>i.status==="pending")) throw new Error("上一批课程仍在导入，请等待或停止后再试");
        const context=message.context?await SchoolAPI.resolveContext(message.context):await SchoolAPI.context(message.tabId);
        const ids=[...new Set(message.subIds||[])].filter(x=>/^\d+$/.test(x)).slice(0,200);
        if(!ids.length) throw new Error("请选择要处理的课时");
        await syncBatch(previous);
        const batch={id:crypto.randomUUID(),context,analysis:!!message.analysis,forceAsr:!!message.forceAsr,items:ids.map(sub_id=>({sub_id,status:"pending",lesson:message.lessons?.find(l=>l.sub_id===sub_id)})),cancelled:false};
        await syncBatch(batch);
        await chrome.storage.local.set({batch});
        await chrome.alarms.create("batch",{periodInMinutes:0.5});
        void runBatch();return batch;
        }finally{startingBatch=false;}
      }
      case "BATCH_STATE": return (await chrome.storage.local.get("batch")).batch||null;
      case "STOP_BATCH": {
        const {batch}=await chrome.storage.local.get("batch");
        if(batch){batch.cancelled=true;for(const item of batch.items)if(item.status==="pending")item.status="cancelled";await chrome.storage.local.set({batch});await syncBatch(batch);}return true;
      }
      case "ATTACH_REPLAY": {
        const session=await A.api(`/api/sessions/${message.sid}`);
        if(session.time_basis==="capture") throw new Error("这份直播字幕从开始捕获时计时，无法直接与后上传的完整回放对齐");
        const context=await SchoolAPI.context(message.tabId);
        if(context.subId!==session.sub_id||context.courseId!==session.course_id) throw new Error("请打开此分析对应的课时再显示字幕");
        await installOverlay(message.tabId);
        await chrome.tabs.sendMessage(message.tabId,{type:"SCUT_REPLAY",session});return true;
      }
      default: throw new Error("未知扩展操作");
    }
  })().then(data=>respond({ok:true,data}),error=>respond({ok:false,error:error.message}));
  return true;
});

chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==="batch") void runBatch();});
chrome.runtime.onStartup.addListener(()=>void runBatch());
const pollStudyJobs=()=>ScutAgentBridge.poll({client:A,processLesson,busy:()=>batchRunning||startingBatch}).catch(()=>{});
chrome.alarms.create("study-agent-jobs",{periodInMinutes:0.5});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==="study-agent-jobs")void pollStudyJobs();});
chrome.runtime.onStartup.addListener(()=>void pollStudyJobs());
chrome.tabs.onRemoved.addListener(async tabId=>{
  const capture=await captureFor({tabId});
  if(capture) await tellRecorder("STOP",{sid:capture.sid,tabId,warning:"课程标签页已关闭；正在保存剩余音频"}).catch(()=>{});
});
chrome.tabs.onUpdated.addListener(async(tabId,change)=>{
  if(change.status==="complete")void chrome.tabs.get(tabId).then(t=>{if(schoolPage(t.url))void pollStudyJobs();}).catch(()=>{});
  if(!change.url) return;
  const capture=await captureFor({tabId});
  if(capture&&change.url!==capture.url) await tellRecorder("STOP",{sid:capture.sid,tabId,warning:"课程页面已切换，已结束上一课时录制"}).catch(()=>{});
  await chrome.storage.session.remove(`media-${tabId}`);
});
// Observe only media URL metadata on the two authorized school hosts, never headers or cookies.
chrome.webRequest.onBeforeRequest.addListener(async details=>{
  if(details.tabId<0||! /\.(m3u8|mp4|flv)(?:\?|$)/i.test(details.url)) return;
  const key=`media-${details.tabId}`;const data=await chrome.storage.session.get(key);
  const urls=data[key]||[];if(!urls.includes(details.url))await chrome.storage.session.set({[key]:[...urls,details.url].slice(-12)});
},{urls:["https://video.jw.scut.edu.cn/*","https://video-jw-443.webvpn.scut.edu.cn/*"]});
