/**
 * Crop Setup — one editing surface for speaker crops + track assignment.
 *
 * Full-width page with the agent panel collapsed. Left column hosts the
 * crop canvas + placement controls; right column is the speakers panel
 * with zoom sliders, track assignments, and the save CTA.
 *
 * Crop math mirrors lib/crop.py (canonical) and the legacy frontend's
 * redrawCropCanvas():
 *   Shorts (9:16): crop_h = srcH / zoom, crop_w = crop_h * 9 / 16
 *   Longform (16:9): crop_w = srcW / (2 * zoom), crop_h = crop_w * 9 / 16
 *   Wide (16:9): crop_w = srcW / zoom, crop_h = crop_w * 9 / 16
 *
 * Audio sync + mixer for H6E episodes ship in a follow-up pass.
 */

import { h, mount } from '../lib/dom';
import { effect, signal, type Signal } from '../lib/signals';
import { api, type SpeakerCropConfig, type CropConfigRequest } from '../lib/api';
import {
  describeStatus,
  episodeDateLabel,
  formatDuration,
  formatTimecode,
} from '../lib/format';
import { episodeDetail, watchEpisode } from '../state/episodes';
import { agentPanelCollapsed, showToast } from '../state/ui';
import { StatusPill } from '../components/StatusPill';
import { Button } from '../components/Button';
import { Icon } from '../components/icons';
import { SyncVerifier } from '../components/audio/SyncVerifier';
import { TrackMixer } from '../components/audio/TrackMixer';
import { link, navigate } from '../lib/router';

interface SpeakerState {
  label: string;
  x: number;
  y: number;
  zoom: number;
  longform_x: number | null;
  longform_y: number | null;
  longform_zoom: number;
  track: number | null;
  volume: number;
}

interface CropState {
  image: HTMLImageElement | null;
  sourceWidth: number;
  sourceHeight: number;
  scaleFactor: number;
  speakers: SpeakerState[];
  wide: { x: number; y: number; zoom: number };
  activeIdx: number; // -1 = wide, 0..n = speaker
  placeMode: 'shorts' | 'longform';
  loadError: string | null;
  saving: boolean;
  scrubMode: 'frame' | 'video';
  videoElement: HTMLVideoElement | null;
  videoTime: number;
  videoDuration: number;
  videoPlaying: boolean;
  videoLoading: boolean;
}

const SPEAKER_CSS_VARS = [
  'var(--speaker-1)',
  'var(--speaker-2)',
  'var(--speaker-3)',
  'var(--speaker-4)',
];

const TRACK_OPTIONS: Array<{ value: number | null; label: string }> = [
  { value: null, label: 'No track' },
  { value: 1, label: 'Track 1' },
  { value: 2, label: 'Track 2' },
  { value: 3, label: 'Track 3' },
  { value: 4, label: 'Track 4' },
];

export function CropSetup(target: HTMLElement, episodeId: string): void {
  watchEpisode(episodeId);
  agentPanelCollapsed.set(true);

  const state = signal<CropState>({
    image: null,
    sourceWidth: 0,
    sourceHeight: 0,
    scaleFactor: 1,
    speakers: [],
    wide: { x: 0, y: 0, zoom: 1.0 },
    activeIdx: 0,
    placeMode: 'shorts',
    loadError: null,
    saving: false,
    scrubMode: 'frame',
    videoElement: null,
    videoTime: 0,
    videoDuration: 0,
    videoPlaying: false,
    videoLoading: false,
  });

  let initialised = false;

  effect(() => {
    const ep = episodeDetail();
    if (!ep || initialised) return;
    initialised = true;
    seedFromEpisode(state, ep);
    loadCropFrame(episodeId, state);
  });

  const page = buildPage(episodeId, state);
  mount(target, page);
}

function seedFromEpisode(
  state: Signal<CropState>,
  ep: Record<string, unknown>
): void {
  const cfg = (ep.crop_config as Record<string, unknown> | undefined) ?? {};
  const speakersRaw = cfg.speakers as SpeakerCropConfig[] | undefined;
  const speakerCountHint = (ep.speaker_count as number | undefined) ?? 2;

  let speakers: SpeakerState[];
  if (speakersRaw && speakersRaw.length > 0) {
    speakers = speakersRaw.map((s, i) => ({
      label: s.label || `Speaker ${i + 1}`,
      x: s.center_x,
      y: s.center_y,
      zoom: s.zoom,
      longform_x: s.longform_center_x ?? null,
      longform_y: s.longform_center_y ?? null,
      longform_zoom: s.longform_zoom ?? 0.75,
      track: s.track ?? null,
      volume: s.volume ?? 1.0,
    }));
  } else if (cfg.speaker_l_center_x != null && cfg.speaker_r_center_x != null) {
    speakers = [
      {
        label: 'Host',
        x: cfg.speaker_l_center_x as number,
        y: cfg.speaker_l_center_y as number,
        zoom: (cfg.speaker_l_zoom as number) ?? 1.4,
        longform_x: null,
        longform_y: null,
        longform_zoom: 0.75,
        track: null,
        volume: 1.0,
      },
      {
        label: 'Guest',
        x: cfg.speaker_r_center_x as number,
        y: cfg.speaker_r_center_y as number,
        zoom: (cfg.speaker_r_zoom as number) ?? 1.4,
        longform_x: null,
        longform_y: null,
        longform_zoom: 0.75,
        track: null,
        volume: 1.0,
      },
    ];
  } else {
    speakers = Array.from({ length: Math.max(2, speakerCountHint) }, (_, i) => ({
      label: i === 0 ? 'Host' : `Guest ${i}`,
      x: 0,
      y: 0,
      zoom: 1.4,
      longform_x: null,
      longform_y: null,
      longform_zoom: 0.75,
      track: speakerCountHint > 2 ? i + 1 : null,
      volume: 1.0,
    }));
  }

  const wide = {
    x: (cfg.wide_center_x as number) ?? 0,
    y: (cfg.wide_center_y as number) ?? 0,
    zoom: (cfg.wide_zoom as number) ?? 1.0,
  };

  state.set((prev) => ({ ...prev, speakers, wide }));
}

function loadCropFrame(episodeId: string, state: Signal<CropState>): void {
  const img = new Image();
  img.onload = () => {
    state.set((prev) => {
      const next = { ...prev, image: img, sourceWidth: img.naturalWidth, sourceHeight: img.naturalHeight };
      // If we have unplaced speakers (x == 0), seed them to a reasonable start.
      const needsSeed = prev.speakers.some((s) => s.x === 0 && s.y === 0);
      if (needsSeed) {
        const cy = Math.round(img.naturalHeight / 2);
        const n = prev.speakers.length;
        next.speakers = prev.speakers.map((s, i) => {
          if (s.x !== 0 || s.y !== 0) return s;
          const cx = Math.round((img.naturalWidth * (i + 1)) / (n + 1));
          return { ...s, x: cx, y: cy };
        });
      }
      if (prev.wide.x === 0 && prev.wide.y === 0) {
        next.wide = {
          ...prev.wide,
          x: Math.round(img.naturalWidth / 2),
          y: Math.round(img.naturalHeight / 2),
        };
      }
      return next;
    });
  };
  img.onerror = () => {
    state.set((prev) => ({
      ...prev,
      loadError:
        'Crop frame not available. Run the stitch agent before opening crop setup.',
    }));
  };
  fetch(api.cropFrameUrl(episodeId))
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob();
    })
    .then((blob) => {
      img.src = URL.createObjectURL(blob);
    })
    .catch((err) => {
      state.set((prev) => ({
        ...prev,
        loadError: `Failed to load crop frame: ${(err as Error).message}`,
      }));
    });
}

/* ------------------------------ UI components ----------------------------- */

function buildPage(episodeId: string, state: Signal<CropState>): HTMLElement {
  const headerHost = h('div');
  const bodyHost = h('div', { class: 'flex-1 min-h-0' });

  effect(() => {
    const ep = episodeDetail();
    headerHost.replaceChildren(renderHeader(episodeId, ep));
  });

  effect(() => {
    const s = state();
    if (s.loadError) {
      bodyHost.replaceChildren(renderLoadError(s.loadError));
      return;
    }
    // First render installs the canvas + panels; subsequent renders
    // happen via per-piece effects below.
    if (!bodyHost.firstChild) {
      bodyHost.replaceChildren(renderBody(episodeId, state));
    }
  });

  return h(
    'div',
    { class: 'min-h-full flex flex-col' },
    headerHost,
    bodyHost
  );
}

function renderHeader(
  episodeId: string,
  ep: Record<string, unknown> | null
): HTMLElement {
  const title = ep
    ? (ep.guest_name as string)?.trim() ||
      (ep.episode_name as string)?.trim() ||
      `Untitled — ${episodeDateLabel(episodeId)}`
    : episodeId;
  const duration = ep ? (ep.duration_seconds as number) : null;
  const cfg = (ep?.crop_config as Record<string, unknown>) ?? {};
  const speakers = (cfg.speakers as unknown[] | undefined) ?? [];
  const speakerCount = Math.max(speakers.length || 2, (ep?.speaker_count as number) ?? 2);
  const isH6E = !!ep?.audio_sync;
  const status = ep ? describeStatus(ep.status as string) : null;

  return h(
    'header',
    {
      class:
        'flex items-center gap-5 px-8 py-4 border-b border-border-subtle bg-surface-canvas',
    },
    h(
      'a',
      {
        ...link(`/episodes/${episodeId}`),
        class:
          'w-8 h-8 flex items-center justify-center text-ink-tertiary hover:text-ink-primary rounded-md hover:bg-surface-2',
        title: 'Back to episode',
      },
      Icon.chevronLeft()
    ),
    h(
      'div',
      { class: 'flex-1 min-w-0' },
      h(
        'div',
        { class: 'flex items-center gap-3 mb-0.5' },
        h(
          'span',
          { class: 'text-heading-sm uppercase text-ink-tertiary' },
          'Crop setup'
        ),
        status ? StatusPill({ descriptor: status, size: 'sm' }) : null
      ),
      h(
        'div',
        { class: 'text-body-lg text-ink-primary font-medium truncate' },
        title
      )
    ),
    h(
      'div',
      { class: 'flex items-center gap-6 text-body-sm text-ink-secondary' },
      metaTag('Duration', formatDuration(duration)),
      metaTag('Speakers', String(speakerCount)),
      metaTag('Audio', isH6E ? 'H6E + Camera' : 'Camera only'),
      metaTag('ID', episodeId)
    )
  );
}

function metaTag(label: string, value: string): HTMLElement {
  return h(
    'div',
    { class: 'flex items-baseline gap-1.5' },
    h('span', { class: 'text-ink-tertiary' }, label),
    h('span', { class: 'text-ink-primary font-mono tabular' }, value)
  );
}

function renderLoadError(message: string): HTMLElement {
  return h(
    'div',
    { class: 'flex-1 grid place-items-center px-10 py-16' },
    h(
      'div',
      {
        class:
          'panel max-w-lg p-8 text-center border-status-danger/40',
      },
      h(
        'div',
        { class: 'font-display text-display-md text-status-danger mb-3' },
        'Can’t load crop frame.'
      ),
      h(
        'p',
        { class: 'text-body text-ink-secondary leading-relaxed' },
        message
      )
    )
  );
}

function renderBody(
  episodeId: string,
  state: Signal<CropState>
): HTMLElement {
  const audioHost = h('div', { class: 'flex flex-col gap-6' });

  // The H6E sync + mixer sections render lazily: we need episodeDetail()
  // loaded to know whether this episode even has H6E audio. The editor
  // and sidebar render once (they're stable, re-rendering would tear the
  // canvas); the audio section rebuilds when isH6E flips.
  let audioMounted = false;
  effect(() => {
    const ep = episodeDetail();
    const isH6E = !!(ep && ep.audio_sync);
    if (isH6E && !audioMounted) {
      audioMounted = true;
      audioHost.replaceChildren(
        SyncVerifier(episodeId),
        renderMixer(state, episodeId)
      );
    } else if (!isH6E && audioMounted) {
      audioMounted = false;
      audioHost.replaceChildren();
    }
  });

  return h(
    'div',
    { class: 'flex-1 flex flex-col gap-6 px-8 py-6 min-h-0' },
    h(
      'div',
      { class: 'grid grid-cols-[minmax(0,1fr)_380px] gap-6' },
      renderEditor(state, episodeId),
      renderSidebar(episodeId, state)
    ),
    audioHost
  );
}

function renderMixer(state: Signal<CropState>, episodeId: string): HTMLElement {
  // Render the mixer once so its internal Web Audio graph is stable, but
  // pass getters that read reactively so assignment + volume changes in
  // the speaker panel flow through without tearing down the graph.
  return TrackMixer({
    episodeId,
    getSpeakers: () =>
      state().speakers.map((spk) => ({
        label: spk.label,
        track: spk.track,
        volume: spk.volume ?? 1.0,
      })),
    getAmbient: () => [],
    onSpeakerVolume: (idx, volume) =>
      state.set((prev) => ({
        ...prev,
        speakers: prev.speakers.map((sp, i) =>
          i === idx ? { ...sp, volume } : sp
        ),
      })),
  });
}

/* ------------------------------ Canvas editor ----------------------------- */

function renderEditor(
  state: Signal<CropState>,
  episodeId: string
): HTMLElement {
  const canvas = document.createElement('canvas');
  canvas.className = 'w-full h-auto block rounded-lg cursor-crosshair bg-surface-inset';

  const canvasWrap = h('div', { class: 'panel-inset p-4' });
  canvasWrap.appendChild(canvas);

  const placementBar = h('div', {
    class: 'flex items-center justify-between gap-4 mb-3 flex-wrap',
  });

  const speakerPickerBar = h('div', {
    class: 'flex items-center gap-2 mb-4 flex-wrap',
  });

  const scrubBar = h('div', {
    class: 'mt-3 flex items-center gap-3',
  });

  const hint = h('div', {
    class: 'text-body-sm text-ink-tertiary mt-3 leading-relaxed',
  });

  canvas.addEventListener('click', (e) => {
    const s = state.peek();
    if (!s.image) return;
    const rect = canvas.getBoundingClientRect();
    const sf = s.scaleFactor;
    const srcX = Math.round((e.clientX - rect.left) * sf);
    const srcY = Math.round((e.clientY - rect.top) * sf);
    state.set((prev) => {
      if (prev.activeIdx === -1) {
        return { ...prev, wide: { ...prev.wide, x: srcX, y: srcY } };
      }
      const speakers = prev.speakers.map((spk, i) => {
        if (i !== prev.activeIdx) return spk;
        if (prev.placeMode === 'longform') {
          return { ...spk, longform_x: srcX, longform_y: srcY };
        }
        return { ...spk, x: srcX, y: srcY };
      });
      return { ...prev, speakers };
    });
  });

  // Keyboard shortcuts: 1..4 select speaker, 9 / 6 toggle shorts/longform,
  // w selects wide, Esc blurs any focused input.
  const keyHandler = (e: KeyboardEvent) => {
    if (
      document.activeElement instanceof HTMLInputElement ||
      document.activeElement instanceof HTMLTextAreaElement ||
      document.activeElement instanceof HTMLSelectElement
    ) {
      return;
    }
    const s = state.peek();
    if (e.key >= '1' && e.key <= '9') {
      const idx = Number(e.key) - 1;
      if (idx < s.speakers.length) state.set({ ...s, activeIdx: idx });
    } else if (e.key.toLowerCase() === 'w') {
      state.set({ ...s, activeIdx: -1 });
    } else if (e.key === '9') {
      state.set({ ...s, placeMode: 'shorts' });
    } else if (e.key === '6') {
      state.set({ ...s, placeMode: 'longform' });
    }
  };
  window.addEventListener('keydown', keyHandler);
  // Clean up when the canvas leaves the DOM
  const observer = new MutationObserver(() => {
    if (!canvas.isConnected) {
      window.removeEventListener('keydown', keyHandler);
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // Resize observer keeps the canvas pixel-sized to its container
  const ro = new ResizeObserver(() => {
    const s = state.peek();
    if (!s.image) return;
    resizeCanvas(canvas, s);
    state.set({ ...s, scaleFactor: s.sourceWidth / canvas.width });
    drawOverlays(canvas, state.peek());
  });
  ro.observe(canvasWrap);

  effect(() => {
    const s = state();
    if (s.image && (canvas.width === 0 || canvas.width === 300)) {
      // First render: size to container
      resizeCanvas(canvas, s);
      state.set({ ...s, scaleFactor: s.sourceWidth / canvas.width });
      return;
    }
    drawOverlays(canvas, s);
  });

  effect(() => {
    const s = state();
    placementBar.replaceChildren(renderPlacementBar(state, s));
    speakerPickerBar.replaceChildren(renderSpeakerPicker(state, s));
    scrubBar.replaceChildren(renderScrubBar(state, s, episodeId));
    hint.replaceChildren(renderHint(s));
  });

  // RAF loop that keeps the canvas in sync with the video element while
  // scrubbing. Only runs when video mode is active and the video is either
  // playing or its time was just seeked; polls readyState.
  let rafId: number | null = null;
  function loop(): void {
    const s = state.peek();
    if (s.scrubMode === 'video' && s.videoElement) {
      drawOverlays(canvas, s);
      if (s.videoElement.currentTime !== s.videoTime) {
        state.set({ ...s, videoTime: s.videoElement.currentTime });
      }
    }
    rafId = window.requestAnimationFrame(loop);
  }
  rafId = window.requestAnimationFrame(loop);
  const stopRaf = new MutationObserver(() => {
    if (!canvas.isConnected && rafId != null) {
      window.cancelAnimationFrame(rafId);
      rafId = null;
      stopRaf.disconnect();
    }
  });
  stopRaf.observe(document.body, { childList: true, subtree: true });

  return h(
    'section',
    { class: 'flex flex-col min-w-0' },
    placementBar,
    speakerPickerBar,
    canvasWrap,
    scrubBar,
    hint
  );
}

function resizeCanvas(canvas: HTMLCanvasElement, s: CropState): void {
  const wrap = canvas.parentElement;
  if (!wrap || !s.image) return;
  const maxWidth = wrap.clientWidth - 32;
  const aspect = s.sourceHeight / s.sourceWidth;
  canvas.width = Math.max(400, maxWidth);
  canvas.height = Math.round(canvas.width * aspect);
}

function drawOverlays(canvas: HTMLCanvasElement, s: CropState): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const sf = s.sourceWidth / canvas.width;
  const srcW = s.sourceWidth;
  const srcH = s.sourceHeight;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Video scrub mode draws the current video frame as the background;
  // otherwise fall back to the static crop frame.
  if (
    s.scrubMode === 'video' &&
    s.videoElement &&
    s.videoElement.readyState >= 2
  ) {
    ctx.drawImage(s.videoElement, 0, 0, canvas.width, canvas.height);
  } else if (s.image) {
    ctx.drawImage(s.image, 0, 0, canvas.width, canvas.height);
  } else {
    return;
  }

  // Wide shot
  if (s.wide && s.wide.zoom > 0) {
    const cx = s.wide.x / sf;
    const cy = s.wide.y / sf;
    const cropW = (srcW / s.wide.zoom) / sf;
    const cropH = cropW * 9 / 16;
    const x = clamp(cx - cropW / 2, 0, canvas.width - cropW);
    const y = clamp(cy - cropH / 2, 0, canvas.height - cropH);
    const active = s.activeIdx === -1;
    ctx.save();
    ctx.strokeStyle = active ? '#f5a524' : 'rgba(243, 238, 227, 0.4)';
    ctx.lineWidth = active ? 2.5 : 1.25;
    ctx.setLineDash([6, 5]);
    ctx.strokeRect(x, y, cropW, cropH);
    ctx.setLineDash([]);
    ctx.fillStyle = active ? '#f5a524' : 'rgba(243, 238, 227, 0.6)';
    ctx.font = '600 11px "Satoshi", ui-sans-serif';
    ctx.fillText(`Wide · ${s.wide.zoom.toFixed(2)}×`, x + 6, y + 16);
    ctx.restore();
  }

  s.speakers.forEach((spk, idx) => {
    const color = SPEAKER_CSS_VARS[idx % SPEAKER_CSS_VARS.length];
    const active = idx === s.activeIdx;

    // Shorts (9:16)
    const cx = spk.x / sf;
    const cy = spk.y / sf;
    const sCropH = (srcH / spk.zoom) / sf;
    const sCropW = sCropH * 9 / 16;
    const sx = clamp(cx - sCropW / 2, 0, canvas.width - sCropW);
    const sy = clamp(cy - sCropH / 2, 0, canvas.height - sCropH);

    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = active && s.placeMode === 'shorts' ? 2.75 : 1.5;
    ctx.setLineDash([7, 4]);
    ctx.strokeRect(sx, sy, sCropW, sCropH);
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = '600 11px "Satoshi", ui-sans-serif';
    ctx.fillText('9:16', sx + 6, sy + 16);

    // crosshair plus at the shorts center
    const cross = 10;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx - cross, cy);
    ctx.lineTo(cx + cross, cy);
    ctx.moveTo(cx, cy - cross);
    ctx.lineTo(cx, cy + cross);
    ctx.stroke();
    ctx.restore();

    // Longform (16:9)
    const lfExplicit = spk.longform_x != null && spk.longform_y != null;
    const lfCx = (lfExplicit ? (spk.longform_x as number) : spk.x) / sf;
    const lfCy = (lfExplicit ? (spk.longform_y as number) : spk.y) / sf;
    const lfZoom = spk.longform_zoom || 0.75;
    const lfCropW = (srcW / (2 * lfZoom)) / sf;
    const lfCropH = lfCropW * 9 / 16;
    const lx = clamp(lfCx - lfCropW / 2, 0, canvas.width - lfCropW);
    const ly = clamp(lfCy - lfCropH / 2, 0, canvas.height - lfCropH);

    ctx.save();
    ctx.globalAlpha = lfExplicit ? 1 : 0.55;
    ctx.strokeStyle = color;
    ctx.lineWidth = active && s.placeMode === 'longform' ? 2.75 : 1.5;
    ctx.setLineDash([2, 3]);
    ctx.strokeRect(lx, ly, lfCropW, lfCropH);
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = '600 11px "Satoshi", ui-sans-serif';
    ctx.fillText('16:9', lx + 6, ly + 16);

    // X crosshair at longform center
    const lfCross = 8;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(lfCx - lfCross, lfCy - lfCross);
    ctx.lineTo(lfCx + lfCross, lfCy + lfCross);
    ctx.moveTo(lfCx + lfCross, lfCy - lfCross);
    ctx.lineTo(lfCx - lfCross, lfCy + lfCross);
    ctx.stroke();
    ctx.restore();

    // Speaker index near the shorts crosshair
    ctx.save();
    ctx.fillStyle = color;
    ctx.font = '700 13px "Satoshi", ui-sans-serif';
    ctx.fillText(`${idx + 1}`, cx + 14, cy - 6);
    ctx.restore();

    if (active) {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, 16, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
  });
}

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

function renderPlacementBar(
  state: Signal<CropState>,
  s: CropState
): HTMLElement {
  const modeBtn = (
    mode: 'shorts' | 'longform',
    label: string
  ): HTMLElement => {
    const active = s.placeMode === mode && s.activeIdx !== -1;
    return h(
      'button',
      {
        onclick: () =>
          state.set({ ...state.peek(), placeMode: mode }),
        class: [
          'h-9 px-3.5 rounded-md border text-body-sm font-medium transition-colors duration-[120ms]',
          active
            ? 'bg-accent text-ink-on-accent border-transparent'
            : 'bg-surface-2 text-ink-primary border-border hover:bg-surface-3',
        ].join(' '),
      },
      label
    );
  };

  const scrubToggleActive = s.scrubMode === 'video';
  const scrubToggle = h(
    'button',
    {
      onclick: () => toggleScrubMode(state),
      class: [
        'h-9 px-3.5 rounded-md border text-body-sm font-medium transition-colors duration-[120ms] flex items-center gap-2',
        scrubToggleActive
          ? 'bg-accent text-ink-on-accent border-transparent'
          : 'bg-surface-2 text-ink-primary border-border hover:bg-surface-3',
      ].join(' '),
      title: scrubToggleActive
        ? 'Back to crop frame'
        : 'Scrub the source video to find a better frame',
    },
    Icon.film({ size: 14 }),
    scrubToggleActive ? 'Using video' : 'Scrub video'
  );

  return h(
    'div',
    { class: 'flex items-center gap-4 w-full flex-wrap' },
    h(
      'div',
      { class: 'flex items-center gap-1.5' },
      h(
        'span',
        { class: 'text-heading-sm uppercase text-ink-tertiary mr-2' },
        'Placing'
      ),
      modeBtn('shorts', '9:16 Shorts'),
      modeBtn('longform', '16:9 Longform')
    ),
    h('div', { class: 'flex-1' }),
    scrubToggle
  );
}

function toggleScrubMode(state: Signal<CropState>): void {
  const prev = state.peek();
  if (prev.scrubMode === 'video') {
    // Turning off — pause and drop back to frame
    if (prev.videoElement) {
      prev.videoElement.pause();
    }
    state.set({ ...prev, scrubMode: 'frame', videoPlaying: false });
    return;
  }
  // Turning on — lazy-create the video element if we haven't yet
  if (!prev.videoElement) {
    const v = document.createElement('video');
    v.preload = 'metadata';
    v.playsInline = true;
    v.crossOrigin = 'anonymous';
    v.muted = true;
    v.addEventListener('loadedmetadata', () => {
      state.set({
        ...state.peek(),
        videoDuration: v.duration,
        videoLoading: false,
      });
    });
    v.addEventListener('play', () => {
      state.set({ ...state.peek(), videoPlaying: true });
    });
    v.addEventListener('pause', () => {
      state.set({ ...state.peek(), videoPlaying: false });
    });
    v.addEventListener('seeked', () => {
      state.set({ ...state.peek(), videoTime: v.currentTime });
    });
    v.addEventListener('error', () => {
      state.set({
        ...state.peek(),
        videoLoading: false,
        scrubMode: 'frame',
        loadError:
          'Couldn’t load the source video for scrubbing. Falling back to the crop frame.',
      });
    });
    state.set({
      ...prev,
      scrubMode: 'video',
      videoElement: v,
      videoLoading: true,
    });
    // Kick off load after state is set so readers see loading state
    // Source URL uses the episode_id from closure; we stash it in the
    // element's dataset so the renderScrubBar can pick it up.
    return;
  }
  state.set({ ...prev, scrubMode: 'video' });
}

function renderScrubBar(
  state: Signal<CropState>,
  s: CropState,
  episodeId: string
): HTMLElement {
  if (s.scrubMode !== 'video') {
    return h(
      'div',
      null,
      // Empty filler — effect replaces host children each render.
      h('span', { class: 'hidden' }, '')
    );
  }

  // Attach the source if not yet set
  if (s.videoElement && !s.videoElement.src) {
    s.videoElement.src = `/api/episodes/${episodeId}/video-preview`;
  }

  if (s.videoLoading || (s.videoElement && s.videoElement.readyState < 1)) {
    return h(
      'div',
      { class: 'flex items-center gap-3 text-body-sm text-ink-tertiary' },
      h('span', { class: 'live-dot' }),
      h('span', null, 'Loading source video…')
    );
  }

  const playPauseBtn = h(
    'button',
    {
      onclick: () => {
        const v = state.peek().videoElement;
        if (!v) return;
        if (v.paused) v.play().catch(() => {});
        else v.pause();
      },
      class:
        'w-9 h-9 rounded-md border border-border bg-surface-2 text-ink-primary flex items-center justify-center hover:bg-surface-3',
      title: s.videoPlaying ? 'Pause' : 'Play',
    },
    s.videoPlaying ? Icon.pause({ size: 16 }) : Icon.play({ size: 16 })
  );

  const seek = h('input', {
    type: 'range',
    min: '0',
    max: String(s.videoDuration || 0),
    step: '0.05',
    value: String(s.videoTime || 0),
    class: 'cascade-slider flex-1',
    oninput: (e: Event) => {
      const v = state.peek().videoElement;
      if (!v) return;
      const t = Number((e.target as HTMLInputElement).value);
      v.currentTime = t;
    },
  });

  return h(
    'div',
    { class: 'flex items-center gap-3 w-full' },
    playPauseBtn,
    h(
      'span',
      { class: 'text-code text-ink-secondary font-mono tabular' },
      formatTimecode(s.videoTime)
    ),
    seek,
    h(
      'span',
      { class: 'text-code text-ink-tertiary font-mono tabular' },
      formatTimecode(s.videoDuration)
    )
  );
}

function renderSpeakerPicker(
  state: Signal<CropState>,
  s: CropState
): HTMLElement {
  const pickerBtn = (idx: number): HTMLElement => {
    const active = idx === s.activeIdx;
    const wide = idx === -1;
    const color = wide ? null : SPEAKER_CSS_VARS[idx % SPEAKER_CSS_VARS.length];
    return h(
      'button',
      {
        onclick: () => state.set({ ...state.peek(), activeIdx: idx }),
        class: [
          'h-9 px-3.5 rounded-md border text-body-sm font-medium transition-colors duration-[120ms] flex items-center gap-2',
          active
            ? 'bg-surface-3 border-border-strong text-ink-primary'
            : 'bg-surface-2 border-border text-ink-secondary hover:text-ink-primary',
        ].join(' '),
      },
      wide
        ? h('span', { class: 'w-2 h-2 rounded-full bg-ink-tertiary' })
        : h('span', {
            class: 'w-2 h-2 rounded-full',
            style: { background: color! },
          }),
      wide ? 'Wide shot' : s.speakers[idx]?.label || `Speaker ${idx + 1}`
    );
  };

  return h(
    'div',
    { class: 'flex items-center gap-2 flex-wrap' },
    h(
      'span',
      { class: 'text-heading-sm uppercase text-ink-tertiary mr-2' },
      'Speaker'
    ),
    ...s.speakers.map((_, i) => pickerBtn(i)),
    pickerBtn(-1)
  );
}

function renderHint(s: CropState): HTMLElement {
  const speaker = s.activeIdx >= 0 ? s.speakers[s.activeIdx] : null;
  if (s.activeIdx === -1) {
    return h(
      'span',
      null,
      'Click the canvas to place the ',
      h('span', { class: 'text-ink-primary' }, 'wide shot'),
      ' center. Wide-shot zoom lives in the sidebar.'
    );
  }
  if (!speaker) return h('span', null, '');
  const rectLabel = s.placeMode === 'shorts' ? '9:16 Shorts' : '16:9 Longform';
  return h(
    'span',
    null,
    'Click the canvas to place the ',
    h('span', { class: 'text-ink-primary' }, rectLabel),
    ' center for ',
    h('span', { class: 'text-ink-primary' }, speaker.label),
    '. Keys: ',
    h(
      'span',
      { class: 'text-code text-ink-secondary font-mono tabular' },
      '1–4'
    ),
    ' speaker · ',
    h(
      'span',
      { class: 'text-code text-ink-secondary font-mono tabular' },
      'w'
    ),
    ' wide.'
  );
}

/* -------------------------------- Sidebar -------------------------------- */

function renderSidebar(
  episodeId: string,
  state: Signal<CropState>
): HTMLElement {
  const host = h('aside', {
    class: 'flex flex-col gap-4 min-w-0 overflow-y-auto',
  });

  effect(() => {
    const s = state();
    const ep = episodeDetail();
    const isH6E = !!(ep && ep.audio_sync);
    host.replaceChildren(
      ...s.speakers.map((spk, i) => renderSpeakerCard(state, s, i, spk, isH6E)),
      renderWideCard(state, s),
      renderSaveCard(episodeId, state, s)
    );
  });

  return host;
}

function renderSpeakerCard(
  state: Signal<CropState>,
  s: CropState,
  idx: number,
  spk: SpeakerState,
  isH6E: boolean
): HTMLElement {
  const color = SPEAKER_CSS_VARS[idx % SPEAKER_CSS_VARS.length];
  const active = idx === s.activeIdx;

  return h(
    'div',
    {
      class: [
        'panel p-4 flex flex-col gap-3 transition-colors duration-[120ms]',
        active ? 'border-border-strong' : '',
      ].join(' '),
      onclick: (e: MouseEvent) => {
        const target = e.target as HTMLElement;
        if (target.closest('input, select, button')) return;
        state.set({ ...state.peek(), activeIdx: idx });
      },
    },
    h(
      'div',
      { class: 'flex items-center gap-2' },
      h('span', {
        class: 'w-2.5 h-2.5 rounded-full',
        style: { background: color },
      }),
      labelInput(state, idx, spk.label),
      h(
        'span',
        { class: 'text-code-sm text-ink-tertiary font-mono tabular ml-auto' },
        `(${spk.x}, ${spk.y})`
      )
    ),
    sliderRow({
      label: 'Shorts zoom',
      value: spk.zoom,
      min: 0.5,
      max: 3,
      step: 0.05,
      suffix: '×',
      onInput: (v) => updateSpeaker(state, idx, { zoom: v }),
    }),
    sliderRow({
      label: 'Longform zoom',
      value: spk.longform_zoom,
      min: 0.5,
      max: 1.5,
      step: 0.05,
      suffix: '×',
      onInput: (v) => updateSpeaker(state, idx, { longform_zoom: v }),
    }),
    spk.longform_x != null
      ? h(
          'div',
          {
            class: 'flex items-center justify-between text-body-sm text-ink-tertiary',
          },
          h('span', null, '16:9 center'),
          h(
            'span',
            { class: 'font-mono tabular text-ink-secondary' },
            `(${spk.longform_x}, ${spk.longform_y})`
          ),
          h(
            'button',
            {
              onclick: () =>
                updateSpeaker(state, idx, { longform_x: null, longform_y: null }),
              class:
                'text-status-danger hover:underline text-code-sm ml-2',
            },
            'clear'
          )
        )
      : h(
          'div',
          { class: 'text-body-sm text-ink-tertiary' },
          '16:9 crop inherits from the 9:16 center.'
        ),
    isH6E
      ? h(
          'div',
          { class: 'flex items-center gap-2' },
          h(
            'label',
            { class: 'text-body-sm text-ink-tertiary' },
            'Track'
          ),
          trackSelect(spk.track, (v) => updateSpeaker(state, idx, { track: v }))
        )
      : null
  );
}

function labelInput(
  state: Signal<CropState>,
  idx: number,
  value: string
): HTMLElement {
  return h('input', {
    type: 'text',
    value,
    class:
      'bg-transparent text-body text-ink-primary font-medium focus:outline-none w-40',
    oninput: (e: Event) =>
      updateSpeaker(state, idx, {
        label: (e.target as HTMLInputElement).value,
      }),
  });
}

function trackSelect(
  value: number | null,
  onChange: (v: number | null) => void
): HTMLElement {
  const sel = h('select', {
    class:
      'bg-surface-2 border border-border rounded-md px-2.5 h-8 text-body-sm text-ink-primary focus:border-accent focus:outline-none',
    onchange: (e: Event) => {
      const raw = (e.target as HTMLSelectElement).value;
      onChange(raw === '' ? null : Number(raw));
    },
  }) as HTMLSelectElement;
  for (const opt of TRACK_OPTIONS) {
    const o = document.createElement('option');
    o.value = opt.value == null ? '' : String(opt.value);
    o.textContent = opt.label;
    if (opt.value === value) o.selected = true;
    sel.appendChild(o);
  }
  return sel;
}

interface SliderRowOpts {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  onInput: (v: number) => void;
}

function sliderRow(opts: SliderRowOpts): HTMLElement {
  const valueEl = h(
    'span',
    { class: 'text-code text-ink-secondary font-mono tabular' },
    `${opts.value.toFixed(2)}${opts.suffix ?? ''}`
  );
  const slider = h('input', {
    type: 'range',
    min: String(opts.min),
    max: String(opts.max),
    step: String(opts.step),
    value: String(opts.value),
    class: 'cascade-slider flex-1',
    oninput: (e: Event) => {
      const v = Number((e.target as HTMLInputElement).value);
      valueEl.textContent = `${v.toFixed(2)}${opts.suffix ?? ''}`;
      opts.onInput(v);
    },
  });
  return h(
    'div',
    { class: 'flex flex-col gap-1.5' },
    h(
      'div',
      { class: 'flex items-baseline justify-between' },
      h('span', { class: 'text-body-sm text-ink-tertiary' }, opts.label),
      valueEl
    ),
    h('div', { class: 'flex items-center gap-3' }, slider)
  );
}

function updateSpeaker(
  state: Signal<CropState>,
  idx: number,
  patch: Partial<SpeakerState>
): void {
  state.set((prev) => ({
    ...prev,
    speakers: prev.speakers.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
  }));
}

function renderWideCard(
  state: Signal<CropState>,
  s: CropState
): HTMLElement {
  const active = s.activeIdx === -1;
  return h(
    'div',
    {
      class: [
        'panel p-4 flex flex-col gap-3',
        active ? 'border-border-strong' : '',
      ].join(' '),
      onclick: (e: MouseEvent) => {
        const target = e.target as HTMLElement;
        if (target.closest('input')) return;
        state.set({ ...state.peek(), activeIdx: -1 });
      },
    },
    h(
      'div',
      { class: 'flex items-center gap-2' },
      h('span', {
        class: 'w-2.5 h-2.5 rounded-full bg-ink-tertiary',
      }),
      h(
        'span',
        { class: 'text-body text-ink-primary font-medium' },
        'Wide shot'
      ),
      h(
        'span',
        { class: 'text-code-sm text-ink-tertiary font-mono tabular ml-auto' },
        `(${s.wide.x}, ${s.wide.y})`
      )
    ),
    sliderRow({
      label: 'Zoom',
      value: s.wide.zoom,
      min: 1.0,
      max: 3.0,
      step: 0.05,
      suffix: '×',
      onInput: (v) =>
        state.set({ ...state.peek(), wide: { ...state.peek().wide, zoom: v } }),
    }),
    h(
      'p',
      { class: 'text-body-sm text-ink-tertiary leading-relaxed' },
      'Used for all-speakers-visible shots. Zoom 1.0× disables the wide crop.'
    )
  );
}

function renderSaveCard(
  episodeId: string,
  state: Signal<CropState>,
  s: CropState
): HTMLElement {
  const valid = s.speakers.every((spk) => spk.x > 0 && spk.y > 0);
  return h(
    'div',
    { class: 'panel p-4 flex flex-col gap-3 sticky bottom-0 bg-surface-1' },
    valid
      ? null
      : h(
          'p',
          { class: 'text-body-sm text-status-warning' },
          'Place every speaker on the canvas before saving.'
        ),
    Button({
      variant: 'primary',
      size: 'lg',
      label: s.saving ? 'Saving…' : 'Save & continue',
      loading: s.saving,
      disabled: !valid,
      onClick: () => doSave(episodeId, state),
      class: 'w-full',
    }),
    Button({
      variant: 'ghost',
      size: 'md',
      label: 'Cancel',
      onClick: () => navigate(`/episodes/${episodeId}`),
      class: 'w-full',
    })
  );
}

async function doSave(
  episodeId: string,
  state: Signal<CropState>
): Promise<void> {
  const s = state.peek();
  state.set({ ...s, saving: true });

  const payload: CropConfigRequest = {
    source_width: s.sourceWidth,
    source_height: s.sourceHeight,
    speakers: s.speakers.map((spk) => {
      const entry: SpeakerCropConfig = {
        label: spk.label,
        center_x: spk.x,
        center_y: spk.y,
        zoom: spk.zoom,
        longform_zoom: spk.longform_zoom,
        volume: spk.volume,
      };
      if (spk.longform_x != null) entry.longform_center_x = spk.longform_x;
      if (spk.longform_y != null) entry.longform_center_y = spk.longform_y;
      if (spk.track != null) entry.track = spk.track;
      return entry;
    }),
  };
  if (s.wide.zoom > 1.0) {
    payload.wide_center_x = s.wide.x;
    payload.wide_center_y = s.wide.y;
    payload.wide_zoom = s.wide.zoom;
  }

  try {
    await api.saveCropConfig(episodeId, payload);
    try {
      await api.resumePipeline(episodeId);
    } catch {
      /* resume is best-effort; the saved crop is the real success */
    }
    showToast('Crops saved — pipeline resuming.', 'success');
    navigate(`/episodes/${episodeId}`);
  } catch (e) {
    state.set({ ...state.peek(), saving: false });
    showToast((e as Error).message, 'error');
  }
}
