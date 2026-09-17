import assert from 'node:assert/strict';
import test from 'node:test';

import { dataUrl, transpileTs } from './load-ts.mjs';

async function loadRouter(initialPath) {
  const listeners = new Map();
  const previousWindow = globalThis.window;
  const location = { hash: `#${initialPath}` };
  globalThis.window = {
    location,
    addEventListener: (name, handler) => listeners.set(name, handler),
  };

  const source = await transpileTs(
    new URL('../src/lib/router.ts', import.meta.url)
  );
  const compiled = source.replace(
    /^import \{ signal, effectScope \} from ['"]\.\/signals['"];$/m,
    `
      const signal = (initial) => {
        let value = initial;
        const read = () => value;
        read.set = (next) => { value = typeof next === 'function' ? next(value) : next; };
        read.peek = () => value;
        return read;
      };
      const effectScope = (run) => {
        run();
        let disposed = false;
        return () => {
          if (disposed) return;
          disposed = true;
          globalThis.__routerDisposals += 1;
        };
      };
    `
  );
  const router = await import(dataUrl(`${compiled}\n// ${crypto.randomUUID()}`));

  return {
    router,
    setPath(path) {
      location.hash = `#${path}`;
      listeners.get('hashchange')?.();
    },
    restore() {
      globalThis.window = previousWindow;
    },
  };
}

test('same-episode aliases preserve the screen across navigation history', async () => {
  globalThis.__routerDisposals = 0;
  const harness = await loadRouter('/episodes/episode%20one');
  const mounts = [];
  const episodeIdentity = (params) => `episode:${params.id}`;

  try {
    for (const pattern of [
      '/episodes/:id',
      '/episodes/:id/audio',
      '/episodes/:id/metadata',
      '/episodes/:id/delivery',
    ]) {
      harness.router.route(
        pattern,
        (params) => mounts.push(`episode:${params.id}`),
        { screenIdentity: episodeIdentity }
      );
    }
    harness.router.route('/episodes/:id/longform/review', (params) => {
      mounts.push(`longform:${params.id}`);
    });
    harness.router.setFallback(() => mounts.push('fallback'));
    harness.router.startRouter();

    assert.deepEqual(mounts, ['episode:episode one']);
    assert.equal(harness.router.currentPath(), '/episodes/episode%20one');

    harness.setPath('/episodes/episode%20one/audio');
    harness.setPath('/episodes/episode%20one/metadata');
    harness.setPath('/episodes/episode%20one/delivery');
    harness.setPath('/episodes/episode%20one/audio');

    assert.deepEqual(mounts, ['episode:episode one']);
    assert.equal(globalThis.__routerDisposals, 0);
    assert.equal(harness.router.currentPath(), '/episodes/episode%20one/audio');

    harness.setPath('/episodes/episode%20two/audio');
    assert.deepEqual(mounts, ['episode:episode one', 'episode:episode two']);
    assert.equal(globalThis.__routerDisposals, 1);

    harness.setPath('/episodes/episode%20two/longform/review');
    assert.deepEqual(mounts, [
      'episode:episode one',
      'episode:episode two',
      'longform:episode two',
    ]);
    assert.equal(globalThis.__routerDisposals, 2);

    harness.setPath('/episodes/episode%20two/metadata');
    assert.deepEqual(mounts, [
      'episode:episode one',
      'episode:episode two',
      'longform:episode two',
      'episode:episode two',
    ]);
    assert.equal(globalThis.__routerDisposals, 3);
  } finally {
    harness.restore();
    delete globalThis.__routerDisposals;
  }
});

test('a failed first mount retries through a same-episode alias', async () => {
  globalThis.__routerDisposals = 0;
  const harness = await loadRouter('/episodes/retry');
  let attempts = 0;
  const mount = () => {
    attempts += 1;
    if (attempts === 1) throw new Error('first mount failed');
  };
  const options = { screenIdentity: (params) => `episode:${params.id}` };

  try {
    harness.router.route('/episodes/:id', mount, options);
    harness.router.route('/episodes/:id/audio', mount, options);

    assert.throws(() => harness.router.startRouter(), /first mount failed/);
    harness.setPath('/episodes/retry/audio');

    assert.equal(attempts, 2);
    assert.equal(harness.router.currentPath(), '/episodes/retry/audio');
    assert.equal(globalThis.__routerDisposals, 0);
  } finally {
    harness.restore();
    delete globalThis.__routerDisposals;
  }
});
