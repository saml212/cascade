import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(
  new URL('../src/lib/stable-control.ts', import.meta.url),
  'utf8'
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { stableControl } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
);

test('poll updates reuse a stateful control for the same output identity', () => {
  const playing = { currentTime: 42, paused: false };
  const first = stableControl(undefined, 'audio:completed-at-1', () => playing);
  const polled = stableControl(first, 'audio:completed-at-1', () => ({ currentTime: 0 }));

  assert.strictEqual(polled.value, playing);
  assert.equal(polled.value.currentTime, 42);
  assert.equal(polled.value.paused, false);
});

test('a newly generated output replaces the cached control', () => {
  const first = stableControl(undefined, 'audio:completed-at-1', () => ({ id: 1 }));
  const regenerated = stableControl(first, 'audio:completed-at-2', () => ({ id: 2 }));

  assert.notStrictEqual(regenerated.value, first.value);
  assert.equal(regenerated.value.id, 2);
});
