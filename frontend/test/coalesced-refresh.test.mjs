import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { coalescedRefresh } = await importTs(
  new URL('../src/lib/coalesced-refresh.ts', import.meta.url)
);

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test('overlapping refreshes share one request and coalesce one latest rerun', async () => {
  const requests = [];
  const refresh = coalescedRefresh(async () => {
    const request = deferred();
    requests.push(request);
    await request.promise;
  });

  const first = refresh();
  const second = refresh();
  const third = refresh();
  assert.strictEqual(first, second);
  assert.strictEqual(second, third);
  assert.equal(requests.length, 1);

  requests[0].resolve();
  await new Promise(setImmediate);
  assert.equal(requests.length, 2);
  requests[1].resolve();
  await Promise.all([first, second, third]);
  assert.equal(requests.length, 2);
});

test('a slow global projection does not delay a local projection', async () => {
  const localRequest = deferred();
  const globalRequest = deferred();
  let localApplied = false;
  let globalApplied = false;
  const refreshLocal = coalescedRefresh(async () => {
    await localRequest.promise;
    localApplied = true;
  });
  const refreshGlobal = coalescedRefresh(async () => {
    await globalRequest.promise;
    globalApplied = true;
  });

  const local = refreshLocal();
  const global = refreshGlobal();
  localRequest.resolve();
  await local;
  assert.equal(localApplied, true);
  assert.equal(globalApplied, false);

  globalRequest.resolve();
  await global;
  assert.equal(globalApplied, true);
});
