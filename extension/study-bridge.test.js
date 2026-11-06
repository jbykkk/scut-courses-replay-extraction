const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
function setup({tabs=[],views={},jobs=[]}={}){
  const calls=[],opened=[];let request=[];
  const sandbox={URL,crypto:{randomUUID:()=>"owner"},chrome:{runtime:{getManifest:()=>({version:'0.13.0'})},storage:{local:{get:async()=>({studyBridgeClient:'owner'}),set:async()=>{}}},tabs:{query:async()=>tabs,create:async x=>opened.push(x),update:async()=>{}},windows:{update:async()=>{}}},SchoolAPI:{inspect:async id=>{if(!views[id])throw Error('login required');return views[id];}},ScutSubtitleCore:{parseCourseUrl:s=>/livingroom\?/.test(s)?{courseId:'1'}:null}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'study-bridge.js'),'utf8'),sandbox);
  const client={api:async(p,o)=>{calls.push([p,o]);if(p.endsWith('heartbeat'))return {};if(p.endsWith('browser-requests'))return request;if(p.endsWith('/work'))return jobs;if(p.endsWith('/claim'))return {};throw Error(p);}};
  return {api:sandbox.ScutAgentBridge,client,calls,opened,setRequest:r=>request=r};
}
test('idle extension reports heartbeat without any batch task',async()=>{const t=setup();await t.api.poll({client:t.client,busy:()=>true});assert.equal(t.calls[0][0],'/api/desktop/heartbeat');assert.equal(t.calls[0][1].body.courses.length,0);});
test('logged-out page is not reported as a readable course',async()=>{const t=setup({tabs:[{id:1,url:'https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=2'}]});await t.api.poll({client:t.client,busy:()=>false});assert.equal(t.calls[0][1].body.unreadable_tabs,1);assert.equal(t.calls[0][1].body.courses.length,0);});
test('only the fixed school origin can be opened by a handoff',async()=>{const t=setup();t.setRequest([{id:'x',url:'https://evil.test'},{id:'y',url:'https://video.jw.scut.edu.cn/'}]);await t.api.poll({client:t.client,busy:()=>true});assert.equal(t.opened.length,1);assert.equal(t.opened[0].url,'https://video.jw.scut.edu.cn/');assert.equal(t.api.safeSchoolURL('https://x@video.jw.scut.edu.cn/'),false);});

function harness({mismatch=false,claimError=false}={}) {
  const calls=[], imports=[], local={}, jobs=[{id:'job1',course:'操作系统',status:'waiting_browser',items:[]}];
  const items=[{sub_id:'1',status:'skipped'},{sub_id:'2',status:'pending',action:'analyze',sid:'a'.repeat(32)},
    {sub_id:'3',status:'pending',action:'import'},{sub_id:'4',status:'pending',action:'retry',sid:'b'.repeat(32)}];
  const context={crypto:require('node:crypto').webcrypto,URL,ScutSubtitleCore:{parseCourseUrl:s=>s.includes('livingroom')?{courseId:'5'}:null},chrome:{runtime:{getManifest:()=>({version:'0.13.0'})},storage:{local:{get:async()=>local,set:async v=>Object.assign(local,v)}},tabs:{query:async()=>[{id:91,url:'https://video.jw.scut.edu.cn/livingroom?course_id=5&sub_id=3'},{id:92,url:'https://video.jw.scut.edu.cn/livingroom?course_id=5&sub_id=3'}]}},
    SchoolAPI:{inspect:async id=>{if(id===91)throw Error('No tab with id');return {status:'6',lesson:{course_title:mismatch?'另一门课':'操作系统'},context:{courseId:'5',url:'https://video.jw.scut.edu.cn/livingroom?course_id=5&sub_id=3'}};},get:async()=>items},ScutStudy:{catalogue:x=>x}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/study-bridge.js','utf8'),context);
  const client={api:async(path,opts={})=>{calls.push({path,...opts});if(path.endsWith('/work'))return jobs;if(path.endsWith('/browser-requests'))return [];
    if(path.endsWith('/claim')){if(claimError)throw Error('leased');return {status:'running'};}
    if(path.endsWith('/catalogue'))return {items};
    if(path.includes('/api/sessions/'))return {id:path.split('/')[3]};
    if(path.endsWith('/result'))items.find(x=>x.sub_id===opts.body.sub_id).status=opts.body.status;
    return {};}};
  const options={client,busy:()=>false,processLesson:async(...args)=>{imports.push(args);return {id:'c'.repeat(32)};}};
  return {poll:()=>context.ScutAgentBridge.poll(options),calls,imports,options,items};
}
test('bridge recovers a closed tab, skips complete lessons and uses existing analysis/retry endpoints',async()=>{
  const h=harness();await h.poll();assert.equal(h.imports.length,1);assert.equal(h.imports[0][1],'3');assert.equal(h.imports[0][4],'job1-3');
  assert.ok(h.calls.some(c=>c.path.endsWith('/'+ 'a'.repeat(32)+'/analyze')));
  assert.ok(h.calls.some(c=>c.path.endsWith('/'+ 'b'.repeat(32)+'/retry')));
  assert.equal(h.calls.filter(c=>c.path.endsWith('/result')).length,3);
  await h.poll();assert.equal(h.imports.length,1,'checkpointed lessons must not be submitted twice');
});
test('bridge never substitutes a different course or races another browser lease',async()=>{
  for(const opts of [{mismatch:true},{claimError:true}]){const h=harness(opts);await h.poll();assert.equal(h.imports.length,0);assert.ok(!h.calls.some(c=>c.path.endsWith('/catalogue')));}
});
test('manual batch and cancellation stop further automatic submissions',async()=>{
  const h=harness();h.options.busy=()=>true;await h.poll();assert.ok(!h.calls.some(c=>c.path.includes('/api/study/')));
  h.options.busy=()=>false;const original=h.options.client.api;
  let leases=0;h.options.client.api=async(p,o)=>{if(p.endsWith('/claim')&&++leases>1)return {status:'cancelled'};return original(p,o);};
  await h.poll();assert.equal(h.imports.length,0);assert.ok(!h.calls.some(c=>c.path.startsWith('/api/sessions/')));
});

test("empty successful school response is not a readable course",async()=>{const t=setup({tabs:[{id:1,url:"https://video.jw.scut.edu.cn/livingroom?course_id=1&sub_id=2"}],views:{1:{status:"undefined",lesson:{course_title:"课程"}}}});await t.api.poll({client:t.client,busy:()=>true});assert.equal(t.calls[0][1].body.courses.length,0);assert.equal(t.calls[0][1].body.unreadable_tabs,1);});
