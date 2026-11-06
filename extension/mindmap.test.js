const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const context={TextEncoder,TextDecoder,URL,setTimeout,clearTimeout,console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve("./markmap-vendor.min.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(require.resolve("./mindmap.js"),"utf8"),context);
const prepare=text=>context.LectureMindmap.prepare(text);
function flatten(root){return [root.content,...(root.children||[]).flatMap(flatten)].join("\n");}

test("real Markmap transformer retains hierarchy and only activates content-addressed evidence links",()=>{
  const key="a".repeat(20),tree=prepare(`# Course\n\n## Topic\n\n- **Point**\n- [Evidence](#lecture-topic-${key})`),html=flatten(tree);
  assert.match(tree.content,/Course/);assert.match(tree.children[0].content,/Topic/);
  assert.match(html,/<strong>Point<\/strong>/);assert.match(html,new RegExp(`href="#lecture-topic-${key}"`));
});

test("raw HTML, image requests, hostile links and Markdown resource directives stay inert",()=>{
  const tree=prepare('# Security\n\n- <img src="https://example.org/track" onerror="alert(1)">\n- ![alt](https://example.org/track.png)\n- [bad](javascript:alert(1))\n- [web](https://example.org)\n- [old](#lecture-topic-1)\n\n<script>alert(1)</script>\n\n<!-- markmap: { extraJs: ["https://example.org/evil.js"] } -->');
  const html=flatten(tree);
  assert.doesNotMatch(html,/<(?:img|script|iframe|style)\b/i);
  assert.doesNotMatch(html,/href\s*=/i);
  assert.match(html,/alt/);assert.match(html,/&lt;img/);
});

test("empty, oversized and excessively nested outlines are rejected before rendering",()=>{
  assert.throws(()=>prepare(" \n"),/不能为空/);
  assert.throws(()=>prepare("x".repeat(200001)),/过大/);
  assert.throws(()=>prepare("# Title\n"+" ".repeat(62)+"- deep"),/过深/);
  assert.throws(()=>prepare("# Title\n"+"- point\n".repeat(2001)),/节点过多/);
});
