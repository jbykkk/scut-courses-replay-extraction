/* Course-scoped originals, lecture quotes and notes share one topic selection. */
globalThis.LectureCompare=(()=>{
  const A=globalThis.AssistantClient;
  const WIDTHS_KEY="scut.lectureCompare.widths.v1",MIN_COLUMN_WIDTH=120;
  function normalizeWidths(value){
    if(!Array.isArray(value)||value.length!==3||value.some(n=>typeof n!=="number"||!Number.isFinite(n)||n<=0))return [1/3,1/3,1/3];
    const total=value.reduce((a,b)=>a+b,0);if(!Number.isFinite(total))return [1/3,1/3,1/3];
    return value.map(n=>n/total);
  }
  function loadWidths(){try{return normalizeWidths(JSON.parse(localStorage.getItem(WIDTHS_KEY)));}catch{return [1/3,1/3,1/3];}}
  let splitWidths=loadWidths();
  function resizeWidths(value,boundary,delta,minimum=.12){
    const next=normalizeWidths(value);
    if(![0,1].includes(boundary)||!Number.isFinite(delta))return next;
    const pair=next[boundary]+next[boundary+1],lower=Math.min(Math.max(0,minimum),pair/2);
    next[boundary]=Math.max(lower,Math.min(pair-lower,next[boundary]+delta));
    next[boundary+1]=pair-next[boundary];return next;
  }
  function saveWidths(){try{localStorage.setItem(WIDTHS_KEY,JSON.stringify(splitWidths));}catch{ /* Resizing remains available without storage. */ }}
  let current=null,root=null,epoch=0,index=0,objectURLs=new Set(),topicStamp="",picker=null,body=null,sourceSubmit=null,sourceInput=null,saving=false;
  function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
  // Build the supported Markdown with DOM nodes so model output stays inert text.
  function markdown(value){
    const content=el("div",undefined,"review-sheet"),lines=String(value??"").replace(/\r\n?/g,"\n").split("\n");
    let paragraph=[],lists=[],code=null,fence="";
    function inline(parent,text){
      const pattern=/\\([\\`*_[\]{}()#+.!>\-])|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|(?<![\w])_([^_\n]+)_(?!\w)|\*([^*\n]+)\*/g;
      let offset=0;
      for(const match of text.matchAll(pattern)){
        parent.append(document.createTextNode(text.slice(offset,match.index)));
        if(match[1])parent.append(document.createTextNode(match[1]));
        else if(match[2])parent.append(el("code",match[2]));
        else{const child=el(match[3]||match[4]?"strong":"em");inline(child,match[3]||match[4]||match[5]||match[6]);parent.append(child);}
        offset=match.index+match[0].length;
      }
      parent.append(document.createTextNode(text.slice(offset)));
    }
    function flush(){if(paragraph.length){const p=el("p");inline(p,paragraph.join("\n"));content.append(p);paragraph=[];}}
    for(const line of lines){
      if(code){if(new RegExp("^\\s*"+fence[0]+"{"+fence.length+",}\\s*$").test(line)){code=null;fence="";}else code.textContent+=(code.textContent?"\n":"")+line;continue;}
      const opening=line.match(/^\s*(`{3,}|~{3,})/);
      if(opening){flush();lists=[];fence=opening[1];code=el("code");const pre=el("pre");pre.append(code);content.append(pre);continue;}
      if(!line.trim()){flush();continue;}
      const heading=line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*$/);
      if(heading){flush();lists=[];const h=el("h"+heading[1].length);inline(h,heading[2]);content.append(h);continue;}
      if(/^\s*(?:\*\s*){3,}$|^\s*(?:-\s*){3,}$|^\s*(?:_\s*){3,}$/.test(line)){flush();lists=[];content.append(el("hr"));continue;}
      const item=line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
      if(item){
        flush();const indent=item[1].replace(/\t/g,"    ").length,tag=/\d/.test(item[2])?"ol":"ul";
        while(lists.length&&lists.at(-1).indent>indent)lists.pop();
        if(lists.length&&lists.at(-1).indent===indent&&lists.at(-1).tag!==tag)lists.pop();
        if(!lists.length||lists.at(-1).indent<indent){const list=el(tag);if(tag==="ol")list.start=parseInt(item[2],10);(lists.at(-1)?.last||content).append(list);lists.push({indent,tag,list,last:null});}
        const li=el("li");inline(li,item[3]);lists.at(-1).list.append(li);lists.at(-1).last=li;continue;
      }
      if(lists.length&&/^\s{2,}\S/.test(line)){lists.at(-1).last.append(el("br"));inline(lists.at(-1).last,line.trim());continue;}
      lists=[];
      const quote=line.match(/^\s*>\s?(.*)$/);
      if(quote){flush();const block=el("blockquote");inline(block,quote[1]);content.append(block);continue;}
      paragraph.push(line);
    }
    flush();return content;
  }
  function rows(record){const byId=new Map((record.segments||[]).map(s=>[s.id,s]));return (record.summary?.topics||[]).map(t=>({topic:t,speech:(t.segment_ids||[]).map(id=>byId.get(id)).filter(Boolean),materials:t.material_refs||[]}));}
  function configureDividers(columns,panels,dividers){
    const titles=["课件","老师讲述","课堂笔记"];
    function actual(){const sizes=panels.map(p=>p.getBoundingClientRect().width),total=sizes.reduce((a,b)=>a+b,0);return {widths:total?normalizeWidths(sizes):splitWidths,total};}
    function apply(){
      splitWidths.forEach((width,i)=>columns.style.setProperty("--compare-col-"+(i+1),width+"fr"));
      const {total}=actual(),minimum=total?Math.min(MIN_COLUMN_WIDTH/total,1/3):.12;
      dividers.forEach((divider,i)=>{const pair=splitWidths[i]+splitWidths[i+1],lower=Math.min(minimum,pair/2),percent=(splitWidths[i]*100).toFixed(1);divider.setAttribute("aria-valuemin",(lower*100).toFixed(1));divider.setAttribute("aria-valuemax",((pair-lower)*100).toFixed(1));divider.setAttribute("aria-valuenow",percent);divider.setAttribute("aria-valuetext",titles[i]+" "+percent+"%，"+titles[i+1]+" "+(splitWidths[i+1]*100).toFixed(1)+"%");});
    }
    function resetWidths(){splitWidths=[1/3,1/3,1/3];apply();saveWidths();}
    dividers.forEach((divider,i)=>{
      divider.tabIndex=0;divider.setAttribute("role","separator");divider.setAttribute("aria-orientation","vertical");divider.setAttribute("aria-label","调整"+titles[i]+"与"+titles[i+1]+"的宽度");divider.setAttribute("aria-controls",panels[i].id);divider.title="拖动调整宽度；方向键微调；双击恢复等宽";
      divider.onfocus=()=>{splitWidths=actual().widths;apply();};
      divider.ondblclick=resetWidths;
      let drag=null,frame=0;
      function flush(){if(frame){cancelAnimationFrame(frame);frame=0;}if(!drag)return;splitWidths=resizeWidths(drag.widths,i,(drag.x-drag.start)/drag.total,Math.min(MIN_COLUMN_WIDTH/drag.total,1/3));apply();}
      function finish(event){if(!drag||(event&&event.pointerId!==drag.id))return;flush();drag=null;columns.classList.remove("is-resizing");saveWidths();}
      divider.onpointerdown=event=>{
        if(event.button!==0||matchMedia("(max-width:680px)").matches)return;
        const sizes=actual();if(!sizes.total)return;
        event.preventDefault();divider.focus({preventScroll:true});drag={id:event.pointerId,start:event.clientX,x:event.clientX,widths:sizes.widths,total:sizes.total};divider.setPointerCapture(event.pointerId);columns.classList.add("is-resizing");
      };
      divider.onpointermove=event=>{if(!drag||event.pointerId!==drag.id)return;drag.x=event.clientX;if(!frame)frame=requestAnimationFrame(()=>{frame=0;flush();});};
      divider.onpointerup=event=>{if(drag&&event.pointerId===drag.id)drag.x=event.clientX;finish(event);};
      divider.onpointercancel=finish;divider.onlostpointercapture=finish;
      divider.onkeydown=event=>{
        if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
        event.preventDefault();const {widths,total}=actual(),delta=event.key==="Home"?-1:event.key==="End"?1:(event.key==="ArrowRight"?1:-1)*(event.shiftKey ? .05 : .02);
        splitWidths=resizeWidths(widths,i,delta,total?Math.min(MIN_COLUMN_WIDTH/total,1/3):.12);apply();saveWidths();
      };
    });
    apply();return resetWidths;
  }
  function revoke(){for(const url of objectURLs)URL.revokeObjectURL(url);objectURLs.clear();}
  function reset(){epoch++;revoke();root?.replaceChildren();current=null;root=null;index=0;topicStamp="";sourceSubmit=null;sourceInput=null;saving=false;}
  function select(value){index=Math.max(0,Math.min(value,rows(current?.record||{}).length-1));if(picker)picker.value=String(index);paint();}
  function paint(){
    if(!body||!current)return;
    revoke();body.replaceChildren();
    const item=rows(current.record)[index];
    if(!item){body.append(el("p","生成笔记后，可按知识点对照课件、老师原话和整理结果。已有笔记可选择课件后重新生成。","compare-empty"));return;}
    const columns=el("div",undefined,"compare-columns"),material=el("section",undefined,"compare-column"),speech=el("section",undefined,"compare-column"),notes=el("section",undefined,"compare-column"),first=el("div",undefined,"compare-divider"),second=el("div",undefined,"compare-divider");
    [material,speech,notes].forEach((panel,i)=>panel.id="lecture-compare-panel-"+(i+1));
    material.append(el("h3","课件"));speech.append(el("h3","老师讲述"));notes.append(el("h3","课堂笔记"));
    if(!item.materials.length)material.append(el("p","此知识点尚未匹配到课件页。可选择本课 PDF 后重新生成，或核对老师是否在补充课外内容。","compare-empty"));
    for(const ref of item.materials){
      const loc=ref.locator||{},page=Number.isInteger(loc.page)&&loc.page>0?loc.page:1;
      material.append(el("strong",ref.title+" · "+(loc.page?"第 "+loc.page+" 页":"第 "+(loc.slide||"?")+" 张")),el("blockquote",ref.quote));
      if(/\.pdf$/i.test(ref.title)&&/^[a-f0-9]{32}$/.test(ref.source_id||"")){
        const preview=el("button","查看 PDF 原页"),slot=el("div",undefined,"compare-preview");material.append(preview,slot);
        preview.onclick=async()=>{
          const token=epoch,sid=current.record.id,topic=index;preview.disabled=true;
          try{
            const blob=await A.api(`/api/sessions/${sid}/materials/${ref.source_id}/pages/${page}`,{blob:true});
            if(token!==epoch||index!==topic)return;
            const objectURL=URL.createObjectURL(blob);objectURLs.add(objectURL);
            const picture=el("img");picture.alt=ref.title+" 第 "+page+" 页";picture.src=objectURL;
            const open=el("a","放大阅读原页 ↗");open.href=objectURL;open.target="_blank";open.rel="noopener noreferrer";
            slot.replaceChildren(open,picture);preview.hidden=true;
          }catch(e){if(token===epoch)current.options.notice(e.message);}finally{preview.disabled=false;}
        };
      }
    }
    speech.append(el("small",current.record.time_basis==="capture"?"时间从本次录音开始":"点击时间回看原视频"));
    if(!item.speech.length)speech.append(el("p","这条旧笔记没有保留可定位的原话，请重新生成。","compare-empty"));
    for(const s of item.speech){const quote=el("div",undefined,"compare-speech");quote.append(current.seek(s.start),el("blockquote",globalThis.SessionView.clean(s)));speech.append(quote);}
    notes.append(el("h4",item.topic.title),markdown(item.topic.detail||"本段未生成详细笔记。"),el("small","课件与讲述按内容对应，识别、匹配和整理结果仍需对照原文核对。"));
    columns.append(material,first,speech,second,notes);
    const tools=el("div",undefined,"compare-layout-tools"),hint=el("p","拖动分隔线调整宽度，选中分隔线后可按左右方向键微调。","compare-layout-hint"),reset=el("button","恢复等宽");reset.type="button";tools.append(hint,reset);body.append(tools,columns);
    reset.onclick=configureDividers(columns,[material,speech,notes],[first,second]);
  }
  function topicControls(){
    const nav=root.querySelector(".compare-topic-nav");nav.replaceChildren();
    const list=rows(current.record);picker=el("select");picker.setAttribute("aria-label","选择对照的知识点");
    list.forEach((r,i)=>{const option=el("option",(i+1)+". "+r.topic.title);option.value=String(i);picker.append(option);});
    index=Math.min(index,Math.max(0,list.length-1));picker.value=String(index);picker.onchange=()=>select(Number(picker.value));
    const prev=el("button","← 上一知识点"),next=el("button","下一知识点 →");prev.onclick=()=>select(index-1);next.onclick=()=>select(index+1);
    nav.append(prev,picker,next);nav.hidden=!list.length;
  }
  async function loadSources(form,token){
    try{
      const data=await A.api(`/api/sessions/${current.record.id}/materials`);
      if(token!==epoch)return;
      form.replaceChildren();
      const selected=new Set(data.selected_ids),choices=el("div",undefined,"compare-source-choices");
      for(const source of data.sources){const label=el("label"),check=el("input");check.type="checkbox";check.value=source.id;check.checked=selected.has(source.id);label.append(check,document.createTextNode(source.title));choices.append(label);}
      if(!data.sources.length)choices.append(el("p","本课程还没有课件，可以直接上传 PDF。","muted"));
      const input=el("input");input.type="file";input.accept=".pdf,.pptx";input.multiple=true;input.setAttribute("aria-label","上传本节课的 PDF 或 PPTX 课件");
      const help=el("p","最多选择 300 份课件；每份上传不超过 12 MB。将相关摘录与字幕发给当前模型生成笔记。扫描版 PDF 需先做文字识别。","compare-help"),state=el("p",undefined,"compare-help");state.setAttribute("role","status");
      const submit=el("button",current.record.stopped?"结合所选课件生成笔记":"保存本课课件","primary");submit.type="submit";
      const busy=()=>current.record.jobs?.some(j=>j.lane==="analysis"&&["pending","running"].includes(j.state));
      sourceSubmit=submit;sourceInput=input;submit.disabled=busy()||saving;input.disabled=busy()||saving;
      if(busy())state.textContent="笔记正在生成，完成后可重新选择课件。";
      form.append(choices,input,help,submit,state);
      form.onsubmit=async e=>{
        e.preventDefault();if(busy()||saving)return;saving=true;submit.disabled=true;input.disabled=true;
        try{
          const source_ids=[...choices.querySelectorAll("input:checked")].map(c=>c.value),files=[...input.files];
          if(source_ids.length+files.length>300)throw new Error("每次最多选择 300 份课件，请减少选择。");
          const attachment_ids=[];
          for(const file of files){
            if(file.size>12*1024*1024)throw new Error("单份课件请控制在 12 MB 内。");
            state.textContent="正在读取 "+file.name+"…";
            const content=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(",")[1]);r.onerror=()=>reject(new Error("课件读取失败"));r.readAsDataURL(file);});
            const attachment=await A.api("/api/study/attachments",{method:"POST",body:{name:file.name,content},timeout:60000});attachment_ids.push(attachment.id);
          }
          if(token!==epoch)return;
          await A.api(`/api/sessions/${current.record.id}/materials`,{method:"POST",body:{source_ids,attachment_ids}});
          if(token!==epoch)return;
          if(current.record.stopped)await A.api(`/api/sessions/${current.record.id}/analyze?restart=true`,{method:"POST"});
          if(token!==epoch)return;
          current.options.notice(current.record.stopped?"已安排结合课件生成笔记，完成后在这里按知识点对照。":"本课课件已保存，下课后会结合讲述整理笔记。");
          topicStamp="";await current.options.saved();
          if(token===epoch)await loadSources(form,token);
        }catch(error){if(token===epoch)state.textContent=error.message;}finally{if(token===epoch){saving=false;if(sourceSubmit)sourceSubmit.disabled=busy();if(sourceInput)sourceInput.disabled=busy();}}
      };
    }catch(e){if(token===epoch){form.replaceChildren(el("p",e.message));const retry=el("button","重新读取课件列表");retry.onclick=()=>loadSources(form,token);form.append(retry);}}
  }
  function mount(target,record,seek,options){
    const changed=current?.record.id!==record.id;
    current={record,seek,options};root=target;
    if(changed){
      epoch++;index=0;topicStamp="";sourceSubmit=null;sourceInput=null;saving=false;revoke();root.replaceChildren();
      const intro=el("p","下方按知识点对照课件、老师原话与整理后的笔记。","overview-intro"),sources=el("details",undefined,"compare-sources"),form=el("form");
      sources.append(el("summary","调整本课笔记使用的课件（可选）"),form);sources.open=false;
      const nav=el("div",undefined,"compare-topic-nav");body=el("div");root.append(intro,sources,nav,body);
      void loadSources(form,epoch);
    }
    const busy=record.jobs?.some(j=>j.lane==="analysis"&&["pending","running"].includes(j.state));
    if(sourceSubmit)sourceSubmit.disabled=!!busy||saving;
    if(sourceInput)sourceInput.disabled=!!busy||saving;
    const stamp=JSON.stringify([record.summary,record.segments,record.analysis_status]);
    if(stamp!==topicStamp){topicStamp=stamp;topicControls();paint();}
  }
  return {mount,reset,select,rows,markdown,resizeWidths};
})();
