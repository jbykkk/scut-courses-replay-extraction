/* Read-only, bounded DOM adapter for the school's timed PPT screenshots. */
function collectScutVisuals(expectedURL) {
  const expected=new URL(expectedURL), current=new URL(location.href);
  const same=expected.origin===current.origin&&current.origin==="https://video.jw.scut.edu.cn"&&current.pathname==="/livingroom"&&
    ["course_id","sub_id","tenant_code"].every(k=>(expected.searchParams.get(k)||"")===(current.searchParams.get(k)||""));
  if(!same)return {error:"学校播放页已切换，请重新选择这节课"};
  const items=[],seen=new Set();
  for(const row of document.querySelectorAll(".ppt_wrap")){
    const label=row.querySelector(".ppt_time")?.textContent?.trim()||"";
    if(!/^\d{1,2}:\d{2}(?::\d{2})?$/.test(label))continue;
    const parts=label.split(":").map(Number);
    if(parts.slice(1).some(n=>n>59))continue;
    const seconds=parts.reduce((a,b)=>a*60+b,0),img=row.querySelector("img");
    const raw=img?.getAttribute("data-src")||img?.getAttribute("src");
    if(!raw)continue;
    let url;try{url=new URL(raw,current.origin);}catch{continue;}
    if(url.origin!==current.origin||url.search||url.hash||!/^\/play\/ai\d+\/ppt\/[\w/-]+\.jpe?g$/.test(url.pathname)||seconds>172800||seen.has(url.href))continue;
    seen.add(url.href);items.push({url:url.href,seconds});
    if(items.length===600)break;
  }
  if(!items.length)return {error:"请先在这节课的学校播放页打开 PPT 栏，再读取截图索引"};
  return {course_url:current.href,title:document.title.slice(0,200),items:items.sort((a,b)=>a.seconds-b.seconds)};
}
if(typeof module!=="undefined")module.exports={collectScutVisuals};
