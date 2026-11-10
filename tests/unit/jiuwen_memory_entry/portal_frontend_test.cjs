// Portal 页面状态回归：不依赖浏览器，用最小 DOM/HTTP 替身执行 index.html 内联脚本。
// 覆盖配置草稿与预览、最新详情、概览错误、导出重新鉴权和批量遗忘部分失败。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
assert.doesNotMatch(html, /id="tab-search"/);
assert.doesNotMatch(html, /id="semantic-search"/);
assert.match(html, /id="mode-semantic"/);
assert.match(html, /id="tab-overview" class="active"[^>]*aria-selected="true"/);
assert.match(html, /id="memories"[^>]* hidden>/);
assert.match(html, /id="breadcrumb-current"[^>]*>记忆概览/);

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.value = ''; this.hidden = false;
    this.disabled = false; this.events = {}; this.dataset = {};
    this.classList = {toggle() {}, add() {}, remove() {}}; this._text = ''; this.className = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  replaceWith(child) { this.replacement = child; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  get options() { return this.children; }
  get childElementCount() { return this.children.length; }
}
const response = (data, ok = true, status = ok ? 200 : 400) => ({
  ok, status, json: async () => data,
});
const field = (base, saved = null, hasOverride = false) => ({
  key: 'llm.default.params.llm_temperature', base, saved, has_override: hasOverride,
  effective_known: false, requires_reassembly: true, writable: true, consumers: [], options: [],
});
const unit = content => ({
  id:'r',scope:{agent:'agent'},segments:[{content}],temporal:{},tier:'episodic',
  system_metadata:{source_app:'app',agent_label:'agent'},
});
function setup(handler, listHandler) {
  const copied=[];
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  for (const id of ['source', 'search-source', 'agent', 'project', 'tier', 'memory-class']) get(id).value = '*';
  get('lifecycle').value = 'active';
  const context = vm.createContext({
    location: {hash:"",pathname:"/portal/",search:""},URLSearchParams,history:{replaceState(){}},
    document: {getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => [], querySelector: () => null, addEventListener() {}},
    setTimeout, clearTimeout, navigator: {clipboard: {writeText: async text => {copied.push(text);}}},
    Option: function (text, value) { const el = new Element('option'); el.textContent = text; el.value = value; return el; },
    fetch: async (url, args) => {
      assert.match(url, /^\/v1\//);
      const action = url.split('/').pop();
      if (action === 'list' && listHandler) {
        const {offset,limit,extensions}=JSON.parse(args.body);
        return listHandler({offset,limit,...extensions.personal});
      }
      if (action === 'list') return response({items: [], count: 0, snapshot: 's', sources: [],
        lifecycle_counts: {active:0,superseded:0,forgotten:0,archived:0},
        add_context: {agent:'rail-agent',agent_locked:true}});
      return handler(action, JSON.parse(args.body));
    },
  });
  vm.runInContext(source, context);
  return {nodes, get, copied, run: code => vm.runInContext(code, context)};
}

async function inFlightDraft() {
  let release;
  let saved = 1;
  let version = 1;
  const page = setup(async (action) => {
    if (action === 'read_config') return response({available: true, version, fields: [field(saved)]});
    if (action === 'save_config') {
      return new Promise(resolve => {
        release = () => { saved = 2; version = 2; resolve(response({version: 2})); };
      });
    }
    throw Error(action);
  });
  await page.run('loadSettings()');
  page.run("drafts.set('llm.default.params.llm_temperature', '2'); $('preview').onclick()");
  const pending = page.run("$('save').onclick()");
  await new Promise(resolve => setImmediate(resolve));
  const input = page.run("settingControls.find(el => el.dataset.key === 'llm.default.params.llm_temperature')");
  assert.equal(input.disabled, true, 'settings inputs must be disabled while saving');
  input.value = '3';
  input.events.input();
  assert.equal(page.run("drafts.get('llm.default.params.llm_temperature')"), '3');
  release();
  await pending;
  assert.equal(page.run("drafts.get('llm.default.params.llm_temperature')"), '3', 'newer draft survives save response');
  assert.equal(page.run("settingControls.find(el => el.dataset.key === 'llm.default.params.llm_temperature').value"), '3');
}

async function stalePreview() {
  let version = 1;
  let base = 1;
  const saves = [];
  const page = setup(async (action, body) => {
    if (action === 'read_config') return response({available: true, version, fields: [field(base)]});
    if (action === 'save_config') {
      saves.push(body);
      if (saves.length === 1) return response({message: 'configuration overlay version changed'}, false, 409);
      return response({version: 3});
    }
    throw Error(action);
  });
  await page.run('loadSettings()');
  page.run("drafts.set('llm.default.params.llm_temperature', '2.00'); $('preview').onclick()");
  await page.run("$('save').onclick()");
  version = 2;
  base = 9;
  await page.run('loadSettings()');
  assert.equal(page.get('save').hidden, true, 'reload must hide save');
  assert.doesNotMatch(page.get('diff').textContent, /1 → 2/, 'reload must discard stale diff');
  await page.run("$('save').onclick()");
  assert.equal(saves.length, 1, 'stale preview must not submit');
  page.run("$('preview').onclick()");
  assert.match(page.get('diff').textContent, /9 → 2/);
  assert.equal(page.get('save').hidden, false);
  await page.run("$('save').onclick()");
  assert.equal(saves.length, 2);
  assert.equal(saves[1].version, 2);
  assert.deepEqual(saves[1].changes, {'llm.default.params.llm_temperature': 2});
  assert.equal(page.run('drafts.size'), 0, 'saving a normalized number clears the original draft');
}

async function detailContent() {
  const page = setup(async action => {
    if (action === 'get') return response(unit('fresh detail content'));
    throw Error(action);
  });
  await new Promise(resolve => setImmediate(resolve));
  page.run("cards('memory-list', [{content:'old list content', source:'app', ref:recordKey({},'r')}])");
  const card = page.get('memory-list').children[0];
  await card.children.find(el => el.className === 'card-open').onclick();
  assert.match(page.get('memory-detail').textContent, /fresh detail content/);
  assert.doesNotMatch(page.get('memory-detail').textContent, /old list content/);
}

async function outOfOrderSettings() {
  let release, calls = 0;
  const page = setup(async action => {
    if (action === 'read_config' && ++calls === 1) {
      return new Promise(resolve => {
        release = () => resolve(response({available: true, version: 1, fields: [field(1)]}));
      });
    }
    return response({available: true, version: 2, fields: [field(9)]});
  });
  const older = page.run('loadSettings()');
  await new Promise(resolve => setImmediate(resolve));
  await page.run('loadSettings()');
  release();
  await older;
  assert.equal(page.run('version'), 2, 'a delayed response must not roll back the displayed version');
  assert.equal(page.run('fields[0].base'), 9);
}

async function modalDuplicateSubmission() {
  let release, writes = 0;
  const page = setup(async (action,body) => {
    if (action !== 'add') throw Error(action);
    assert.deepEqual(body,{content:'new memory',scope:{},system_metadata:{project:'demo'}});
    writes++;
    return new Promise(resolve => {
      release = () => resolve(response([unit('new memory')]));
    });
  });
  await page.run("openAdd()");
  assert.equal(page.get('new-memory-agent').value,'rail-agent');
  assert.equal(page.get('new-memory-agent').readOnly,true);
  page.run("$('new-memory').value = 'new memory'; $('new-memory-project').value = 'demo'");
  const first = page.run("$('add-form').onsubmit({preventDefault(){}})");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.get('submit-memory').disabled, true);
  assert.equal(page.get('close-memory-dialog').disabled, true);
  await page.run("$('add-form').onsubmit({preventDefault(){}})");
  assert.equal(writes, 1, 'repeated submission while saving must not write twice');
  release();
  await first;
  assert.equal(page.get('memory-dialog').open, false);
  assert.match(page.get('notice').textContent, /已保存 1 条记忆/);
}

async function partialBatchForget() {
  const writes=[];
  const page=setup(async(action,body)=>{
    if(action==='delete'){
      assert.equal(body.selector.mode,'forget');
      const [id]=body.selector.unit_ids;
      writes.push(id);
      return id==='bad'?response({message:'permission denied'},false,403):response(['ok']);
    }
    throw Error(action);
  });
  await new Promise(resolve=>setImmediate(resolve));
  page.run("cards('memory-list',[{ref:recordKey({},'ok'),content:'success',lifecycle:'active'},{ref:recordKey({},'bad'),content:'failure',lifecycle:'active'}]);checked.add(recordKey({},'ok'));checked.add(recordKey({},'bad'));$('forget-selected').onclick()");
  assert.equal(writes.length,0,'batch action needs confirmation');
  await page.run("$('confirm-forget').onclick()");
  assert.deepEqual(writes,['ok','bad']);
  assert.match(page.get('notice').textContent,/已遗忘 1 条/);
  assert.match(page.get('notice').textContent,/1 条未完成/);
  assert.match(page.get('notice').textContent,/permission denied/);
}
async function exportAuthorizationFailure(){
  const page=setup(async action=>{if(action==='get')return response({message:'access revoked'},false,403);throw Error(action);});
  await new Promise(resolve=>setImmediate(resolve));
  page.run("cards('memory-list',[{ref:recordKey({},'r'),content:'cached private content'}]);checked.add(recordKey({},'r'))");
  await page.run("exportSelected('copy')");
  assert.match(page.get('notice').textContent,/未输出数据：access revoked/);
  assert.doesNotMatch(page.get('notice').textContent,/cached private content/);
  assert.equal(page.run('batchBusy'),false);
  assert.equal(page.copied.length,0,'revoked details must never fall back to cached content');
}
async function selectionClearsOnRefresh(){
  const page=setup(async action=>{throw Error(action);});
  await new Promise(resolve=>setImmediate(resolve));
  page.run("checked.add('old')");await page.run('loadMemories(true)');
  assert.equal(page.run('checked.size'),0);
  assert.equal(page.get('batch-bar').hidden,true);
}
async function freshCopy(){
  const page=setup(async action=>{if(action==='get')return response(unit('fresh authorized text'));throw Error(action);});
  await new Promise(resolve=>setImmediate(resolve));
  page.run("cards('memory-list',[{ref:recordKey({},'r'),content:'old cache'}]);checked.add(recordKey({},'r'))");
  await page.run("exportSelected('copy')");assert.deepEqual(page.copied,['fresh authorized text']);
}
async function overviewCountsAndFailure(){
  let fail=false, overviewCalls=0, listCalls=0;
  const page=setup(async action=>{throw Error(action);},async body=>{
    listCalls++;
    if(body.limit===20)return response({items:[],count:0,snapshot:'s',sources:[]});
    overviewCalls++;
    assert.equal(body.include_lifecycle_counts,true);
    if(fail)return response({message:'unavailable'},false,503);
    return response({items:[],count:180,lifecycle_counts:{active:180,superseded:12,forgotten:5,archived:3},sources:['','app-a','app-b']});
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(listCalls,1,'opening the overview should only fetch the visible view');
  await page.run('loadOverview()');
  assert.equal(overviewCalls,2,'startup and explicit refresh each use one list request');
  assert.equal(page.get('stat-all').textContent,'200','overview uses server count rather than sample length');
  assert.equal(page.get('stat-active').textContent,'180');assert.equal(page.get('stat-forgotten').textContent,'5');assert.equal(page.get('stat-superseded').textContent,'12');assert.equal(page.get('stat-archived').textContent,'3');assert.equal(page.get('stat-sources').textContent,'2');
  for(const [id,state]of [['metric-superseded','superseded'],['metric-archived','archived']]){await page.run(`$('${id}').onclick()`);assert.equal(page.get('lifecycle').value,state);assert.equal(page.run('activeTab'),'memories');}
  fail=true;await page.run('loadOverview()');assert.equal(page.get('stat-all').textContent,'—');assert.match(page.get('overview-error').textContent,/unavailable/);
}
async function topicShortcutsUseExistingFilters(){
  const requests=[];
  const page=setup(async action=>{throw Error(action);},async body=>{if(body.limit===20)requests.push(body);return response({items:[],count:0,snapshot:'s',sources:[],agents:[],projects:[],tiers:[],memory_classes:[]});});
  await new Promise(resolve=>setImmediate(resolve));
  page.run("setFilterValue('source','my-app');setFilterValue('agent','my-agent');setFilterValue('project','my-project');$('lifecycle').value='forgotten';$('memory-filter').value='keyword';offset=40;snapshot='old'");
  for(const [name,memoryClass,tier]of [['profile','user_profile',null],['knowledge','long_term_memory','semantic'],['procedure',null,'procedural'],['all',null,null]]){
    await page.run(`switchTab('${name==='all'?'memories':name}')`);const body=requests.at(-1);
    assert.equal(body.memory_class,memoryClass);assert.equal(body.tier,tier);assert.equal(body.offset,0);assert.equal(body.snapshot,null);
    assert.equal(body.source,'my-app');assert.equal(body.agent,'my-agent');assert.equal(body.project,'my-project');assert.equal(body.lifecycle,'forgotten');assert.equal(body.query,'keyword');
    assert.equal(page.get('tab-'+(name==='all'?'memories':name))['aria-selected'],'true','empty facets must retain the requested shortcut');
    assert.equal(page.get('breadcrumb-current').textContent,{profile:'用户画像',knowledge:'项目知识',procedure:'过程与做法',all:'我的记忆'}[name]);
  }
  page.run("setFilterValue('memory-class','custom-class');syncTopicShortcut()");
  assert.equal(page.get('tab-memories')['aria-selected'],'true');assert.match(page.get('memory-page-description').textContent,/按当前分类/);
  for(const [name,memoryClass,tier]of [['profile','user_profile',null],['knowledge','long_term_memory','semantic'],['procedure',null,'procedural']]){
    await page.run(`switchTab('${name}')`);
    assert.equal(page.get('tier-filter').hidden,true);assert.equal(page.get('class-filter').hidden,true);
    assert.equal(page.get('tier').disabled,true);assert.equal(page.get('memory-class').disabled,true);
    page.run("setFilterValue('memory-class','wrong');setFilterValue('tier','working')");await page.run('loadMemories(true)');
    assert.equal(requests.at(-1).memory_class,memoryClass);assert.equal(requests.at(-1).tier,tier);
    await page.run("$('reset-filters').onclick()");
    const body=requests.at(-1);assert.equal(body.memory_class,memoryClass);assert.equal(body.tier,tier);assert.equal(body.source,null);assert.equal(body.query,'');assert.equal(body.lifecycle,'active');
    assert.equal(page.run('activeTab'),name,'reset must keep the current category');assert.equal(page.get('tab-'+name)['aria-selected'],'true');
  }
  await page.run("switchTab('memories')");assert.equal(page.get('tier-filter').hidden,false);assert.equal(page.get('class-filter').hidden,false);
  await page.run("switchTab('search')");assert.equal(page.run('activeTab'),'memories');assert.equal(page.run('searchMode'),'semantic');assert.equal(page.get('semantic-results').hidden,false);assert.equal(page.get('browse-results').hidden,true);
}
async function unifiedSearchKeepsScopesSeparate(){
  const calls=[];const page=setup(async(action,body)=>{if(action==='search'){calls.push(body);return response({items:[],scores_comparable:false});}throw Error(action);});
  await new Promise(resolve=>setImmediate(resolve));await page.run("switchTab('memories')");
  page.run("setFilterValue('project','my-project');setFilterValue('memory-class','custom-class');$('memory-filter').value='我喜欢什么';$('top-k').value='10';setFilterValue('search-source','source-app');setSearchMode('semantic')");
  assert.equal(page.get('more-filters').hidden,true);assert.equal(page.get('browse-results').hidden,true);assert.equal(page.get('semantic-results').hidden,false);
  await page.run("$('search-form').onsubmit({preventDefault(){}})");assert.deepEqual(calls,[{query:'我喜欢什么',top_k:10,context:{extensions:{personal:{source:'source-app'}}}}]);
  assert.match(page.get('search-explanation').textContent,/同一范围内排序/);
  page.run("setSearchMode('text',false)");assert.equal(page.get('project').value,'my-project');assert.equal(page.get('memory-class').value,'custom-class');assert.equal(page.get('memory-filter').value,'我喜欢什么');assert.equal(page.get('semantic-results').hidden,true);assert.equal(page.get('browse-results').hidden,false);assert.equal(page.get('top-k').disabled,true);
  await page.run("switchTab('profile')");page.run("setSearchMode('semantic')");assert.equal(page.run('searchMode'),'text');assert.equal(page.get('mode-semantic').hidden,true);
}
async function filterBadgeCountsAdditionalPanelConditions(){
  const page=setup(async action=>{throw Error(action);});
  await new Promise(resolve=>setImmediate(resolve));
  for(const state of ['active','superseded','forgotten','archived','*']){page.get('lifecycle').value=state;await page.run('loadMemories(true)');assert.equal(page.get('filter-count').textContent,state==='*'?'':'· 1 项筛选','every concrete lifecycle must count as one filter');}
  page.get('memory-filter').value='coffee';await page.run('loadMemories(true)');assert.equal(page.get('filter-count').textContent,'','search input is outside the filter panel');
  page.run("setFilterValue('source','my-app')");await page.run('loadMemories(true)');assert.equal(page.get('filter-count').textContent,'· 1 项筛选');
  page.get('lifecycle').value='forgotten';await page.run('loadMemories(true)');assert.equal(page.get('filter-count').textContent,'· 2 项筛选');
  await page.run("switchTab('profile');$('reset-filters').onclick()");assert.equal(page.get('filter-count').textContent,'· 1 项筛选','reset keeps the active lifecycle filter but excludes the fixed category');
}
async function expiredReferenceOffersListRefresh(){
  let detailRequests=0;
  const page=setup(async action=>{
    if(action==='get'){detailRequests++;return response({error:'NotFound'},false,404);}
    throw Error(action);
  });
  await new Promise(resolve=>setImmediate(resolve));
  await page.run("selectMemory({ref:recordKey({},'expired')},'memories')");
  const detail=page.get('memory-detail');
  const state=detail.children[0];
  assert.match(JSON.stringify(state),/请刷新列表后重试/);
  const refresh=state.children.find(child=>child.textContent==='刷新列表');
  assert.ok(refresh);
  await refresh.onclick();
  assert.equal(detailRequests,1,'refresh must not reuse an expired reference');
}
async function expiredSessionStopsPortalActions(){
  let requests=0;
  const page=setup(async()=>{requests++;return response({},false,401);});
  await new Promise(resolve=>setImmediate(resolve));
  await assert.rejects(page.run("request('get',{ref:'old'})"),/从应用重新打开/);
  assert.equal(page.get('portal-shell').hidden,true);
  assert.equal(page.get('session-expired').hidden,false);
  await assert.rejects(page.run("request('add',{content:'wrong scope'})"),/从应用重新打开/);
  assert.equal(requests,1);
}
(async () => {
  await filterBadgeCountsAdditionalPanelConditions();
  await unifiedSearchKeepsScopesSeparate();
  await topicShortcutsUseExistingFilters();
  await overviewCountsAndFailure();
  await freshCopy();
  await partialBatchForget();
  await exportAuthorizationFailure();
  await selectionClearsOnRefresh();
  await inFlightDraft();
  await stalePreview();
  await detailContent();
  await expiredReferenceOffersListRefresh();
  await expiredSessionStopsPortalActions();
  await outOfOrderSettings();
  await modalDuplicateSubmission();
  console.log('portal frontend fixed-behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
