import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { ClipReviewSurfaceMemory } = await importTs(
  new URL('../src/lib/clip-review-surface.ts', import.meta.url)
);

test('remembers each clip review surface across card reconstruction', () => {
  const memory = new ClipReviewSurfaceMemory();

  assert.equal(memory.get('clip_11', true), 'base');
  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_11', true), 'background');
  assert.equal(memory.get('clip_12', true), 'base');
  assert.equal(memory.get('clip_11', true), 'background');
});

test('falls back to base when a clip has no background surface', () => {
  const memory = new ClipReviewSurfaceMemory();

  memory.select('clip_11', 'background');
  assert.equal(memory.get('clip_11', false), 'base');
});
