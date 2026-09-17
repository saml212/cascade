import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { acceptSavedPatch, buildMetadataPatch, metadataValues } = await importTs(new URL('../src/lib/metadata-draft.ts', import.meta.url));

test('metadata draft retains all ten reviewed fields', () => {
  assert.deepEqual(Object.keys(metadataValues({})), [
    'guest_name',
    'guest_title',
    'episode_name',
    'episode_description',
    'title',
    'description',
    'tags',
    'youtube_longform_url',
    'spotify_longform_url',
    'link_tree_url',
  ]);
});

test('saving a title does not overwrite a remotely populated URL', () => {
  const saved = metadataValues({ title: 'Draft', youtube_longform_url: '' });
  const edited = { ...saved, title: 'Final title' };

  const patch = buildMetadataPatch(saved, edited);

  assert.deepEqual(patch, { title: 'Final title' });
  assert.equal('youtube_longform_url' in patch, false);
});

test('typing during an in-flight save remains a pending change', () => {
  const saved = metadataValues({ title: 'Draft' });
  const submitted = { ...saved, title: 'First edit' };
  const typedWhileSaving = { ...submitted, title: 'Second edit' };
  const confirmed = acceptSavedPatch(saved, submitted, ['title']);

  assert.deepEqual(buildMetadataPatch(confirmed, typedWhileSaving), {
    title: 'Second edit',
  });
});
