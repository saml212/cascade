import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { displaySpeakerLabel, transcriptSpeakerLabels } = await importTs(
  new URL('../src/lib/speaker-labels.ts', import.meta.url)
);

test('canonical transcript labels replace machine speaker identifiers', () => {
  const labels = transcriptSpeakerLabels([
    { index: 0, label: 'Christopher' },
    { index: 1, label: 'Host' },
  ]);

  assert.equal(displaySpeakerLabel('speaker_0', labels), 'Christopher');
  assert.equal(displaySpeakerLabel('speaker_1', labels), 'Host');
});

test('dict maps work and missing identities remain neutral', () => {
  const labels = transcriptSpeakerLabels({ 0: 'Arnold' });

  assert.equal(displaySpeakerLabel('speaker_0', labels), 'Arnold');
  assert.equal(displaySpeakerLabel('speaker_2', labels), 'Speaker 3');
  assert.equal(displaySpeakerLabel('BOTH', labels), 'BOTH');
  assert.equal(displaySpeakerLabel(undefined, labels), null);
});
