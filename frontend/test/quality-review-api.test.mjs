import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { api } = await importTs(new URL('../src/lib/api.ts', import.meta.url));

test('semantic output review posts every exact revision binding', async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (path, init) => {
    captured = { path, init };
    return new Response(JSON.stringify({ status: 'recorded' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    const body = {
      decision: 'false_positive',
      reviewer: 'Sam',
      evidence_note: 'Reviewed the exact current output preview.',
      expected_report_fingerprint: 'report-fingerprint',
      expected_event_fingerprint: 'event-fingerprint',
      expected_output_revision: 'output-revision',
    };
    await api.reviewAudioOutputFinding('episode-id', 'event-id', body);
    assert.equal(
      captured.path,
      '/api/episodes/episode-id/audio-qc/output-findings/event-id/review'
    );
    assert.equal(captured.init.method, 'POST');
    assert.deepEqual(JSON.parse(captured.init.body), body);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('stale semantic review conflicts surface the backend reason', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: 'Output findings changed; reload' }), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    });
  try {
    await assert.rejects(
      api.reviewAudioOutputFinding('episode-id', 'event-id', {
        decision: 'accepted',
        reviewer: 'Sam',
        evidence_note: 'Reviewed exact output.',
        expected_report_fingerprint: 'old-report',
        expected_event_fingerprint: 'old-event',
        expected_output_revision: 'old-output',
      }),
      (error) => error.status === 409 && error.message.includes('Output findings changed')
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
