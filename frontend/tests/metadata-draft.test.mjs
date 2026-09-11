import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(
  new URL('../src/lib/metadata-draft.ts', import.meta.url),
  'utf8'
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { acceptSavedPatch, buildMetadataPatch, metadataValues } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
);

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
