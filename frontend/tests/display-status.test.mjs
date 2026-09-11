import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/lib/format.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { describeEpisodeStatus, episodeDisplayDuration } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
);

test('verified delivery status supersedes stale pipeline display status', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'awaiting_longform_review',
      delivery: { status: 'ready', video_status: 'ready' },
    }).key,
    'delivery_ready'
  );
});

test('audio-only delivery never claims the video is ready', () => {
  assert.equal(
    describeEpisodeStatus({
      status: 'ready_to_render',
      delivery: { status: 'ready', video_status: 'not_prepared' },
    }).key,
    'delivery_audio_ready'
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

test('display duration uses a ready delivery and otherwise falls back to source', () => {
  assert.equal(
    episodeDisplayDuration({
      duration_seconds: 5400,
      delivery: { status: 'ready', duration_seconds: 5267.7 },
    }),
    5267.7
  );
  assert.equal(
    episodeDisplayDuration({
      duration_seconds: 5400,
      delivery: { status: 'preparing', duration_seconds: 5267.7 },
    }),
    5400
  );
});
