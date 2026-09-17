import assert from 'node:assert/strict';
import test from 'node:test';

import { importTs } from './load-ts.mjs';

const { canPrepareDeliveryVideo, showDeliveryVideoSection } = await importTs(
  new URL('../src/lib/delivery-view.ts', import.meta.url)
);

test('current video remains reviewable when podcast MP3 is not prepared', () => {
  const delivery = {
    status: 'not_prepared',
    video_status: 'ready',
    video_download_url: '/api/episodes/ep_test/delivery/video?v=current',
    video: { filename: 'upload_video.mp4' },
  };

  assert.equal(showDeliveryVideoSection(delivery), true);
  assert.equal(canPrepareDeliveryVideo(delivery), false);
});

test('audio-ready delivery keeps its video preparation section', () => {
  const delivery = {
    status: 'ready',
    video_status: 'not_prepared',
  };

  assert.equal(showDeliveryVideoSection(delivery), true);
  assert.equal(canPrepareDeliveryVideo(delivery), true);
});

test('unprepared audio and video do not expose video controls', () => {
  assert.equal(
    showDeliveryVideoSection({
      status: 'not_prepared',
      video_status: 'not_prepared',
    }),
    false
  );
});
