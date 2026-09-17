import assert from 'node:assert/strict';
import test from 'node:test';

import { dataUrl, transpileTs } from './load-ts.mjs';

const signalsUrl = dataUrl(
  await transpileTs(new URL('../src/lib/signals.ts', import.meta.url))
);
const apiUrl = dataUrl(
  await transpileTs(new URL('../src/lib/api.ts', import.meta.url))
);
const refreshUrl = dataUrl(
  await transpileTs(new URL('../src/lib/coalesced-refresh.ts', import.meta.url))
);
const stateSource = (
  await transpileTs(new URL('../src/state/episodes.ts', import.meta.url))
)
  .replace("'../lib/signals'", JSON.stringify(signalsUrl))
  .replace("'../lib/api'", JSON.stringify(apiUrl))
  .replace("'../lib/coalesced-refresh'", JSON.stringify(refreshUrl));

function loadState() {
  return import(dataUrl(`${stateSource}\n// ${crypto.randomUUID()}`));
}

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
    const state = await loadState();
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

test('an explicit detail refresh queues behind an older in-flight poll', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests = [];
  globalThis.window = {
    setInterval: () => 1,
    clearInterval: () => {},
  };
  globalThis.fetch = () => new Promise((resolve) => requests.push(resolve));

  try {
    const state = await loadState();
    state.watchEpisode('episode-a');
    const refreshed = state.refreshEpisode('episode-a');
    assert.equal(requests.length, 1);

    requests[0](Response.json({ revision: 'before write' }));
    await new Promise(setImmediate);
    assert.equal(requests.length, 2);
    requests[1](Response.json({ revision: 'after write' }));
    await refreshed;

    assert.deepEqual(state.episodeDetail.peek(), { revision: 'after write' });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});

test('leaving an episode cancels a queued detail refresh', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests = [];
  globalThis.window = {
    setInterval: () => 1,
    clearInterval: () => {},
  };
  globalThis.fetch = () => new Promise((resolve) => requests.push(resolve));

  try {
    const state = await loadState();
    state.watchEpisode('episode-a');
    const refreshed = state.refreshEpisode('episode-a');
    state.watchEpisode(null);

    requests[0](Response.json({ revision: 'left behind' }));
    await refreshed;

    assert.equal(requests.length, 1);
    assert.equal(state.episodeDetail.peek(), null);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
