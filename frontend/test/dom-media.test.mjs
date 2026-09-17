import assert from 'node:assert/strict';
import test from 'node:test';

import { dataUrl, transpileTs } from './load-ts.mjs';

const compiled = await transpileTs(
  new URL('../src/lib/dom.ts', import.meta.url)
);
const { h } = await import(
  dataUrl(
    compiled.replace(
      /^import \{ onCleanup \} from ['"]\.\/signals['"];$/m,
      'const onCleanup = (dispose) => globalThis.__domCleanups.push(dispose);'
    )
  )
);

test('owned media cleanup detaches the source and resets the decoder', () => {
  const previousDocument = globalThis.document;
  const previousMedia = globalThis.HTMLMediaElement;
  globalThis.__domCleanups = [];

  class FakeMediaElement {
    events = [];
    source = null;

    setAttribute(name, value) {
      if (name === 'src') this.source = value;
    }

    pause() {
      this.events.push('pause');
    }

    removeAttribute(name) {
      this.events.push(`remove:${name}`);
      if (name === 'src') this.source = null;
    }

    load() {
      this.events.push('load');
    }
  }

  try {
    globalThis.HTMLMediaElement = FakeMediaElement;
    globalThis.document = {
      createElement: () => new FakeMediaElement(),
    };
    const media = h('video', { src: '/media/clip.mp4' });
    assert.equal(media.source, '/media/clip.mp4');
    assert.equal(globalThis.__domCleanups.length, 1);

    globalThis.__domCleanups[0]();

    assert.equal(media.source, null);
    assert.deepEqual(media.events, ['pause', 'remove:src', 'load']);
  } finally {
    globalThis.document = previousDocument;
    globalThis.HTMLMediaElement = previousMedia;
    delete globalThis.__domCleanups;
  }
});
