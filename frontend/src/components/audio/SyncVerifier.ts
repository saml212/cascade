/**
 * H6E sync verification — dual-waveform canvas + offset slider.
 *
 * Pulls /api/episodes/:id/sync-preview which returns:
 *   { camera_waveform: number[], h6e_waveform: number[],
 *     offset_seconds, duration, peaks_per_second, confidence,
 *     drift_rate_ppm, tempo_factor }
 *
 * Camera peaks are drawn in the top half of the canvas (amber),
 * H6E peaks in the bottom half (teal), shifted by the current offset.
 * The user nudges the offset via the slider or ± buttons until the
 * transients line up, then hits Save to persist.
 */

import { h } from '../../lib/dom';
import { signal, effect, type Signal } from '../../lib/signals';
import { api } from '../../lib/api';
import { formatOffsetMs } from '../../lib/format';
import { Button } from '../Button';
import { showToast } from '../../state/ui';

interface SyncState {
  cameraPeaks: Float32Array | null;
  h6ePeaks: Float32Array | null;
  peaksPerSecond: number;
  duration: number;
  originalOffset: number;
  currentOffset: number;
  confidence: number;
  drift: number;
  loading: boolean;
  saving: boolean;
  error: string | null;
  viewStart: number; // seconds
  viewEnd: number;
}

export function SyncVerifier(episodeId: string): HTMLElement {
  const state = signal<SyncState>({
    cameraPeaks: null,
    h6ePeaks: null,
    peaksPerSecond: 100,
    duration: 0,
    originalOffset: 0,
    currentOffset: 0,
    confidence: 0,
    drift: 0,
    loading: true,
    saving: false,
    error: null,
    viewStart: 0,
    viewEnd: 0,
  });

  (async () => {
    try {
      const data = (await api.syncPreview(episodeId)) as Record<string, unknown>;
      const camera = new Float32Array(data.camera_waveform as number[]);
      const h6e = new Float32Array(data.h6e_waveform as number[]);
      state.set({
        cameraPeaks: camera,
        h6ePeaks: h6e,
        peaksPerSecond: (data.peaks_per_second as number) ?? 100,
        duration: (data.duration as number) ?? 0,
        originalOffset: (data.offset_seconds as number) ?? 0,
        currentOffset: (data.offset_seconds as number) ?? 0,
        confidence: (data.confidence as number) ?? 0,
        drift: (data.drift_rate_ppm as number) ?? 0,
        loading: false,
        saving: false,
        error: null,
        viewStart: 0,
        viewEnd: (data.duration as number) ?? 0,
      });
    } catch (e) {
      state.set((prev) => ({
        ...prev,
        loading: false,
        error: (e as Error).message,
      }));
    }
  })();

  const canvas = document.createElement('canvas');
  canvas.className = 'w-full h-48 block rounded-md bg-surface-inset';

  const statsRow = h('div', {
    class:
      'grid grid-cols-[1fr_1fr_1fr_auto] gap-5 items-end mt-3',
  });

  const controlRow = h('div', {
    class: 'flex flex-wrap items-center gap-2 mt-4',
  });

  const host = h(
    'div',
    { class: 'panel p-5' },
    h(
      'div',
      { class: 'flex items-baseline justify-between mb-3' },
      h(
        'span',
        { class: 'text-heading-sm uppercase text-ink-tertiary' },
        'Audio sync — camera vs. H6E'
      ),
      h(
        'span',
        { class: 'text-body-sm text-ink-tertiary' },
        'Drag or nudge until the transients line up, then save.'
      )
    ),
    canvas,
    statsRow,
    controlRow
  );

  // Redraw on any state change.
  effect(() => {
    const s = state();
    drawWaveform(canvas, s);
    statsRow.replaceChildren(
      stat('Offset', formatOffsetMs(s.currentOffset)),
      stat(
        'Auto-detected',
        formatOffsetMs(s.originalOffset),
        s.currentOffset !== s.originalOffset ? 'text-ink-tertiary' : 'text-ink-primary'
      ),
      stat(
        'Confidence',
        s.confidence > 0 ? `${Math.round(s.confidence * 100)}%` : '—',
        confidenceTone(s.confidence)
      ),
      stat('Drift', s.drift !== 0 ? `${s.drift.toFixed(1)} ppm` : '—')
    );
    controlRow.replaceChildren(...renderControls(episodeId, state, s));
  });

  // Resize: canvas width matches container, rerender
  const ro = new ResizeObserver(() => {
    canvas.width = canvas.clientWidth || 800;
    canvas.height = 160;
    drawWaveform(canvas, state.peek());
  });
  ro.observe(canvas);

  // Click-seek: not wired to real audio yet (no playback layer), but we
  // still let users scrub the view by dragging the horizontal ruler.
  // Scroll-wheel on the canvas nudges offset by 1px worth of time.
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const step = e.shiftKey ? 0.5 : 0.05;
    state.set((prev) => ({
      ...prev,
      currentOffset: roundOffset(prev.currentOffset + (e.deltaY > 0 ? step : -step)),
    }));
  });

  return host;
}

function confidenceTone(c: number): string {
  if (c >= 0.8) return 'text-status-success';
  if (c >= 0.5) return 'text-status-warning';
  if (c > 0) return 'text-status-danger';
  return 'text-ink-tertiary';
}

function stat(label: string, value: string, tone = 'text-ink-primary'): HTMLElement {
  return h(
    'div',
    null,
    h(
      'div',
      { class: 'text-heading-sm uppercase text-ink-tertiary mb-1' },
      label
    ),
    h(
      'div',
      {
        class: `text-body-lg ${tone} font-mono tabular`,
      },
      value
    )
  );
}

function renderControls(
  episodeId: string,
  state: Signal<SyncState>,
  s: SyncState
): Node[] {
  const nudge = (delta: number): HTMLElement =>
    h(
      'button',
      {
        onclick: () =>
          state.set({
            ...state.peek(),
            currentOffset: roundOffset(state.peek().currentOffset + delta),
          }),
        class:
          'h-8 px-2.5 rounded-md border border-border bg-surface-2 text-body-sm font-mono tabular text-ink-primary hover:bg-surface-3',
      },
      (delta > 0 ? '+' : '') + delta.toFixed(2) + 's'
    );

  const numericInput = h('input', {
    type: 'number',
    step: '0.01',
    value: s.currentOffset.toFixed(2),
    class:
      'w-28 h-9 bg-surface-2 border border-border rounded-md px-2.5 text-body font-mono tabular text-ink-primary focus:border-accent focus:outline-none',
    oninput: (e: Event) => {
      const v = Number((e.target as HTMLInputElement).value);
      if (!isNaN(v)) state.set({ ...state.peek(), currentOffset: v });
    },
  });

  const resetBtn = Button({
    variant: 'ghost',
    size: 'sm',
    label: 'Reset to auto',
    onClick: () =>
      state.set({ ...state.peek(), currentOffset: state.peek().originalOffset }),
    disabled: s.currentOffset === s.originalOffset,
  });

  const saveBtn = Button({
    variant: 'primary',
    size: 'sm',
    label: s.saving ? 'Saving…' : 'Save offset',
    loading: s.saving,
    disabled: s.currentOffset === s.originalOffset || s.saving,
    onClick: async () => {
      state.set({ ...state.peek(), saving: true });
      try {
        await api.saveSyncOffset(episodeId, state.peek().currentOffset);
        state.set({
          ...state.peek(),
          saving: false,
          originalOffset: state.peek().currentOffset,
        });
        showToast('Sync offset saved.', 'success');
      } catch (e) {
        state.set({ ...state.peek(), saving: false });
        showToast((e as Error).message, 'error');
      }
    },
  });

  return [
    h(
      'span',
      {
        class: 'text-heading-sm uppercase text-ink-tertiary mr-1',
      },
      'Nudge'
    ),
    nudge(-1),
    nudge(-0.1),
    nudge(-0.01),
    nudge(0.01),
    nudge(0.1),
    nudge(1),
    h('span', { class: 'mx-2 text-ink-disabled' }, '·'),
    numericInput,
    h('span', { class: 'text-body-sm text-ink-tertiary' }, 'seconds'),
    h('div', { class: 'flex-1' }),
    resetBtn,
    saveBtn,
  ];
}

function roundOffset(v: number): number {
  return Math.round(v * 100) / 100;
}

/* ------------------------------ Canvas drawing ---------------------------- */

function drawWaveform(canvas: HTMLCanvasElement, s: SyncState): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (s.loading) {
    ctx.fillStyle = 'rgba(184, 178, 164, 0.35)';
    ctx.font = '500 13px "Satoshi", ui-sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Loading waveforms…', w / 2, h / 2);
    return;
  }
  if (s.error || !s.cameraPeaks || !s.h6ePeaks) {
    ctx.fillStyle = 'rgba(226, 109, 90, 0.8)';
    ctx.font = '500 13px "Satoshi", ui-sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(
      s.error ?? 'No waveform data available.',
      w / 2,
      h / 2
    );
    return;
  }

  const midY = h / 2;
  const halfH = h / 2 - 14;
  const view = s.viewEnd - s.viewStart;

  // Horizontal guide lines
  ctx.strokeStyle = 'rgba(42, 42, 47, 1)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, midY);
  ctx.lineTo(w, midY);
  ctx.stroke();

  // Axis ticks every 10s
  ctx.fillStyle = 'rgba(122, 116, 102, 0.8)';
  ctx.font = '500 10px "JetBrains Mono", ui-monospace';
  ctx.textAlign = 'left';
  for (let sec = Math.ceil(s.viewStart / 10) * 10; sec < s.viewEnd; sec += 10) {
    const x = ((sec - s.viewStart) / view) * w;
    ctx.strokeStyle = 'rgba(31, 31, 34, 0.9)';
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    ctx.fillText(`${sec}s`, x + 3, h - 4);
  }

  // Draw camera peaks (top half, accent-amber)
  drawHalf(ctx, s.cameraPeaks, s.peaksPerSecond, s.viewStart, s.viewEnd, w, midY, halfH, '#f5a524', 0);
  // Draw h6e peaks (bottom half, teal, shifted by offset)
  drawHalf(ctx, s.h6ePeaks, s.peaksPerSecond, s.viewStart, s.viewEnd, w, midY, halfH, '#6bb7b7', -s.currentOffset);

  // Track labels
  ctx.textAlign = 'right';
  ctx.fillStyle = '#f5a524';
  ctx.font = '600 10px "Satoshi", ui-sans-serif';
  ctx.fillText('CAMERA', w - 6, 12);
  ctx.fillStyle = '#6bb7b7';
  ctx.fillText('H6E', w - 6, h - 18);
}

function drawHalf(
  ctx: CanvasRenderingContext2D,
  peaks: Float32Array,
  peaksPerSecond: number,
  viewStart: number,
  viewEnd: number,
  canvasWidth: number,
  midY: number,
  halfH: number,
  color: string,
  offset: number // seconds added to the time axis before sampling
): void {
  // Peak values come through as raw RMS (typical range 0.01-0.15 for speech).
  // Multiply by a visibility gain so dialog-level peaks actually read on
  // screen — clamped so spikes don't overflow the lane.
  const GAIN = 4;
  const isTop = color === '#f5a524';
  ctx.fillStyle = color;
  const viewSpan = viewEnd - viewStart;
  for (let px = 0; px < canvasWidth; px++) {
    const sec = viewStart + (px / canvasWidth) * viewSpan + offset;
    const idx = Math.floor(sec * peaksPerSecond);
    if (idx < 0 || idx >= peaks.length) continue;
    const amp = peaks[idx] * GAIN;
    const barH = Math.min(halfH, Math.max(0.5, amp * halfH));
    if (isTop) ctx.fillRect(px, midY - barH, 1, barH);
    else ctx.fillRect(px, midY, 1, barH);
  }
}
