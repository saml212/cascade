import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { editSourceRange } = await importTs(
  new URL('../src/lib/edit-range.ts', import.meta.url)
);

test('terminal trims use their absolute source-clock boundary', () => {
  assert.deepEqual(editSourceRange({ type: 'trim_start', seconds: 92.15 }, 5359.896), [
    0,
    92.15,
  ]);
  assert.deepEqual(editSourceRange({ type: 'trim_end', seconds: 5354.48 }, 5359.896), [
    5354.48,
    5359.896,
  ]);
});

test('a zero-tail trim remains a terminal marker instead of moving to zero', () => {
  assert.deepEqual(editSourceRange({ type: 'trim_end', seconds: 5359.896 }, 5359.896), [
    5359.896,
    5359.896,
  ]);
});

test('cuts are bounded to the source and malformed ranges are rejected', () => {
  assert.deepEqual(
    editSourceRange({ type: 'cut', start_seconds: -2, end_seconds: 12 }, 10),
    [0, 10]
  );
  assert.equal(
    editSourceRange({ type: 'cut', start_seconds: 7, end_seconds: 3 }, 10),
    null
  );
  assert.equal(editSourceRange({ type: 'trim_end' }, 10), null);
});
