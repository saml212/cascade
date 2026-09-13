import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const {
  appendCropSpeaker,
  cropBindingState,
  removalBlockedReason,
} = await importTs(new URL('../src/lib/crop-speakers.ts', import.meta.url));

function speaker(label) {
  return {
    label,
    x: 100,
    y: 200,
    zoom: 1.2,
    longform_x: null,
    longform_y: null,
    longform_zoom: 0.75,
    track: null,
    volume: 1,
  };
}

test('appending a speaker preserves stable existing crop indexes', () => {
  const original = [speaker('Laura'), speaker('Todd')];
  const appended = appendCropSpeaker(original, 1920, 1080);

  assert.equal(appended.length, 3);
  assert.equal(appended[0], original[0]);
  assert.equal(appended[1], original[1]);
  assert.deepEqual(appended[2], {
    label: 'Speaker 3',
    x: 1440,
    y: 540,
    zoom: 1.4,
    longform_x: null,
    longform_y: null,
    longform_zoom: 0.75,
    track: null,
    volume: 1,
  });

  const four = appendCropSpeaker(appended, 1920, 1080);
  assert.equal(appendCropSpeaker(four, 1920, 1080), four);
});

test('speaker bindings resolve explicit and canonical target crop indexes', () => {
  const bindings = cropBindingState(
    [
      {
        index: 2,
        person: 'Laura',
        crop_speaker_index: 0,
        mapping_method: 'manual_review',
      },
      {
        index: 0,
        label: 'Todd',
        target_speaker: 'speaker_1',
        mapping_method: 'diarization_segment_overlap',
      },
      { index: 3, label: 'Narrator', target_speaker: 'BOTH' },
      { index: 4, label: 'Out of range', target_speaker: 'speaker_8' },
    ],
    3
  );

  assert.deepEqual(bindings.byIndex[0], [
    { asrSpeaker: 2, label: 'Laura', reviewed: true },
  ]);
  assert.deepEqual(bindings.byIndex[1], [
    { asrSpeaker: 0, label: 'Todd', reviewed: false },
  ]);
  assert.deepEqual(bindings.byIndex[2], []);
  assert.deepEqual(bindings.unassigned, [
    { asrSpeaker: 3, label: 'Narrator', reviewed: false },
  ]);
});

test('only an unbound final speaker can be removed after identity loads', () => {
  const unbound = [[], [], []];
  assert.equal(removalBlockedReason(2, 3, unbound, 'ready'), null);
  assert.match(removalBlockedReason(1, 3, unbound, 'ready'), /final speaker/);
  assert.match(removalBlockedReason(1, 2, unbound, 'ready'), /At least 2/);
  assert.match(removalBlockedReason(2, 3, unbound, 'loading'), /Checking/);
  assert.match(removalBlockedReason(2, 3, unbound, 'unavailable'), /Retry/);

  const reviewed = [
    [],
    [],
    [{ asrSpeaker: 3, label: 'Sam', reviewed: true }],
  ];
  assert.match(removalBlockedReason(2, 3, reviewed, 'ready'), /ASR 3/);
});
