/* Course-wide imports and page/subtitle navigation, independent of generated notes. */
globalThis.Courseware=(()=>{
  const A=globalThis.AssistantClient;
  let active=null,serial=0,dialog=null,courseInput=null,courseList=null,pageBrowser=null,browseEpoch=0,catalogueStamp="",uploadState=null,files=null,folder=null,busy=false,conflictPanel=null;
  const labels={queued:"等待匹配",running:"正在匹配",complete:"匹配完成",failed:"匹配中断",waiting_materials:"等待课件",waiting_subtitles:"等待字幕",unavailable:"记录不可用"};
  function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
  function position(material){const loc=material.locator||{};return material.title+" · 第 "+(loc.page||loc.slide)+ (loc.page?" 页":" 张");}
  function time(seconds){const n=Math.max(0,Math.floor(seconds||0));return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(x=>String(x).padStart(2,"0")).join(":");}
  function forSegment(rows,id){return rows.findIndex(row=>(row.segment_ids||[]).includes(id));}
  function forPage(rows,pageId){return rows.filter(row=>row.material?.id===pageId);}
  function locateSegment(rows,segments,id){const index=forSegment(rows,id);if(index>=0)return {rows,index};const segment=segments.find(s=>s.id===id);return segment?{rows:[...rows,{id:"unlinked:"+id,start:segment.start,end:segment.end,segment_ids:[id],status:"unmatched",material:null}],index:rows.length}:{rows,index:-1};}
  function reset(){serial++;active=null;}
  function selectSegment(id){if(!active)return;active.selectedSegment=id;if(!active.data)return;const found=locateSegment(active.data.rows,active.record.segments||[],id);if(found.index>=0){active.data.rows=found.rows;select(found.index);}else active.options.notice("这条字幕暂时不可用，请刷新后重试。");}
  function select(index){
    if(!active)return;active.index=index;active.selectedSegment=active.data?.rows[index]?.segment_ids?.[0]||active.selectedSegment;paint();
    const target=active.root.querySelector(".courseware-reader");target?.scrollIntoView({block:"start",behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"});
  }
  async function load(token){
    if(!active)return;const state=active,request=state.request=(state.request||0)+1;
    try{const data=await A.api(`/api/courseware/sessions/${state.record.id}`);if(token!==serial||active!==state||request!==state.request)return;state.data=data;
      if(state.selectedSegment){const found=locateSegment(data.rows,state.record.segments||[],state.selectedSegment);data.rows=found.rows;if(found.index>=0)state.index=found.index;}paint();
    }catch(e){if(token===serial&&request===state.request){state.data={status:"failed",error:e.message,rows:[]};paint();}}
  }
  function paint(){
    if(!active)return;const {root,record,data}=active;root.replaceChildren();
    const head=el("div",undefined,"courseware-heading"),intro=el("div"),manage=el("button","管理整门课课件");
    intro.append(el("h3","课件与字幕时间轴"),el("p","一次上传整门课课件，保存字幕后自动对应。","muted"));manage.onclick=()=>open(record.course_title);head.append(intro,manage);root.append(head);
    if(!data){root.append(el("p","正在读取课件对应关系…","muted"));return;}
    const matched=data.rows.filter(r=>r.material).length,status=el("p",(labels[data.status]||data.status)+(data.rows.length?` · ${matched}/${data.rows.length} 段找到候选页`:"")+" · "+(data.progress||data.notice||""),"courseware-status");status.setAttribute("role","status");root.append(status);
    if(data.error)root.append(el("p",data.error,"notice error"));
    if(data.status==="failed"){const retry=el("button","重试课件匹配");retry.onclick=async()=>{retry.disabled=true;try{await A.api("/api/courseware/retry?"+new URLSearchParams({course:record.course_title}),{method:"POST"});await active.options.saved();await load(serial);}catch(e){active?.options.notice(e.message);}finally{retry.disabled=false;}};root.append(retry);}
    if(!data.rows.length){root.append(el("p",data.status==="complete"?"尚未找到可对应的字幕片段。可检查课件是否包含可提取的文字。":"可先上传课件；匹配在后台完成，不需要先生成笔记。","compare-help"));return;}
    active.index=Math.max(0,Math.min(active.index,data.rows.length-1));const current=data.rows[active.index];
    const nav=el("div",undefined,"courseware-nav"),picker=el("select");picker.setAttribute("aria-label","按字幕时间选择课件对应段落");
    data.rows.forEach((row,i)=>{const option=el("option",time(row.start)+" · "+(row.material?position(row.material):"老师补充 / 未对应"));option.value=i;picker.append(option);});picker.value=active.index;picker.onchange=()=>select(Number(picker.value));
    const prev=el("button","← 上一段"),next=el("button","下一段 →");prev.disabled=active.index===0;next.disabled=active.index===data.rows.length-1;prev.onclick=()=>select(active.index-1);next.onclick=()=>select(active.index+1);nav.append(prev,picker,next);root.append(nav);
    const reader=el("div",undefined,"courseware-reader"),page=el("section"),speech=el("section");
    page.append(el("h4","课件页"));speech.append(el("h4","对应字幕"));
    if(current.material){
      const material=current.material;page.append(el("strong",position(material)),el("span",current.status==="matched"?"语义对应":"待核对","courseware-badge"));
      const text=el("div",material.text,"courseware-page-text");page.append(text);
      if(/\.pdf$/i.test(material.title)){
        const preview=el("button","查看 PDF 原页"),slot=el("div",undefined,"compare-preview");page.append(preview,slot);
        preview.onclick=async()=>{const token=serial,index=active.index;preview.disabled=true;let url;
          try{const blob=await A.api(`/api/sessions/${record.id}/materials/${material.source_id}/pages/${material.locator.page}`,{blob:true});if(token!==serial||active.index!==index)return;
            url=URL.createObjectURL(blob);const image=el("img");image.alt=position(material);image.src=url;image.onload=()=>URL.revokeObjectURL(url);image.onerror=()=>URL.revokeObjectURL(url);slot.replaceChildren(image);preview.hidden=true;
          }catch(e){if(token===serial)active.options.notice(e.message);}finally{preview.disabled=false;}};
      }
      const related=forPage(data.rows,material.id);page.append(el("h4","这页的讲解位置"));
      const times=el("div",undefined,"courseware-times");for(const row of related){const button=el("button",time(row.start)+"–"+time(row.end));button.setAttribute("aria-pressed",String(row.id===current.id));button.onclick=()=>select(data.rows.indexOf(row));times.append(button);}page.append(times);
    }else page.append(el("p","本段未找到足够可靠的对应页，可能是补充讲解、课堂安排或课件缺少文字。","muted"));
    speech.append(el("small",record.time_basis==="capture"?"时间从本次录音开始":"点击时间回看原视频；匹配范围按字幕估计"));
    const ids=new Set(current.segment_ids);for(const segment of record.segments||[]){if(!ids.has(segment.id))continue;const line=el("div",undefined,"compare-speech");line.append(active.seek(segment.start),el("blockquote",globalThis.SessionView.clean(segment)));speech.append(line);}
    reader.append(page,speech);root.append(reader);
  }
  function mount(root,record,seek,options){
    const changed=active?.record.id!==record.id,stamp=JSON.stringify([record.alignment_generation,record.alignment_status,record.alignment_error,record.alignment_progress]);
    if(changed){serial++;active={root,record,seek,options,index:0,stamp};paint();void load(serial);}
    else{const stale=active.record.alignment_generation!==record.alignment_generation;active.root=root;active.record=record;active.seek=seek;active.options=options;if(stamp!==active.stamp){active.stamp=stamp;if(stale){active.data=null;paint();}void load(serial);}}
  }
  function makeDialog(){
    dialog=el("dialog",undefined,"courseware-dialog");dialog.setAttribute("aria-labelledby","courseware-title");const header=el("header"),title=el("h2","整门课课件"),close=el("button","×");title.id="courseware-title";close.setAttribute("aria-label","关闭课件管理");close.onclick=()=>dialog.close();header.append(title,close);
    const label=el("label","课程名称"),list=el("datalist");list.id="courseware-courses";courseInput=el("input");courseInput.setAttribute("list",list.id);courseInput.maxLength=160;courseInput.placeholder="选择已有课程，或填写课程名称";courseInput.onchange=()=>{browseEpoch++;pageBrowser.replaceChildren();void renderCatalogue().catch(e=>uploadState.textContent=e.message);};label.append(courseInput);
    const help=el("p","课件保存在本机。每份不超过 12 MB，支持 PDF、PPTX；扫描版请先做文字识别。相关课件摘录与字幕会发送给当前学习模型。","compare-help");
    const actions=el("div",undefined,"courseware-times"),upload=el("button","批量选择课件"),directory=el("button","选择课件文件夹"),retry=el("button","重新匹配本课程");
    files=el("input");files.type="file";files.accept=".pdf,.pptx";files.multiple=true;files.hidden=true;files.onchange=()=>void uploadFiles([...files.files]);
    folder=el("input");folder.type="file";folder.multiple=true;folder.webkitdirectory=true;folder.hidden=true;folder.onchange=()=>void uploadFiles([...folder.files]);
    upload.onclick=()=>files.click();directory.onclick=()=>folder.click();retry.onclick=async()=>{if(busy)return;try{await A.api("/api/courseware/retry?"+new URLSearchParams({course:courseInput.value.trim()}),{method:"POST"});await renderCatalogue();}catch(e){uploadState.textContent=e.message;}};actions.append(upload,directory,retry);
    uploadState=el("p",undefined,"courseware-status");uploadState.setAttribute("role","status");conflictPanel=el("section",undefined,"courseware-conflicts");conflictPanel.hidden=true;courseList=el("div");pageBrowser=el("section",undefined,"courseware-page-browser");dialog.append(header,label,list,help,actions,files,folder,uploadState,conflictPanel,courseList,pageBrowser);document.body.append(dialog);
    setInterval(()=>{if(dialog.open&&!busy)void renderCatalogue().catch(e=>uploadState.textContent=e.message);},3000);
  }
  async function open(course=""){
    if(!dialog)makeDialog();if(busy){if(!dialog.open)dialog.showModal();return;}courseInput.value=course||courseInput.value;if(!dialog.open)dialog.showModal();
    browseEpoch++;pageBrowser.replaceChildren();
    try{const data=await A.api("/api/courseware/courses");const list=dialog.querySelector("datalist");list.replaceChildren();for(const name of data.courses){const o=el("option");o.value=name;list.append(o);}if(!courseInput.value&&data.courses.length===1)courseInput.value=data.courses[0];await renderCatalogue();}catch(e){uploadState.textContent=e.message;}
  }
  async function renderCatalogue(){
    const course=courseInput.value.trim();if(!course){courseList.replaceChildren(el("p","先选择或填写课程名称。"));return;}
    const data=await A.api("/api/courseware?"+new URLSearchParams({course}));if(courseInput.value.trim()!==course)return;const stamp=JSON.stringify(data);if(stamp===catalogueStamp)return;catalogueStamp=stamp;courseList.replaceChildren();
    courseList.append(el("h3",`课件顺序 · ${data.sources.length} 份`),el("p","按讲课顺序排列文件，页内按原页码对齐。新字幕自动匹配，新课件自动补齐已有课时。","compare-help"));
    data.sources.forEach((source,index)=>{const row=el("div",undefined,"courseware-file"),name=el("button",`${index+1}. ${source.title} · 浏览页码与讲解`);name.className="courseware-browse";name.onclick=()=>void browsePages(course,source).catch(e=>uploadState.textContent=e.message);row.append(name);
      for(const [delta,label] of [[-1,"上移"],[1,"下移"]]){const move=el("button",label);move.disabled=busy||index+delta<0||index+delta>=data.sources.length;move.onclick=async()=>{move.disabled=true;try{const ids=data.sources.map(s=>s.id);[ids[index],ids[index+delta]]=[ids[index+delta],ids[index]];await A.api("/api/courseware/order",{method:"POST",body:{course,source_ids:ids}});await renderCatalogue();if(active)await active.options.saved();}catch(e){uploadState.textContent=e.message;move.disabled=false;}};row.append(move);}courseList.append(row);});
    if(!data.sources.length)courseList.append(el("p","还没有课件，可以一次选择多个文件或整个文件夹。","muted"));
    courseList.append(el("h3",`字幕对应进度 · ${data.sessions.length} 节课`));for(const session of data.sessions){const row=el("div",undefined,"courseware-file"),link=el("a",session.title);link.href="dashboard.html?"+new URLSearchParams({session:session.id,view:"compare"});row.append(link,el("span",labels[session.status]||session.status));if(session.error)row.append(el("small",session.error));courseList.append(row);}
  }
  async function browsePages(course,source){
    const token=++browseEpoch;pageBrowser.replaceChildren(el("p","正在读取课件页…"));
    const data=await A.api("/api/courseware/pages?"+new URLSearchParams({course,source_id:source.id}));if(token!==browseEpoch)return;
    const picker=el("select"),body=el("div");picker.setAttribute("aria-label","选择课件页码");
    for(const page of data.pages){const option=el("option",position(page));option.value=page.locator.page||page.locator.slide;picker.append(option);}pageBrowser.replaceChildren(el("h3",source.title),picker,body);
    let request=0;const show=async()=>{const pageRequest=++request;body.replaceChildren(el("p","正在查找这页的课堂讲解…"));
      try{const result=await A.api("/api/courseware/mentions?"+new URLSearchParams({course,source_id:source.id,page:picker.value}));if(token!==browseEpoch||pageRequest!==request)return;
        body.replaceChildren(el("div",result.page.text,"courseware-page-text"),el("h4","整门课中对应的讲解"));
        for(const mention of result.mentions){const line=el("p"),link=el("a",mention.title+" · "+time(mention.start)+"–"+time(mention.end)+" ↗");link.href="dashboard.html?"+new URLSearchParams({session:mention.session_id,view:"compare",segment:mention.segment_id});link.target="_blank";link.rel="noopener noreferrer";line.append(link,el("small"," · "+(mention.time_basis==="capture"?"录音时间":"回放时间")+(mention.status==="needs_check"?" · 待核对":"")));body.append(line);}
        if(!result.mentions.length)body.append(el("p","这页暂时没有已完成的讲解对应；匹配完成后可重新查看。","muted"));
      }catch(e){if(token===browseEpoch&&pageRequest===request)body.replaceChildren(el("p",e.message));}};
    picker.onchange=()=>void show();if(data.pages.length)await show();else body.append(el("p","这份课件未提取到可读页面。"));
  }
  async function chooseReplacements(items){
    const conflicts=items.filter(item=>!item.duplicate_id&&item.conflicts.length);
    if(!conflicts.length)return {};
    conflictPanel.replaceChildren(el("h3","发现同名课件"),el("p","默认保留两份。只有选择“替换”才会更新旧课件，旧引用仍可回查。","compare-help"));
    const choices=[];
    for(const item of conflicts){
      const label=el("label",item.name),select=el("select"),keep=el("option","保留两份，作为另一份课件");keep.value="";select.append(keep);
      select.setAttribute("aria-label",item.name+"的同名文件处理方式");
      for(const source of item.conflicts){const option=el("option","替换为新版："+source.title);option.value=source.id;select.append(option);}
      select.value="";label.append(select);conflictPanel.append(label);choices.push({id:item.attachment_id,select});
    }
    const actions=el("div",undefined,"courseware-times"),submit=el("button","按所选方式导入"),cancel=el("button","取消本次导入");submit.className="primary";actions.append(submit,cancel);conflictPanel.append(actions);conflictPanel.hidden=false;
    return new Promise(resolve=>{
      const finish=value=>{dialog.removeEventListener("close",closed);conflictPanel.hidden=true;conflictPanel.replaceChildren();resolve(value);};
      const closed=()=>finish(null);dialog.addEventListener("close",closed,{once:true});
      cancel.onclick=()=>finish(null);
      submit.onclick=()=>{
        const selected=choices.filter(choice=>choice.select.value),targets=selected.map(choice=>choice.select.value);
        if(new Set(targets).size!==targets.length){uploadState.textContent="同一份旧课件只能替换一次，请调整选择。";return;}
        finish(Object.fromEntries(selected.map(choice=>[choice.id,choice.select.value])));
      };
    });
  }
  async function uploadFiles(selected){
    const course=courseInput.value.trim();if(busy||!selected.length)return;if(!course){uploadState.textContent="先选择或填写课程名称，再上传课件。";return;}
    const readable=selected.filter(f=>/\.(pdf|pptx)$/i.test(f.name));if(!readable.length){uploadState.textContent="所选文件中没有 PDF 或 PPTX。";return;}
    if(readable.length>300){uploadState.textContent="一次最多导入 300 份课件。";return;}
    busy=true;courseInput.disabled=true;for(const button of courseList.querySelectorAll("button"))button.disabled=true;const attachments=[],failed=[];
    try{for(const [index,file] of readable.entries()){
        uploadState.textContent=`正在读取 ${index+1}/${readable.length}：${file.name}`;
        try{if(file.size>12*1024*1024)throw new Error("超过 12 MB");const content=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(",")[1]);r.onerror=()=>reject(new Error("文件读取失败"));r.readAsDataURL(file);});
          const value=await A.api("/api/study/attachments",{method:"POST",body:{name:file.name,content},timeout:60000});attachments.push(value.id);
        }catch(e){failed.push(file.name+"："+e.message);}}
      if(attachments.length){
        const body={course,attachment_ids:[...new Set(attachments)]};
        const preview=await A.api("/api/courseware/preview",{method:"POST",body,timeout:60000});
        const replacements=await chooseReplacements(preview.items);
        if(replacements===null){uploadState.textContent="已取消本次导入，课程课件保持原样。";return;}
        await A.api("/api/courseware/import",{method:"POST",body:{...body,replacements},timeout:60000});
      }
      uploadState.textContent=`已导入 ${attachments.length} 份课件，已有字幕将在后台自动对应。`+(selected.length>readable.length?` 跳过 ${selected.length-readable.length} 个其他文件。`:"")+(failed.length?"\n未导入：\n"+failed.join("\n"):"");
      if(active){await active.options.saved();await load(serial);}
    }catch(e){uploadState.textContent="课件关联未完成："+e.message+"；可以重新选择文件重试，已上传文件会复用。";}
    finally{busy=false;courseInput.disabled=false;files.value="";folder.value="";catalogueStamp="";try{await renderCatalogue();}catch(e){uploadState.textContent+="\n"+e.message;}}
  }
  return {mount,reset,open,selectSegment,forSegment,forPage,locateSegment};
})();
