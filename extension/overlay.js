(() => {
const VERSION="0.5.0";
if(globalThis.__scutOverlayVersion===VERSION)return;
globalThis.__scutOverlayDispose?.();
globalThis.__scutOverlayVersion=VERSION;
document.getElementById("scut-local-captions")?.remove();
const host=document.createElement("div");host.id="scut-local-captions";
const shadow=host.attachShadow({mode:"closed"}),V=SessionView;
shadow.innerHTML=`<style>
:host{all:initial;position:fixed;inset:0;z-index:2147483646;pointer-events:none;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#edf0fa}*{box-sizing:border-box}
button{font:inherit;cursor:pointer;color:#c2c6d9;border:1px solid #ffffff12;background:#ffffff09;border-radius:8px;padding:5px 9px;font-size:10px;transition:background .2s,transform .2s}button:hover{background:#ffffff22}button:active{transform:scale(.95)}button:focus-visible{outline:2px solid #84baff;outline-offset:3px}
.caption{pointer-events:auto;position:absolute;bottom:7%;left:15%;width:70%;max-width:960px;background:#222330e8;backdrop-filter:blur(30px) saturate(1.5);border:1px solid #ffffff20;border-radius:20px;box-shadow:0 12px 60px #0003,inset 0 1px #ffffff0b;padding:11px 18px 16px;text-align:center;display:none}
.bar{display:flex;gap:6px;align-items:center;color:#a7aec6;cursor:move;touch-action:none;flex-wrap:wrap}.status{flex:1;text-align:left;font-size:10px;white-space:nowrap;min-width:130px}.status:before{content:"";display:inline-block;width:5px;height:5px;background:#96b3f3;border-radius:50%;margin:0 8px 2px 0}
.words{font-size:22px;line-height:1.65;margin:10px 5px 0;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;text-shadow:0 2px 8px #0004}.history{max-height:min(34vh,360px);overflow:auto;text-align:left;margin-top:13px;overscroll-behavior:contain;overflow-anchor:none;padding:0 9px}
.history .live-line{padding:8px 9px;border-left:1px solid #ffffff12}.history time{font-size:9px;color:#8c93ad;font-variant-numeric:tabular-nums}.history p{font-size:15px;color:#aeb5cc;line-height:1.85;margin:4px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}.history .latest{border-left:2px solid #9cb8f4;background:#ffffff04}.history .latest p{color:#f3f5fe}.history .uncertain p{color:#d3b586;font-size:12px}
.context-head{display:flex;justify-content:space-between;align-items:center;font-size:10px;color:#969fbb;margin:12px 8px 0}.context-head button{border:0;color:#a6bdef;background:transparent}.mini{width:auto;max-width:75vw;padding:10px 15px;border-radius:40px}.mini .smaller,.mini .larger,.mini .expand,.mini .room{display:none}.mini .status{min-width:0}
.alerts{position:absolute;right:24px;top:30px;width:min(350px,85vw);display:grid;gap:12px}.alert{pointer-events:auto;background:#fafbffed;backdrop-filter:blur(30px);color:#515160;border:1px solid #fff9;box-shadow:0 15px 55px #15172826;border-radius:20px;padding:18px 20px;animation:enter .5s cubic-bezier(.18,1.15,.3,1);line-height:1.8;font-size:13px;transition:opacity .2s,transform .2s}.alert:before{content:"SCUT · 课堂提醒";display:block;font-size:9px;letter-spacing:1.3px;color:#aaa6bd;margin-bottom:9px}.alert strong{display:block;color:#555070;font-size:15px;margin-bottom:7px}.alert button{float:right;color:#8e83b0;border-color:#ebe7f3;background:#f3f0f8}.evidence{font-size:11px;line-height:1.8;margin:12px 0;border-left:2px solid #dfd7ef;padding-left:10px;color:#9090a1}.tag{font-size:9px;color:#a39caf}.alert.leaving{opacity:0;transform:translateX(25px) scale(.97)}[hidden]{display:none!important}
@keyframes enter{from{opacity:0;transform:translateX(30px) scale(.95)}to{opacity:1;transform:none}}@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}@media(prefers-reduced-motion:no-preference){.caption{animation:rise .4s ease-out}.history .latest{animation:rise .25s ease-out}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style><section class="caption" role="region" aria-label="课堂字幕"><div class="bar"><span class="status">SCUT 课堂字幕</span><button class="smaller" aria-label="减小字幕字号">A−</button><button class="larger" aria-label="增大字幕字号">A+</button><button class="expand">展开上下文</button><button class="room">课堂窗 ↗</button><button class="hide">最小化</button></div><div class="words" aria-live="polite"></div><div class="context-head" hidden><span>最近的连续字幕 · 完整记录在课堂窗</span><button class="follow">跟随最新 ↓</button></div><div class="history" aria-label="最近的连续字幕" hidden></div></section><aside class="alerts" aria-live="assertive"></aside>`;
const q=s=>shadow.querySelector(s),caption=q(".caption"),words=q(".words"),history=q(".history"),status=q(".status"),alerts=q(".alerts");
let session=null,replay=false,seen=new Set(),lastTime=-1,liveActive=false,mode="compact",fontSize=22,follow=true,disposed=false,lastRows=[];
const controller=new AbortController(),signal=controller.signal;
function save(){chrome.runtime.sendMessage({type:"SET_OVERLAY_PREFS",mode,fontSize}).catch(()=>{});}
function layout(){caption.classList.toggle("mini",mode==="minimized");words.hidden=mode!=="compact";history.hidden=mode!=="context";q(".context-head").hidden=mode!=="context";q(".expand").textContent=mode==="context"?"精简字幕":"展开上下文";q(".hide").textContent=mode==="minimized"?"展开字幕":"最小化";words.style.fontSize=fontSize+"px";if(mode==="context")V.paint(history,lastRows,follow);}
function mount(){const root=document.fullscreenElement;(root&&root.tagName!=="VIDEO"?root:document.documentElement).append(host);}
mount();document.addEventListener("fullscreenchange",mount,{signal});
q(".hide").onclick=()=>{mode=mode==="minimized"?"compact":"minimized";layout();save();};
q(".expand").onclick=()=>{mode=mode==="context"?"compact":"context";layout();save();};
q(".room").onclick=()=>chrome.runtime.sendMessage({type:"OPEN_CLASSROOM"}).then(r=>{if(!r?.ok)status.textContent="请从工具栏扩展中打开课堂窗";}).catch(()=>{});
q(".smaller").onclick=()=>{fontSize=Math.max(16,fontSize-2);layout();save();};q(".larger").onclick=()=>{fontSize=Math.min(36,fontSize+2);layout();save();};
history.addEventListener("scroll",()=>{follow=history.scrollHeight-history.scrollTop-history.clientHeight<45;q(".follow").textContent=follow?"跟随最新 ↓":"回到最新 ↓";},{signal});
q(".follow").onclick=()=>{follow=true;history.scrollTop=history.scrollHeight;q(".follow").textContent="跟随最新 ↓";};
const bar=q(".bar");let drag;
bar.addEventListener("pointerdown",e=>{if(e.target.tagName==="BUTTON")return;drag={x:e.clientX,y:e.clientY,left:caption.offsetLeft,top:caption.offsetTop};bar.setPointerCapture(e.pointerId);},{signal});
bar.addEventListener("pointermove",e=>{if(!drag)return;caption.style.left=Math.max(0,Math.min(innerWidth-caption.offsetWidth,drag.left+e.clientX-drag.x))+"px";caption.style.top=Math.max(0,Math.min(innerHeight-caption.offsetHeight,drag.top+e.clientY-drag.y))+"px";caption.style.bottom="auto";},{signal});
bar.addEventListener("pointerup",()=>drag=null,{signal});bar.addEventListener("pointercancel",()=>drag=null,{signal});
function alert(event){
 if(seen.has(event.id))return;seen.add(event.id);const el=document.createElement("div");el.className="alert";const dismiss=()=>{el.classList.add("leaving");setTimeout(()=>el.remove(),220);};
 const close=document.createElement("button");close.textContent="知道了";close.onclick=dismiss;const title=document.createElement("strong");title.textContent=event.label;const text=document.createElement("div");text.textContent=event.message;const evidence=document.createElement("p");evidence.className="evidence";evidence.textContent=V.clean({text:event.evidence});const tag=document.createElement("span");tag.className="tag";tag.textContent=event.source==="deepseek"?"AI 分析 · 原文已保存":"本地规则初筛 · 请核对";
 el.append(close,title,text,evidence,tag);alerts.append(el);while(alerts.children.length>3)alerts.firstChild.remove();setTimeout(dismiss,25000);
}
function transcript(rows,current){lastRows=rows.slice(-24);words.textContent=current.map(V.clean).join(" ")||"正在听课，等待新的字幕…";if(mode==="context")V.paint(history,lastRows,follow);}
function incoming(message){
 if(disposed)return;
 if(message.type==="SCUT_UPDATE"){
  if(session?.id!==message.session.id){seen=new Set(message.session.events.map(e=>e.id));alerts.replaceChildren();}
  session=message.session;replay=false;liveActive=!message.finished;caption.style.display="block";transcript(session.segments,session.segments.slice(-2));
  const language=session.segments.at(-1)?.language;status.textContent=message.recorder?.error||"SCUT · "+(message.finished?"已结束":"正在听课")+(language?" · "+language.toUpperCase():"")+(message.recorder?.pending?" · 补传 "+message.recorder.pending+" 段":"");
  status.title="识别耗时 "+(session.last_latency||"—")+" 秒；完整上下文请展开或打开课堂窗";
  for(const e of session.events.slice(-12))alert(e);
 }else if(message.type==="SCUT_REPLAY"){session=message.session;replay=true;liveActive=false;seen.clear();lastTime=-1;alerts.replaceChildren();caption.style.display="block";status.textContent="SCUT · 回放字幕 · 跟随视频";}
}
chrome.runtime.onMessage.addListener(incoming);
const timer=setInterval(()=>{if(!session||!replay||disposed)return;const video=document.querySelector("video");if(!video)return;const t=video.currentTime;const rows=session.segments.filter(s=>s.start<=t);transcript(rows,rows.filter(s=>s.end>=t));if(t<lastTime-2||Math.abs(t-lastTime)>5)seen=new Set(session.events.filter(e=>e.start<t-1).map(e=>e.id));for(const e of session.events)if(e.start<=t&&e.start>t-2)alert(e);lastTime=t;},250);
const watched=new WeakSet();function watch(){for(const video of document.querySelectorAll("video")){if(watched.has(video))continue;watched.add(video);for(const type of ["pause","seeking","ratechange"])video.addEventListener(type,()=>{if(liveActive)chrome.runtime.sendMessage({type:"PLAYER_CHANGED"}).catch(()=>{});},{signal});}}
watch();const observer=new MutationObserver(watch);observer.observe(document.documentElement,{childList:true,subtree:true});
globalThis.__scutOverlayDispose=()=>{disposed=true;controller.abort();observer.disconnect();clearInterval(timer);chrome.runtime.onMessage.removeListener(incoming);host.remove();};
chrome.runtime.sendMessage({type:"OVERLAY_READY"}).then(r=>{const prefs=r?.data?.prefs;if(prefs){mode=prefs.mode||"compact";fontSize=prefs.fontSize||22;layout();}}).catch(()=>{});
})();
