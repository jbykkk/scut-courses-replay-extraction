(() => {
const A=AssistantClient,V=SessionView,$=id=>document.getElementById(id),extension=!!globalThis.chrome?.runtime?.id;
let sid=null,session=null,following=true,polling=false,editing=false,stamp=0,eventStamp='',configured=false,llmModelField='deepseek_model',llmLabel='AI 分析';
const sourceTab=Number(new URLSearchParams(location.search).get('tab'))||null;
function message(text,error=false){$('room-message').textContent=text;$('room-message').className='notice'+(error?' error':'');}
const MODEL_LABELS={'deepseek-flash':'V4.1 Flash','deepseek-v4-pro':'V4 Pro','gpt-5-mini':'GPT-5 mini','gpt-5':'GPT-5','gpt-4o-mini':'GPT-4o mini','gpt-4o':'GPT-4o'};
function modelOption(value){for(const option of [...$('room-model').options])if(option.value==='deepseek-v4-flash')option.remove();if(![...$('room-model').options].some(o=>o.value===value))$('room-model').add(new Option(MODEL_LABELS[value]||value,value));$('room-model').value=value;}
async function preferences(){const cfg=await A.api('/api/settings');configured=cfg.llm_configured!==undefined?cfg.llm_configured:cfg.deepseek_configured;llmLabel=cfg.llm_label||'AI 分析';llmModelField=cfg.llm_provider==='openai'?'openai_model':'deepseek_model';$('room-model').replaceChildren(...(cfg.llm_provider==='openai'?['gpt-5-mini','gpt-5','gpt-4o-mini','gpt-4o']:['deepseek-flash','deepseek-v4-pro']).map(v=>new Option(MODEL_LABELS[v]||v,v)));modelOption(cfg.llm_model||cfg.deepseek_model);$('room-language').textContent=cfg.language==='auto'?'自动识别 · 中英切换':({zh:'中文',en:'英语',yue:'粤语'}[cfg.language]||cfg.language);if(!editing&&!session){const pref=extension?await chrome.storage.local.get({analysisDefault:true}):{analysisDefault:true};$('room-analysis').checked=pref.analysisDefault;}$('room-analysis').disabled=!configured;$('room-analysis-status').textContent=configured?'仅控制即时提醒；保存后自动生成笔记':'先在智能分析设置中配置 API Key';}
function render(value,state){
  session=value;const active=!!state?.recording||(!extension&&!value.stopped&&value.status==='recording');
  $('room-course').textContent=value.course_title;$('room-lesson').textContent=value.title;
  $('room-details').href=A.dashboardURL(value.id,sourceTab);
  $('room-lifecycle').textContent=active?'关窗或最小化，录音继续。再点浏览器中的助手即可回来。':'字幕已保留。点“本课详情与总结”查看，或到课程笔记找历史记录。';
  $('room-status').textContent=active?'正在听课':value.stopped?'已结束 · 已保存':'等待音频';$('room-status').className='chip'+(active?' room-status-live':'');
  $('room-count').textContent=value.segments.length+' 条字幕';$('room-clock').textContent=V.time(value.segments.at(-1)?.end||0);
  if(!editing)$('room-analysis').checked=!!value.analysis;
  $('room-analysis-status').textContent=!configured?'尚未配置 Key':value.analysis?'本节课已开启 · '+({running:'正在分析',queued:'等待分析',failed:'分析需重试',complete:'分析已更新'}[value.analysis_status]||'等待新的课堂文字'):'实时提醒已关闭 · 保存后自动生成笔记';
  $('room-stop').disabled=!extension||!state?.recording;$('room-export').disabled=false;
  $('room-save').textContent=state?.pending?`暂存 ${state.pending} 段，正在补传`:value.repair_status==='running'?'正在用当前语言修复旧字幕':active?(extension?'最小化后，录音与分析继续':'本地实况 · 录音由浏览器扩展继续'):'本课字幕与笔记已保存在本机';
  if(value.updated_at!==stamp){stamp=value.updated_at;$('room-empty')?.remove();V.paint($('room-transcript'),value.segments,following);}
  const eventKey=JSON.stringify(value.events);
  if(eventStamp!==eventKey){eventStamp=eventKey;$('room-event-count').textContent=value.events.length;$('room-events').replaceChildren();
    for(const e of [...value.events].reverse()){const el=document.createElement('article');el.className='event';const title=document.createElement('strong'),text=document.createElement('p'),quote=document.createElement('p'),time=document.createElement('small');title.textContent=e.label;text.textContent=e.message;quote.textContent=e.evidence;time.textContent=V.time(e.start)+' · '+(e.source==='deepseek'?llmLabel:'本地初筛');el.append(title,text,quote,time);$('room-events').append(el);}
    if(!value.events.length){const empty=document.createElement('div');empty.className='room-empty';empty.textContent='需要留意的点名、作业和课堂要求，会保存在这里。';$('room-events').append(empty);}
  }
}
async function refresh(){if(polling)return;polling=true;try{
  const state=extension?await A.send('RECORDER_STATE',{sid,tabId:sourceTab}).catch(()=>null):null;
  if(state?.sid&&state.sid!==sid){sid=state.sid;stamp=0;following=true;}
  if(!sid){const all=await A.api('/api/sessions');sid=(all.find(s=>s.mode==='live'&&!s.stopped)||all[0])?.id;}
  if(sid)render(await A.api('/api/sessions/'+sid),state);
}catch(e){message(e.message,true);}finally{polling=false;}}
$('room-transcript').addEventListener('scroll',()=>{const box=$('room-transcript');following=box.scrollHeight-box.scrollTop-box.clientHeight<65;$('follow').classList.toggle('active',following);$('follow').textContent=following?'跟随最新 ↓':'回到最新 ↓';});
$('follow').onclick=()=>{following=true;$('room-transcript').scrollTop=$('room-transcript').scrollHeight;$('follow').classList.add('active');$('follow').textContent='跟随最新 ↓';};
document.querySelectorAll('[data-room-view]').forEach(button=>button.onclick=()=>{const view=button.dataset.roomView;document.querySelectorAll('[data-room-view]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-selected',String(b===button));});$('room-transcript').hidden=view!=='transcript';$('room-events').hidden=view!=='events';$('follow').hidden=view!=='transcript';if(view==='transcript'&&following)$('follow').click();});
$('room-analysis').onchange=async()=>{editing=true;const wanted=$('room-analysis').checked;try{if(sid)await A.api('/api/sessions/'+sid+'/analysis-preference',{method:'POST',body:{enabled:wanted}});if(extension)await chrome.storage.local.set({analysisDefault:wanted});AssistantUI.toast(wanted?'实时提醒已开启':'实时提醒已关闭，课后笔记仍会自动生成');}catch(e){$('room-analysis').checked=!wanted;message(e.message,true);}finally{editing=false;await refresh();}};
$('room-model').onchange=async()=>{const select=$('room-model');select.disabled=true;try{await A.api('/api/settings',{method:'POST',body:{[llmModelField]:select.value}});AssistantUI.toast('模型已切换，下一次分析开始使用');}catch(e){message(e.message,true);await preferences();}finally{select.disabled=false;}};
$('room-stop').onclick=async()=>{$('room-stop').disabled=true;try{await A.send('STOP_CAPTURE',{sid,tabId:sourceTab});message('录音已结束，剩余字幕与总结会继续保存。');await refresh();}catch(e){message(e.message,true);}};
$('room-export').onclick=()=>A.download(sid).catch(e=>message(e.message,true));
$('room-overlay').hidden=!extension;
$('room-overlay').onclick=async()=>{try{const state=await A.send('RECORDER_STATE',{sid,tabId:sourceTab});if(!state?.recording||!state.tabId)throw new Error('当前没有正在录音的课程。回放字幕请在课程笔记中附加。');await chrome.scripting.executeScript({target:{tabId:state.tabId},files:['session-view.js','overlay.js']});AssistantUI.toast('已同步课堂字幕浮层');}catch(e){message(e.message,true);}};
$('minimize').onclick=async()=>{if(!extension){location.href='dashboard.html';return;}try{const win=await chrome.windows.getCurrent();if(win.type==='popup'){await chrome.windows.update(win.id,{state:'minimized'});}else if(sourceTab){const tab=await chrome.tabs.update(sourceTab,{active:true});await chrome.windows.update(tab.windowId,{focused:true});}else{message('从扩展面板点击「打开课堂实况」，即可使用独立窗口和最小化。');}}catch(e){message(e.message,true);}};
if(!extension){$('minimize').title='返回课程笔记';$('minimize').setAttribute('aria-label','返回课程笔记');}
window.addEventListener('focus',()=>void preferences().then(refresh).catch(e=>message(e.message,true)));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
(async()=>{try{await preferences();await refresh();}catch(e){message(e.message,true);}setInterval(()=>void refresh(),1500);})();
})();
