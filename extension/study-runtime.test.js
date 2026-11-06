const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
function harness(){
  let document;const timers=new Map(),calls=[];let next=0,api;
  function node(tag='div'){
    const n={tagName:tag,id:'',children:[],dataset:{},attrs:{},textContent:'',open:false,hidden:false,scrollTop:0,handlers:{},classList:{toggle(){}},
      append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
      setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,f){(this.handlers[k]||=[]).push(f);},
      dispatch(k){for(const fn of this.handlers[k]||[])fn({});},showModal(){this.open=true;},close(){this.open=false;this.dispatch('close');},
      focus(){document.activeElement=this;}};
    Object.defineProperty(n,'innerHTML',{set(){throw new Error('unsafe HTML rendering');}});return n;
  }
  const walk=(n)=>[n,...n.children.flatMap(walk)];const body=node('body'),trigger=node('button');trigger.id='open-runtime';body.append(trigger);
  document={body,hidden:false,activeElement:null,handlers:{},createElement:node,getElementById:id=>walk(body).find(n=>n.id===id),
    addEventListener(k,f){(this.handlers[k]||=[]).push(f);},dispatchEvent(e){for(const fn of this.handlers[e.type]||[])fn(e);}};
  const run=(id,label)=>({id,label,course:'操作系统',kind:'chat',status:'running',started:100,updated:100,events:[]});
  const runs=[run('a','线程问题'),run('b','进程问题')];
  api=async route=>{if(route.includes('?limit'))return {items:runs};if(route.endsWith('/catalog'))return {prompts:[],purpose_rules:{},tools:[],retrieval_tools:[],budgets:{},storage:{}};
    const url=new URL('http://local'+route),id=url.pathname.split('/').at(-1),after=Number(url.searchParams.get('after'));
    const selected=runs.find(r=>r.id===id);return {...selected,events:selected.events.filter(e=>e.seq>after)};};
  const window={},context={window,document,URL,Date,CustomEvent:class{constructor(type,opts={}){this.type=type;this.detail=opts.detail;}},StudyTools:{showEvidence(){}},
    AssistantClient:{api(route){calls.push(route);return api(route);}},setTimeout(fn){timers.set(++next,fn);return next;},clearTimeout(id){timers.delete(id);}};
  vm.runInNewContext(fs.readFileSync(require.resolve('./study-runtime.js'),'utf8'),context);
  return {window,document,runs,calls,node:id=>document.getElementById(id),walk:()=>walk(body),
    setApi(fn){const old=api;api=(...args)=>fn(old,...args);},
    async tick(){const item=timers.entries().next().value;if(item){timers.delete(item[0]);item[1]();}await settle();}};
}
test('runtime inspector polls only while open and visible',async()=>{
  const h=harness();await h.tick();assert.equal(h.calls.length,0);
  h.window.StudyRuntime.open();await h.tick();assert.equal(h.calls.length,2);
  h.node('runtime-dialog').close();await h.tick();assert.equal(h.calls.length,2);
  h.document.hidden=true;h.window.StudyRuntime.open();await h.tick();assert.equal(h.calls.length,2);
  h.document.hidden=false;h.document.dispatchEvent({type:'visibilitychange'});await h.tick();assert.equal(h.calls.length,4);
});
test('late execution responses cannot replace a newly chosen run',async()=>{
  const h=harness();let resolve;
  h.setApi(async(base,route)=>route.includes('/a?')?new Promise(r=>{resolve=r;}):base(route));
  h.window.StudyRuntime.open('a');await h.tick();assert.equal(typeof resolve,'function');
  h.window.StudyRuntime.open('b');resolve({...h.runs[0],events:[]});await settle();await h.tick();
  const texts=h.walk().map(n=>n.textContent);assert.ok(texts.includes('进程问题'));
  assert.equal(h.node('runtime-summary').children[0].children[1].textContent,'进程问题');
});
test('incremental events preserve open prompt details and render model text safely',async()=>{
  const h=harness(),text='<img src=x onerror=alert(1)> literal';
  h.runs[0].events=[{seq:1,at:101,kind:'model.start',label:'请求模型',data:{model:'test',phase:'answer',input_characters:100,max_output_tokens:2400,request:{messages:[{role:'system',content:text}]}}}];
  h.window.StudyRuntime.open('a');await h.tick();
  const details=h.walk().find(n=>n.tagName==='details');details.open=true;
  h.runs[0].events.push({seq:2,at:102,kind:'model.finish',label:'已返回',data:{duration_ms:1000,usage:{input_tokens:20,output_tokens:5,cached_input_tokens:0},response:{content:'完成'}}});
  await h.tick();assert.ok(h.calls.some(p=>p.endsWith('?after=1')));assert.equal(h.node('runtime-timeline').children.length,2);
  assert.ok(h.walk().includes(details));assert.equal(details.open,true);assert.ok(h.walk().some(n=>n.tagName==='pre'&&n.textContent===text));
});
test('closing the inspector discards an in-flight update',async()=>{
  const h=harness();let resolve;h.setApi(async(base,route)=>route.includes('?limit')?new Promise(r=>{resolve=r;}):base(route));
  h.window.StudyRuntime.open();await h.tick();h.node('runtime-dialog').close();resolve({items:h.runs});await settle();
  assert.equal(h.node('runtime-summary'),undefined);const count=h.calls.length;await h.tick();assert.equal(h.calls.length,count);
});
