import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const {
  clipApprovalIdentity,
  clipIsApproved,
  reconcileClipApprovalFeedback,
  saveClipApproval,
} = await importTs(new URL('../src/lib/clip-approval.ts', import.meta.url));

function reviewedClip(revision, current = false) {
  return {
    id: 'clip_01',
    title: 'Current copy',
    metadata: { youtube: { title: 'Current title' } },
    review: {
      render: {
        current: true,
        recorded_fingerprint: 'sha256:current-render',
      },
      approval: { current, revision },
    },
  };
}

test('uses canonical revisions instead of incidental response shape', () => {
  const original = reviewedClip('sha256:current-copy');
  const reordered = {
    response_age_ms: 120,
    review: {
      approval: { revision: 'sha256:current-copy', current: false },
      render: {
        recorded_fingerprint: 'sha256:current-render',
        current: true,
        detail: 'Freshly polled',
      },
    },
    id: 'clip_01',
  };

  assert.equal(
    clipApprovalIdentity(reordered),
    clipApprovalIdentity(original)
  );
});

test('publishes saving feedback before the approval request resolves', async () => {
  const updates = [];
  let finishRequest = () => {};
  const request = new Promise((resolve) => {
    finishRequest = resolve;
  });

  const result = saveClipApproval(
    clipApprovalIdentity(reviewedClip('sha256:current-copy')),
    () => request,
    (feedback) => updates.push(feedback)
  );

  assert.equal(updates[0].status, 'saving');
  assert.equal(updates.length, 1);
  finishRequest();
  assert.equal((await result).status, 'approved');
  assert.equal(updates[1].status, 'approved');
});

test('keeps a failed request visible and retryable', async () => {
  const clip = reviewedClip('sha256:current-copy');
  const identity = clipApprovalIdentity(clip);
  let latest;
  const outcome = await saveClipApproval(
    identity,
    () => Promise.reject(new Error('network unavailable')),
    (feedback) => {
      latest = feedback;
    }
  );

  const reconciled = reconcileClipApprovalFeedback(
    new Map([['clip_01', latest]]),
    [structuredClone(clip)]
  );
  assert.equal(outcome.status, 'error');
  assert.equal(outcome.message, 'network unavailable');
  assert.equal(reconciled.get('clip_01'), latest);
  assert.equal(clipIsApproved(clip, latest), false);
});

test('keeps success through a stale poll until server approval is current', async () => {
  const clip = reviewedClip('sha256:current-copy');
  let approved;
  await saveClipApproval(
    clipApprovalIdentity(clip),
    () => Promise.resolve(),
    (feedback) => {
      approved = feedback;
    }
  );

  const stalePoll = reconcileClipApprovalFeedback(
    new Map([['clip_01', approved]]),
    [structuredClone(clip)]
  );
  assert.equal(stalePoll.get('clip_01'), approved);
  assert.equal(clipIsApproved(clip, approved), true);

  const confirmed = reviewedClip('sha256:current-copy', true);
  const confirmedPoll = reconcileClipApprovalFeedback(stalePoll, [confirmed]);
  assert.equal(confirmedPoll.size, 0);
  assert.equal(clipIsApproved(confirmed, undefined), true);
});

test('clears local acknowledgement when the server revision changes', () => {
  const original = reviewedClip('sha256:current-copy');
  const feedback = {
    status: 'approved',
    identity: clipApprovalIdentity(original),
  };
  const changed = reviewedClip('sha256:changed-copy');

  const reconciled = reconcileClipApprovalFeedback(
    new Map([['clip_01', feedback]]),
    [changed]
  );

  assert.equal(reconciled.size, 0);
  assert.equal(clipIsApproved(changed, feedback), false);
});
