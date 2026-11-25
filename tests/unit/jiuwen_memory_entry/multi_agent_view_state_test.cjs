// Deterministic regression for multi-Agent reference and task-source state.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(process.argv[2], 'utf8');
const helperStart = source.indexOf('  const referenceChanged');
const needsRecordReadAt = source.indexOf('  const needsRecordRead', helperStart);
assert.ok(helperStart >= 0 && needsRecordReadAt > helperStart);
const helperEnd = source.indexOf('\n', needsRecordReadAt) + 1;
const helperSource = source.slice(helperStart, helperEnd);
const blank = value => typeof value === 'string' && value.trim() ? value.trim() : '';
const scopeKey = scope => [scope?.org, scope?.space, scope?.user, scope?.agent, scope?.session]
  .map(value => value || '').join('\u0000');
const recordKey = (scope, id) => scopeKey(scope) + '\u0000' + id;
const helpers = vm.runInNewContext(
  `${helperSource}\n;({referenceChanged, needsRecordRead});`,
  {blank},
);

const scope = {org: 'demo', space: 'project', user: 'owner', agent: 'worker', session: 'run-1'};
const oldRef = {scope, unit_id: 'unit-1', content_digest: 'a'.repeat(64)};
const newRef = {scope, unit_id: 'unit-1', content_digest: 'b'.repeat(64)};
assert.equal(helpers.referenceChanged(oldRef, {content_digest: oldRef.content_digest}), false);
assert.equal(helpers.referenceChanged(oldRef, {content_digest: newRef.content_digest}), true);
assert.equal(helpers.referenceChanged({scope, unit_id: 'unit-1'}, {content_digest: ''}), false);
assert.equal(helpers.needsRecordRead({system_metadata: {}}), true);
assert.equal(helpers.needsRecordRead({system_metadata: {continuity_connector: 'codex'}}), true);
assert.equal(helpers.needsRecordRead({
  system_metadata: {continuity_connector: 'codex'}, temporal: {},
}), false);

const pathSource = source.match(/  const pathSourceText = [\s\S]*?\n  };/)[0];
const key = recordKey(scope, newRef.unit_id);
const pathState = {units: new Map(), unitRefs: new Map()};
const pathText = vm.runInNewContext(`${helperSource}\n${pathSource}\npathSourceText;`, {
  blank, recordKey, state: pathState, tr: (_zh, en) => en,
  sourceLabelForRef: () => 'Codex', firstLine: text => text.split('\n')[0],
});
assert.match(pathText(oldRef), /View related memory/);
pathState.units.set(key, {segments: [{content: 'Latest title\nLatest body'}]});
pathState.unitRefs.set(key, newRef);
assert.equal(pathText(oldRef), 'Referenced content changed');
assert.equal(pathText(newRef), 'Latest title · Source: Codex');
assert.equal(pathText({...newRef, content_digest: ''}), 'Latest title · Source: Codex');

const roleStart = source.indexOf('  function supportsRoleView(');
const roleEnd = source.indexOf('  function bindPaths(', roleStart);
const recordRoot = {};
const column = {
  querySelector: selector => selector === '.ma-role-records' ? recordRoot : null,
  querySelectorAll: () => [],
};
const roleRoot = {querySelector: () => column};
const roleState = {
  capability: {scope, project_name: 'Shared'}, roleResults: {}, roleErrors: {},
  rolePending: new Set(),
};
const rendered = [];
const bound = [];
const renderRole = vm.runInNewContext(`${source.slice(roleStart, roleEnd)}\nrenderRole;`, {
  state: roleState, renderRoleScope: () => {}, roleCatalogUsable: () => true,
  selectedRoleDuties: () => ['general'], dutyName: duty => duty,
  $ma: id => ({'ma-role-results': roleRoot, 'ma-role-query': {value: 'query'}})[id],
  tr: (_zh, en) => en, esc: value => value, gapBanner: () => '',
  renderRecords: (...args) => rendered.push(args), bindPaths: (...args) => bound.push(args),
});
const item = {unit_id: 'unit-1', content: 'Record text', view: {kind: 'risk'}};
const paths = [{steps: [{source: oldRef, target: newRef}]}];
roleState.roleResults.general = {
  items: [item], paths, view_meta: {mode: 'role', schema_version: 'role-view/3'},
};
renderRole();
assert.match(roleRoot.innerHTML, /Relations are unavailable/);
assert.doesNotMatch(roleRoot.innerHTML, /class="ma-paths"/);
assert.doesNotMatch(roleRoot.innerHTML, /Retrieval technical details/);
assert.equal(rendered[0][1][0].content, 'Record text');
assert.equal(rendered[0][1][0].view, null);
assert.equal(rendered[0][3].length, 0);
assert.equal(bound.length, 0);
roleState.roleResults.general.view_meta.schema_version = 'role-view/2';
renderRole();
assert.match(roleRoot.innerHTML, /class="ma-paths"/);
assert.equal(rendered[1][1][0], item);
assert.equal(rendered[1][3], paths);
assert.equal(bound[0][3], paths);

const taskStart = source.indexOf('  function renderTask()');
const taskEnd = source.indexOf('  async function loadTaskSources(', taskStart);
const viewHelpersEnd = source.indexOf('  function renderRole()', roleStart);
const taskElements = new Map();
const taskRoot = {querySelectorAll: () => []};
taskElements.set('ma-task-page', taskRoot);
const taskRecords = [];
const taskState = {
  capability: {scope}, task: {task_id: 'task-1'}, taskTab: 'background',
  taskResult: {
    items: [{...item, view: {...item.view, discovery: 'background', required: true}}],
    paths, view_meta: {mode: 'role', schema_version: 'role-view/3'},
  },
};
const renderTask = vm.runInNewContext(
  `${source.slice(roleStart, viewHelpersEnd)}\n${source.slice(taskStart, taskEnd)}\nrenderTask;`,
  {
    state: taskState, $ma: id => {
      if (!taskElements.has(id)) taskElements.set(id, {id});
      return taskElements.get(id);
    },
    continuityTaskUsable: () => true, roleCatalogUsable: () => true, clearTask: () => {},
    tr: (_zh, en) => en, esc: value => value, icon: () => '', gapBanner: () => '',
    renderRecords: (...args) => taskRecords.push(args),
  },
);
renderTask();
assert.match(taskRoot.innerHTML, /Search results/);
assert.doesNotMatch(taskRoot.innerHTML, /data-ma-required/);
assert.equal(taskRecords[0][0].id, 'ma-task-timeline');
assert.equal(taskRecords[0][1].length, 0);
assert.equal(taskRecords[0][3].length, 0);
assert.equal(taskRecords[1][0].id, 'ma-task-ordinary-results');
assert.equal(taskRecords[1][1][0].content, 'Record text');
assert.equal(taskRecords[1][1][0].view, null);
assert.equal(taskRecords[1][3], undefined);

const invalidateMatch = source.match(/  function invalidateTaskSources\(\) \{[\s\S]*?\n  \}/);
assert.ok(invalidateMatch, 'invalidateTaskSources must remain independently testable');
const state = {
  taskSourceToken: 7,
  taskSourceSelection: {query: 'old task'},
  taskSourceResult: {items: ['old task source']},
  taskSourceError: 'old task error',
};
const invalidateTaskSources = vm.runInNewContext(`(${invalidateMatch[0]})`, {state});
const pendingTaskSource = state.taskSourceToken;
invalidateTaskSources();
assert.equal(state.taskSourceToken, 8);
assert.notEqual(pendingTaskSource, state.taskSourceToken);
assert.equal(state.taskSourceSelection, null);
assert.equal(state.taskSourceResult, null);
assert.equal(state.taskSourceError, '');
assert.equal(source.includes('state.records'), false, 'dead result cache must be removed');
assert.equal(source.includes('bindingSeq'), false, 'dead binding sequence must be removed');
assert.equal(source.includes('data-ma-bind'), false, 'dead binding attribute must be removed');
assert.equal(source.includes('if (force || needsRecordRead(item)) refs.push(ref);'), true);
assert.equal(source.includes('temporal: unit.temporal || {}'), true);
assert.equal(source.includes('temporalFor(item, scope).t_event'), true);

const searchStart = source.indexOf('  async function search(selection, offset = 0) {');
const searchResetAt = source.indexOf('state.searchResult = null;', searchStart);
const searchRequestAt = source.indexOf('await sourceRequest', searchStart);
assert.ok(searchResetAt > searchStart && searchResetAt < searchRequestAt);

const taskSourcesStart = source.indexOf('  async function loadTaskSources() {');
const taskSourcesResetAt = source.indexOf('state.taskSourceResult = null;', taskSourcesStart);
const taskSourcesRequestAt = source.indexOf('await sourceRequest', taskSourcesStart);
assert.ok(taskSourcesResetAt > taskSourcesStart && taskSourcesResetAt < taskSourcesRequestAt);
assert.equal((source.match(/invalidateTaskSources\(\);/g) || []).length, 2);
const openTaskAt = source.indexOf('async function openTask');
const taskFetchAt = source.indexOf('continuity_task', openTaskAt);
const taskInvalidationAt = source.indexOf('invalidateTaskSources();', openTaskAt);
assert.ok(taskInvalidationAt > openTaskAt && taskInvalidationAt < taskFetchAt);
