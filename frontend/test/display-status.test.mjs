import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const {
  describeEpisodeStatus,
  episodeDisplayDuration,
  isVideoPreparationActive,
} = await importTs(new URL('../src/lib/format.ts', import.meta.url));

test('rendered delivery without a quality decision still requires review', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'awaiting_longform_review',
      delivery: { video_status: 'ready' },
    }).key,
    'quality_review_required'
  );
});

test('only the current release gate presents rendered delivery as ready', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'awaiting_longform_review',
      delivery: { video_status: 'ready' },
      quality: {
        quality: { status: 'passed' },
        release_gate: { status: 'ready' },
      },
    }).key,
    'delivery_ready'
  );
});

test('missing and stale quality reports stay in review', () => {
  for (const qualityStatus of ['missing', 'stale']) {
    assert.equal(
      describeEpisodeStatus({
        delivery: { video_status: 'ready' },
        quality: {
          quality: { status: qualityStatus },
          release_gate: { status: 'blocked' },
        },
      }).key,
      'quality_review_required'
    );
  }
});

test('a current failed report presents rendered delivery as blocked', () => {
  assert.equal(
    describeEpisodeStatus({
      delivery: { video_status: 'ready' },
      quality: {
        quality: { status: 'blocked' },
        release_gate: { status: 'blocked' },
      },
    }).key,
    'quality_blocked'
  );
});

test('passed quality still requires explicit publish approval', () => {
  assert.equal(
    describeEpisodeStatus({
      delivery: { video_status: 'ready' },
      quality: {
        quality: { status: 'passed' },
        release_gate: { status: 'awaiting_publish_approval' },
      },
    }).key,
    'awaiting_publish'
  );
});

test('passed technical QA labels pending editorial decisions as approval work', () => {
  const status = describeEpisodeStatus({
    delivery: { video_status: 'ready' },
    quality: {
      quality: { status: 'passed' },
      release_gate: {
        status: 'blocked',
        blockers: [
          { code: 'clips_pending_review' },
          { code: 'editorial_approval_missing_or_stale' },
          { code: 'publish_approval_missing_or_stale' },
        ],
      },
    },
  });

  assert.equal(status.key, 'approval_required');
  assert.equal(status.label, 'Approval needed');
  assert.match(status.hint, /Technical QA passed/);
});

test('historical audio state does not replace the pipeline status', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'ready_to_render',
      delivery: { status: 'ready', video_status: 'not_prepared' },
    }).key,
    'ready_to_render'
  );
});

test('historical audio preparation does not present as active video work', () => {
  assert.equal(
    isVideoPreparationActive({
      status: 'preparing',
      video_status: 'not_prepared',
    }),
    false
  );
  assert.equal(
    isVideoPreparationActive({
      status: 'ready',
      video_status: 'preparing',
    }),
    true
  );
});

test('live publishing status retains display priority', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'published',
      delivery: { status: 'preparing', video_status: 'preparing' },
    }).key,
    'live'
  );
});

test('display duration uses a ready video and otherwise falls back to source', () => {
  assert.equal(
    episodeDisplayDuration({
      duration_seconds: 5400,
      delivery: {
        video_status: 'ready',
        video: { duration_seconds: 5267.7 },
      },
    }),
    5267.7
  );
  assert.equal(
    episodeDisplayDuration({
      duration_seconds: 5400,
      delivery: {
        status: 'ready',
        video_status: 'preparing',
        video: { duration_seconds: 5267.7 },
      },
    }),
    5400
  );
});
