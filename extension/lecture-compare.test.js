const test=require('node:test'),assert=require('node:assert/strict');
require('./lecture-compare.js');

test('moving either divider preserves the third column and keeps every pane readable',()=>{
  const equal=[1/3,1/3,1/3],close=(a,b)=>assert.ok(Math.abs(a-b)<1e-10);
  for(const edge of [0,1])for(const delta of [-10,-.1,.1,10]){
    const result=LectureCompare.resizeWidths(equal,edge,delta,.15);
    close(result.reduce((a,b)=>a+b,0),1);
    close(result[edge===0?2:0],equal[edge===0?2:0]);
    assert.ok(result.every(n=>n>=.15));
    assert.ok(delta>0?result[edge]>equal[edge]:result[edge]<equal[edge]);
  }
  assert.deepEqual(equal,[1/3,1/3,1/3]);
  assert.deepEqual(LectureCompare.resizeWidths(null,0,0),equal);
  assert.deepEqual(LectureCompare.resizeWidths([0,-1,2],0,0),equal);
});
test('comparison follows cited speech IDs and preserves exact material page and quote',()=>{
  const speech=[{id:'s1',text:'老师原话',start:50},{id:'s2',text:'另一个知识点',start:90}];
  const ref={title:'课件.pdf',locator:{page:7},quote:'课件原文'};
  const result=LectureCompare.rows({segments:speech,summary:{topics:[{title:'知识点',segment_ids:['s1'],material_refs:[ref]},{title:'补充',segment_ids:['missing','s2']}]}});
  assert.deepEqual(result[0].speech,[speech[0]]);assert.deepEqual(result[0].materials,[ref]);
  assert.deepEqual(result[1].speech,[speech[1]]);assert.deepEqual(result[1].materials,[]);
  assert.deepEqual(LectureCompare.rows({}),[]);
});

test('review sheet renders hierarchy, bilingual emphasis and full text without executing HTML',()=>{
  class Node{
    constructor(tagName,text=''){this.tagName=tagName;this.value=text;this.children=[];}
    append(...nodes){this.children.push(...nodes);}
    get textContent(){return this.value+this.children.map(n=>n.textContent).join('');}
    set textContent(value){this.value=String(value);this.children=[];}
  }
  const previous=globalThis.document;
  globalThis.document={createElement:tag=>new Node(tag),createTextNode:text=>new Node('#text',text)};
  try{
    const ending='完整解释。'.repeat(1000)+'END',
      text='# 复习讲义 (Review Sheet)\n\n## 条件变量\n\n**定义 (Definition)**：_等待条件_。\n\n1. 持有互斥锁\n   - 检查条件\n2. 使用 `while`\n\n```c\nwhile (!ready) wait();\n```\n\n<img src=x onerror=alert(1)>\n\n'+ending,
      rendered=LectureCompare.markdown(text),walk=n=>[n,...n.children.flatMap(walk)],nodes=walk(rendered);
    assert.equal(nodes.find(n=>n.tagName==='h1').textContent,'复习讲义 (Review Sheet)');
    assert.equal(nodes.find(n=>n.tagName==='h2').textContent,'条件变量');
    assert.equal(nodes.find(n=>n.tagName==='strong').textContent,'定义 (Definition)');
    assert.equal(nodes.find(n=>n.tagName==='em').textContent,'等待条件');
    const list=nodes.find(n=>n.tagName==='ol');assert.equal(list.children.length,2);
    assert.equal(list.children[0].children.find(n=>n.tagName==='ul').textContent,'检查条件');
    assert.equal(nodes.find(n=>n.tagName==='pre').textContent,'while (!ready) wait();');
    assert.equal(nodes.filter(n=>['img','script','a'].includes(n.tagName)).length,0);
    assert.ok(rendered.textContent.includes('<img src=x onerror=alert(1)>'));
    assert.ok(rendered.textContent.endsWith(ending));
  }finally{if(previous===undefined)delete globalThis.document;else globalThis.document=previous;}
});
