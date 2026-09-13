import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function compile(path) {
  return ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
}

function dataUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
}

const signalsUrl = dataUrl(compile('../src/lib/signals.ts'));
const apiUrl = dataUrl(compile('../src/lib/api.ts'));
const stateSource = compile('../src/state/episodes.ts')
  .replace("'../lib/signals'", JSON.stringify(signalsUrl))
  .replace("'../lib/api'", JSON.stringify(apiUrl));

test('returning to an episode starts a fresh request and ignores the old response', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests = [];
  globalThis.window = {
    setInterval: () => 1,
    clearInterval: () => {},
  };
  globalThis.fetch = () => new Promise((resolve) => requests.push(resolve));

  try {
    const state = await import(dataUrl(stateSource));
    state.watchEpisode('episode-a');
    state.watchEpisode(null);
    state.watchEpisode('episode-a');

    assert.equal(requests.length, 2);
    requests[1](Response.json({ revision: 'new' }));
    await new Promise(setImmediate);
    assert.deepEqual(state.episodeDetail.peek(), { revision: 'new' });

    requests[0](Response.json({ revision: 'old' }));
    await new Promise(setImmediate);
    assert.deepEqual(state.episodeDetail.peek(), { revision: 'new' });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
