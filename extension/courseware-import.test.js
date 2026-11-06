const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };

function harness({duplicate = false} = {}) {
  const imports = [], uploads = [];
  function all(root) { return [root, ...root.children.flatMap(all)]; }
  function element(tag = '') {
    return {tag, children: [], value: '', textContent: '', dataset: {}, listeners: {},
      append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this[name] = value; },
      addEventListener(type, fn) { (this.listeners[type] ||= new Set()).add(fn); },
      removeEventListener(type, fn) { this.listeners[type]?.delete(fn); },
      querySelector(selector) { return all(this).find(node => node.tag === selector); },
      querySelectorAll(selector) { return all(this).filter(node => node.tag === selector); },
      showModal() { this.open = true; }, close() { this.open = false; for (const fn of [...(this.listeners.close || [])]) fn(); },
    };
  }
  const body = element('body');
  const context = {URLSearchParams, setInterval() {}, document: {body, createElement: element},
    FileReader: class { readAsDataURL(file) { this.result = 'data:application/pdf;base64,' + file.content; this.onload(); } },
    AssistantClient: {async api(route, options) {
      if (route === '/api/courseware/courses') return {courses: ['OS']};
      if (route.startsWith('/api/courseware?')) return {sources: [{id: 'old-source', title: 'slides.pdf'}], sessions: []};
      if (route === '/api/study/attachments') { const id = 'upload-' + (uploads.length + 1); uploads.push(id); return {id}; }
      if (route === '/api/courseware/preview') return {items: uploads.map(id => ({attachment_id: id, name: 'slides.pdf', duplicate_id: duplicate ? 'old-source' : null, conflicts: duplicate ? [] : [{id: 'old-source', title: 'slides.pdf'}]}))};
      if (route === '/api/courseware/import') { imports.push(JSON.parse(JSON.stringify(options.body))); return {}; }
      throw new Error('Unexpected route ' + route);
    }},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'courseware.js'), 'utf8'), context);
  const find = predicate => { const node = all(body).find(predicate); assert.ok(node); return node; };
  return {imports, uploads, find,
    async upload(count = 1) {
      await context.Courseware.open('OS');
      const picker = find(node => node.tag === 'input' && node.type === 'file' && node.accept);
      picker.files = Array.from({length: count}, (_, index) => ({name: 'slides.pdf', size: 100, content: 'content-' + index}));
      picker.onchange(); await settle();
    },
    async click(text) { await find(node => node.tag === 'button' && node.textContent === text).onclick(); await settle(); },
    choices() { return all(body).filter(node => node.tag === 'select'); },
    status() { return find(node => node.className === 'courseware-status').textContent; },
  };
}

test('same-name upload waits for a choice and defaults to keeping both files', async () => {
  const h = harness(); await h.upload();
  assert.equal(h.imports.length, 0); assert.equal(h.choices()[0].value, '');
  await h.click('按所选方式导入');
  assert.deepEqual(h.imports, [{course: 'OS', attachment_ids: ['upload-1'], replacements: {}}]);
});

test('replacing a named source is sent only after explicit selection', async () => {
  const h = harness(); await h.upload(); h.choices()[0].value = 'old-source';
  await h.click('按所选方式导入');
  assert.deepEqual(h.imports[0].replacements, {'upload-1': 'old-source'});
});

test('cancelling or closing the conflict dialog leaves the course unchanged', async () => {
  for (const close of [false, true]) {
    const h = harness(); await h.upload();
    if (close) { h.find(node => node.tag === 'dialog').close(); await settle(); }
    else await h.click('取消本次导入');
    assert.equal(h.imports.length, 0); assert.match(h.status(), /已取消/);
    assert.equal(h.find(node => node.tag === 'input' && node.list === 'courseware-courses').disabled, false);
  }
});

test('a repeated file is reused without asking to replace an unrelated source', async () => {
  const h = harness({duplicate: true}); await h.upload();
  assert.equal(h.imports.length, 1); assert.deepEqual(h.imports[0].replacements, {});
  assert.equal(h.choices().length, 0);
});

test('two incoming files cannot replace the same source in one submission', async () => {
  const h = harness(); await h.upload(2);
  for (const choice of h.choices()) choice.value = 'old-source';
  await h.click('按所选方式导入'); assert.equal(h.imports.length, 0); assert.match(h.status(), /只能替换一次/);
  h.choices()[1].value = ''; await h.click('按所选方式导入');
  assert.deepEqual(h.imports[0].replacements, {'upload-1': 'old-source'});
});
