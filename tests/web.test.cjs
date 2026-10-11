const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../agentteam/web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function element() {
  const classes = new Set();
  return {
    value:'', textContent:'', innerHTML:'', disabled:false,
    classList:{contains:c=>classes.has(c), add:c=>classes.add(c), remove:c=>classes.delete(c),
      toggle(c, on) { on = on ?? !classes.has(c); on ? classes.add(c) : classes.delete(c); }},
    querySelectorAll:()=>[], insertAdjacentHTML(_, text){ this.innerHTML += text; }, focus() {},
  };
}
async function harness() {
  const elements = new Map();
  const el = id => { if(!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const state = {status:'idle', modes:[{name:'workshop', label:'编剧室'}], ledger:{revision:0}};
  const theater = {status:'done', calls:9};
  const records = {};
  const requests = [];
  let intercept;
  const context = vm.createContext({document:{getElementById:el, querySelectorAll:()=>[]},
    setInterval(){}, setTimeout, alert(){},
    fetch:async (url, options={}) => {
      requests.push({url, ...options});
      let body;
      if(intercept) body = await intercept(url, options);
      if(body === undefined) {
        if(url === '/api/state') body = structuredClone(state);
        else if(url === '/api/theater/state') body = theater;
        else if(url === '/api/runs') body = {runs:Object.values(records)};
        else body = records[decodeURIComponent(url.split('/').pop())];
      }
      return {ok:!!body, json:async()=>body || {error:'not found'}};
    }});
  const run = code => vm.runInContext(code, context);
  run(script);
  await new Promise(setImmediate);
  return {el, state, theater, records, requests, run, intercept(fn){intercept=fn;}};
}
function draft(id, scene=id, extra={}) {
  return {id, scene, status:'done', mode:'workshop', mock:true, request:'写一场戏',
    workflow_status:'ready', check:{status:'pass'}, proposal:{}, can_adopt:false,
    adoption_error:'mock 稿件不能采纳', ...extra};
}

test('polls current pipeline state and renders completed draft, not theater status', async()=>{
  const h = await harness();
  h.state.run_id = 'a'; h.state.status = 'running';
  h.records.a = draft('a', '', {status:'running'});
  await h.run("tab='pipeline'; tick()");
  assert.equal(h.el('status').textContent, 'running');
  h.state.status = 'done'; h.records.a = draft('a', '最新正文', {calls:6});
  await h.run('tick()');
  assert.equal(h.el('p-scene').textContent, '最新正文');
  assert.equal(h.el('calls').textContent, '6 次调用');
  assert.equal(h.el('p-adopt').disabled, true);
  assert.equal(h.el('p-recheck').disabled, false);
});

test('late history response cannot replace a newer selection', async()=>{
  const h = await harness();
  h.records.a = draft('a'); h.records.b = draft('b');
  let release;
  h.intercept(url => url === '/api/runs/a' ? new Promise(resolve=>{release=resolve;}) : undefined);
  h.run("tab='pipeline'");
  const pending = h.run("selectRun('a')");
  await h.run("selectRun('b')");
  release(h.records.a); await pending;
  assert.equal(h.el('p-scene').textContent, 'b');
  assert.equal(h.el('p-selected').textContent, 'b');
});

test('editing survives polls; save creates a child and next scene stays unselected', async()=>{
  const h = await harness();
  h.state.run_id='a'; h.state.status='done';
  h.records.a=draft('a', '原文');
  await h.run("tab='pipeline'; selectRun('a')");
  h.el('p-edit').onclick(); h.el('p-edit-scene').value='手工修改';
  await h.run('tick()');
  assert.equal(h.el('p-edit-scene').value, '手工修改');
  let patch;
  h.intercept((url, options)=>{
    if(options.method === 'PATCH') {
      patch=JSON.parse(options.body); h.records.b=draft('b', patch.scene, {workflow_status:'unreviewed'});
      return {ok:true, run_id:'b'};
    }
  });
  await h.el('p-save').onclick();
  assert.equal(patch.scene, '手工修改');
  assert.equal(h.records.a.scene, '原文');
  assert.equal(h.el('p-selected').textContent, 'b');
  h.el('p-next').onclick(); await h.run('tick()');
  assert.equal(h.el('p-scene').textContent, '（空）');
  assert.match(h.el('p-selected').textContent, /未选择/);
});

test('recheck sends the selected source with workshop mode and no revision feedback', async()=>{
  const h = await harness();
  h.records.a=draft('a'); h.records.b=draft('b');
  await h.run("tab='pipeline'; selectRun('a')");
  h.el('p-mode').value='single'; h.el('p-feedback').value='不应带入重查';
  let request;
  h.intercept((url, options)=>{
    if(url === '/api/run' && options.method === 'POST') {
      request=JSON.parse(options.body); return {ok:true, run_id:'b'};
    }
  });
  await h.el('p-recheck').onclick();
  assert.equal(request.source_run_id, 'a');
  assert.equal(request.mode, 'workshop');
  assert.equal(request.feedback, null);
  assert.equal(request.closed_loop, true);
  assert.equal(h.el('p-selected').textContent, 'b');
});

test('failed edits retain the editor and report server rejection', async()=>{
  const h=await harness(); h.records.a=draft('a');
  await h.run("tab='pipeline'; selectRun('a')"); h.el('p-edit').onclick();
  h.intercept((_, options)=>options.method==='PATCH' ? {ok:false, error:'提案不合法'} : undefined);
  await h.el('p-save').onclick();
  assert.equal(h.el('p-error').textContent, '提案不合法');
  assert.equal(h.el('p-editor').classList.contains('hidden'), false);
  assert.equal(h.el('p-save').disabled, false);
});

test('adoption targets the selected reviewed draft and refreshes the ledger', async()=>{
  const h=await harness();
  h.records.a=draft('a', '已审正文', {mock:false, can_adopt:true, adoption_error:null});
  await h.run("tab='pipeline'; selectRun('a')");
  assert.equal(h.el('p-adopt').disabled, false);
  let adopted;
  h.intercept((url, options)=>{
    if(options.method==='POST') {
      adopted=url; h.state.ledger.revision=1;
      Object.assign(h.records.a, {can_adopt:false, adopted_at:'now', adoption_error:'已采纳'});
      return {ok:true, revision:1};
    }
  });
  await h.el('p-adopt').onclick();
  assert.equal(adopted, '/api/runs/a/adopt');
  assert.match(h.el('p-ledger').innerHTML, /revision.*1/s);
  assert.equal(h.el('p-adopt').disabled, true);
  assert.equal(h.el('p-recheck').disabled, true);
});

test('polling does not overlap and recovers from a connection failure', async()=>{
  const h=await harness();
  let release;
  h.intercept(url=>url==='/api/state' ? new Promise(resolve=>{release=resolve;}) : undefined);
  const pending=h.run('tick()');
  const before=h.requests.length;
  await h.run('tick()'); assert.equal(h.requests.length, before);
  release(h.state); await pending;
  h.intercept(()=>{throw new Error('offline');}); await h.run('tick()');
  assert.equal(h.el('status').textContent, '连接失败');
  h.intercept(null); await h.run('tick()');
  assert.equal(h.el('status').textContent, 'done');
});
