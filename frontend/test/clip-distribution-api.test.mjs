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

test('sends an exact idempotent re-release preparation request', async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (path, init) => {
    captured = { path, method: init.method, body: JSON.parse(init.body) };
    return new Response(
      JSON.stringify({
        status: 'prepared',
        clip_id: 'clip-id',
        requires_publish_approval: true,
        distribution: {
          variant_id: 'background_motion_v1',
          revision: 'sha256:background-revision',
          re_release_request_consumed: false,
          re_release_request: {
            request_id: '323e4567-e89b-12d3-a456-426614174000',
            actor: 'Sam',
            reason: 'Prepare a reviewed motion version',
            variant_id: 'background_motion_v1',
            target_revision: 'sha256:background-revision',
            render_fingerprint: 'sha256:render',
            receipt_history_revision: 'sha256:history',
            revision: 'sha256:request',
            created_at: '2026-09-13T20:00:00+00:00',
          },
        },
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
  };
  const body = {
    variant_id: 'background_motion_v1',
    expected_revision: 'sha256:background-revision',
    request_id: '323e4567-e89b-12d3-a456-426614174000',
    actor: 'Sam',
    reason: 'Prepare a reviewed motion version',
  };
  try {
    await api.prepareClipReRelease('episode-id', 'clip-id', body);
    assert.deepEqual(captured, {
      path: '/api/episodes/episode-id/clips/clip-id/re-release',
      method: 'POST',
      body,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
