import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const episodeSource = await readFile(
  new URL('../src/screens/episode/index.ts', import.meta.url),
  'utf8',
);
const stateSource = await readFile(
  new URL('../src/state/episodes.ts', import.meta.url),
  'utf8',
);

test('slow Schedule work stays behind local refresh and outside recurring polls', () => {
  const refreshAll = episodeSource.match(
    /async function refreshAll\(\): Promise<void> \{(?<body>.*?)\n  \}/s,
  );
  assert.match(refreshAll?.groups?.body ?? '', /await refreshCanonical\(\);\s+void refreshSchedule\(\);/);
  assert.equal((episodeSource.match(/void refreshSchedule\(\);/g) ?? []).length, 1);
  assert.equal((episodeSource.match(/void refreshAll\(\);/g) ?? []).length, 1);
  assert.equal((episodeSource.match(/context\.refreshAll\(\)/g) ?? []).length, 1);
  assert.match(episodeSource, /onUpdated: context\.refreshCanonical/);
  assert.match(episodeSource, /setTimeout\(\(\) => void refreshDelivery\(\), 2000\)/);
  assert.match(stateSource, /DETAIL_POLL_MS = 4000/);
});
