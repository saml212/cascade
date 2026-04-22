/**
 * Minimal Web Audio graph for sync + mixer preview.
 *
 * Builds a ctx + per-track buffer + gain + delay graph so the SyncVerifier
 * and TrackMixer can play camera + H6E in sync with the current offset
 * applied, and so Sam can mute / solo individual tracks in the mixer.
 *
 * Preview scope is intentionally small: load up to ~2 minutes from each
 * source URL, decode once, then control via play/pause. No streaming.
 */

export interface TrackBinding {
  key: string;
  url: string;
  delaySeconds?: number;
}

export interface AudioGraph {
  ctx: AudioContext;
  /** Promise that resolves when all tracks are decoded and ready. */
  ready: Promise<void>;
  /** Tracks keyed by their input TrackBinding.key. */
  tracks: Map<string, TrackNode>;
  play(): void;
  pause(): void;
  stop(): void;
  setTrackGain(key: string, value: number): void;
  setTrackMute(key: string, muted: boolean): void;
  setSolo(key: string | null): void;
  updateDelay(key: string, seconds: number): void;
  /** Current position inside the loaded preview, in seconds. */
  currentTime(): number;
  /** Duration of the longest loaded track. */
  duration(): number;
  dispose(): void;
}

interface TrackNode {
  key: string;
  buffer: AudioBuffer | null;
  source: AudioBufferSourceNode | null;
  gain: GainNode;
  delay: DelayNode;
  delaySeconds: number;
  baseGain: number;
  muted: boolean;
  loading: boolean;
  error: string | null;
}

export function createAudioGraph(bindings: TrackBinding[]): AudioGraph {
  const Ctx =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext: typeof AudioContext })
      .webkitAudioContext;
  const ctx = new Ctx();
  const tracks = new Map<string, TrackNode>();
  let playing = false;
  let startedAtCtxTime = 0;
  let pausedOffset = 0;
  let solo: string | null = null;

  for (const b of bindings) {
    const gain = ctx.createGain();
    gain.gain.value = 1;
    const delay = ctx.createDelay(10);
    delay.delayTime.value = b.delaySeconds ?? 0;
    delay.connect(gain).connect(ctx.destination);
    tracks.set(b.key, {
      key: b.key,
      buffer: null,
      source: null,
      gain,
      delay,
      delaySeconds: b.delaySeconds ?? 0,
      baseGain: 1,
      muted: false,
      loading: true,
      error: null,
    });
  }

  const ready = Promise.all(
    bindings.map(async (b) => {
      const node = tracks.get(b.key)!;
      try {
        const res = await fetch(b.url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const arr = await res.arrayBuffer();
        const buffer = await ctx.decodeAudioData(arr);
        node.buffer = buffer;
      } catch (e) {
        node.error = (e as Error).message;
      } finally {
        node.loading = false;
      }
    })
  ).then(() => undefined);

  function stopSources(): void {
    for (const t of tracks.values()) {
      if (t.source) {
        try {
          t.source.stop();
        } catch {
          /* already stopped */
        }
        t.source.disconnect();
        t.source = null;
      }
    }
  }

  function startSources(offsetSeconds: number): void {
    const now = ctx.currentTime;
    for (const t of tracks.values()) {
      if (!t.buffer) continue;
      const src = ctx.createBufferSource();
      src.buffer = t.buffer;
      src.connect(t.delay);
      const safeOffset = Math.max(0, offsetSeconds);
      if (safeOffset < t.buffer.duration) {
        src.start(now, safeOffset);
        t.source = src;
      }
    }
    startedAtCtxTime = now - offsetSeconds;
  }

  function applyMix(): void {
    for (const t of tracks.values()) {
      const hide = solo != null && solo !== t.key;
      const effective = t.muted || hide ? 0 : t.baseGain;
      t.gain.gain.setTargetAtTime(effective, ctx.currentTime, 0.02);
    }
  }

  return {
    ctx,
    ready,
    tracks,
    play(): void {
      if (playing) return;
      if (ctx.state === 'suspended') ctx.resume();
      startSources(pausedOffset);
      playing = true;
      applyMix();
    },
    pause(): void {
      if (!playing) return;
      pausedOffset = Math.max(0, ctx.currentTime - startedAtCtxTime);
      stopSources();
      playing = false;
    },
    stop(): void {
      stopSources();
      playing = false;
      pausedOffset = 0;
    },
    setTrackGain(key, value): void {
      const t = tracks.get(key);
      if (!t) return;
      t.baseGain = Math.max(0, Math.min(value, 3));
      applyMix();
    },
    setTrackMute(key, muted): void {
      const t = tracks.get(key);
      if (!t) return;
      t.muted = muted;
      applyMix();
    },
    setSolo(key): void {
      solo = key;
      applyMix();
    },
    updateDelay(key, seconds): void {
      const t = tracks.get(key);
      if (!t) return;
      t.delaySeconds = Math.max(0, seconds);
      t.delay.delayTime.setTargetAtTime(
        t.delaySeconds,
        ctx.currentTime,
        0.02
      );
    },
    currentTime(): number {
      if (playing) return Math.max(0, ctx.currentTime - startedAtCtxTime);
      return pausedOffset;
    },
    duration(): number {
      let max = 0;
      for (const t of tracks.values()) {
        if (t.buffer && t.buffer.duration > max) max = t.buffer.duration;
      }
      return max;
    },
    dispose(): void {
      stopSources();
      ctx.close().catch(() => {});
    },
  };
}
