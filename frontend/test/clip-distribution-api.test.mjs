import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { api } = await importTs(new URL('../src/lib/api.ts', import.meta.url));

test('sends exact revision-bound distribution selections', async () => {
  const originalFetch = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async (path, init) => {
    captured.push({ path, method: init.method, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ status: 'selected' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    await api.selectClipDistribution(
      'episode-id',
      'clip-id',
      'background_motion_v1',
      'sha256:background-revision'
    );
    await api.selectClipDistribution(
      'episode-id',
      'clip-id',
      null,
      'sha256:base-revision'
    );
    assert.deepEqual(captured, [
      {
        path: '/api/episodes/episode-id/clips/clip-id/distribution',
        method: 'PUT',
        body: {
          variant_id: 'background_motion_v1',
          expected_revision: 'sha256:background-revision',
        },
      },
      {
        path: '/api/episodes/episode-id/clips/clip-id/distribution',
        method: 'PUT',
        body: {
          variant_id: null,
          expected_revision: 'sha256:base-revision',
        },
      },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
