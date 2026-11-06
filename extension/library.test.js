const test=require("node:test"),assert=require("node:assert/strict"),L=require("./library-core.js");
const lesson={id:"a",course_id:"1",course_title:"课程",stopped:true,status:"complete",segment_count:1418,analysis_status:"failed",warning:"输出截断",summary:null};

test("lesson cards sort by class date and time even when imports arrive out of order or timestamps are missing",()=>{
  const epoch=value=>Date.parse(value+"+08:00")/1000;
  const records=[
    {id:"morning",title:"2026-11-02 8:00-09:40",created_at:epoch("2026-11-05T12:00:00")},
    {id:"scheduled",start_at:epoch("2026-11-04T08:00:00"),created_at:1},
    {id:"evening",title:"2026年11月2日 19:00-20:40",created_at:2},
    {id:"other-version",title:"课堂录音",created_at:3,versions:[{start_at:epoch("2026-11-03T10:00:00")}]},
    {id:"date-only",title:"2026-11-01",created_at:epoch("2026-11-06T12:00:00")},
    {id:"legacy",title:"旧记录",created_at:epoch("2026-10-30T12:00:00")},
    {id:"undated",start_at:"invalid",created_at:null},
  ];
  assert.deepEqual([...records].sort(L.compareLessons).map(s=>s.id),["scheduled","other-version","evening","morning","date-only","legacy","undated"]);
  assert.equal(records[0].id,"morning");
});

test("saved subtitles wait for automatic notes and retry with a bounded backoff",()=>{
  assert.deepEqual(L.action({...lesson,analysis_status:"idle"},true),{label:"等待自动生成笔记",disabled:true});
  assert.equal(L.action({...lesson,notes_retry_at:Date.now()/1000+60,notes_auto_attempts:1},true).label,"稍后自动重试");
  assert.equal(L.action({...lesson,notes_retry_at:Date.now()/1000+60,notes_auto_attempts:3},true).label,"继续生成笔记");
  assert.equal(L.action({...lesson,analysis_status:"waiting_key"},false).settings,true);
});

test("school subtitles, local ASR and capture clips share one lesson card",()=>{
  const rows=L.lessons([{...lesson,sub_id:"2",id:"a",coverage_seconds:100},{...lesson,sub_id:"2",id:"b",coverage_seconds:200},{...lesson,sub_id:"3",id:"c"}]);
  assert.equal(rows.length,2);assert.equal(rows[0].id,"b");assert.equal(rows[0].record_count,2);assert.equal(rows[0].versions.length,2);
});

test("grouping preserves event links from every source and prefers full lecture coverage",()=>{
  const partial={...lesson,id:"p",sub_id:"2",time_basis:"video",coverage_seconds:30,analysis_status:"complete",summary:{headline:"局部片段"},important_events:[{start:1,category:"assignment",message:"作业要求"}]};
  const full={...lesson,id:"f",sub_id:"2",time_basis:"video",coverage_seconds:9000,important_events:[{start:5,category:"schedule",message:"下周安排"}]};
  const rows=L.lessons([partial,full]);assert.equal(rows[0].id,"f");
  const events=L.groups(rows)[0].events;assert.equal(events.length,2);assert.equal(events.find(e=>e.category==="assignment").sid,"p");
});

test("failed and pending imports appear without subtitles and survive unrelated batches",()=>{
  const imports=[{course_id:"1",sub_id:"10",status:"failed",created_at:1},{course_id:"1",sub_id:"11",status:"pending",created_at:2}];
  let rows=L.importRows(imports);assert.equal(rows.length,2);assert.ok(rows.every(L.pending));assert.equal(L.groups(rows)[0].lessons.length,2);
  rows=L.importRows([...imports,{course_id:"1",sub_id:"10",status:"queued",created_at:3}]);
  assert.equal(rows.length,1);assert.equal(rows[0].sub_id,"11");
  assert.equal(L.pending({...lesson,status:"downloading",segment_count:0}),true);
});
test("a failed replay with a saved key offers resume instead of configuring key",()=>{
  assert.equal(L.action(lesson,true).label,"继续生成笔记");
  assert.equal(L.action(lesson,false).settings,true);
  assert.equal(L.needsNotes(lesson),true);
});
test("completed audio and incomplete notes have independent states",()=>{
  const partial={...lesson,summary:{partial:true,topics:[]},analysis_status:"running",analysis_progress:"3/21"};
  assert.equal(L.hasNotes(partial),false);assert.equal(L.action(partial,true).disabled,true);
  const done={...partial,analysis_status:"complete",summary:{partial:false,topics:[]}};
  assert.equal(L.action(done,true).label,"整理完整事项");assert.equal(L.action({...done,events_version:1},true).hidden,true);assert.equal(L.needsNotes(done),false);
});
test("an interrupted partial recording does not keep a fully noted lesson pending",()=>{
  const ready={...lesson,id:"school",sub_id:"15",time_basis:"video",coverage_seconds:7200,analysis_status:"complete",summary:{partial:false,topics:[]}};
  const interrupted={...lesson,id:"live",sub_id:"15",time_basis:"capture",coverage_seconds:1800,status:"interrupted",stopped:false,analysis_status:"complete",summary:null};
  const combined=L.lessons([ready,interrupted])[0];
  assert.equal(combined.id,"school");assert.equal(combined.lesson_pending,false);
});

test("a broader video result still returns a completed lesson to pending",()=>{
  const ready={...lesson,id:"old",sub_id:"16",time_basis:"video",coverage_seconds:1000,analysis_status:"complete",summary:{partial:false,topics:[]}};
  const broader={...lesson,id:"new",sub_id:"16",time_basis:"video",coverage_seconds:2000,status:"transcribing",stopped:true,analysis_status:"idle",summary:null};
  assert.equal(L.lessons([ready,broader])[0].lesson_pending,true);
});
test("course overview groups actual topics and preserves event provenance",()=>{
  const s={...lesson,analysis_status:"complete",summary:{topics:[{title:"进程同步"},{title:"进程同步"}]},important_events:[{category:"assignment",message:"周五提交"}]};
  const grouped=L.groups([s,{...s,id:"b",course_id:"2",course_title:"第二门课"}]);
  assert.equal(grouped.length,2);assert.deepEqual(grouped[0].topics,["进程同步"]);
  assert.equal(grouped[0].events[0].sid,"a");assert.equal(grouped[0].ready,1);
});

test("confirmed course events sort by lesson date then by position, with semantic assignments visible",()=>{
  const base={...lesson,course_id:"neuro",course_title:"神经科学",analysis_status:"complete",summary:{partial:false,topics:[]},time_basis:"video"};
  const older={...base,id:"sep-02",sub_id:"02",title:"9月2日",start_at:100,created_at:300,important_events:[{id:"old",category:"assignment",message:"下周提交记录",start:500}]};
  const recent={...base,id:"sep-20",sub_id:"20",title:"9月20日",start_at:200,created_at:201,important_events:[
    {id:"paper",category:"assignment",message:"阅读论文，写两页启发",start:630,end:650},
    {id:"quiz",category:"quiz",message:"课堂测试",start:1800,end:1810}]};
  const latest={...base,id:"sep-22",sub_id:"22",title:"9月22日",start_at:220,created_at:221,important_events:[{id:"qr",category:"qr",message:"扫码答题",start:20,end:30}]};
  const timeline=L.eventTimeline(L.groups(L.lessons([older,recent,latest])));
  assert.deepEqual(timeline.map(e=>e.id),["qr","quiz","paper","old"]);
  assert.equal(timeline.find(e=>e.id==="paper").lesson_at,200);
  assert.deepEqual(L.eventTimeline(L.groups(L.lessons([older,recent,latest])),"assignment").map(e=>e.id),["paper","old"]);
  assert.equal(L.eventCategories.some(c=>c.id==="qr"),true);
});
