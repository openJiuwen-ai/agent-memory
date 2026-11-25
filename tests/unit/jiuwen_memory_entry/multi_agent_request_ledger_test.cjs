// Deterministic regression for per-duty request invalidation in the multi-Agent Portal.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const scriptPath = process.argv[2];
const source = fs.readFileSync(scriptPath, 'utf8');
const match = source.match(/function createRoleRequestLedger\(\) \{[\s\S]*?\n  \}/);
assert.ok(match, 'createRoleRequestLedger must remain independently testable');
const createRoleRequestLedger = vm.runInNewContext(`(${match[0]})`);

const ledger = createRoleRequestLedger();
const first = ledger.begin(['a', 'b', 'c']);
assert.equal(first('a'), true);
assert.equal(first('b'), true);
assert.equal(first('c'), true);

// Retrying A invalidates only A's previous response.
const retryA = ledger.begin(['a']);
assert.equal(first('a'), false);
assert.equal(first('b'), true);
assert.equal(first('c'), true);
assert.equal(retryA('a'), true);

const results = {};
const pending = new Set(['a', 'b', 'c']);
const settle = (duty, current, value) => {
  if (!current(duty)) return;
  results[duty] = value;
  pending.delete(duty);
};
settle('a', first, 'stale A');
settle('b', first, 'B');
settle('a', retryA, 'new A');
settle('c', first, 'C');
assert.deepEqual(results, {b: 'B', a: 'new A', c: 'C'});
assert.equal(pending.size, 0);

// A full refresh invalidates every in-flight column.
const beforeRefresh = ledger.begin(['a', 'b', 'c']);
ledger.invalidate();
assert.equal(beforeRefresh('a'), false);
assert.equal(beforeRefresh('b'), false);
assert.equal(beforeRefresh('c'), false);
