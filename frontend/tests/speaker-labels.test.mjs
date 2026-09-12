import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(
  new URL('../src/lib/speaker-labels.ts', import.meta.url),
  'utf8'
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { displaySpeakerLabel, transcriptSpeakerLabels } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
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
