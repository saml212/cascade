import assert from 'node:assert/strict';
import test from 'node:test';

import { dataUrl, transpileTs } from './load-ts.mjs';

async function importScreen(path) {
  const source = await transpileTs(new URL(path, import.meta.url));
  return import(dataUrl(source.replace(/^import .*;$/gm, '')));
}

const { CLIP_METADATA_PLATFORMS } = await importScreen(
  '../src/screens/clip-review.ts'
);
const { DESTINATION_COLORS } = await importScreen('../src/screens/publish.ts');

test('defines the enabled destination editor fields and limits', () => {
  const specs = Object.fromEntries(
    CLIP_METADATA_PLATFORMS.map(({ key, label, fields }) => [
      key,
      {
        label,
        fields: fields.map(({ name, maxLength, hint }) => ({
          name,
          maxLength: maxLength ?? null,
          hint,
        })),
      },
    ])
  );

  assert.deepEqual(specs.facebook, {
    label: 'Facebook Reels',
    fields: [
      { name: 'title', maxLength: 255, hint: 'Max 255 chars' },
      {
        name: 'description',
        maxLength: 63_206,
        hint: 'Max 63,206 chars',
      },
    ],
  });
  assert.deepEqual(specs.threads, {
    label: 'Threads',
    fields: [
      { name: 'text', maxLength: null, hint: 'Max 500 UTF-8 bytes' },
    ],
  });
  assert.deepEqual(specs.bluesky, {
    label: 'Bluesky',
    fields: [{ name: 'text', maxLength: 300, hint: 'Max 300 chars' }],
  });
  assert.deepEqual(specs.linkedin, {
    label: 'LinkedIn',
    fields: [
      {
        name: 'title',
        maxLength: 400,
        hint: 'Max 400 UTF-16 units',
      },
      {
        name: 'description',
        maxLength: 3_000,
        hint: 'Max 3,000 chars',
      },
    ],
  });
  assert.deepEqual(specs.pinterest, {
    label: 'Pinterest',
    fields: [
      { name: 'title', maxLength: 100, hint: 'Max 100 chars' },
      { name: 'description', maxLength: 800, hint: 'Max 800 chars' },
    ],
  });
});

test('assigns visible colors to every new publish destination', () => {
  for (const platform of [
    'facebook',
    'threads',
    'bluesky',
    'linkedin',
    'pinterest',
  ]) {
    assert.match(DESTINATION_COLORS[platform], /^#[0-9a-f]{6}$/i);
  }
});
