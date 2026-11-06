/* Offline Markmap viewer. Edited outlines are separate from the original notes. */
globalThis.LectureMindmap=(()=>{
  const A=globalThis.AssistantClient,MAX_LENGTH=200000,MAX_NODES=2000;
  let state=null,transformer=null;
  function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
  function button(text,fn){const n=el("button",text);n.type="button";n.onclick=fn;return n;}
  function prepare(markdown){
    if(!markdown.trim())throw new Error("提纲不能为空，请至少保留一个标题或知识点。");
    if(markdown.length>MAX_LENGTH||markdown.split("\n").length>4000||/^[ \t]{61}/m.test(markdown))throw new Error("提纲过大或层级过深，请拆分为较小的导图。");
    if(!globalThis.SCUTMarkmap)throw new Error("导图组件未能加载，请刷新页面重试。");
    if(!transformer){
      transformer=new SCUTMarkmap.Transformer([]);
      const md=transformer.md;
      md.set({html:false,linkify:false});
      // Only our content-addressed evidence links are active. Images remain alt text;
      // neither imported notes nor edited Markdown can load remote resources.
      md.renderer.rules.image=(tokens,i)=>md.utils.escapeHtml(tokens[i].content);
      md.renderer.rules.link_open=(tokens,i,_options,env)=>{
        const href=tokens[i].attrGet("href")||"",allowed=/^#lecture-topic-[a-f0-9]{20}$/.test(href);
        (env.scutLinks??=[]).push(allowed);
        return allowed?'<a href="'+href+'">':"<span>";
      };
      md.renderer.rules.link_close=(_tokens,_i,_options,env)=>env.scutLinks?.pop()?"</a>":"</span>";
    }
    const {root}=transformer.transform(markdown),pending=[[root,0]];let count=0;
    while(pending.length){const [node,depth]=pending.pop();if(++count>MAX_NODES||depth>24)throw new Error("导图节点过多或层级过深，请精简提纲后查看。");for(const child of node.children||[])pending.push([child,depth+1]);}
    return root;
  }
  function active(s){return s===state;}
  function dirty(s){return s.editor.value!==s.savedText||s.sourceHash!==s.savedSourceHash;}
  function say(s,text,error=false){if(!active(s))return;s.message.textContent=text;s.message.classList.toggle("error",error);}
  function update(s){
    if(!active(s))return;
    const changed=dirty(s),available=!!(s.meta?.available||s.meta?.custom);
    s.save.disabled=!changed||s.saving||s.loading||!!s.renderError;
    s.rebuild.disabled=!s.meta?.available||s.loading||s.saving;
    s.edit.disabled=!available||s.loading;s.mdExport.disabled=!available;
    for(const control of s.graphControls)control.disabled=!s.map||!!s.renderError||s.rendering;
    s.saved.textContent=s.loading?"正在读取导图…":changed?"有未保存的修改":s.meta?.custom?"提纲已保存在本机":"根据现有笔记生成";
    s.saved.classList.toggle("is-dirty",changed);
    const warnings=[];
    if(s.meta?.partial)warnings.push("课程笔记尚未整理完，当前导图只包含已保存的内容。");
    if(s.meta&&s.sourceHash!==s.meta.current_source_hash)warnings.push("课程笔记已更新，当前提纲保留了旧版本。可导出备份，或从最新笔记重建。");
    s.warning.textContent=warnings.join(" ");s.warning.hidden=!warnings.length;
  }
  function fit(s){if(s&&active(s)&&s.map&&!s.root.hidden&&s.canvas.getBoundingClientRect().width)void s.map.fit().catch(e=>say(s,e.message,true));}
  function render(s){
    clearTimeout(s.timer);
    const version=++s.renderVersion;
    s.renderQueue=s.renderQueue.catch(()=>{}).then(async()=>{
      if(!active(s)||version!==s.renderVersion||!s.meta)return;
      if(!s.meta.available&&!s.meta.custom){s.empty.hidden=false;s.empty.textContent="本课还没有知识点笔记。先在“知识点笔记”中生成笔记，再查看导图。";s.svg.hidden=true;update(s);return;}
      s.rendering=true;update(s);
      try{
        const tree=prepare(s.editor.value);
        if(!s.map)s.map=SCUTMarkmap.Markmap.create(s.svg,{
          duration:0,autoFit:false,embedGlobalCSS:false,style:()=>"",
          initialExpandLevel:s.expandLevel,maxWidth:280,spacingHorizontal:70,spacingVertical:12,
          maxInitialScale:1,zoom:true,pan:true,
          color:node=>["#395f85","#4d7890","#6a779c","#5b8b7c"][Number(node.state?.path?.split(".")[1]||0)%4],
        });
        s.map.setOptions({initialExpandLevel:s.expandLevel});
        s.svg.hidden=false;s.empty.hidden=true;
        await s.map.setData(tree);
        if(!active(s)||version!==s.renderVersion)return;
        s.renderError="";await s.map.fit();
        if(active(s)&&version===s.renderVersion)say(s,"");
      }catch(e){if(active(s)&&version===s.renderVersion){s.renderError=e.message;say(s,e.message,true);}}
      finally{if(active(s)){s.rendering=false;update(s);}}
    });
    return s.renderQueue;
  }
  async function load(s){
    if(!active(s)||s.loading)return;
    s.loading=true;update(s);
    try{
      const data=await A.api(`/api/sessions/${s.record.id}/mindmap`);
      if(!active(s))return;
      const keep=s.meta&&dirty(s);s.meta=data;
      if(!keep){s.editor.value=data.markdown;s.savedText=data.markdown;s.revision=data.revision;s.sourceHash=data.source_hash;s.savedSourceHash=data.source_hash;}
      s.refreshNeeded=false;
      await render(s);
    }catch(e){if(active(s)){say(s,e.message,true);if(!s.meta){s.empty.hidden=false;s.empty.textContent="导图暂时无法读取，可点击“重新读取”重试。";}}}
    finally{if(active(s)){s.loading=false;update(s);}}
  }
  async function save(s){
    if(s.saving||!s.meta)return;
    try{prepare(s.editor.value);}catch(e){say(s,e.message,true);return;}
    const markdown=s.editor.value,sourceHash=s.sourceHash;s.saving=true;update(s);
    try{
      const data=await A.api(`/api/sessions/${s.record.id}/mindmap`,{method:"POST",body:{markdown,revision:s.revision,source_hash:sourceHash}});
      if(!active(s))return;
      s.meta=data;s.savedText=markdown;s.savedSourceHash=sourceHash;s.revision=data.revision;
      say(s,dirty(s)?"已保存；之后的修改尚未保存。":"提纲已保存，重新打开这节课仍可继续查看。");
    }catch(e){say(s,e.message,true);}
    finally{if(active(s)){s.saving=false;update(s);}}
  }
  function rebuild(s){
    if(!s.meta?.available)return;
    if((dirty(s)||s.meta.custom)&&!confirm("用最新课程笔记替换当前提纲？如需保留修改，请先导出 Markdown。替换后点击保存才会覆盖本机提纲。"))return;
    s.editor.value=s.meta.generated_markdown;s.sourceHash=s.meta.current_source_hash;
    void render(s);update(s);
  }
  function download(blob,name){
    const url=URL.createObjectURL(blob),a=el("a");a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  function filename(s){return (s.record.summary?.headline||s.record.title||"课程导图").replace(/[<>:"/\\|?*\u0000-\u001f]/g,"_").slice(0,80);}
  function svgExport(s){
    if(!s.map||s.renderError)throw new Error("请先生成可查看的导图。");
    const group=s.svg.querySelector("g"),box=group?.getBBox();
    if(!box?.width||!box.height)throw new Error("导图尚未绘制完成，请稍后重试。");
    const clone=s.svg.cloneNode(true),padding=28,width=Math.ceil(box.width+padding*2),height=Math.ceil(box.height+padding*2);
    clone.setAttribute("xmlns","http://www.w3.org/2000/svg");clone.setAttribute("width",String(width));clone.setAttribute("height",String(height));
    clone.setAttribute("viewBox",[box.x-padding,box.y-padding,width,height].join(" "));
    clone.querySelector("g").removeAttribute("transform");clone.removeAttribute("style");
    for(const link of clone.querySelectorAll("a"))link.removeAttribute("href");
    const style=document.createElementNS("http://www.w3.org/2000/svg","style");
    style.textContent=[...document.styleSheets].filter(sheet=>/\/(markmap-vendor|mindmap)\.css(?:\?|$)/.test(sheet.href||"")).flatMap(sheet=>[...sheet.cssRules].map(rule=>rule.cssText)).join("\n");
    clone.prepend(style);
    return {text:new XMLSerializer().serializeToString(clone),width,height};
  }
  async function exportImage(s,png=false){
    try{
      const name=filename(s),{text,width,height}=svgExport(s);
      if(!png){download(new Blob([text],{type:"image/svg+xml;charset=utf-8"}),name+".svg");return;}
      s.pngExport.disabled=true;
      const image=new Image();
      await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error("图片导出失败，可先导出 SVG。"));image.src="data:image/svg+xml;charset=utf-8,"+encodeURIComponent(text);});
      const scale=Math.min(2,4096/width,4096/height),canvas=document.createElement("canvas");
      canvas.width=Math.max(1,Math.ceil(width*scale));canvas.height=Math.max(1,Math.ceil(height*scale));
      const ctx=canvas.getContext("2d");ctx.fillStyle="#fff";ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
      const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error("PNG 编码失败，可先导出 SVG。")),"image/png"));
      download(blob,name+".png");
    }catch(e){say(s,e.message,true);}
    finally{if(active(s))update(s);}
  }
  async function fullscreen(s){
    try{if(document.fullscreenElement===s.workspace)await document.exitFullscreen();else await s.workspace.requestFullscreen();}
    catch{say(s,"浏览器未能进入全屏，可使用浏览器的全屏功能。");}
  }
  function reset(){
    if(!state)return;
    const s=state;state=null;clearTimeout(s.timer);s.resize?.disconnect();s.map?.destroy();s.root.replaceChildren();
    if(document.fullscreenElement===s.workspace)void document.exitFullscreen();
  }
  function mount(target,record,options){
    if(state?.record.id!==record.id){
      reset();
      const s=state={root:target,record,options,meta:null,savedText:"",revision:"",sourceHash:"",savedSourceHash:"",renderVersion:0,renderQueue:Promise.resolve(),expandLevel:3,refreshNeeded:true};
      const workspace=s.workspace=el("section",undefined,"mindmap-workspace"),toolbar=el("div",undefined,"mindmap-toolbar"),actions=el("div",undefined,"mindmap-actions"),view=el("div",undefined,"mindmap-view-actions");
      s.saved=el("span","正在读取导图…","mindmap-save-state");s.saved.setAttribute("role","status");
      s.edit=button("编辑提纲",()=>{s.editorPane.hidden=!s.editorPane.hidden;s.edit.setAttribute("aria-expanded",String(!s.editorPane.hidden));if(!s.editorPane.hidden)s.editor.focus();});
      s.edit.setAttribute("aria-expanded","false");s.edit.setAttribute("aria-controls","mindmap-editor-pane");
      s.save=button("保存提纲",()=>void save(s));s.save.className="primary";
      s.rebuild=button("从笔记重建",()=>rebuild(s));
      const retry=button("重新读取",()=>void load(s));
      actions.append(s.edit,s.save,s.rebuild,retry,s.saved);
      const fitButton=button("适应画布",()=>fit(s)),expand=button("展开全部",()=>{s.expandLevel=-1;void render(s);}),collapse=button("折叠分支",()=>{s.expandLevel=3;void render(s);});
      s.full=button("全屏查看",()=>void fullscreen(s));
      const exports=el("details",undefined,"mindmap-exports"),menu=el("div"),caption=el("summary","导出 ↓");
      s.mdExport=button("Markdown 提纲",()=>download(new Blob([s.editor.value],{type:"text/markdown;charset=utf-8"}),filename(s)+".md"));
      const svgButton=button("SVG 图片",()=>void exportImage(s));s.pngExport=button("PNG 图片",()=>void exportImage(s,true));
      menu.append(s.mdExport,svgButton,s.pngExport);exports.append(caption,menu);view.append(fitButton,expand,collapse,s.full,exports);toolbar.append(actions,view);
      s.graphControls=[fitButton,expand,collapse,s.full,svgButton,s.pngExport];
      s.warning=el("p",undefined,"mindmap-warning");s.warning.hidden=true;
      s.message=el("p",undefined,"mindmap-message");s.message.setAttribute("role","status");
      const stage=el("div",undefined,"mindmap-stage"),editorPane=s.editorPane=el("div",undefined,"mindmap-editor");editorPane.id="mindmap-editor-pane";editorPane.hidden=true;
      const label=el("label","Markdown 提纲");label.htmlFor="mindmap-outline";
      s.editor=el("textarea");s.editor.id="mindmap-outline";s.editor.maxLength=MAX_LENGTH;s.editor.spellcheck=false;s.editor.setAttribute("aria-label","编辑思维导图 Markdown 提纲");
      s.editor.oninput=()=>{update(s);clearTimeout(s.timer);s.timer=setTimeout(()=>void render(s),250);};
      editorPane.append(label,s.editor,el("p","用 # 标题、## 分支和 - 列表组织内容。修改后实时预览，点击保存留在本机。","mindmap-editor-help"));
      s.canvas=el("div",undefined,"mindmap-canvas");s.svg=document.createElementNS("http://www.w3.org/2000/svg","svg");s.svg.classList.add("scut-mindmap");s.svg.setAttribute("aria-label","课程知识点思维导图，点击圆点展开或折叠，点击带下划线的知识点查看出处");s.svg.setAttribute("role","img");s.svg.hidden=true;
      s.svg.addEventListener("click",event=>{
        const link=event.target.closest?.("a");if(!link)return;event.preventDefault();event.stopPropagation();
        const key=(link.getAttribute("href")||"").match(/^#lecture-topic-([a-f0-9]{20})$/)?.[1],index=s.meta?.sources[key];
        if(!Number.isInteger(index)){say(s,"这条出处对应的笔记已更新，请从最新笔记重建导图。");return;}
        if(document.fullscreenElement===workspace)void document.exitFullscreen();
        s.options.topic(index);
      },true);
      s.empty=el("p","正在准备导图…","mindmap-empty");s.canvas.append(s.svg,s.empty);stage.append(editorPane,s.canvas);
      workspace.append(toolbar,s.warning,stage,s.message,el("p","滚轮缩放，拖动画布移动，点击圆点展开或折叠。节点展示笔记摘录；点击知识点或出处可查看完整笔记、课件与老师原话。","mindmap-help"));target.append(workspace);
      s.resize=new ResizeObserver(()=>fit(s));s.resize.observe(s.canvas);update(s);
    }
    const s=state,stamp=JSON.stringify([record.summary,record.mindmap?.revision]);s.record=record;s.options=options;
    if(stamp!==s.noteStamp){s.noteStamp=stamp;s.refreshNeeded=true;}
    if(!target.hidden)activate();
  }
  function activate(){if(!state)return;const s=state;if(s.refreshNeeded||!s.meta)void load(s);else requestAnimationFrame(()=>fit(s));}
  function canLeave(){return !state||!dirty(state)||confirm("这份导图有未保存的修改。离开前请保存或导出提纲。仍要离开吗？");}
  if(typeof window!=="undefined"){
    window.addEventListener("beforeunload",event=>{if(state&&dirty(state)){event.preventDefault();event.returnValue="";}});
    document.addEventListener("fullscreenchange",()=>{if(state){state.full.textContent=document.fullscreenElement===state.workspace?"退出全屏":"全屏查看";requestAnimationFrame(()=>fit(state));}});
  }
  return {mount,reset,activate,canLeave,prepare};
})();
