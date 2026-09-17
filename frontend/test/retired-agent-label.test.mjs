import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { CANONICAL_AGENTS, describeAgent } = await importTs(
  new URL('../src/lib/format.ts', import.meta.url)
);

test('retired metadata generation stays labeled only in historical runs', () => {
  assert.equal(CANONICAL_AGENTS.includes('metadata_gen'), false);
  assert.equal(describeAgent('metadata_gen'), 'Writing metadata');
});
