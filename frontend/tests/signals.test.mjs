import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/lib/signals.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
const {signal, effect, effectScope, onCleanup} = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);

test('leaving an episode disposes screen effects and resources', () => {
  const episode = signal('first');
  const visits = [];
  let cleaned = false;
  const leave = effectScope(() => {
    effect(() => visits.push(episode()));
    onCleanup(() => { cleaned = true; });
  });
  episode.set('second');
  leave();
  episode.set('third');
  assert.deepEqual(visits, ['first', 'second']);
  assert.equal(cleaned, true);
});

test('a canvas installed once stays reactive when its parent checks loading state', () => {
  const loaded = signal(false);
  const values = [];
  let mounted = false;
  const leave = effectScope(() => effect(() => {
    loaded();
    if (!mounted) {
      mounted = true;
      effect(() => values.push(loaded()));
    }
  }));
  loaded.set(true);
  assert.deepEqual(values, [false, true]);
  leave();
  loaded.set(false);
  assert.equal(values.length, 2);
});

test('an isolated editor survives unrelated header rerenders until navigation', () => {
  const status = signal('review');
  const draft = signal('original');
  const values = [];
  let editor = null;
  const leave = effectScope(() => {
    onCleanup(() => editor?.());
    effect(() => {
      status();
      if (!editor) editor = effectScope(() => effect(() => values.push(draft())));
    });
  });
  draft.set('unsaved edit');
  status.set('ready');
  draft.set('saved edit');
  assert.deepEqual(values, ['original', 'unsaved edit', 'saved edit']);
  leave();
  draft.set('ignored');
  assert.equal(values.length, 3);
});
