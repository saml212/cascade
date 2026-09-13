import assert from 'node:assert/strict';
import test from 'node:test';

import { dataUrl, transpileTs } from './load-ts.mjs';

const signalsUrl = dataUrl(
  await transpileTs(new URL('../src/lib/signals.ts', import.meta.url))
);
const apiUrl = dataUrl(
  await transpileTs(new URL('../src/lib/api.ts', import.meta.url))
);
const stateSource = (
  await transpileTs(new URL('../src/state/episodes.ts', import.meta.url))
)
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
