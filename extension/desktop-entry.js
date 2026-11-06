(() => {
  "use strict";
  const A=AssistantClient, $=id=>document.getElementById(id);
  const dialog=document.createElement("dialog");dialog.className="school-dialog";dialog.id="school-dialog";
  dialog.innerHTML=`<div class="section-head"><div><p class="eyebrow">百步梯 · 学校课程</p><h2>需要学校课程时，再连接</h2></div><button id="close-school" aria-label="关闭">×</button></div>
    <p id="school-context">本地笔记和资料可以直接使用，不需要先登录学校。</p>
    <ol class="school-steps"><li><span>1</span><div><strong>浏览器扩展</strong><p id="school-extension">正在检查…</p></div></li><li><span>2</span><div><strong>在百步梯登录</strong><p>账号和密码只在学校页面输入。</p></div></li><li><span>3</span><div><strong>打开要处理的课程</strong><p id="school-resume">确认能读取后，已提交的任务会继续，无需重新发送。</p></div></li></ol>
    <div id="school-status" role="status"></div><div class="school-buttons"><button id="school-go" class="primary">打开百步梯 ↗</button><button id="school-vpn">校外入口 ↗</button></div>
    <details id="school-install"><summary>首次使用：安装或更新扩展</summary><p>在 Edge 地址栏输入 <b>edge://extensions</b>（Chrome 用 chrome://extensions），打开开发人员模式，点击“加载解压缩的扩展”，选择下面的文件夹。更新时点击扩展卡片的刷新按钮。</p><code id="school-path"></code><button id="copy-extension">复制文件夹路径</button><p>安装后重新打开这个窗口即可自动连接。只需做一次。扩展在浏览器中读取学校课程，不传出登录 Cookie。</p></details>
    <p class="fine">已登录但没有进展？请在同一浏览器打开这门课的播放页；校园网络不可用时尝试校外入口。</p>`;
  document.body.append(dialog);
  let course="", snapshot=null, busy=false, lastConnected=null;
  $("close-school").onclick=()=>dialog.close();
  async function refresh(){
    try{
      snapshot=await A.api("/api/desktop/status",{timeout:5000});
      const connected=snapshot.extension_connected;
      $("school-extension").textContent=connected?"已连接 · 会自动接收课程任务":"尚未检测到 · 首次使用请按下方步骤安装";
      if(lastConnected!==connected)$("school-install").open=!connected;
      lastConnected=connected;
      $("school-path").textContent=snapshot.extension_path;
      const readable=course?snapshot.readable_courses.includes(course):snapshot.readable_courses.length>0;
      $("school-status").textContent=readable?`已能读取${course||"学校课程"}。待处理任务将自动继续。`:connected?"等待学校播放页。登录后选中课程即可。":"学校入口可以先打开；处理课程还需要连接扩展。";
      const badge=$("school-entry"); if(badge&&!badge.querySelector("strong"))badge.textContent=readable?"百步梯已连接":"连接百步梯 ↗";
    }catch{ $("school-status").textContent="本地服务暂未就绪，请从桌面“SCUT 学习助手”重新打开。"; }
  }
  function show(detail={}){
    course=detail.course||$("course")?.value||"";
    $("school-context").textContent=course?`连接「${course}」。你刚提交的任务会保留，登录后继续。`:"本地笔记和资料可以直接使用，不需要先登录学校。";
    if(!dialog.open)dialog.showModal();void refresh();
  }
  async function connect(external){
    if(busy)return;busy=true;
    // Open synchronously only without the bridge, so browser popup rules cannot swallow the login page.
    const fallback=!snapshot?.extension_connected?window.open("about:blank","scut-school-login"):null;
    try{
      const req=await A.api("/api/desktop/school",{method:"POST",body:{course,external}});
      if(req.via_extension){fallback?.close();$("school-status").textContent="正在交给浏览器打开（最多约 30 秒）。登录后回到这里即可。";}
      else if(fallback)fallback.location.replace(req.url);
      else {const a=document.createElement("a");a.href=req.url;a.target="_blank";a.rel="noopener noreferrer";a.textContent="点击打开学校页面 ↗";$("school-status").replaceChildren(a);}
    }catch(e){fallback?.close();$("school-status").textContent=e.message;}finally{busy=false;}
  }
  $("school-go").onclick=()=>connect(false);$("school-vpn").onclick=()=>connect(true);
  $("copy-extension").onclick=async()=>{await navigator.clipboard.writeText($("school-path").textContent);$("copy-extension").textContent="已复制";};
  $("school-entry")?.addEventListener("click",()=>show());
  document.addEventListener("study-connect-school",e=>show(e.detail));
  setInterval(()=>{if(!document.hidden&&dialog.open)void refresh();},5000);
  void refresh();
})();
