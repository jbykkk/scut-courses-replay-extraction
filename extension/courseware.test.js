const test=require('node:test'),assert=require('node:assert/strict');
require('./courseware.js');

test('subtitle to page navigation uses original segment IDs, including multiple subtitles per page',()=>{
  const rows=[{id:0,segment_ids:['s1','s2'],material:{id:'source:page:7'}},{id:1,segment_ids:['s3'],material:null},{id:2,segment_ids:['s4'],material:{id:'source:page:7'}}];
  assert.equal(Courseware.forSegment(rows,'s2'),0);
  assert.equal(Courseware.forSegment(rows,'s3'),1);
  assert.equal(Courseware.forSegment(rows,'missing'),-1);
  assert.deepEqual(Courseware.forPage(rows,'source:page:7').map(r=>r.id),[0,2]);
  assert.deepEqual(Courseware.forPage(rows,'other:page:7'),[]);
});

test('a subtitle omitted from alignment opens its own unmatched span instead of the previous page',()=>{
  const rows=[{segment_ids:['s1'],material:{id:'page1'}}],segments=[{id:'uncertain',start:70,end:90,text:'需核对',quality:'uncertain'}];
  const found=Courseware.locateSegment(rows,segments,'uncertain');
  assert.equal(found.index,1);assert.equal(found.rows[1].material,null);
  assert.deepEqual(found.rows[1].segment_ids,['uncertain']);assert.equal(found.rows[1].start,70);
  assert.equal(rows.length,1);assert.equal(Courseware.locateSegment(rows,segments,'missing').index,-1);
});
