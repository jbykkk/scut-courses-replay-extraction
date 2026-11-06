/* Authenticated local jobs -> fixed school reads, using the existing replay pipeline. */
globalThis.ScutAgentBridge = (() => {
  let running=false;
  const inspections=new Map();
  function safeSchoolURL(value){
    try{const u=new URL(value);return u.protocol==="https:"&&!u.username&&!u.password&&!u.port&&["video.jw.scut.edu.cn","video-jw-443.webvpn.scut.edu.cn"].includes(u.hostname)&&(u.pathname==="/"||!!ScutSubtitleCore.parseCourseUrl(value));}catch{return false;}
  }
  async function poll({client,processLesson,busy}) {
    if(running)return;
    running=true;
    try {
      const saved=await chrome.storage.local.get("studyBridgeClient");
      const owner=saved.studyBridgeClient||crypto.randomUUID();
      if(!saved.studyBridgeClient)await chrome.storage.local.set({studyBridgeClient:owner});
      const tabs=await chrome.tabs.query({url:["https://video.jw.scut.edu.cn/*","https://video-jw-443.webvpn.scut.edu.cn/*"]});
      for(const id of inspections.keys())if(!tabs.some(t=>t.id===id))inspections.delete(id);
      const selected=tabs.filter(t=>ScutSubtitleCore.parseCourseUrl(t.url||"")).slice(0,12);
      const checked=await Promise.allSettled(selected.map(async tab=>{
        const cached=inspections.get(tab.id);
        if(cached&&cached.url===tab.url&&Date.now()-cached.at<25000)return cached.view;
        try{const view=await SchoolAPI.inspect(tab.id,{includeCatalogue:false});
          if(!view.status||view.status==="undefined"||!view.lesson?.course_title||view.lesson.course_title==="课程")throw new Error("学校未返回有效课程信息");
          inspections.set(tab.id,{url:tab.url,at:Date.now(),view});return view;}
        catch(e){inspections.delete(tab.id);throw e;}
      }));
      const views=checked.filter(r=>r.status==="fulfilled").map(r=>r.value),unreadable=checked.length-views.length;
      await client.api("/api/desktop/heartbeat",{method:"POST",body:{client:owner,version:chrome.runtime.getManifest().version,courses:[...new Set(views.map(v=>v.lesson.course_title))],school_tabs:tabs.length,unreadable_tabs:unreadable},timeout:3000});
      const requests=await client.api("/api/desktop/browser-requests",{timeout:3000});
      for(const req of requests){
        if(!safeSchoolURL(req.url))continue;
        try{
          await client.api(`/api/desktop/browser-requests/${req.id}/claim`,{method:"POST",body:{client:owner}});
          const existing=tabs.find(t=>t.url===req.url);
          if(existing){await chrome.tabs.update(existing.id,{active:true});await chrome.windows.update(existing.windowId,{focused:true});}
          else await chrome.tabs.create({url:req.url});
        }catch{/* Another browser may already have opened this one request. */}
      }
      if(busy())return;
      const jobs=await client.api("/api/study/browser/work",{timeout:3000});
      if(!Array.isArray(jobs)||!jobs.length)return;
      const post=(path,body)=>client.api("/api/study/browser/"+path,{method:"POST",body});
      for(const job of jobs.slice(0,3)) {
        const match=views.find(view=>view.lesson.course_title===job.course);
        if(!match)continue;
        let claim;
        try {claim=await post(job.id+"/claim",{client:owner});}catch{continue;}
        const catalogue=ScutStudy.catalogue(await SchoolAPI.get(match.context,"catalogue"));
        if(catalogue.length>1000)throw new Error("课程目录超过 1000 节，请分批选择课时");
        let current=await post(job.id+"/catalogue",{client:owner,course_id:match.context.courseId,course_title:match.lesson.course_title,page_url:match.context.url,items:catalogue});
        // Bounded work per alarm; every submitted item is checkpointed server-side.
        for(const item of current.items.filter(x=>x.status==="pending").slice(0,6)) {
          if(busy())break;
          try {
            claim=await post(job.id+"/claim",{client:owner});
            if(claim.status==="cancelled")break;
            let session;
            if(item.action==="analyze")session=await client.api(`/api/sessions/${item.sid}/analyze`,{method:"POST"});
            else if(item.action==="retry")session=await client.api(`/api/sessions/${item.sid}/retry`,{method:"POST"});
            else session=await processLesson(match.context,item.sub_id,true,false,job.id+"-"+item.sub_id,async()=>{});
            await post(job.id+"/result",{client:owner,sub_id:item.sub_id,status:"queued",sid:session.id||item.sid,error:""});
          } catch(error) {
            await post(job.id+"/result",{client:owner,sub_id:item.sub_id,status:"failed",error:String(error.message||error).slice(0,300)}).catch(()=>{});
          }
        }
      }
    } finally {running=false;}
  }
  return {poll,safeSchoolURL};
})();
