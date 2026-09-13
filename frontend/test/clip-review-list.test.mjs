import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import ts from 'typescript';

const source = await readFile(
  new URL('../src/lib/clip-review-list.ts', import.meta.url),
  'utf8'
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`;
const { groupClipReviewCandidates, isRejectedClipId } = await import(moduleUrl);

function candidate(id, status, current = false) {
  return {
    id,
    review: {
      selection: { status },
      render: { current },
    },
  };
}

test('separates rejected clips and prioritizes selected/current work', () => {
  const clips = [
    candidate('rejected-old', 'rejected', true),
    candidate('unselected-stale', 'unselected'),
    candidate('selected-stale', 'selected'),
    candidate('unselected-current', 'unselected', true),
    candidate('selected-current', 'selected', true),
  ];

  const groups = groupClipReviewCandidates(clips);

  assert.deepEqual(
    groups.active.map((clip) => clip.id),
    [
      'selected-current',
      'selected-stale',
      'unselected-current',
      'unselected-stale',
    ]
  );
  assert.deepEqual(groups.rejected.map((clip) => clip.id), ['rejected-old']);
  assert.deepEqual(clips.map((clip) => clip.id), [
    'rejected-old',
    'unselected-stale',
    'selected-stale',
    'unselected-current',
    'selected-current',
  ]);
});

test('preserves source order when candidates have equal priority', () => {
  const clips = [
    candidate('first-selected', 'selected'),
    candidate('second-selected', 'selected'),
    candidate('first-rejected', 'rejected'),
    candidate('second-rejected', 'rejected'),
  ];

  const groups = groupClipReviewCandidates(clips);

  assert.deepEqual(groups.active.map((clip) => clip.id), [
    'first-selected',
    'second-selected',
  ]);
  assert.deepEqual(groups.rejected.map((clip) => clip.id), [
    'first-rejected',
    'second-rejected',
  ]);
});

test('identifies an exact rejected deep link by either clip id field', () => {
  const clips = [
    candidate('selected', 'selected'),
    { ...candidate(undefined, 'rejected'), clip_id: 'legacy-rejected' },
  ];

  assert.equal(isRejectedClipId(clips, 'legacy-rejected'), true);
  assert.equal(isRejectedClipId(clips, 'selected'), false);
  assert.equal(isRejectedClipId(clips, 'missing'), false);
});
