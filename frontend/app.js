// Cascade — Client-side application
// Single-page app with hash-based routing, vanilla JS

const API = '/api';
const MEDIA = '/media';

// ── State ──────────────────────────────────────────────────────────

const state = {
  currentView: 'dashboard',
  currentEpisodeId: null,
  currentClipId: null,
  episodes: [],
  clips: [],
  schedule: [],
  activeTab: 'longform',
  chatMessages: [],
  pipelinePollers: {},
  expandedClipId: null,
  currentEpisode: null,
};

// Human-readable agent names and full pipeline order
const AGENT_LABELS = {
  ingest: 'Ingest',
  stitch: 'Stitch',
  audio_analysis: 'Audio Analysis',
  speaker_cut: 'Speaker Cut',
  transcribe: 'Transcribe',
  clip_miner: 'Clip Miner',
  longform_render: 'Longform Render',
  shorts_render: 'Shorts Render',
  metadata_gen: 'Metadata',
  thumbnail_gen: 'Thumbnails',
  qa: 'QA',
  podcast_feed: 'Podcast Feed',
  publish: 'Publish',
  backup: 'Backup',
};

const PIPELINE_AGENTS = [
  'ingest', 'stitch', 'audio_analysis', 'speaker_cut', 'transcribe',
  'clip_miner', 'longform_render', 'shorts_render', 'metadata_gen', 'thumbnail_gen', 'qa',
  'podcast_feed', 'publish', 'backup',
];

const SPEAKER_COLORS = [
  { name: 'Blue', css: 'rgba(59, 130, 246, 0.8)', bg: 'bg-blue-700', ring: 'ring-blue-500' },
  { name: 'Green', css: 'rgba(16, 185, 129, 0.8)', bg: 'bg-emerald-700', ring: 'ring-emerald-500' },
  { name: 'Amber', css: 'rgba(245, 158, 11, 0.8)', bg: 'bg-amber-700', ring: 'ring-amber-500' },
  { name: 'Purple', css: 'rgba(168, 85, 247, 0.8)', bg: 'bg-purple-700', ring: 'ring-purple-500' },
  { name: 'Rose', css: 'rgba(244, 63, 94, 0.8)', bg: 'bg-rose-700', ring: 'ring-rose-500' },
  { name: 'Cyan', css: 'rgba(6, 182, 212, 0.8)', bg: 'bg-cyan-700', ring: 'ring-cyan-500' },
  { name: 'Lime', css: 'rgba(132, 204, 22, 0.8)', bg: 'bg-lime-700', ring: 'ring-lime-500' },
  { name: 'Pink', css: 'rgba(236, 72, 153, 0.8)', bg: 'bg-pink-700', ring: 'ring-pink-500' },
];

const cropState = {
  activeIdx: 0,       // which speaker index we're placing
  image: null,        // HTMLImageElement
  scaleFactor: 1,     // canvas-to-source ratio
  sourceWidth: 1920,
  sourceHeight: 1080,
  speakers: [],       // [{label, x, y, zoom, track}]
  // Legacy compat
  get mode() { return this.activeIdx === 0 ? 'L' : 'R'; },
  get speakerL() { return this.speakers[0] || { x: 480, y: 540 }; },
  get speakerR() { return this.speakers[1] || { x: 1440, y: 540 }; },
  get zoomL() { return (this.speakers[0] || {}).zoom || 1.0; },
  get zoomR() { return (this.speakers[1] || {}).zoom || 1.0; },
};

// ── Audio Sync Verification State ──
const syncState = {
  loaded: false,
  cameraWaveform: [],
  h6eWaveform: [],
  offset: 0,           // current offset in seconds
  autoOffset: null,     // auto-detected offset (for reset)
  duration: 120,
  pps: 200,             // peaks per second
  animFrame: null,
  videoElement: null,
  episodeId: null,
  // Zoom/pan state
  viewStart: 0,         // visible window start (seconds)
  viewEnd: 30,          // visible window end (seconds)
  isDragging: false,
  dragStartX: 0,
  dragStartViewStart: 0,
};

// ── Audio Mixer State ──
const mixerState = {
  audioCtx: null,
  playing: false,
  loaded: false,
  previewStart: 30,
  previewDuration: 60,
  tracks: [],
  animFrame: null,
  episodeId: null,
};

// ── Router ─────────────────────────────────────────────────────────

function getRoute() {
  const hash = window.location.hash || '#/';
  const parts = hash.replace('#/', '').split('/').filter(Boolean);

  if (parts[0] === 'episodes' && parts[2] === 'crop-setup') {
    return { view: 'crop-setup', episodeId: parts[1] };
  }
  if (parts[0] === 'episodes' && parts[2] === 'clips' && parts[3]) {
    return { view: 'clip-detail', episodeId: parts[1], clipId: parts[3] };
  }
  if (parts[0] === 'episodes' && parts[1]) {
    return { view: 'episode-detail', episodeId: parts[1], tab: parts[2] || null };
  }
  if (parts[0] === 'schedule') return { view: 'schedule' };
  if (parts[0] === 'analytics') return { view: 'analytics' };
  return { view: 'dashboard' };
}

async function navigate() {
  const route = getRoute();
  state.currentView = route.view;
  state.currentEpisodeId = route.episodeId || null;
  state.currentClipId = route.clipId || null;

  // Clear any pipeline pollers that are no longer relevant
  clearAllPollers();

  updateNav();

  const app = document.getElementById('app');
  app.innerHTML = '<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8"><div class="text-zinc-500 text-center py-20">Loading...</div></div>';

  try {
    switch (route.view) {
      case 'dashboard': await renderDashboard(); break;
      case 'episode-detail': await renderEpisodeDetail(route.episodeId, route.tab); break;
      case 'crop-setup': await renderCropSetup(route.episodeId); break;
      case 'clip-detail': await renderClipDetail(route.episodeId, route.clipId); break;
      case 'schedule': await renderSchedule(); break;
      case 'analytics': await renderAnalytics(); break;
      default: await renderDashboard();
    }
  } catch (err) {
    app.innerHTML = `<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div class="text-red-400 text-center py-20">Error loading view: ${escapeHtml(err.message)}</div>
    </div>`;
  }
}

function updateNav() {
  document.querySelectorAll('.nav-link').forEach(link => {
    const nav = link.dataset.nav;
    const isActive =
      (nav === 'dashboard' && (state.currentView === 'dashboard' || state.currentView === 'episode-detail' || state.currentView === 'clip-detail')) ||
      (nav === 'schedule' && state.currentView === 'schedule') ||
      (nav === 'analytics' && state.currentView === 'analytics');
    link.classList.toggle('active', isActive);
  });
}

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', navigate);

// ── API Helpers ────────────────────────────────────────────────────

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

// ── Utilities ──────────────────────────────────────────────────────

function escapeHtml(str) {
  if (str == null) return '';
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

function formatTime(seconds) {
  if (seconds == null) return '--:--';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatTimeFull(seconds) {
  if (seconds == null) return '--:--:--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatDuration(seconds) {
  if (seconds == null) return '--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function parseTime(str) {
  if (!str) return NaN;
  const parts = str.trim().split(':').map(Number);
  if (parts.some(isNaN)) return NaN;
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return NaN;
}

function scoreBadge(score) {
  if (score == null) return '<span class="score-badge score-low">?</span>';
  const cls = score >= 7 ? 'score-high' : score >= 4 ? 'score-mid' : 'score-low';
  return `<span class="score-badge ${cls}">${score}</span>`;
}

function statusBadge(status) {
  if (!status) status = 'pending';
  const label = status.replace(/_/g, ' ');
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium status-${status}">${label}</span>`;
}

function speakerLabel(speaker) {
  const colors = { L: 'text-blue-400', R: 'text-emerald-400', BOTH: 'text-amber-400' };
  return `<span class="${colors[speaker] || 'text-zinc-400'} text-xs font-medium">${speaker || '?'}</span>`;
}

function container() {
  return '<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">';
}

function clearAllPollers() {
  Object.keys(state.pipelinePollers).forEach(key => {
    clearInterval(state.pipelinePollers[key]);
    delete state.pipelinePollers[key];
  });
}

function startPipelinePoller(episodeId, onUpdate) {
  const key = `pipeline-${episodeId}`;
  if (state.pipelinePollers[key]) clearInterval(state.pipelinePollers[key]);
  state.pipelinePollers[key] = setInterval(async () => {
    try {
      const status = await api(`/episodes/${episodeId}/pipeline-status`);
      onUpdate(status);
      if (!status.is_running) {
        clearInterval(state.pipelinePollers[key]);
        delete state.pipelinePollers[key];
        // Pipeline finished or paused — refresh the page to show banners/media
        if (['ready_for_review', 'error', 'awaiting_crop_setup', 'awaiting_backup_approval'].includes(status.status)) {
          setTimeout(() => navigate(), 1000);
        }
      }
    } catch {
      // Silently ignore polling errors
    }
  }, 3000);
}

function showToast(message, type) {
  const existing = document.getElementById('cascade-toast');
  if (existing) existing.remove();

  const colors = {
    success: 'bg-green-800 border-green-700 text-green-200',
    error: 'bg-red-900 border-red-800 text-red-200',
    info: 'bg-zinc-800 border-zinc-700 text-zinc-200',
  };

  const toast = document.createElement('div');
  toast.id = 'cascade-toast';
  toast.className = `fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg border text-sm font-medium shadow-lg toast-enter ${colors[type] || colors.info}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('toast-exit');
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}


// ════════════════════════════════════════════════════════════════════
//  DASHBOARD VIEW
// ════════════════════════════════════════════════════════════════════

async function renderDashboard() {
  let episodes = [];
  try { episodes = await api('/episodes/'); } catch { episodes = []; }
  state.episodes = episodes;

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-2xl font-bold">Dashboard</h1>
        <p class="text-zinc-500 text-sm mt-1">Manage your podcast episodes</p>
      </div>
      <button onclick="triggerNewEpisode()" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
        New Episode
      </button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="lg:col-span-2 space-y-3" id="episode-list">
        ${episodes.length === 0
          ? '<div class="text-zinc-600 text-center py-16 border border-dashed border-zinc-800 rounded-xl">No episodes yet. Click "New Episode" to get started.</div>'
          : episodes.map(ep => episodeCard(ep)).join('')
        }
      </div>

      <div class="space-y-4">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Upcoming Schedule</h3>
          <div id="schedule-sidebar" class="space-y-2 text-sm text-zinc-500">Loading...</div>
        </div>
      </div>
    </div>
  </div>`;

  loadScheduleSidebar();

  // Check pipeline status for episodes that might be running
  // (status could be 'processing', 'awaiting_crop_setup', 'awaiting_backup_approval', etc.)
  episodes.forEach(async (ep) => {
    const id = ep.episode_id || ep.id;
    if (ep.status === 'processing') {
      startPipelinePoller(id, (status) => updateEpisodeCardStatus(id, status));
    } else if (!['ready_for_review', 'error', 'cancelled', 'approved'].includes(ep.status)) {
      // Check if pipeline is actually running despite non-processing status
      try {
        const status = await api(`/episodes/${id}/pipeline-status`);
        if (status.is_running) {
          updateEpisodeCardStatus(id, status);
          startPipelinePoller(id, (status) => updateEpisodeCardStatus(id, status));
        }
      } catch {}
    }
  });
}

function episodeCard(ep) {
  const id = ep.episode_id || ep.id;
  const clipCount = ep.clips ? ep.clips.length : 0;
  const approved = ep.clips ? ep.clips.filter(c => c.status === 'approved').length : 0;
  const isProcessing = ep.status === 'processing';
  const displayName = ep.episode_name || ep.title || id;
  const subtitle = ep.guest_title ? `${ep.guest_title}` : '';
  return `
    <a href="#/episodes/${id}" class="clip-card block bg-zinc-900 border border-zinc-800 rounded-xl p-4 hover:border-zinc-700 transition-colors" id="episode-card-${id}">
      <div class="flex items-start justify-between">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-1">
            <h3 class="text-sm font-semibold truncate">${escapeHtml(displayName)}</h3>
            ${statusBadge(ep.status || 'processing')}
          </div>
          ${subtitle ? `<p class="text-xs text-zinc-400 mb-0.5">${escapeHtml(subtitle)}</p>` : ''}
          <p class="text-xs text-zinc-500">${formatDuration(ep.duration_seconds)} &middot; ${clipCount} clips &middot; ${approved} approved</p>
          <p class="text-xs text-zinc-600 mt-1">${ep.episode_name ? escapeHtml(id) + ' &middot; ' : ''}${ep.created_at ? new Date(ep.created_at).toLocaleDateString() : ''}</p>
        </div>
        <div class="flex items-center gap-1 flex-shrink-0 mt-1">
          <button onclick="event.preventDefault(); event.stopPropagation(); deleteEpisodeFromDashboard('${id}')" class="p-1.5 text-zinc-500 hover:text-red-400 hover:bg-red-900/30 transition-colors rounded" title="Delete episode">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
          </button>
          <svg class="w-4 h-4 text-zinc-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
        </div>
      </div>
      <div class="mt-3 pipeline-progress-mini ${isProcessing ? '' : 'hidden'}" id="pipeline-mini-${id}">
        <div class="flex items-center gap-2 text-xs text-blue-400">
          <span class="inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
          <span>Pipeline running...</span>
        </div>
      </div>
    </a>`;
}

function updateEpisodeCardStatus(episodeId, status) {
  const mini = document.getElementById(`pipeline-mini-${episodeId}`);
  if (!mini) return;
  mini.classList.remove('hidden');
  if (status.is_running) {
    const pct = status.progress ? Math.round(status.progress.percent || 0) : 0;
    const pctText = pct > 0 ? ` (${pct}%)` : '';
    mini.innerHTML = `
      <div class="flex items-center gap-2 text-xs text-blue-400">
        <span class="inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
        <span>${escapeHtml(status.current_agent || 'Running')}${pctText}...</span>
      </div>`;
  } else {
    mini.innerHTML = `
      <div class="flex items-center gap-2 text-xs text-green-400">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
        <span>Pipeline complete</span>
      </div>`;
  }
}

async function loadScheduleSidebar() {
  const el = document.getElementById('schedule-sidebar');
  if (!el) return;
  try {
    const data = await api('/schedule');
    const items = data.schedule || data.items || [];
    if (items.length === 0) {
      el.textContent = 'No upcoming publishes.';
      return;
    }
    el.innerHTML = items.slice(0, 7).map(day => `
      <div class="border-b border-zinc-800 pb-2">
        <div class="text-zinc-400 text-xs font-medium mb-1">${day.date}</div>
        ${(day.items || []).map(item => `
          <div class="text-zinc-500 text-xs pl-2">${item.type === 'longform' ? 'Longform' : item.id || 'Clip'}</div>
        `).join('')}
      </div>
    `).join('');
  } catch {
    el.textContent = 'Schedule unavailable.';
  }
}

function triggerNewEpisode() {
  // Show modal dialog for episode creation
  const overlay = document.createElement('div');
  overlay.id = 'new-episode-overlay';
  overlay.className = 'fixed inset-0 bg-black/70 flex items-center justify-center z-50';
  overlay.innerHTML = `
    <div class="bg-zinc-900 border border-zinc-700 rounded-xl p-6 w-full max-w-md mx-4 space-y-4">
      <h2 class="text-lg font-bold text-white">New Episode</h2>
      <div>
        <label class="block text-sm text-zinc-400 mb-1">Source Video Path</label>
        <input id="ne-source-path" type="text" placeholder="/Volumes/CAMERA/DCIM/DJI_001/"
          class="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-600 focus:border-brand-500 focus:outline-none">
      </div>
      <div>
        <label class="block text-sm text-zinc-400 mb-1">External Audio Path <span class="text-zinc-600">(optional — Zoom H6E, etc.)</span></label>
        <input id="ne-audio-path" type="text" placeholder="/Volumes/ZOOM_H6E/260311_143505/"
          class="w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-600 focus:border-brand-500 focus:outline-none">
      </div>
      <div>
        <label class="block text-sm text-zinc-400 mb-1">Number of Speakers <span class="text-zinc-600">(for crop setup)</span></label>
        <input id="ne-speaker-count" type="number" min="1" max="8" value="2"
          class="w-24 px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white focus:border-brand-500 focus:outline-none">
      </div>
      <div class="flex gap-3 pt-2">
        <button onclick="submitNewEpisode()" class="flex-1 px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors">Create &amp; Run Pipeline</button>
        <button onclick="document.getElementById('new-episode-overlay').remove()" class="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-sm text-zinc-400 transition-colors">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  document.getElementById('ne-source-path').focus();
}

async function submitNewEpisode() {
  const sourcePath = document.getElementById('ne-source-path').value.trim();
  const audioPath = document.getElementById('ne-audio-path').value.trim();
  const speakerCount = parseInt(document.getElementById('ne-speaker-count').value) || 2;

  document.getElementById('new-episode-overlay').remove();

  try {
    const ep = await api('/episodes/', {
      method: 'POST',
      body: JSON.stringify({
        source_path: sourcePath || null,
        audio_path: audioPath || null,
        speaker_count: speakerCount,
      }),
    });
    const id = ep.episode_id || ep.id;

    // Trigger pipeline if source path was provided
    if (sourcePath) {
      try {
        await api(`/episodes/${id}/run-pipeline`, {
          method: 'POST',
          body: JSON.stringify({
            source_path: sourcePath,
            audio_path: audioPath || null,
          }),
        });
      } catch {
        // Pipeline trigger is best-effort; episode was created
      }
    }

    window.location.hash = `#/episodes/${id}`;
  } catch (err) {
    showToast('Failed to create episode: ' + err.message, 'error');
  }
}


// ════════════════════════════════════════════════════════════════════
//  EPISODE DETAIL VIEW
// ════════════════════════════════════════════════════════════════════

async function renderEpisodeDetail(episodeId, tab) {
  const ep = await api(`/episodes/${episodeId}`);
  let clips = [];
  try { clips = await api(`/episodes/${episodeId}/clips`); } catch { clips = ep.clips || []; }
  state.clips = clips;
  state.currentEpisode = ep;
  state.chatMessages = [];
  state.expandedClipId = null;

  // Load persisted chat history
  try {
    const historyResp = await api(`/episodes/${episodeId}/chat/history`);
    if (historyResp.messages && historyResp.messages.length > 0) {
      state.chatMessages = historyResp.messages.map(msg => ({
        role: msg.role,
        content: msg.content,
        actions: [],
      }));
    }
  } catch { /* no history yet */ }

  const activeTab = tab || state.activeTab || 'longform';
  state.activeTab = activeTab;

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <!-- Breadcrumb -->
    <div class="flex items-center gap-2 text-sm text-zinc-500 mb-6">
      <a href="#/" class="hover:text-white transition-colors">Dashboard</a>
      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      <span class="text-zinc-300">${escapeHtml(ep.episode_name || ep.title || episodeId)}</span>
    </div>

    <!-- Episode header -->
    <div class="flex items-center justify-between mb-6">
      <div>
        <div class="flex items-center gap-3">
          <h1 class="text-xl font-bold">${escapeHtml(ep.episode_name || ep.title || episodeId)}</h1>
          ${statusBadge(ep.status || 'processing')}
        </div>
        ${ep.guest_title ? `<p class="text-sm text-zinc-400 mt-0.5">${escapeHtml(ep.guest_title)}</p>` : ''}
        <p class="text-xs text-zinc-600 mt-1">${escapeHtml(episodeId)}</p>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="rerunPipeline('${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-xs font-medium transition-colors flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
          Re-run Pipeline
        </button>
        <button onclick="cancelPipeline('${episodeId}')" id="cancel-pipeline-btn" class="px-3 py-1.5 bg-red-900 hover:bg-red-800 border border-red-800 rounded-lg text-xs font-medium transition-colors flex items-center gap-1.5 hidden">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
          Cancel Pipeline
        </button>
        <button onclick="approveAll('${episodeId}')" class="px-3 py-1.5 bg-green-700 hover:bg-green-600 rounded-lg text-xs font-medium transition-colors">
          Approve All
        </button>
        <button onclick="deleteEpisode('${episodeId}')" class="px-3 py-1.5 bg-red-900/50 hover:bg-red-800 border border-red-900 rounded-lg text-xs font-medium transition-colors text-red-400 hover:text-red-200">
          Delete
        </button>
      </div>
    </div>

    <!-- Crop setup banner — show when crop_config is missing and stitch is done -->
    ${(!ep.crop_config && (ep.pipeline?.agents_completed || []).includes('stitch')) || ep.status === 'awaiting_crop_setup' ? `
    <div class="mb-6 bg-amber-900/30 border border-amber-700/50 rounded-lg p-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <span class="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
        <div>
          <p class="text-sm font-medium text-amber-200">Crop setup required</p>
          <p class="text-xs text-amber-400/70">Position the speaker crop frames before rendering can continue.</p>
        </div>
      </div>
      <a href="#/episodes/${episodeId}/crop-setup" class="px-4 py-2 bg-amber-600 hover:bg-amber-500 rounded-lg text-sm font-medium transition-colors text-white">
        Set Up Crops
      </a>
    </div>
    ` : ''}

    ${ep.status === 'awaiting_backup_approval' ? `
    <div class="mb-6 bg-blue-900/30 border border-blue-700/50 rounded-lg p-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <span class="inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
        <div>
          <p class="text-sm font-medium text-blue-200">Backup &amp; SD card cleanup ready</p>
          <p class="text-xs text-blue-400/70">This will back up the episode to the external drive and delete the source files from the SD card.</p>
        </div>
      </div>
      <button onclick="approveBackup('${episodeId}')" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium transition-colors text-white">
        Approve Backup
      </button>
    </div>
    ` : ''}

    <!-- Episode Info (editable) -->
    <div class="mb-6 bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-semibold text-zinc-300">Episode Info</h3>
        <button onclick="toggleEpisodeInfoEdit('${episodeId}')" id="episode-info-edit-btn" class="text-xs text-zinc-500 hover:text-white transition-colors">Edit</button>
      </div>
      <div id="episode-info-display">
        <div class="grid grid-cols-2 gap-3 text-sm">
          <div><span class="text-zinc-500">Guest:</span> <span class="text-zinc-300">${escapeHtml(ep.guest_name || '—')}</span></div>
          <div><span class="text-zinc-500">Title:</span> <span class="text-zinc-300">${escapeHtml(ep.guest_title || '—')}</span></div>
          <div class="col-span-2"><span class="text-zinc-500">Episode Name:</span> <span class="text-zinc-300">${escapeHtml(ep.episode_name || '—')}</span></div>
          <div class="col-span-2"><span class="text-zinc-500">Description:</span> <span class="text-zinc-300">${escapeHtml(ep.episode_description || '—')}</span></div>
        </div>
      </div>
      <div id="episode-info-form" class="hidden">
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-zinc-500 block mb-1">Guest Name</label>
            <input id="edit-guest-name" type="text" value="${escapeHtml(ep.guest_name || '')}" class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:border-brand-500 focus:outline-none" placeholder="e.g. John Smith">
          </div>
          <div>
            <label class="text-xs text-zinc-500 block mb-1">Guest Title</label>
            <input id="edit-guest-title" type="text" value="${escapeHtml(ep.guest_title || '')}" class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:border-brand-500 focus:outline-none" placeholder="e.g. Nuclear Navy Officer">
          </div>
          <div class="col-span-2">
            <label class="text-xs text-zinc-500 block mb-1">Episode Name</label>
            <input id="edit-episode-name" type="text" value="${escapeHtml(ep.episode_name || '')}" class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:border-brand-500 focus:outline-none" placeholder="e.g. John Smith">
          </div>
          <div class="col-span-2">
            <label class="text-xs text-zinc-500 block mb-1">Episode Description</label>
            <textarea id="edit-episode-description" rows="2" class="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-1.5 text-sm text-white focus:border-brand-500 focus:outline-none resize-none" placeholder="Brief episode description...">${escapeHtml(ep.episode_description || '')}</textarea>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-3">
          <button onclick="toggleEpisodeInfoEdit('${episodeId}')" class="px-3 py-1.5 text-xs text-zinc-400 hover:text-white transition-colors">Cancel</button>
          <button onclick="saveEpisodeInfo('${episodeId}')" class="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 rounded text-xs font-medium transition-colors">Save</button>
        </div>
      </div>
    </div>

    <!-- Pipeline status bar -->
    <div id="pipeline-bar" class="mb-6 hidden">
      <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2 text-sm">
            <span class="inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse" id="pipeline-pulse"></span>
            <span class="text-zinc-300 font-medium" id="pipeline-label">Pipeline running...</span>
          </div>
          <span class="text-xs text-zinc-500" id="pipeline-agent-count"></span>
        </div>
        <div id="pipeline-agents" class="flex gap-1 flex-wrap mb-2"></div>
        <div id="pipeline-progress" class="hidden">
          <div class="flex items-center gap-3">
            <div class="flex-1 bg-zinc-800 rounded-full h-2 overflow-hidden">
              <div id="pipeline-progress-fill" class="h-full bg-blue-500 rounded-full transition-all duration-500" style="width: 0%"></div>
            </div>
            <span id="pipeline-progress-label" class="text-xs text-zinc-400 font-mono w-12 text-right">0%</span>
          </div>
          <p id="pipeline-progress-detail" class="text-xs text-zinc-500 mt-1"></p>
        </div>
      </div>
    </div>

    <!-- Audio mix controls are in the Audio tab -->

    <!-- Tab navigation -->
    <div class="flex items-center gap-1 mb-6 border-b border-zinc-800 pb-px">
      <button onclick="switchEpisodeTab('longform')" class="episode-tab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${activeTab === 'longform' ? 'active' : ''}" data-tab="longform">Longform</button>
      <button onclick="switchEpisodeTab('shorts')" class="episode-tab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${activeTab === 'shorts' ? 'active' : ''}" data-tab="shorts">
        Shorts
        <span class="ml-1 text-xs text-zinc-500">(${clips.length})</span>
      </button>
      <button onclick="switchEpisodeTab('audio')" class="episode-tab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${activeTab === 'audio' ? 'active' : ''}" data-tab="audio">Audio</button>
      <button onclick="switchEpisodeTab('metadata')" class="episode-tab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${activeTab === 'metadata' ? 'active' : ''}" data-tab="metadata">Metadata</button>
      <button onclick="switchEpisodeTab('chat')" class="episode-tab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors ${activeTab === 'chat' ? 'active' : ''}" data-tab="chat">Chat</button>
    </div>

    <!-- Tab content -->
    <div id="tab-longform" class="tab-panel ${activeTab !== 'longform' ? 'hidden' : ''}"></div>
    <div id="tab-shorts" class="tab-panel ${activeTab !== 'shorts' ? 'hidden' : ''}"></div>
    <div id="tab-audio" class="tab-panel ${activeTab !== 'audio' ? 'hidden' : ''}"></div>
    <div id="tab-metadata" class="tab-panel ${activeTab !== 'metadata' ? 'hidden' : ''}"></div>
    <div id="tab-chat" class="tab-panel ${activeTab !== 'chat' ? 'hidden' : ''}"></div>
  </div>`;

  // Render active tab content
  renderLongformTab(episodeId, ep);
  renderShortsTab(episodeId, clips, ep);
  renderAudioTab(episodeId, ep);
  renderMetadataTab(episodeId, ep, clips);
  renderChatTab(episodeId);

  // Check pipeline status
  checkAndShowPipeline(episodeId);
}

function switchEpisodeTab(tabName) {
  state.activeTab = tabName;
  document.querySelectorAll('.episode-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('hidden', panel.id !== `tab-${tabName}`);
  });

  // Load sync waveforms when switching to Audio tab
  if (tabName === 'audio' && state.currentEpisode) {
    const ep = state.currentEpisode;
    if (ep.audio_sync && ep.audio_sync.offset_seconds != null && !syncState.loaded) {
      loadSyncPreview(state.currentEpisodeId, ep.audio_sync.offset_seconds);
    }
  }
}

async function checkAndShowPipeline(episodeId) {
  try {
    const status = await api(`/episodes/${episodeId}/pipeline-status`);
    updatePipelineBar(status);
    if (status.is_running || status.status === 'processing') {
      startPipelinePoller(episodeId, updatePipelineBar);
    }
  } catch {
    // No pipeline status available
  }
}

function updatePipelineBar(status) {
  const bar = document.getElementById('pipeline-bar');
  if (!bar) return;

  const completed = new Set(status.agents_completed || []);
  const errors = status.errors || {};
  const current = status.current_agent;
  const hasData = completed.size > 0 || status.is_running;

  // Use agents_requested if this is a partial re-run, otherwise show all
  const visibleAgents = status.agents_requested || PIPELINE_AGENTS;

  if (!hasData && status.status !== 'ready_for_review' && status.status !== 'error') {
    bar.classList.add('hidden');
    return;
  }

  bar.classList.remove('hidden');

  // Clear old error details if not in error state
  if (status.status !== 'error') {
    const errDiv = document.getElementById('pipeline-error-details');
    if (errDiv) errDiv.remove();
  }

  // Show/hide cancel button based on running state
  const cancelBtn = document.getElementById('cancel-pipeline-btn');
  if (cancelBtn) {
    cancelBtn.classList.toggle('hidden', !status.is_running);
  }

  const pulse = document.getElementById('pipeline-pulse');
  const label = document.getElementById('pipeline-label');
  const count = document.getElementById('pipeline-agent-count');
  const agents = document.getElementById('pipeline-agents');

  if (status.is_running) {
    pulse.className = 'inline-block w-2 h-2 rounded-full bg-blue-400 animate-pulse';
    const agentLabel = current ? AGENT_LABELS[current] || current : '';
    label.textContent = agentLabel ? `Running: ${agentLabel}` : 'Pipeline running...';
  } else if (status.status === 'cancelled') {
    pulse.className = 'inline-block w-2 h-2 rounded-full bg-red-400';
    label.textContent = 'Pipeline cancelled';
  } else if (status.status === 'error') {
    pulse.className = 'inline-block w-2 h-2 rounded-full bg-red-400';
    label.textContent = 'Pipeline error';
    // Show error details below the pipeline bar
    const errorEntries = Object.entries(errors);
    if (errorEntries.length > 0) {
      let errDiv = document.getElementById('pipeline-error-details');
      if (!errDiv) {
        errDiv = document.createElement('div');
        errDiv.id = 'pipeline-error-details';
        bar.appendChild(errDiv);
      }
      errDiv.innerHTML = errorEntries.map(([agent, msg]) =>
        `<div class="mt-2 px-3 py-2 bg-red-950/50 border border-red-900/50 rounded text-xs text-red-300">
          <span class="font-semibold text-red-400">${AGENT_LABELS[agent] || agent}:</span> ${escapeHtml(String(msg))}
        </div>`
      ).join('');
    }
  } else if (status.status === 'ready_for_review') {
    pulse.className = 'inline-block w-2 h-2 rounded-full bg-green-400';
    label.textContent = 'Pipeline complete';
  } else {
    pulse.className = 'inline-block w-2 h-2 rounded-full bg-green-400';
    label.textContent = 'Pipeline complete';
  }

  const completedInView = [...completed].filter(a => visibleAgents.includes(a)).length;
  count.textContent = `${completedInView} of ${visibleAgents.length} steps`;

  // Build agent pills: completed=green, active=blue, error=red, remaining=gray
  agents.innerHTML = visibleAgents.map(agent => {
    const agentLabel = AGENT_LABELS[agent] || agent;
    if (completed.has(agent)) {
      const hasError = errors[agent];
      if (hasError) {
        return `<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-amber-900/30 border border-amber-800/50 rounded text-xs text-amber-400" title="Completed with warning: ${escapeHtml(hasError)}">
          <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
          ${agentLabel}
        </span>`;
      }
      return `<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-green-900/30 border border-green-800/50 rounded text-xs text-green-400">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
        ${agentLabel}
      </span>`;
    }
    if (agent === current && status.is_running) {
      return `<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-900/30 border border-blue-800/50 rounded text-xs text-blue-400">
        <span class="inline-block w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse"></span>
        ${agentLabel}
      </span>`;
    }
    if (errors[agent]) {
      return `<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-red-900/30 border border-red-800/50 rounded text-xs text-red-400" title="${escapeHtml(errors[agent])}">
        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        ${agentLabel}
      </span>`;
    }
    return `<span class="inline-flex items-center gap-1 px-2 py-0.5 bg-zinc-800/50 border border-zinc-700/50 rounded text-xs text-zinc-500">
      ${agentLabel}
    </span>`;
  }).join('');

  // Progress bar for active agent
  const progressDiv = document.getElementById('pipeline-progress');
  const progressFill = document.getElementById('pipeline-progress-fill');
  const progressLabel = document.getElementById('pipeline-progress-label');
  const progressDetail = document.getElementById('pipeline-progress-detail');

  if (status.progress && status.is_running) {
    progressDiv.classList.remove('hidden');
    const pct = Math.round(status.progress.percent || 0);
    progressFill.style.width = pct + '%';
    progressLabel.textContent = pct + '%';
    const detail = status.progress.detail || '';
    const counts = `${status.progress.current} / ${status.progress.total}`;
    progressDetail.textContent = detail ? `${counts} — ${detail}` : counts;
  } else {
    progressDiv.classList.add('hidden');
  }

  // Update the status badge in the episode header
  const statusBadgeEl = document.querySelector('.status-processing, .status-ready_for_review, .status-error, .status-cancelled, .status-approved, .status-awaiting_crop_setup, .status-awaiting_backup_approval');
  if (statusBadgeEl && status.status) {
    statusBadgeEl.className = `inline-block px-2 py-0.5 rounded text-xs font-medium status-${status.status}`;
    statusBadgeEl.textContent = status.status.replace(/_/g, ' ');
  }
}

// ── Longform Tab ───────────────────────────────────────────────────

function renderLongformTab(episodeId, ep) {
  const panel = document.getElementById('tab-longform');
  if (!panel) return;

  const pipelineRunning = ep.status === 'processing';
  const currentAgent = ep.pipeline?.current_agent;
  const agentsCompleted = ep.pipeline?.agents_completed || [];

  // Longform is "stale" if pipeline is re-running and hasn't completed longform_render yet in this run
  // Detect by checking: if pipeline is running and current_agent is before or at longform_render
  const renderAgents = ['speaker_cut', 'transcribe', 'clip_miner', 'longform_render'];
  const longformStale = pipelineRunning && renderAgents.includes(currentAgent);
  const longformReady = !longformStale && agentsCompleted.includes('longform_render');

  const clips = state.clips || [];
  const approvedCount = clips.filter(c => c.status === 'approved').length;
  const isReadyForReview = ep.status === 'ready_for_review';

  panel.innerHTML = `
    ${isReadyForReview && clips.length > 0 && approvedCount === 0 ? `
    <div class="mb-4 bg-brand-900/30 border border-brand-700/50 rounded-lg p-3 flex items-center justify-between">
      <div class="flex items-center gap-2">
        <svg class="w-5 h-5 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7l5 5m0 0l-5 5m5-5H6"/></svg>
        <span class="text-sm text-brand-300">Pipeline complete! Review and approve your shorts clips.</span>
      </div>
      <button onclick="switchEpisodeTab('shorts')" class="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 rounded text-xs font-medium transition-colors">
        Review Shorts
      </button>
    </div>` : ''}

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="lg:col-span-2 space-y-4">
        <!-- Video player -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden relative">
          <div class="video-container aspect-video">
            ${longformReady
              ? `<video id="longform-video" controls preload="metadata" class="w-full">
                  <source src="${MEDIA}/episodes/${episodeId}/longform.mp4" type="video/mp4">
                </video>`
              : longformStale
                ? `<div class="flex flex-col items-center justify-center h-full min-h-[300px] text-blue-400 text-sm gap-3">
                    <svg class="w-8 h-8 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                    <span>Re-rendering longform video...</span>
                    <span class="text-xs text-zinc-500">Check the pipeline status bar above for progress</span>
                  </div>`
                : `<div class="flex items-center justify-center h-full min-h-[300px] text-zinc-500 text-sm">
                    Longform video not yet rendered. Pipeline must complete the longform_render step.
                  </div>`
            }
          </div>
        </div>

        <!-- Current time display -->
        ${longformReady ? `
        <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span class="text-xs text-zinc-500 uppercase tracking-wide">Current Time</span>
            <span id="longform-current-time" class="text-lg font-mono font-bold text-brand-500">0:00</span>
          </div>
          <div class="flex items-center gap-2 text-xs text-zinc-500">
            <span>Duration: ${formatDuration(ep.duration_seconds)}</span>
          </div>
        </div>` : ''}

        <!-- Trim controls -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 ${pipelineRunning ? 'opacity-50 pointer-events-none' : ''}">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-semibold text-zinc-300">Trim Video</h3>
            ${pipelineRunning ? '<span class="text-xs text-amber-400">Disabled while pipeline is running</span>' : ''}
          </div>
          <div class="flex items-end gap-3">
            <label class="block flex-1">
              <span class="text-xs text-zinc-500">Trim Start (MM:SS)</span>
              <input type="text" id="trim-start" value="0:00" placeholder="0:00" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 font-mono focus:outline-none focus:border-brand-500" ${pipelineRunning ? 'disabled' : ''}>
            </label>
            <label class="block flex-1">
              <span class="text-xs text-zinc-500">Trim End (MM:SS)</span>
              <input type="text" id="trim-end" value="${formatTime(ep.duration_seconds)}" placeholder="${formatTime(ep.duration_seconds)}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 font-mono focus:outline-none focus:border-brand-500" ${pipelineRunning ? 'disabled' : ''}>
            </label>
            <button onclick="trimVideo('${episodeId}')" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors whitespace-nowrap" ${pipelineRunning ? 'disabled' : ''}>
              Trim Video
            </button>
          </div>
          <p class="text-xs text-zinc-600 mt-2">Tip: use the video player to find exact timestamps, then enter them above.</p>
        </div>
      </div>

      <!-- Sidebar: episode info -->
      <div class="space-y-4">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Episode Info</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between"><span class="text-zinc-500">Status</span>${statusBadge(ep.status || 'processing')}</div>
            <div class="flex justify-between"><span class="text-zinc-500">Duration</span><span>${formatDuration(ep.duration_seconds)}</span></div>
            <div class="flex justify-between"><span class="text-zinc-500">Clips</span><span>${clips.length}</span></div>
            <div class="flex justify-between"><span class="text-zinc-500">Approved</span><span>${approvedCount}</span></div>
            <div class="flex justify-between"><span class="text-zinc-500">Created</span><span class="text-zinc-400">${ep.created_at ? new Date(ep.created_at).toLocaleDateString() : '--'}</span></div>
          </div>
        </div>

        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Quick Actions</h3>
          <div class="space-y-2">
            ${isReadyForReview ? `
            <button onclick="switchEpisodeTab('shorts')" class="w-full px-3 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">
              Review &amp; Approve Shorts
            </button>` : ''}
            <button onclick="approveAll('${episodeId}')" class="w-full px-3 py-2 bg-green-700 hover:bg-green-600 rounded-lg text-xs font-medium transition-colors">
              Approve All &amp; Schedule
            </button>
            <button onclick="rerunPipeline('${episodeId}')" class="w-full px-3 py-2 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-lg text-xs font-medium transition-colors" ${pipelineRunning ? 'disabled' : ''}>
              Re-run Pipeline
            </button>
          </div>
        </div>
      </div>
    </div>`;

  // Time display updater
  const video = document.getElementById('longform-video');
  const timeDisplay = document.getElementById('longform-current-time');
  if (video && timeDisplay) {
    video.addEventListener('timeupdate', () => {
      timeDisplay.textContent = formatTimeFull(video.currentTime);
    });
  }
}

async function trimVideo(episodeId) {
  const startStr = document.getElementById('trim-start').value;
  const endStr = document.getElementById('trim-end').value;
  const startSec = parseTime(startStr);
  const endSec = parseTime(endStr);

  if (isNaN(startSec) || isNaN(endSec)) {
    showToast('Invalid time format. Use MM:SS.', 'error');
    return;
  }
  if (endSec <= startSec) {
    showToast('Trim end must be after trim start.', 'error');
    return;
  }

  if (!confirm(`Trim video from ${startStr} to ${endStr}? This will trim the longform and re-run downstream agents.`)) return;

  const trimBtn = document.querySelector('[onclick*="trimVideo"]');
  if (trimBtn) {
    trimBtn.disabled = true;
    trimBtn.textContent = 'Trimming...';
  }

  try {
    await api(`/episodes/${episodeId}/trim`, {
      method: 'POST',
      body: JSON.stringify({ trim_start_seconds: startSec, trim_end_seconds: endSec }),
    });
    showToast('Trim complete. Re-running pipeline from speaker cut...', 'success');

    // Re-run pipeline from speaker_cut onward (segments, transcription, clips, renders all need to update)
    await api(`/episodes/${episodeId}/run-pipeline`, {
      method: 'POST',
      body: JSON.stringify({ agents: ['speaker_cut', 'transcribe', 'clip_miner', 'shorts_render', 'metadata_gen', 'qa'] }),
    });
    checkAndShowPipeline(episodeId);
  } catch (err) {
    showToast('Trim failed: ' + err.message, 'error');
  } finally {
    if (trimBtn) {
      trimBtn.disabled = false;
      trimBtn.textContent = 'Trim Video';
    }
  }
}

async function rerunPipeline(episodeId) {
  if (!confirm('Re-run the full pipeline for this episode?')) return;
  try {
    await api(`/episodes/${episodeId}/resume-pipeline`, {
      method: 'POST',
    });
    showToast('Pipeline resumed.', 'success');
    checkAndShowPipeline(episodeId);
  } catch (err) {
    showToast('Failed to start pipeline: ' + err.message, 'error');
  }
}

async function approveAll(episodeId) {
  // Require episode name, description, and longform links before approving
  const ep = state.currentEpisode;
  if (ep && (!ep.episode_name || !ep.episode_description)) {
    showToast('Please fill in the Episode Name and Description before approving.', 'error');
    // Open the edit form so they can fill it in
    const form = document.getElementById('episode-info-form');
    const display = document.getElementById('episode-info-display');
    if (form && display) {
      form.classList.remove('hidden');
      display.classList.add('hidden');
    }
    // Highlight the missing fields
    if (!ep.episode_name) {
      const nameInput = document.getElementById('edit-episode-name');
      if (nameInput) { nameInput.classList.add('border-red-500'); nameInput.focus(); }
    } else if (!ep.episode_description) {
      const descInput = document.getElementById('edit-episode-description');
      if (descInput) { descInput.classList.add('border-red-500'); descInput.focus(); }
    }
    return;
  }
  if (ep && (!ep.youtube_longform_url || !ep.spotify_longform_url || !ep.link_tree_url)) {
    const missing = [];
    if (!ep.youtube_longform_url) missing.push('YouTube Longform URL');
    if (!ep.spotify_longform_url) missing.push('Spotify Longform URL');
    if (!ep.link_tree_url) missing.push('Link Tree URL');
    showToast(`Please fill in ${missing.join(', ')} in the Metadata tab before approving.`, 'error');
    return;
  }
  if (!confirm('Approve all pending clips and schedule for publishing?')) return;
  try {
    await api(`/episodes/${episodeId}/auto-approve`, { method: 'POST' });
    showToast('All clips approved.', 'success');
    await renderEpisodeDetail(episodeId);
  } catch (err) {
    showToast('Failed: ' + err.message, 'error');
  }
}

async function approveBackup(episodeId) {
  if (!confirm('Back up episode to external drive and delete source files from SD card?')) return;
  try {
    await api(`/episodes/${episodeId}/approve-backup`, { method: 'POST' });
    showToast('Backup approved — backing up and cleaning SD card.', 'success');
    await renderEpisodeDetail(episodeId);
  } catch (err) {
    showToast('Failed: ' + err.message, 'error');
  }
}

async function cancelPipeline(episodeId) {
  if (!confirm('Cancel the running pipeline for this episode?')) return;
  try {
    await api(`/episodes/${episodeId}/cancel-pipeline`, { method: 'POST' });
    showToast('Pipeline cancellation requested.', 'success');
    await renderEpisodeDetail(episodeId);
  } catch (err) {
    showToast('Cancel failed: ' + err.message, 'error');
  }
}

async function deleteEpisode(episodeId) {
  if (!confirm('Are you sure you want to delete this episode? This will permanently remove all files and cannot be undone.')) return;
  try {
    await api(`/episodes/${episodeId}`, { method: 'DELETE' });
    showToast('Episode deleted.', 'success');
    window.location.hash = '#/';
  } catch (err) {
    showToast('Delete failed: ' + err.message, 'error');
  }
}

async function deleteEpisodeFromDashboard(episodeId) {
  if (!confirm(`Delete episode ${episodeId}? This permanently removes all files and cannot be undone.`)) return;
  try {
    await api(`/episodes/${episodeId}`, { method: 'DELETE' });
    showToast('Episode deleted.', 'success');
    await renderDashboard();
  } catch (err) {
    showToast('Delete failed: ' + err.message, 'error');
  }
}

function toggleEpisodeInfoEdit(episodeId) {
  const display = document.getElementById('episode-info-display');
  const form = document.getElementById('episode-info-form');
  const btn = document.getElementById('episode-info-edit-btn');
  if (!display || !form) return;
  const isEditing = !form.classList.contains('hidden');
  display.classList.toggle('hidden', !isEditing);
  form.classList.toggle('hidden', isEditing);
  btn.textContent = isEditing ? 'Edit' : 'Cancel';
}

async function saveEpisodeInfo(episodeId) {
  const guestName = document.getElementById('edit-guest-name')?.value || '';
  const guestTitle = document.getElementById('edit-guest-title')?.value || '';
  const episodeName = document.getElementById('edit-episode-name')?.value || '';
  const episodeDescription = document.getElementById('edit-episode-description')?.value || '';
  try {
    await api(`/episodes/${episodeId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        guest_name: guestName,
        guest_title: guestTitle,
        episode_name: episodeName,
        episode_description: episodeDescription,
      }),
    });
    showToast('Episode info saved.', 'success');
    await renderEpisodeDetail(episodeId);
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  }
}

// ── Shorts Tab ─────────────────────────────────────────────────────

function renderShortsTab(episodeId, clips, ep) {
  const panel = document.getElementById('tab-shorts');
  if (!panel) return;

  if (clips.length === 0) {
    panel.innerHTML = `
      <div class="text-zinc-600 text-center py-16 border border-dashed border-zinc-800 rounded-xl">
        No shorts generated yet. Run the pipeline to generate clip candidates.
      </div>`;
    return;
  }

  const shortsReady = (ep && ep.pipeline?.agents_completed || []).includes('shorts_render');
  const sorted = [...clips].sort((a, b) => (b.virality_score || 0) - (a.virality_score || 0));

  const chatSuggestions = [
    'Approve all clips with score 8+',
    'Update all TikTok captions',
    'Find me a clip about...',
    'Regenerate metadata for all clips',
    'Reject low-scoring clips',
  ];

  panel.innerHTML = `
    <div class="flex items-center justify-between mb-4">
      <p class="text-sm text-zinc-500">${clips.filter(c => c.status === 'approved').length}/${clips.length} approved</p>
    </div>
    <div class="shorts-grid space-y-3">
      ${sorted.map(clip => shortCard(episodeId, clip, shortsReady)).join('')}
    </div>

    <!-- Collapsible AI Chat Panel -->
    <div class="mt-6" id="shorts-chat-panel">
      <button onclick="toggleShortsChat()" class="w-full flex items-center justify-between px-4 py-3 bg-zinc-900 border border-zinc-800 rounded-xl hover:bg-zinc-800/80 transition-colors" id="shorts-chat-toggle">
        <div class="flex items-center gap-2">
          <svg class="w-4 h-4 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
          <span class="text-sm font-medium text-zinc-300">AI Assistant</span>
        </div>
        <svg class="w-4 h-4 text-zinc-500 transition-transform" id="shorts-chat-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
      </button>
      <div id="shorts-chat-body" class="hidden mt-1 bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
        <div id="shorts-chat-messages" class="overflow-y-auto p-4 space-y-3" style="max-height: 350px;">
          <div class="text-center py-4">
            <div class="text-zinc-600 text-xs mb-3">Quick actions for this episode</div>
            <div class="flex flex-wrap gap-2 justify-center">
              ${chatSuggestions.map(s => `
                <button onclick="sendShortsChatSuggestion(this, '${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-full text-xs text-zinc-400 hover:text-zinc-200 transition-colors">${escapeHtml(s)}</button>
              `).join('')}
            </div>
          </div>
        </div>
        <div class="border-t border-zinc-800 p-3">
          <div class="flex gap-2">
            <input type="text" id="shorts-chat-input" placeholder="Ask the AI about your clips..." class="flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500 placeholder-zinc-600" onkeydown="if(event.key==='Enter')sendShortsChat('${episodeId}')">
            <button onclick="sendShortsChat('${episodeId}')" id="shorts-chat-send" class="px-3 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">Send</button>
          </div>
        </div>
      </div>
    </div>`;
}

function shortCard(episodeId, clip, shortsReady) {
  const clipId = clip.id;
  const isExpanded = state.expandedClipId === clipId;

  return `
    <div class="short-card bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden transition-all ${isExpanded ? 'ring-1 ring-brand-500' : ''}" id="short-${clipId}">
      <div class="flex gap-4 p-3">
        <!-- Video thumbnail / player -->
        <div class="short-video-wrapper flex-shrink-0" style="width: 120px;">
          <div class="aspect-[9/16] bg-black rounded-lg overflow-hidden">
            ${shortsReady
              ? `<video class="w-full h-full object-cover" preload="metadata" muted playsinline
                  onmouseenter="this.play()" onmouseleave="this.pause();this.currentTime=0;">
                  <source src="${MEDIA}/episodes/${episodeId}/shorts/${clipId}.mp4" type="video/mp4">
                </video>`
              : `<div class="w-full h-full flex items-center justify-center text-zinc-600 text-xs text-center p-2">Not yet rendered</div>`
            }
          </div>
        </div>

        <!-- Info -->
        <div class="flex-1 min-w-0 flex flex-col justify-between py-1">
          <div>
            <div class="flex items-start justify-between gap-2 mb-1">
              <h3 class="text-sm font-semibold leading-tight line-clamp-2">${escapeHtml(clip.title || clipId)}</h3>
              <div class="flex-shrink-0">${scoreBadge(Math.round(clip.virality_score || 0))}</div>
            </div>
            <div class="flex items-center gap-2 text-xs text-zinc-500 mb-2">
              <span>${formatTime(clip.start_seconds || clip.start)} - ${formatTime(clip.end_seconds || clip.end)}</span>
              <span class="text-zinc-700">&middot;</span>
              <span>${formatDuration(clip.duration || ((clip.end_seconds || clip.end) - (clip.start_seconds || clip.start)))}</span>
              <span class="text-zinc-700">&middot;</span>
              ${speakerLabel(clip.speaker)}
            </div>
            ${clip.hook_text ? `<p class="text-xs text-zinc-600 line-clamp-2">"${escapeHtml(clip.hook_text)}"</p>` : ''}
          </div>

          <div class="flex items-center gap-2 mt-2">
            ${statusBadge(clip.status || 'pending')}
            <div class="flex-1"></div>
            <button onclick="event.preventDefault(); approveClip('${episodeId}', '${clipId}')" class="px-2.5 py-1 bg-green-800 hover:bg-green-700 rounded text-xs font-medium transition-colors" title="Approve">
              <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            </button>
            <button onclick="event.preventDefault(); rejectClip('${episodeId}', '${clipId}')" class="px-2.5 py-1 bg-red-900 hover:bg-red-800 rounded text-xs font-medium transition-colors" title="Reject">
              <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
            </button>
            <button onclick="event.preventDefault(); toggleExpandShort('${clipId}')" class="px-2.5 py-1 bg-zinc-800 hover:bg-zinc-700 rounded text-xs font-medium transition-colors" title="Expand">
              <svg class="w-3.5 h-3.5 inline ${isExpanded ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
            </button>
            <a href="#/episodes/${episodeId}/clips/${clipId}" class="px-2.5 py-1 bg-zinc-800 hover:bg-zinc-700 rounded text-xs font-medium transition-colors" title="Full detail">
              <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
            </a>
          </div>
        </div>
      </div>

      ${isExpanded ? shortCardExpanded(episodeId, clip) : ''}
    </div>`;
}

function shortCardExpanded(episodeId, clip) {
  const clipId = clip.id;
  const metadata = clip.metadata || {};
  return `
    <div class="border-t border-zinc-800 p-4 bg-zinc-950/50 space-y-3">
      <div class="grid grid-cols-2 gap-3 text-xs">
        <div><span class="text-zinc-500">Virality Score:</span> <span class="text-zinc-300">${clip.virality_score || '--'}</span></div>
        <div><span class="text-zinc-500">Speaker:</span> ${speakerLabel(clip.speaker)}</div>
        <div><span class="text-zinc-500">Rank:</span> <span class="text-zinc-300">#${clip.rank || '--'}</span></div>
        <div><span class="text-zinc-500">Duration:</span> <span class="text-zinc-300">${formatDuration(clip.duration || ((clip.end_seconds || clip.end) - (clip.start_seconds || clip.start)))}</span></div>
      </div>
      ${clip.hook_text ? `<div class="text-xs"><span class="text-zinc-500">Hook:</span> <span class="text-zinc-400">"${escapeHtml(clip.hook_text)}"</span></div>` : ''}
      ${clip.compelling_reason ? `<div class="text-xs"><span class="text-zinc-500">Reason:</span> <span class="text-zinc-400">${escapeHtml(clip.compelling_reason)}</span></div>` : ''}
      <div class="flex gap-2 pt-1">
        <a href="#/episodes/${episodeId}/clips/${clipId}" class="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 rounded text-xs font-medium transition-colors">
          Open Full Detail
        </a>
      </div>
    </div>`;
}

function toggleExpandShort(clipId) {
  state.expandedClipId = state.expandedClipId === clipId ? null : clipId;
  // Re-render shorts tab in-place
  if (state.currentEpisodeId) {
    renderShortsTab(state.currentEpisodeId, state.clips, state.currentEpisode);
  }
}

async function approveClip(episodeId, clipId) {
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/approve`, { method: 'POST' });
    showToast('Clip approved.', 'success');
    // Refresh clips
    const clips = await api(`/episodes/${episodeId}/clips`);
    state.clips = clips;
    renderShortsTab(episodeId, clips, state.currentEpisode);
  } catch (err) {
    showToast('Approve failed: ' + err.message, 'error');
  }
}

async function rejectClip(episodeId, clipId) {
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/reject`, { method: 'POST' });
    showToast('Clip rejected.', 'success');
    const clips = await api(`/episodes/${episodeId}/clips`);
    state.clips = clips;
    renderShortsTab(episodeId, clips, state.currentEpisode);
  } catch (err) {
    showToast('Reject failed: ' + err.message, 'error');
  }
}

// ── Metadata Tab ───────────────────────────────────────────────────

function renderMetadataTab(episodeId, ep, clips) {
  const panel = document.getElementById('tab-metadata');
  if (!panel) return;

  panel.innerHTML = `
    <div class="space-y-6">
      <!-- Longform metadata -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-5">
        <h3 class="text-sm font-semibold text-zinc-300 mb-4">Longform Metadata</h3>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <label class="block">
            <span class="text-xs text-zinc-500">YouTube Title</span>
            <input type="text" id="lf-title" value="${escapeHtml(ep.title || '')}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
          </label>
          <label class="block">
            <span class="text-xs text-zinc-500">Tags (comma-separated)</span>
            <input type="text" id="lf-tags" value="${escapeHtml((ep.tags || []).join(', '))}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500" placeholder="podcast, interview, tech">
          </label>
          <label class="block lg:col-span-2">
            <span class="text-xs text-zinc-500">YouTube Description</span>
            <textarea id="lf-desc" rows="4" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">${escapeHtml(ep.description || '')}</textarea>
          </label>
        </div>
        <div class="mt-3 flex gap-3">
          <button onclick="saveLongformMetadata('${episodeId}')" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">
            Save Longform Metadata
          </button>
          <button onclick="autoCompleteMetadata('${episodeId}')" id="auto-complete-btn" class="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-xs font-medium transition-colors">
            Auto-Complete Metadata
          </button>
        </div>
      </div>

      <!-- Longform Links -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-5">
        <h3 class="text-sm font-semibold text-zinc-300 mb-4">Longform Links</h3>
        <p class="text-xs text-zinc-500 mb-3">Required before approval. Paste the URLs after uploading longform content.</p>
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <label class="block">
            <span class="text-xs text-zinc-500">YouTube Longform URL</span>
            <input type="text" id="lf-youtube-url" value="${escapeHtml(ep.youtube_longform_url || '')}" placeholder="https://youtube.com/watch?v=..." class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
          </label>
          <label class="block">
            <span class="text-xs text-zinc-500">Spotify Longform URL</span>
            <input type="text" id="lf-spotify-url" value="${escapeHtml(ep.spotify_longform_url || '')}" placeholder="https://open.spotify.com/episode/..." class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
          </label>
          <label class="block">
            <span class="text-xs text-zinc-500">Link Tree URL</span>
            <input type="text" id="lf-linktree-url" value="${escapeHtml(ep.link_tree_url || 'https://pub-eb7a2f9e3c574c519db9e95d779b30c4.r2.dev/links/index.html')}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
          </label>
        </div>
        <div class="mt-3">
          <button onclick="saveLongformLinks('${episodeId}')" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">
            Save Longform Links
          </button>
        </div>
      </div>

      <!-- Per-clip metadata table -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
        <div class="px-5 py-4 border-b border-zinc-800 flex items-center justify-between">
          <h3 class="text-sm font-semibold text-zinc-300">Clip Metadata</h3>
          <span class="text-xs text-zinc-500">${clips.length} clips</span>
        </div>
        <div class="overflow-x-auto">
          <div class="metadata-clip-list">
            ${clips.length === 0
              ? '<div class="p-8 text-center text-zinc-600 text-sm">No clips available.</div>'
              : clips.map((clip, i) => metadataClipRow(episodeId, clip, i)).join('')
            }
          </div>
        </div>
      </div>

      <!-- Schedule info -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-5">
        <h3 class="text-sm font-semibold text-zinc-300 mb-4">Publish Schedule</h3>
        <div id="metadata-schedule" class="text-sm text-zinc-500">Loading schedule...</div>
      </div>
    </div>`;

  loadMetadataSchedule(episodeId);
}

// Platform config: all supported Upload-Post platforms
const ALL_PLATFORMS = [
  { key: 'youtube',   label: 'YouTube',   color: 'text-red-400',     fields: ['title', 'description'] },
  { key: 'tiktok',    label: 'TikTok',    color: 'text-zinc-300',    fields: ['caption', 'hashtags'] },
  { key: 'instagram', label: 'Instagram', color: 'text-purple-400',  fields: ['caption', 'hashtags'] },
  { key: 'linkedin',  label: 'LinkedIn',  color: 'text-blue-400',    fields: ['title', 'description'] },
  { key: 'x',         label: 'X',         color: 'text-zinc-200',    fields: ['text'] },
  { key: 'facebook',  label: 'Facebook',  color: 'text-blue-500',    fields: ['title', 'description'] },
  { key: 'threads',   label: 'Threads',   color: 'text-zinc-300',    fields: ['text'] },
  { key: 'pinterest', label: 'Pinterest', color: 'text-red-500',     fields: ['title', 'description'] },
  { key: 'bluesky',   label: 'Bluesky',   color: 'text-sky-400',     fields: ['text'] },
];

function platformFieldInput(clipId, platform, field, value, dataAttr) {
  const attr = dataAttr || 'data-clip';
  const maxLen = (platform === 'x' && field === 'text') ? 280
    : (platform === 'bluesky' && field === 'text') ? 300
    : (platform === 'threads' && field === 'text') ? 500
    : null;
  const maxAttr = maxLen ? `maxlength="${maxLen}"` : '';
  const charCounter = maxLen ? `oninput="updateCharCount(this, ${maxLen})"` : '';

  if (field === 'description') {
    return `<textarea ${attr}="${clipId}" data-platform="${platform}" data-field="${field}" rows="2" placeholder="${field}" class="block w-full bg-zinc-800 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-brand-500" ${maxAttr} ${charCounter}>${escapeHtml(value || '')}</textarea>
      ${maxLen ? `<div class="text-right text-[10px] text-zinc-600">${(value || '').length}/${maxLen}</div>` : ''}`;
  }
  if (field === 'text') {
    return `<textarea ${attr}="${clipId}" data-platform="${platform}" data-field="${field}" rows="2" placeholder="${field}" class="block w-full bg-zinc-800 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-brand-500" ${maxAttr} ${charCounter}>${escapeHtml(value || '')}</textarea>
      ${maxLen ? `<div class="text-right text-[10px] text-zinc-600">${(value || '').length}/${maxLen}</div>` : ''}`;
  }
  return `<input type="text" ${attr}="${clipId}" data-platform="${platform}" data-field="${field}" value="${escapeHtml(value || '')}" placeholder="${field}" class="block w-full bg-zinc-800 border border-zinc-700 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-brand-500" ${maxAttr} ${charCounter}>
    ${maxLen ? `<div class="text-right text-[10px] text-zinc-600">${(value || '').length}/${maxLen}</div>` : ''}`;
}

function updateCharCount(el, max) {
  const counter = el.nextElementSibling;
  if (counter) counter.textContent = el.value.length + '/' + max;
}

function metadataClipRow(episodeId, clip, index) {
  const clipId = clip.id;
  const meta = clip.metadata || {};

  return `
    <div class="metadata-clip-row border-b border-zinc-800/50 p-4 hover:bg-zinc-800/20 transition-colors" id="meta-row-${clipId}">
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
          <span class="text-xs font-bold text-zinc-600">#${index + 1}</span>
          ${scoreBadge(Math.round(clip.virality_score || 0))}
          <input type="text" data-clip-title="${clipId}" value="${escapeHtml(clip.title || clipId)}" class="text-sm font-medium bg-transparent border border-transparent hover:border-zinc-700 focus:border-brand-500 rounded px-1.5 py-0.5 text-zinc-200 focus:outline-none transition-colors w-64">
          ${statusBadge(clip.status || 'pending')}
        </div>
        <button onclick="saveClipMetadata('${episodeId}', '${clipId}')" class="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 rounded text-xs font-medium transition-colors">Save</button>
      </div>

      <!-- Platform tab bar -->
      <div class="flex gap-1 mb-3 overflow-x-auto pb-1 scrollbar-thin" id="meta-tabs-${clipId}">
        ${ALL_PLATFORMS.map((p, i) => `
          <button onclick="switchMetaClipTab('${clipId}', '${p.key}')" class="meta-clip-tab-btn whitespace-nowrap px-2.5 py-1 rounded text-xs font-medium transition-colors ${i === 0 ? 'bg-zinc-700 text-white' : 'bg-zinc-800/50 text-zinc-500 hover:text-zinc-300'}" data-metatab="${clipId}-${p.key}">
            ${p.label}
          </button>
        `).join('')}
      </div>

      ${ALL_PLATFORMS.map((p, i) => {
        const pm = meta[p.key] || {};
        const defaultTitle = p.fields.includes('title') ? (clip.title || '') : '';
        return `
        <div id="metatab-${clipId}-${p.key}" class="meta-clip-tab-content ${i > 0 ? 'hidden' : ''} space-y-2">
          <div class="text-xs font-semibold ${p.color} uppercase tracking-wide">${p.label}</div>
          ${p.fields.map(f => {
            const val = pm[f] || (f === 'title' ? defaultTitle : '');
            return platformFieldInput(clipId, p.key, f, val, 'data-clip');
          }).join('')}
        </div>`;
      }).join('')}
    </div>`;
}

function switchMetaClipTab(clipId, platform) {
  // Toggle tab buttons
  document.querySelectorAll(`#meta-tabs-${clipId} .meta-clip-tab-btn`).forEach(btn => {
    const isActive = btn.dataset.metatab === `${clipId}-${platform}`;
    btn.classList.toggle('bg-zinc-700', isActive);
    btn.classList.toggle('text-white', isActive);
    btn.classList.toggle('bg-zinc-800/50', !isActive);
    btn.classList.toggle('text-zinc-500', !isActive);
  });
  // Toggle content
  ALL_PLATFORMS.forEach(p => {
    const el = document.getElementById(`metatab-${clipId}-${p.key}`);
    if (el) el.classList.toggle('hidden', p.key !== platform);
  });
}

async function saveClipMetadata(episodeId, clipId) {
  const meta = {};
  document.querySelectorAll(`[data-clip="${clipId}"]`).forEach(el => {
    const p = el.dataset.platform;
    const f = el.dataset.field;
    if (!meta[p]) meta[p] = {};
    meta[p][f] = el.value;
  });
  // Include clip title if edited
  const titleInput = document.querySelector(`[data-clip-title="${clipId}"]`);
  const title = titleInput ? titleInput.value : undefined;
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/metadata`, {
      method: 'PATCH',
      body: JSON.stringify({ metadata: meta, ...(title !== undefined && { title }) }),
    });
    showToast('Clip metadata saved.', 'success');
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  }
}

async function saveLongformMetadata(episodeId) {
  const title = document.getElementById('lf-title')?.value;
  const description = document.getElementById('lf-desc')?.value;
  const tags = document.getElementById('lf-tags')?.value;
  try {
    await api(`/episodes/${episodeId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        title,
        description,
        tags: tags ? tags.split(',').map(t => t.trim()).filter(Boolean) : [],
      }),
    });
    showToast('Longform metadata saved.', 'success');
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  }
}

async function saveLongformLinks(episodeId) {
  const youtubeUrl = document.getElementById('lf-youtube-url')?.value;
  const spotifyUrl = document.getElementById('lf-spotify-url')?.value;
  const linkTreeUrl = document.getElementById('lf-linktree-url')?.value;
  try {
    await api(`/episodes/${episodeId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        youtube_longform_url: youtubeUrl,
        spotify_longform_url: spotifyUrl,
        link_tree_url: linkTreeUrl,
      }),
    });
    // Update local state
    if (state.currentEpisode) {
      state.currentEpisode.youtube_longform_url = youtubeUrl;
      state.currentEpisode.spotify_longform_url = spotifyUrl;
      state.currentEpisode.link_tree_url = linkTreeUrl;
    }
    showToast('Longform links saved.', 'success');
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  }
}

async function autoCompleteMetadata(episodeId) {
  const btn = document.getElementById('auto-complete-btn');
  if (!btn) return;
  const originalText = btn.textContent;
  btn.textContent = 'Working...';
  btn.disabled = true;
  btn.classList.add('opacity-50');
  try {
    const result = await api(`/episodes/${episodeId}/complete-metadata`, { method: 'POST' });
    if (result.complete) {
      showToast(`All metadata filled in ${result.iterations} iteration(s). ${result.actions_taken.length} actions taken.`, 'success');
    } else {
      showToast(`Completed ${result.iterations} iterations, ${result.actions_taken.length} actions. Some fields may still be missing.`, 'info');
    }
    // Reload episode detail to show updated metadata
    navigate();
  } catch (err) {
    showToast('Auto-complete failed: ' + err.message, 'error');
  } finally {
    btn.textContent = originalText;
    btn.disabled = false;
    btn.classList.remove('opacity-50');
  }
}

async function loadMetadataSchedule(episodeId) {
  const el = document.getElementById('metadata-schedule');
  if (!el) return;
  try {
    const data = await api('/schedule');
    const items = data.schedule || data.items || [];
    if (items.length === 0) {
      el.innerHTML = '<p class="text-zinc-600">No schedule data available.</p>';
      return;
    }
    el.innerHTML = `
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead>
            <tr class="text-left text-zinc-500 border-b border-zinc-800">
              <th class="pb-2 pr-4">Date</th>
              <th class="pb-2 pr-4">Content</th>
              <th class="pb-2 pr-4">Platform(s)</th>
              <th class="pb-2">Type</th>
            </tr>
          </thead>
          <tbody>
            ${items.flatMap(day => (day.items || []).map(item => `
              <tr class="border-b border-zinc-800/30">
                <td class="py-1.5 pr-4 text-zinc-400">${day.date}</td>
                <td class="py-1.5 pr-4 text-zinc-300">${escapeHtml(item.title || item.id || 'Untitled')}</td>
                <td class="py-1.5 pr-4">${(item.platforms || []).map(p => `<span class="platform-${p}">${p}</span>`).join(', ') || '--'}</td>
                <td class="py-1.5">${item.type || '--'}</td>
              </tr>
            `)).join('')}
          </tbody>
        </table>
      </div>`;
  } catch {
    el.innerHTML = '<p class="text-zinc-600">Schedule unavailable.</p>';
  }
}

// ── Chat Tab ───────────────────────────────────────────────────────

function renderChatTab(episodeId) {
  const panel = document.getElementById('tab-chat');
  if (!panel) return;

  const suggestedPrompts = [
    'Change clip 3 title to...',
    'Find me a clip about...',
    'Make clip 5 start 10 seconds earlier',
    'Which clips have the highest virality scores?',
    'Suggest better titles for all clips',
  ];

  panel.innerHTML = `
    <div class="chat-container bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden flex flex-col" style="height: 600px;">
      <!-- Chat messages -->
      <div id="chat-messages" class="flex-1 overflow-y-auto p-4 space-y-3">
        <div class="text-center py-8">
          <div class="text-zinc-600 text-sm mb-4">Chat with the AI agent about this episode.</div>
          <div class="flex flex-wrap gap-2 justify-center">
            ${suggestedPrompts.map(p => `
              <button onclick="sendChatFromSuggestion(this, '${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-full text-xs text-zinc-400 hover:text-zinc-200 transition-colors">${escapeHtml(p)}</button>
            `).join('')}
          </div>
        </div>
      </div>

      <!-- Input area -->
      <div class="border-t border-zinc-800 p-3">
        <div class="flex gap-2">
          <input type="text" id="chat-input" placeholder="Ask the AI agent anything about this episode..." class="flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-4 py-2.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500 placeholder-zinc-600" onkeydown="if(event.key==='Enter')sendChat('${episodeId}')">
          <button onclick="sendChat('${episodeId}')" id="chat-send-btn" class="px-4 py-2.5 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>
            Send
          </button>
        </div>
      </div>
    </div>`;

  // Re-render any existing messages
  renderChatMessages();
}

function renderChatMessages() {
  const container = document.getElementById('chat-messages');
  if (!container || state.chatMessages.length === 0) return;

  // Keep the initial suggestion area if no messages yet
  container.innerHTML = state.chatMessages.map(msg => {
    if (msg.role === 'user') {
      return `
        <div class="flex justify-end">
          <div class="chat-bubble chat-user max-w-[80%] px-4 py-2.5 rounded-2xl rounded-br-sm text-sm">
            ${escapeHtml(msg.content)}
          </div>
        </div>`;
    } else {
      return `
        <div class="flex justify-start">
          <div class="max-w-[80%]">
            <div class="chat-bubble chat-ai px-4 py-2.5 rounded-2xl rounded-bl-sm text-sm">
              ${formatChatResponse(msg.content)}
            </div>
            ${msg.actions && msg.actions.length > 0 ? `
              <div class="flex flex-wrap gap-1 mt-1.5 ml-1">
                ${msg.actions.map(a => `
                  <span class="inline-flex items-center gap-1 px-2 py-0.5 bg-brand-900/30 border border-brand-800/50 rounded-full text-xs text-brand-400">
                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                    ${escapeHtml(typeof a === 'string' ? a : a.action || a.type || 'Action')}
                  </span>
                `).join('')}
              </div>
            ` : ''}
          </div>
        </div>`;
    }
  }).join('');

  // Auto-scroll to bottom
  container.scrollTop = container.scrollHeight;
}

function formatChatResponse(text) {
  if (!text) return '';
  // Basic markdown-like formatting
  return escapeHtml(text)
    .replace(/\n/g, '<br>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.*?)`/g, '<code class="bg-zinc-700 px-1 rounded text-xs">$1</code>');
}

function sendChatFromSuggestion(btn, episodeId) {
  const input = document.getElementById('chat-input');
  if (input) {
    input.value = btn.textContent.trim();
    input.focus();
  }
}

async function sendChat(episodeId) {
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('chat-send-btn');
  if (!input) return;

  const message = input.value.trim();
  if (!message) return;

  // Add user message
  state.chatMessages.push({ role: 'user', content: message });
  input.value = '';
  renderChatMessages();

  // Disable input while waiting
  input.disabled = true;
  if (sendBtn) sendBtn.disabled = true;

  try {
    const result = await api(`/episodes/${episodeId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });

    state.chatMessages.push({
      role: 'assistant',
      content: result.response,
      actions: result.actions_taken || [],
    });
    renderChatMessages();

    // If actions were taken, refresh clips in background
    if (result.actions_taken && result.actions_taken.length > 0) {
      try {
        const clips = await api(`/episodes/${episodeId}/clips`);
        state.clips = clips;
      } catch {
        // Silently ignore
      }
    }
  } catch (err) {
    state.chatMessages.push({
      role: 'assistant',
      content: 'Sorry, I encountered an error: ' + err.message,
      actions: [],
    });
    renderChatMessages();
  } finally {
    input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  }
}


// ── Shorts / Detail Collapsible Chat ────────────────────────────────

function toggleShortsChat() {
  const body = document.getElementById('shorts-chat-body');
  const chevron = document.getElementById('shorts-chat-chevron');
  if (body) {
    body.classList.toggle('hidden');
    if (chevron) chevron.classList.toggle('rotate-180');
  }
}

function sendShortsChatSuggestion(btn, episodeId) {
  const input = document.getElementById('shorts-chat-input');
  if (input) {
    input.value = btn.textContent.trim();
    input.focus();
  }
}

async function sendShortsChat(episodeId) {
  const input = document.getElementById('shorts-chat-input');
  const sendBtn = document.getElementById('shorts-chat-send');
  if (!input) return;

  const message = input.value.trim();
  if (!message) return;

  // Add user message to shared chat state
  state.chatMessages.push({ role: 'user', content: message });
  input.value = '';
  _renderInlineChat('shorts-chat-messages');

  input.disabled = true;
  if (sendBtn) sendBtn.disabled = true;

  try {
    const result = await api(`/episodes/${episodeId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });

    state.chatMessages.push({
      role: 'assistant',
      content: result.response,
      actions: result.actions_taken || [],
    });
    _renderInlineChat('shorts-chat-messages');

    // Refresh clips if actions were taken
    if (result.actions_taken && result.actions_taken.length > 0) {
      try {
        const clips = await api(`/episodes/${episodeId}/clips`);
        state.clips = clips;
        renderShortsTab(episodeId, clips, state.currentEpisode);
      } catch { /* ignore */ }
    }
  } catch (err) {
    state.chatMessages.push({
      role: 'assistant',
      content: 'Sorry, I encountered an error: ' + err.message,
      actions: [],
    });
    _renderInlineChat('shorts-chat-messages');
  } finally {
    input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  }
}

function toggleDetailChat() {
  const body = document.getElementById('detail-chat-body');
  const chevron = document.getElementById('detail-chat-chevron');
  if (body) {
    body.classList.toggle('hidden');
    if (chevron) chevron.classList.toggle('rotate-180');
  }
}

function sendDetailChatSuggestion(btn, episodeId) {
  const input = document.getElementById('detail-chat-input');
  if (input) {
    input.value = btn.textContent.trim();
    input.focus();
  }
}

async function sendDetailChat(episodeId) {
  const input = document.getElementById('detail-chat-input');
  const sendBtn = document.getElementById('detail-chat-send');
  if (!input) return;

  const message = input.value.trim();
  if (!message) return;

  state.chatMessages.push({ role: 'user', content: message });
  input.value = '';
  _renderInlineChat('detail-chat-messages');

  input.disabled = true;
  if (sendBtn) sendBtn.disabled = true;

  try {
    const result = await api(`/episodes/${episodeId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });

    state.chatMessages.push({
      role: 'assistant',
      content: result.response,
      actions: result.actions_taken || [],
    });
    _renderInlineChat('detail-chat-messages');

    // Refresh clip detail if actions were taken
    if (result.actions_taken && result.actions_taken.length > 0 && state.currentClipId) {
      try {
        await renderClipDetail(episodeId, state.currentClipId);
      } catch { /* ignore */ }
    }
  } catch (err) {
    state.chatMessages.push({
      role: 'assistant',
      content: 'Sorry, I encountered an error: ' + err.message,
      actions: [],
    });
    _renderInlineChat('detail-chat-messages');
  } finally {
    input.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  }
}

function _renderInlineChat(containerId) {
  const container = document.getElementById(containerId);
  if (!container || state.chatMessages.length === 0) return;

  container.innerHTML = state.chatMessages.map(msg => {
    if (msg.role === 'user') {
      return `<div class="flex justify-end">
        <div class="chat-bubble chat-user max-w-[80%] px-3 py-2 rounded-2xl rounded-br-sm text-xs">${escapeHtml(msg.content)}</div>
      </div>`;
    }
    return `<div class="flex justify-start">
      <div class="max-w-[80%]">
        <div class="chat-bubble chat-ai px-3 py-2 rounded-2xl rounded-bl-sm text-xs">${formatChatResponse(msg.content)}</div>
        ${msg.actions && msg.actions.length > 0 ? `
          <div class="flex flex-wrap gap-1 mt-1 ml-1">
            ${msg.actions.map(a => `
              <span class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-brand-900/30 border border-brand-800/50 rounded-full text-[10px] text-brand-400">
                <svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                ${escapeHtml(typeof a === 'string' ? a : a.action || a.type || 'Action')}
              </span>
            `).join('')}
          </div>
        ` : ''}
      </div>
    </div>`;
  }).join('');

  container.scrollTop = container.scrollHeight;
}

// ════════════════════════════════════════════════════════════════════
//  CLIP DETAIL VIEW
// ════════════════════════════════════════════════════════════════════

async function renderClipDetail(episodeId, clipId) {
  let clip;
  try {
    clip = await api(`/episodes/${episodeId}/clips/${clipId}`);
  } catch {
    // If individual clip endpoint fails, get from list
    const allClips = await api(`/episodes/${episodeId}/clips`);
    clip = allClips.find(c => c.id === clipId);
    if (!clip) throw new Error('Clip not found');
  }

  const allClips = state.clips.length ? state.clips : (await api(`/episodes/${episodeId}/clips`));
  state.clips = allClips;

  const idx = allClips.findIndex(c => c.id === clipId);
  const prevClip = idx > 0 ? allClips[idx - 1] : null;
  const nextClip = idx < allClips.length - 1 ? allClips[idx + 1] : null;

  const metadata = clip.metadata || {};

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <!-- Breadcrumb -->
    <div class="flex items-center gap-2 text-sm text-zinc-500 mb-6">
      <a href="#/" class="hover:text-white transition-colors">Dashboard</a>
      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      <a href="#/episodes/${episodeId}" class="hover:text-white transition-colors">Episode</a>
      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      <span class="text-zinc-300">${escapeHtml(clip.title || clipId)}</span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <!-- Video player: large 9:16 -->
      <div class="lg:col-span-4">
        <div class="sticky top-20">
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
            <div class="clip-detail-video aspect-[9/16] bg-black">
              ${(state.currentEpisode?.pipeline?.agents_completed || []).includes('shorts_render')
                ? `<video id="clip-video" controls preload="metadata" class="w-full h-full object-contain">
                    <source src="${MEDIA}/episodes/${episodeId}/shorts/${clipId}.mp4" type="video/mp4">
                  </video>`
                : `<div class="w-full h-full flex items-center justify-center text-zinc-600 text-sm">Short not yet rendered</div>`
              }
            </div>
          </div>

          <!-- Prev/Next nav -->
          <div class="flex gap-2 mt-3">
            ${prevClip
              ? `<a href="#/episodes/${episodeId}/clips/${prevClip.id}" class="flex-1 px-3 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 rounded-lg text-xs font-medium text-center transition-colors flex items-center justify-center gap-1">
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>
                  Previous
                </a>`
              : '<span class="flex-1"></span>'
            }
            ${nextClip
              ? `<a href="#/episodes/${episodeId}/clips/${nextClip.id}" class="flex-1 px-3 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 rounded-lg text-xs font-medium text-center transition-colors flex items-center justify-center gap-1">
                  Next
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                </a>`
              : '<span class="flex-1"></span>'
            }
          </div>
        </div>
      </div>

      <!-- Right side: metadata and controls -->
      <div class="lg:col-span-8 space-y-4">
        <!-- Header with actions -->
        <div class="flex items-start justify-between">
          <div>
            <h1 class="text-lg font-bold mb-1">${escapeHtml(clip.title || clipId)}</h1>
            <div class="flex items-center gap-3 text-sm text-zinc-500">
              ${scoreBadge(Math.round(clip.virality_score || 0))}
              <span>${formatTime(clip.start_seconds || clip.start)} - ${formatTime(clip.end_seconds || clip.end)}</span>
              <span>${formatDuration(clip.duration || ((clip.end_seconds || clip.end) - (clip.start_seconds || clip.start)))}</span>
              ${speakerLabel(clip.speaker)}
              ${statusBadge(clip.status || 'pending')}
            </div>
          </div>
        </div>

        <!-- Action buttons -->
        <div class="flex gap-2">
          <button onclick="approveClipDetail('${episodeId}', '${clipId}')" class="px-4 py-2 bg-green-700 hover:bg-green-600 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            Approve
          </button>
          <button onclick="rejectClipDetail('${episodeId}', '${clipId}')" class="px-4 py-2 bg-red-900 hover:bg-red-800 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
            Reject
          </button>
          <button onclick="requestAlternative('${episodeId}', '${clipId}')" class="px-4 py-2 bg-amber-800 hover:bg-amber-700 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Request Alt
          </button>
        </div>

        <!-- Time range editor -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Time Range</h3>
          <div class="flex items-end gap-3">
            <label class="block flex-1">
              <span class="text-xs text-zinc-500">Start (MM:SS)</span>
              <input type="text" id="edit-start" value="${formatTime(clip.start_seconds || clip.start)}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 font-mono text-center focus:outline-none focus:border-brand-500">
            </label>
            <label class="block flex-1">
              <span class="text-xs text-zinc-500">End (MM:SS)</span>
              <input type="text" id="edit-end" value="${formatTime(clip.end_seconds || clip.end)}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 font-mono text-center focus:outline-none focus:border-brand-500">
            </label>
            <button onclick="updateTimeRange('${episodeId}', '${clipId}')" class="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-sm font-medium transition-colors whitespace-nowrap">
              Update Range
            </button>
          </div>
        </div>

        <!-- Per-platform metadata -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <div class="flex items-center justify-between mb-4">
            <h3 class="text-sm font-semibold text-zinc-300">Platform Metadata</h3>
            <button onclick="saveClipDetailMetadata('${episodeId}', '${clipId}')" class="px-3 py-1.5 bg-brand-600 hover:bg-brand-700 rounded text-xs font-medium transition-colors">Save All</button>
          </div>
          <div class="flex gap-1 mb-4 overflow-x-auto pb-1 scrollbar-thin">
            ${ALL_PLATFORMS.map((p, i) => `
              <button onclick="switchPlatformTab('${p.key}')" class="tab-btn platform-tab-btn whitespace-nowrap ${i === 0 ? 'active' : ''}" data-ptab="${p.key}">
                ${p.label}
              </button>
            `).join('')}
          </div>
          ${ALL_PLATFORMS.map((p, i) => {
            const pm = metadata[p.key] || {};
            return `
            <div id="ptab-${p.key}" class="platform-tab-content space-y-3 ${i > 0 ? 'hidden' : ''}">
              ${p.fields.map(f => {
                const val = pm[f] || (f === 'title' ? (clip.title || '') : '');
                const maxLen = (p.key === 'x' && f === 'text') ? 280
                  : (p.key === 'bluesky' && f === 'text') ? 300
                  : (p.key === 'threads' && f === 'text') ? 500
                  : null;
                const maxAttr = maxLen ? 'maxlength="' + maxLen + '"' : '';
                const charCounter = maxLen ? 'oninput="updateCharCount(this, ' + maxLen + ')"' : '';
                if (f === 'description' || f === 'text') {
                  return '<label class="block">' +
                    '<span class="text-xs text-zinc-500">' + f.charAt(0).toUpperCase() + f.slice(1) + '</span>' +
                    '<textarea data-clipmeta="' + clipId + '" data-platform="' + p.key + '" data-field="' + f + '" rows="3" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500" ' + maxAttr + ' ' + charCounter + '>' + escapeHtml(val) + '</textarea>' +
                    (maxLen ? '<div class="text-right text-[10px] text-zinc-600 mt-0.5">' + (val || '').length + '/' + maxLen + '</div>' : '') +
                    '</label>';
                }
                return '<label class="block">' +
                  '<span class="text-xs text-zinc-500">' + f.charAt(0).toUpperCase() + f.slice(1) + '</span>' +
                  '<input type="text" data-clipmeta="' + clipId + '" data-platform="' + p.key + '" data-field="' + f + '" value="' + escapeHtml(val) + '" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500" ' + maxAttr + ' ' + charCounter + '>' +
                  (maxLen ? '<div class="text-right text-[10px] text-zinc-600 mt-0.5">' + (val || '').length + '/' + maxLen + '</div>' : '') +
                  '</label>';
              }).join('')}
            </div>`;
          }).join('')}
        </div>

        <!-- Clip details -->
        ${clip.hook_text || clip.compelling_reason ? `
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-2">
          ${clip.hook_text ? `<div class="text-sm"><span class="text-zinc-500 text-xs">Hook:</span> <span class="text-zinc-300">"${escapeHtml(clip.hook_text)}"</span></div>` : ''}
          ${clip.compelling_reason ? `<div class="text-sm"><span class="text-zinc-500 text-xs">Why this clip:</span> <span class="text-zinc-400">${escapeHtml(clip.compelling_reason)}</span></div>` : ''}
        </div>
        ` : ''}

        <!-- Transcript -->
        ${clip.transcript_excerpt ? `
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-2">Transcript</h3>
          <div class="max-h-48 overflow-y-auto space-y-1 text-sm text-zinc-400">
            ${clip.transcript_excerpt.split('\n').map(line => `<p class="transcript-line py-0.5">${escapeHtml(line)}</p>`).join('')}
          </div>
        </div>
        ` : ''}

        <!-- AI Chat Panel -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
          <button onclick="toggleDetailChat()" class="w-full flex items-center justify-between px-4 py-3 hover:bg-zinc-800/50 transition-colors">
            <div class="flex items-center gap-2">
              <svg class="w-4 h-4 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
              <span class="text-sm font-medium text-zinc-300">AI Assistant</span>
            </div>
            <svg class="w-4 h-4 text-zinc-500 transition-transform" id="detail-chat-chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div id="detail-chat-body" class="hidden border-t border-zinc-800">
            <div id="detail-chat-messages" class="overflow-y-auto p-4 space-y-3" style="max-height: 300px;">
              <div class="text-center py-3">
                <div class="flex flex-wrap gap-2 justify-center">
                  <button onclick="sendDetailChatSuggestion(this, '${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-full text-xs text-zinc-400 hover:text-zinc-200 transition-colors">Make this title more engaging</button>
                  <button onclick="sendDetailChatSuggestion(this, '${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-full text-xs text-zinc-400 hover:text-zinc-200 transition-colors">Suggest better hashtags</button>
                  <button onclick="sendDetailChatSuggestion(this, '${episodeId}')" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-full text-xs text-zinc-400 hover:text-zinc-200 transition-colors">Trim 5 seconds from the beginning</button>
                </div>
              </div>
            </div>
            <div class="border-t border-zinc-800 p-3">
              <div class="flex gap-2">
                <input type="text" id="detail-chat-input" placeholder="Ask about this clip..." class="flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-brand-500 placeholder-zinc-600" onkeydown="if(event.key==='Enter')sendDetailChat('${episodeId}')">
                <button onclick="sendDetailChat('${episodeId}')" id="detail-chat-send" class="px-3 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">Send</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}

function switchPlatformTab(platform) {
  document.querySelectorAll('.platform-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.ptab === platform));
  document.querySelectorAll('.platform-tab-content').forEach(c => c.classList.toggle('hidden', c.id !== `ptab-${platform}`));
}

async function approveClipDetail(episodeId, clipId) {
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/approve`, { method: 'POST' });
    showToast('Clip approved.', 'success');
    await renderClipDetail(episodeId, clipId);
  } catch (err) {
    showToast('Approve failed: ' + err.message, 'error');
  }
}

async function rejectClipDetail(episodeId, clipId) {
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/reject`, { method: 'POST' });
    showToast('Clip rejected.', 'success');
    await renderClipDetail(episodeId, clipId);
  } catch (err) {
    showToast('Reject failed: ' + err.message, 'error');
  }
}

async function requestAlternative(episodeId, clipId) {
  try {
    const result = await api(`/episodes/${episodeId}/clips/${clipId}/alternative`, { method: 'POST' });
    showToast(result.message || 'Alternative requested.', 'success');
    await renderClipDetail(episodeId, clipId);
  } catch (err) {
    showToast('Failed: ' + err.message, 'error');
  }
}

async function updateTimeRange(episodeId, clipId) {
  const start = parseTime(document.getElementById('edit-start').value);
  const end = parseTime(document.getElementById('edit-end').value);
  if (isNaN(start) || isNaN(end) || end <= start) {
    showToast('Invalid time range. End must be after start.', 'error');
    return;
  }
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/metadata`, {
      method: 'PATCH',
      body: JSON.stringify({ start_seconds: start, end_seconds: end }),
    });
    showToast('Time range updated.', 'success');
    await renderClipDetail(episodeId, clipId);
  } catch (err) {
    showToast('Update failed: ' + err.message, 'error');
  }
}

async function saveClipDetailMetadata(episodeId, clipId) {
  const meta = {};
  document.querySelectorAll(`[data-clipmeta="${clipId}"]`).forEach(el => {
    const p = el.dataset.platform;
    const f = el.dataset.field;
    if (!meta[p]) meta[p] = {};
    meta[p][f] = el.value;
  });
  try {
    await api(`/episodes/${episodeId}/clips/${clipId}/metadata`, {
      method: 'PATCH',
      body: JSON.stringify({ metadata: meta }),
    });
    showToast('Metadata saved.', 'success');
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  }
}


// ════════════════════════════════════════════════════════════════════
//  CROP SETUP VIEW
// ════════════════════════════════════════════════════════════════════

async function renderCropSetup(episodeId) {
  // Load existing config
  let ep;
  try { ep = await api(`/episodes/${episodeId}`); } catch { ep = {}; }

  const speakerCount = ep.speaker_count || 2;
  const existing = ep.crop_config;

  // Initialize speakers array
  cropState.sourceWidth = (existing && existing.source_width) || 1920;
  cropState.sourceHeight = (existing && existing.source_height) || 1080;

  if (existing && existing.speakers && existing.speakers.length) {
    cropState.speakers = existing.speakers.map(s => ({
      label: s.label, x: s.center_x, y: s.center_y, zoom: s.zoom || 1.0,
      longform_zoom: s.longform_zoom || 0.75, track: s.track || null,
    }));
  } else if (existing && existing.speaker_l_center_x != null) {
    // Legacy L/R format
    cropState.speakers = [
      { label: 'Speaker 0', x: existing.speaker_l_center_x, y: existing.speaker_l_center_y, zoom: existing.speaker_l_zoom || 1.0, track: 1 },
      { label: 'Speaker 1', x: existing.speaker_r_center_x, y: existing.speaker_r_center_y, zoom: existing.speaker_r_zoom || 1.0, track: 2 },
    ];
  } else {
    // Defaults: spread speakers evenly across the frame
    cropState.speakers = [];
    for (let i = 0; i < speakerCount; i++) {
      const xFrac = (i + 1) / (speakerCount + 1);
      cropState.speakers.push({
        label: `Speaker ${i}`,
        x: Math.round(cropState.sourceWidth * xFrac),
        y: Math.round(cropState.sourceHeight / 2),
        zoom: 1.0,
        longform_zoom: 0.75,
        track: i + 1,
      });
    }
  }
  cropState.activeIdx = 0;

  // Initialize wide shot (all-speakers) crop
  cropState.wide = {
    x: (existing && existing.wide_center_x) || Math.round(cropState.sourceWidth / 2),
    y: (existing && existing.wide_center_y) || Math.round(cropState.sourceHeight / 2),
    zoom: (existing && existing.wide_zoom) || 1.0,
  };

  // Get audio track info from episode data
  const allAudioTracks = ep.audio_tracks || [];
  const inputTracks = allAudioTracks.filter(t => t.track_type === 'input');

  // Initialize mixer state
  if (mixerState.playing) stopMixerPlayback();
  if (mixerState.audioCtx) { mixerState.audioCtx.close().catch(() => {}); mixerState.audioCtx = null; }
  mixerState.loaded = false;
  mixerState.episodeId = episodeId;
  mixerState.tracks = allAudioTracks.map(t => {
    const isInput = t.track_type === 'input';
    return {
      stem: t.filename.replace(/\.(WAV|wav)$/, ''),
      label: t.track_type === 'stereo_mix' ? 'Mix' : t.track_type === 'builtin_mic' ? 'Mic' : 'Tr' + t.track_number,
      trackNumber: t.track_number || null,
      trackType: t.track_type,
      volume: isInput ? 1.0 : 0.2,
      muted: false, soloed: false, assignment: '',
      buffer: null, source: null, gain: null, analyser: null,
    };
  });
  // Pre-fill assignments from existing crop config
  for (let si = 0; si < cropState.speakers.length; si++) {
    const trk = cropState.speakers[si].track;
    if (trk) {
      const ti = mixerState.tracks.findIndex(t => t.trackNumber === trk);
      if (ti >= 0) mixerState.tracks[ti].assignment = `speaker-${si}`;
    }
  }
  for (const t of mixerState.tracks) {
    if (!t.assignment && t.trackType !== 'input') t.assignment = 'ambient';
  }
  // Load existing volumes from crop config
  const existingAmbient = (existing && existing.ambient_tracks) || [];
  for (const t of mixerState.tracks) {
    if (t.assignment === 'ambient' && t.trackNumber) {
      const ea = existingAmbient.find(a => a.track_number === t.trackNumber);
      if (ea) t.volume = ea.volume;
    }
  }
  if (existing && existing.speakers) {
    for (const spk of existing.speakers) {
      if (spk.track && spk.volume != null) {
        const mt = mixerState.tracks.find(t => t.trackNumber === spk.track);
        if (mt) mt.volume = spk.volume;
      }
    }
  }

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <div class="flex items-center gap-2 text-sm text-zinc-500 mb-6">
      <a href="#/" class="hover:text-white transition-colors">Dashboard</a>
      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      <a href="#/episodes/${episodeId}" class="hover:text-white transition-colors">${escapeHtml(ep.title || episodeId)}</a>
      <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      <span class="text-zinc-300">Crop Setup</span>
    </div>

    <h1 class="text-xl font-bold mb-2">Speaker Crop Setup</h1>
    <p class="text-zinc-500 text-sm mb-6">Click on the video frame to set each speaker's center point. The system derives both 9:16 (shorts) and 16:9 (longform) crop rectangles from these centers.</p>

    ${(ep.audio_sync && ep.audio_sync.offset_seconds != null) ? `
    <div id="sync-verify" class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 mb-6">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-semibold text-zinc-300">Audio Sync</h3>
        <span id="sync-status" class="text-xs text-zinc-600">Loading waveforms...</span>
      </div>

      <div class="mb-3 flex gap-4 items-start">
        <video id="sync-video" width="480" class="rounded-lg bg-black" preload="metadata" controls>
          <source src="${API}/episodes/${episodeId}/video-preview" type="video/mp4">
        </video>
        <div class="flex flex-col gap-2 min-w-[160px]">
          <div class="flex items-center gap-2">
            <button onclick="toggleSyncMute('camera')" id="sync-mute-cam" class="px-2 py-1 text-xs rounded font-medium bg-orange-800 text-orange-200">Camera ON</button>
            <span class="text-xs text-zinc-500">Video mic</span>
          </div>
          <div class="flex items-center gap-2">
            <button onclick="toggleSyncMute('h6e')" id="sync-mute-h6e" class="px-2 py-1 text-xs rounded font-medium bg-blue-800 text-blue-200">H6E ON</button>
            <span class="text-xs text-zinc-500">External mic</span>
          </div>
          <audio id="sync-h6e-audio" preload="auto"></audio>
          <div class="mt-2">
            <label class="text-xs text-zinc-500">Offset (scroll or type):</label>
            <input type="number" id="sync-offset-input" step="0.1"
              value="${ep.audio_sync.offset_seconds.toFixed(2)}"
              class="w-full mt-1 px-2 py-1 text-sm font-mono bg-zinc-800 border border-zinc-700 rounded text-white text-center"
              onchange="setSyncOffset(parseFloat(this.value)||0)"
            >
          </div>
          <button onclick="saveSyncOffset('${episodeId}')" id="sync-save-btn" class="px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 rounded font-medium transition-colors">Save Offset</button>
        </div>
      </div>

      <div class="mb-1 flex items-center gap-4">
        <div class="flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(251,146,60,0.8);"></span><span class="text-xs text-zinc-400">Camera</span></div>
        <div class="flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(96,165,250,0.8);"></span><span class="text-xs text-zinc-400">H6E (shifted by offset)</span></div>
        <span class="text-xs text-zinc-600 ml-auto">Scroll to zoom, drag to pan</span>
      </div>
      <canvas id="sync-waveform" class="w-full rounded cursor-crosshair" style="height:180px;background:#0a0a0a;"></canvas>
      <div class="flex items-center justify-between mt-1">
        <span id="sync-view-range" class="text-xs text-zinc-600 font-mono"></span>
        <button onclick="syncZoomToFit()" class="text-xs text-zinc-500 hover:text-zinc-300 transition-colors">Zoom to fit</button>
      </div>
    </div>` : ''}

    ${allAudioTracks.length > 0 ? `
    <div id="audio-mixer" class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 mb-6">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-semibold text-zinc-300">Audio Track Mixer</h3>
        <div class="flex items-center gap-3">
          <span class="text-xs text-zinc-500">Preview: <span id="mixer-range">${fmtMixerTime(mixerState.previewStart)} – ${fmtMixerTime(mixerState.previewStart + mixerState.previewDuration)}</span></span>
          <button onclick="shiftMixerWindow(-30,'${episodeId}')" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors">&larr;</button>
          <button onclick="shiftMixerWindow(30,'${episodeId}')" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors">&rarr;</button>
          <button id="mixer-load-btn" onclick="loadAudioPreviews('${episodeId}')" class="px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 rounded font-medium transition-colors">Load Audio</button>
          <button id="mixer-play-btn" onclick="toggleMixerPlayback()" disabled class="px-3 py-1.5 text-xs bg-zinc-700 rounded font-medium text-zinc-500 transition-colors">&#9654; Play</button>
        </div>
      </div>
      <div id="mixer-tracks" class="space-y-1">
        ${mixerState.tracks.map((t, i) => `
        <div class="flex items-center gap-2 py-0.5">
          <div class="w-10 text-xs font-mono ${t.trackType === 'input' ? 'text-zinc-300' : 'text-zinc-500'} truncate">${escapeHtml(t.label)}</div>
          <button onclick="toggleTrackSolo(${i})" id="mixer-solo-${i}" class="w-6 h-6 text-[10px] font-bold rounded bg-zinc-800 text-zinc-500 hover:text-yellow-400 transition-colors" title="Solo">S</button>
          <button onclick="toggleTrackMute(${i})" id="mixer-mute-${i}" class="w-6 h-6 text-[10px] font-bold rounded bg-zinc-800 text-zinc-500 hover:text-red-400 transition-colors" title="Mute">M</button>
          <canvas id="mixer-meter-${i}" width="200" height="16" class="rounded" style="background:#111;min-width:100px;height:16px;flex:2;"></canvas>
          <input type="range" id="mixer-vol-${i}" min="0" max="200" value="${Math.round(t.volume * 100)}" oninput="setMixerTrackVolume(${i},this.value)" class="w-16 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
          <span id="mixer-vol-label-${i}" class="text-xs text-zinc-400 w-8 text-right font-mono">${Math.round(t.volume * 100)}%</span>
          <select id="mixer-assign-${i}" onchange="assignMixerTrack(${i},this.value)" class="bg-zinc-800 border border-zinc-700 rounded text-xs text-zinc-300 px-1.5 py-1 w-24">
            <option value="">—</option>
            ${cropState.speakers.map((s, si) => `<option value="speaker-${si}" ${t.assignment === 'speaker-' + si ? 'selected' : ''}>${escapeHtml(s.label)}</option>`).join('')}
            <option value="ambient" ${t.assignment === 'ambient' ? 'selected' : ''}>Ambient</option>
          </select>
        </div>`).join('')}
      </div>
      <p id="mixer-status" class="text-xs text-zinc-600 mt-2">Click "Load Audio" to preview tracks and identify speakers.</p>
    </div>` : ''}

    <div class="grid grid-cols-1 lg:grid-cols-4 gap-6">
      <div class="lg:col-span-3">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <canvas id="crop-canvas" class="w-full cursor-crosshair rounded-lg" style="background: #000;"></canvas>
        </div>
      </div>

      <div class="space-y-4">
        <!-- Speaker selector -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Placing Speaker</h3>
          <div id="crop-speaker-btns" class="flex flex-wrap gap-2">
            ${cropState.speakers.map((s, i) => {
              const c = SPEAKER_COLORS[i % SPEAKER_COLORS.length];
              const active = i === 0;
              return `<button id="crop-mode-${i}" onclick="setCropSpeaker(${i})" class="flex-1 min-w-[60px] px-3 py-2 rounded-lg text-sm font-medium transition-colors ${active ? c.bg + ' text-white' : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700'}">${i}</button>`;
            }).join('')}
            <button id="crop-mode-wide" onclick="setCropSpeaker(-1)" class="flex-1 min-w-[60px] px-3 py-2 rounded-lg text-sm font-medium transition-colors bg-zinc-800 text-zinc-400 hover:bg-zinc-700">Wide</button>
          </div>
          <p class="text-xs text-zinc-600 mt-2">Click the frame to set the selected speaker's center point.</p>
        </div>

        <!-- Positions & zoom per speaker -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Speakers</h3>
          <div id="crop-speaker-details" class="space-y-3">
            ${cropState.speakers.map((s, i) => {
              const c = SPEAKER_COLORS[i % SPEAKER_COLORS.length];
              return `
              <div class="border-b border-zinc-800 pb-2 last:border-0 last:pb-0">
                <div class="flex justify-between items-center mb-1">
                  <input type="text" value="${escapeHtml(s.label)}" onchange="cropState.speakers[${i}].label=this.value"
                    class="bg-transparent text-sm font-medium border-none focus:outline-none p-0" style="color: ${c.css}; width: 120px;">
                  <span id="crop-pos-${i}" class="text-zinc-400 font-mono text-xs">${s.x}, ${s.y}</span>
                </div>
                <div class="flex items-center gap-2">
                  <span class="text-xs text-zinc-500 w-10">Zoom</span>
                  <input type="range" id="crop-zoom-${i}" min="0.5" max="3.0" step="0.1" value="${s.zoom}"
                    oninput="setCropZoomN(${i}, this.value)"
                    class="flex-1 h-1.5 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
                  <span id="crop-zoom-${i}-val" class="text-zinc-400 font-mono text-xs w-8">${s.zoom.toFixed(1)}x</span>
                </div>
                ${inputTracks.length > 0 ? `
                <div class="flex items-center gap-2 mt-1">
                  <span class="text-xs text-zinc-500 w-10">Track</span>
                  <select id="crop-track-${i}" onchange="cropState.speakers[${i}].track=parseInt(this.value)||null"
                    class="flex-1 bg-zinc-800 border border-zinc-700 rounded text-xs text-zinc-300 px-2 py-1">
                    <option value="">None</option>
                    ${inputTracks.map(t => `<option value="${t.track_number}" ${s.track === t.track_number ? 'selected' : ''}>Tr${t.track_number}</option>`).join('')}
                  </select>
                </div>` : ''}
              </div>`;
            }).join('')}
          </div>

          <!-- Wide shot (all speakers) -->
          <div class="border-t border-zinc-800 pt-2 mt-2">
            <div class="flex justify-between items-center mb-1">
              <span class="text-sm font-medium text-zinc-400">Wide Shot</span>
              <span id="crop-pos-wide" class="text-zinc-400 font-mono text-xs">${cropState.wide.x}, ${cropState.wide.y}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-xs text-zinc-500 w-10">Zoom</span>
              <input type="range" id="crop-zoom-wide" min="0.5" max="3.0" step="0.1" value="${cropState.wide.zoom}"
                oninput="setCropWideZoom(this.value)"
                class="flex-1 h-1.5 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
              <span id="crop-zoom-wide-val" class="text-zinc-400 font-mono text-xs w-8">${cropState.wide.zoom.toFixed(1)}x</span>
            </div>
            <p class="text-xs text-zinc-600 mt-1">Used when all speakers are on screen.</p>
          </div>
        </div>

        <!-- Instructions -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-2">How it works</h3>
          <ul class="text-xs text-zinc-500 space-y-1 list-disc list-inside">
            <li>Select a speaker number above</li>
            <li>Click on the frame where that speaker's face is centered</li>
            <li>Use zoom sliders to adjust how tight the crop is</li>
            <li>Dashed rect = 9:16 shorts crop</li>
            <li>Inner rect = 16:9 longform crop</li>
            ${inputTracks.length > 0 ? '<li>Map each speaker to their H6E audio track</li>' : ''}
          </ul>
        </div>

        <button onclick="saveCropConfig('${episodeId}')" id="crop-save-btn" class="w-full px-4 py-3 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors">
          Save &amp; Continue
        </button>
      </div>
    </div>
  </div>`;

  // Load the crop frame image
  const canvas = document.getElementById('crop-canvas');
  const img = new Image();
  img.onload = function() {
    cropState.image = img;
    cropState.sourceWidth = img.naturalWidth;
    cropState.sourceHeight = img.naturalHeight;
    const containerWidth = canvas.parentElement.clientWidth - 32;
    const aspect = img.naturalHeight / img.naturalWidth;
    canvas.width = containerWidth;
    canvas.height = Math.round(containerWidth * aspect);
    cropState.scaleFactor = img.naturalWidth / canvas.width;
    redrawCropCanvas();
  };
  img.onerror = function() {
    const ctx = canvas.getContext('2d');
    canvas.width = 800;
    canvas.height = 450;
    ctx.fillStyle = '#27272a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#71717a';
    ctx.font = '14px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText(cropState._loadError || 'Crop frame not available. Run stitch first.', canvas.width / 2, canvas.height / 2);
  };
  fetch(`${API}/episodes/${episodeId}/crop-frame`)
    .then(res => { if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.blob(); })
    .then(blob => { img.src = URL.createObjectURL(blob); })
    .catch(err => { cropState._loadError = `Failed to load crop frame: ${err.message}`; img.onerror(); });

  // Canvas click handler
  canvas.addEventListener('click', function(e) {
    const rect = canvas.getBoundingClientRect();
    const srcX = Math.round((e.clientX - rect.left) * cropState.scaleFactor);
    const srcY = Math.round((e.clientY - rect.top) * cropState.scaleFactor);
    if (cropState.activeIdx === -1) {
      // Wide shot mode
      cropState.wide.x = srcX;
      cropState.wide.y = srcY;
      const posEl = document.getElementById('crop-pos-wide');
      if (posEl) posEl.textContent = `${srcX}, ${srcY}`;
    } else {
      const spk = cropState.speakers[cropState.activeIdx];
      if (spk) {
        spk.x = srcX;
        spk.y = srcY;
        const posEl = document.getElementById(`crop-pos-${cropState.activeIdx}`);
        if (posEl) posEl.textContent = `${srcX}, ${srcY}`;
      }
    }
    redrawCropCanvas();
  });

  // Load sync preview if audio_sync data exists
  if (ep.audio_sync && ep.audio_sync.offset_seconds != null) {
    loadSyncPreview(episodeId, ep.audio_sync.offset_seconds);
  }
}

function setCropSpeaker(idx) {
  cropState.activeIdx = idx;
  cropState.speakers.forEach((_, i) => {
    const btn = document.getElementById(`crop-mode-${i}`);
    if (!btn) return;
    const c = SPEAKER_COLORS[i % SPEAKER_COLORS.length];
    btn.className = `flex-1 min-w-[60px] px-3 py-2 rounded-lg text-sm font-medium transition-colors ${i === idx ? c.bg + ' text-white' : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700'}`;
  });
  const wideBtn = document.getElementById('crop-mode-wide');
  if (wideBtn) wideBtn.className = `flex-1 min-w-[60px] px-3 py-2 rounded-lg text-sm font-medium transition-colors ${idx === -1 ? 'bg-zinc-600 text-white' : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700'}`;
}

function setCropWideZoom(value) {
  cropState.wide.zoom = parseFloat(value);
  const valEl = document.getElementById('crop-zoom-wide-val');
  if (valEl) valEl.textContent = cropState.wide.zoom.toFixed(1) + 'x';
  redrawCropCanvas();
}

function setCropZoomN(idx, value) {
  const zoom = parseFloat(value);
  if (cropState.speakers[idx]) cropState.speakers[idx].zoom = zoom;
  const valEl = document.getElementById(`crop-zoom-${idx}-val`);
  if (valEl) valEl.textContent = zoom.toFixed(1) + 'x';
  redrawCropCanvas();
}

// Legacy compat wrappers
function setCropZoom(speaker, value) {
  setCropZoomN(speaker === 'L' ? 0 : 1, value);
}
function setCropMode(mode) {
  setCropSpeaker(mode === 'L' ? 0 : 1);
}

function redrawCropCanvas() {
  const canvas = document.getElementById('crop-canvas');
  if (!canvas || !cropState.image) return;
  const ctx = canvas.getContext('2d');
  const sf = cropState.scaleFactor;
  const srcW = cropState.sourceWidth;
  const srcH = cropState.sourceHeight;

  ctx.drawImage(cropState.image, 0, 0, canvas.width, canvas.height);

  // Draw wide shot crop rect
  // Crop formulas must match lib/crop.py compute_crop() — that is the single source of truth.
  // Wide: crop_w = srcW / zoom. Speaker: crop_w = srcW / (2 * zoom). Short: crop_h = srcH / zoom.
  if (cropState.wide && cropState.wide.zoom > 1.0) {
    const wZoom = cropState.wide.zoom;
    const wCx = cropState.wide.x / sf;
    const wCy = cropState.wide.y / sf;
    const wCropW = Math.round((srcW / wZoom) / sf);
    const wCropH = Math.round(wCropW * 9 / 16);
    const wX = Math.max(0, Math.min(wCx - wCropW / 2, canvas.width - wCropW));
    const wY = Math.max(0, Math.min(wCy - wCropH / 2, canvas.height - wCropH));
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(wX, wY, wCropW, wCropH);
    ctx.fillStyle = 'rgba(255,255,255,0.04)';
    ctx.fillRect(wX, wY, wCropW, wCropH);
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = 'bold 12px system-ui';
    ctx.fillText(`Wide ${wZoom.toFixed(1)}x`, wX + 4, wY + 14);
  }

  cropState.speakers.forEach(function(spk, idx) {
    const color = SPEAKER_COLORS[idx % SPEAKER_COLORS.length].css;
    const cx = spk.x / sf;
    const cy = spk.y / sf;
    const zoom = spk.zoom;

    // 9:16 shorts crop rect
    const shortsCropH = Math.round((srcH / zoom) / sf);
    const shortsCropW = Math.round((shortsCropH * 9 / 16));
    const shortsX = Math.max(0, Math.min(cx - shortsCropW / 2, canvas.width - shortsCropW));
    const shortsY = Math.max(0, Math.min(cy - shortsCropH / 2, canvas.height - shortsCropH));
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([8, 4]);
    ctx.strokeRect(shortsX, shortsY, shortsCropW, shortsCropH);

    // 16:9 longform crop rect (uses longform_zoom, independent of shorts zoom)
    const lfZoom = s.longform_zoom || 0.75;
    const lfCropW = Math.round((srcW / (2 * lfZoom)) / sf);
    const lfCropH = Math.round(lfCropW * 9 / 16);
    const lfX = Math.max(0, Math.min(cx - lfCropW / 2, canvas.width - lfCropW));
    const lfY = Math.max(0, Math.min(cy - lfCropH / 2, canvas.height - lfCropH));
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1.5;
    ctx.strokeRect(lfX, lfY, lfCropW, lfCropH);

    ctx.fillStyle = color.replace('0.8', '0.08');
    ctx.fillRect(lfX, lfY, lfCropW, lfCropH);
    ctx.setLineDash([]);

    // Crosshair
    const crossSize = 12;
    ctx.beginPath();
    ctx.moveTo(cx - crossSize, cy); ctx.lineTo(cx + crossSize, cy);
    ctx.moveTo(cx, cy - crossSize); ctx.lineTo(cx, cy + crossSize);
    ctx.lineWidth = 2; ctx.strokeStyle = color; ctx.stroke();

    // Label
    ctx.fillStyle = color;
    ctx.font = 'bold 14px system-ui';
    ctx.fillText(`${idx} ${zoom.toFixed(1)}x`, cx + crossSize + 4, cy - crossSize + 4);

    // Highlight active speaker
    if (idx === cropState.activeIdx) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 3;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(cx, cy, crossSize + 4, 0, 2 * Math.PI);
      ctx.stroke();
    }
  });
}

async function saveCropConfig(episodeId) {
  const btn = document.getElementById('crop-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

  // Collect ambient tracks from mixer (some tracks like Mix/Mic have no trackNumber)
  const ambientTracks = [];
  for (const t of mixerState.tracks) {
    if (t.assignment === 'ambient') {
      const entry = { volume: t.volume };
      if (t.trackNumber) entry.track_number = t.trackNumber;
      if (t.stem) entry.stem = t.stem;
      ambientTracks.push(entry);
    }
  }

  try {
    await api(`/episodes/${episodeId}/crop-config`, {
      method: 'POST',
      body: JSON.stringify({
        speakers: cropState.speakers.map(s => {
          let volume = 1.0;
          if (mixerState.tracks.length > 0 && s.track) {
            const mt = mixerState.tracks.find(t => t.trackNumber === s.track);
            if (mt) volume = mt.volume;
          }
          return {
            label: s.label,
            center_x: s.x,
            center_y: s.y,
            zoom: s.zoom,
            longform_zoom: s.longform_zoom || 0.75,
            track: s.track,
            volume: volume,
          };
        }),
        ambient_tracks: ambientTracks.length > 0 ? ambientTracks : undefined,
        wide_center_x: cropState.wide ? cropState.wide.x : undefined,
        wide_center_y: cropState.wide ? cropState.wide.y : undefined,
        wide_zoom: cropState.wide ? cropState.wide.zoom : undefined,
      }),
    });

    if (mixerState.playing) stopMixerPlayback();
    try { await api(`/episodes/${episodeId}/resume-pipeline`, { method: 'POST' }); } catch {}
    showToast('Crop config saved. Pipeline resuming.', 'success');
    window.location.hash = `#/episodes/${episodeId}`;
  } catch (err) {
    showToast('Failed to save crop config: ' + err.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Save & Continue'; }
  }
}

// ════════════════════════════════════════════════════════════════════
//  AUDIO TAB (dedicated tab with sync, mixer, volume controls)
// ════════════════════════════════════════════════════════════════════

function renderAudioTab(episodeId, ep) {
  const panel = document.getElementById('tab-audio');
  if (!panel) return;

  const audioTracks = ep.audio_tracks || [];
  const hasH6E = audioTracks.length > 0;
  const hasSync = ep.audio_sync && ep.audio_sync.offset_seconds != null;
  const mixCfg = ep.audio_mix || { tracks: [], master_volume: 1.0 };
  const mixMap = {};
  for (const t of (mixCfg.tracks || [])) mixMap[t.stem] = t.volume;
  const cropSpeakers = (ep.crop_config || {}).speakers || [];
  const ambientTracks = (ep.crop_config || {}).ambient_tracks || [];
  const cutCfg = ep.speaker_cut_config || {};
  const speechMargin = cutCfg.speech_db_margin || 12;
  const minSegment = cutCfg.min_segment_seconds || 2.0;
  const masterPct = Math.round((mixCfg.master_volume || 1.0) * 100);
  const hasMix = (mixCfg.tracks || []).length > 0;

  // Initialize unified mixer state from ALL audio tracks
  if (audioTracks.length > 0 && mixerState.tracks.length === 0) {
    mixerState.tracks = audioTracks.map(t => {
      const stem = t.filename.replace(/\.[^.]+$/, '');
      const isInput = t.track_type === 'input';
      let defaultVol = isInput ? 1.0 : 0.2;
      let assignment = '';
      for (let si = 0; si < cropSpeakers.length; si++) {
        if (cropSpeakers[si].track === t.track_number) {
          defaultVol = cropSpeakers[si].volume || 1.0;
          assignment = `speaker-${si}`;
          break;
        }
      }
      for (const amb of ambientTracks) {
        if (amb.track_number === t.track_number) {
          defaultVol = amb.volume || 0.2;
          assignment = 'ambient';
        }
      }
      return {
        stem,
        label: isInput ? `Tr${t.track_number}` : (t.track_type === 'stereo_mix' ? 'LR' : 'Mic'),
        trackNumber: t.track_number || null,
        trackType: t.track_type,
        volume: stem in mixMap ? mixMap[stem] : defaultVol,
        muted: false,
        soloed: false,
        buffer: null,
        source: null,
        gain: null,
        analyser: null,
        assignment,
      };
    });
  }
  // Sync volumes from mixMap if tracks already exist
  if (mixerState.tracks.length > 0 && Object.keys(mixMap).length > 0) {
    for (const t of mixerState.tracks) {
      if (t.stem in mixMap) t.volume = mixMap[t.stem];
    }
  }

  // Build the speaker assignment label for each track
  function getAssignmentLabel(t) {
    for (let i = 0; i < cropSpeakers.length; i++) {
      if (cropSpeakers[i].track === t.trackNumber) return cropSpeakers[i].label || `Speaker ${i}`;
    }
    for (const amb of ambientTracks) {
      if (amb.track_number === t.trackNumber) return 'Ambient';
    }
    if (t.trackType === 'stereo_mix') return 'Stereo Mix';
    if (t.trackType === 'builtin_mic') return 'XY Mic';
    return '';
  }

  panel.innerHTML = `
    <div class="space-y-6">

      ${!hasH6E ? `
      <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-6 text-center">
        <p class="text-zinc-400 text-sm">No external audio tracks detected.</p>
        <p class="text-zinc-600 text-xs mt-1">Camera audio is used directly. External audio controls are available when recording with a Zoom H6E or similar.</p>
      </div>` : ''}

      ${hasSync ? `
      <!-- Audio Sync -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-sm font-semibold text-zinc-300">Audio Sync</h3>
          <div class="flex items-center gap-2">
            <span class="text-xs text-zinc-500">Auto: ${ep.audio_sync.offset_seconds.toFixed(4)}s</span>
            ${ep.audio_sync.confidence ? `<span class="text-xs ${parseFloat(ep.audio_sync.confidence) > 0.3 ? 'text-green-500' : 'text-amber-500'}">conf: ${parseFloat(ep.audio_sync.confidence).toFixed(3)}</span>` : ''}
            ${ep.audio_sync.drift_rate_ppm ? `<span class="text-xs text-zinc-500">drift: ${ep.audio_sync.drift_rate_ppm.toFixed(1)} ppm</span>` : ''}
            <span id="sync-status" class="text-xs text-zinc-600"></span>
          </div>
        </div>

        <div class="mb-3">
          <video id="sync-video" width="100%" style="max-height:240px;" class="rounded-lg bg-black" preload="metadata" controls>
            <source src="${API}/episodes/${episodeId}/video-preview" type="video/mp4">
          </video>
        </div>

        <div class="mb-1 flex items-center gap-4">
          <div class="flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(251,146,60,0.8);"></span><span class="text-xs text-zinc-400">Camera</span></div>
          <div class="flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(96,165,250,0.8);"></span><span class="text-xs text-zinc-400">H6E (shifted by offset)</span></div>
          <span class="text-xs text-zinc-600 ml-auto">Scroll to zoom, drag to pan, click to seek</span>
        </div>
        <canvas id="sync-waveform" class="w-full rounded cursor-crosshair" style="height:140px;background:#0a0a0a;"></canvas>
        <div class="flex items-center justify-between mt-1 mb-3">
          <span id="sync-view-range" class="text-xs text-zinc-600 font-mono"></span>
          <button onclick="syncZoomToFit()" class="text-xs text-zinc-500 hover:text-zinc-300 transition-colors">Zoom to fit</button>
        </div>

        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-xs text-zinc-500">Offset:</span>
          <button onclick="adjustSyncOffset(-10)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">-10</button>
          <button onclick="adjustSyncOffset(-5)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">-5</button>
          <button onclick="adjustSyncOffset(-1)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">-1</button>
          <button onclick="adjustSyncOffset(-0.1)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">-0.1</button>
          <span id="sync-offset-display" class="text-sm font-mono text-white px-2 py-1 bg-zinc-800 rounded min-w-[100px] text-center">${ep.audio_sync.offset_seconds.toFixed(2)}s</span>
          <button onclick="adjustSyncOffset(0.1)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">+0.1</button>
          <button onclick="adjustSyncOffset(1)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">+1</button>
          <button onclick="adjustSyncOffset(5)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">+5</button>
          <button onclick="adjustSyncOffset(10)" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors font-mono">+10</button>
          <button onclick="saveSyncOffset('${episodeId}')" id="sync-save-btn" class="px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 rounded font-medium transition-colors">Save Offset</button>
          <button onclick="resetSyncOffset()" class="px-3 py-1.5 text-xs bg-zinc-700 hover:bg-zinc-600 rounded font-medium transition-colors text-zinc-300">Reset</button>
        </div>
      </div>` : ''}

      ${hasH6E ? `
      <!-- Unified Audio Mixer -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-sm font-semibold text-zinc-300">Audio Mixer</h3>
          <div class="flex items-center gap-2">
            <span class="text-xs text-zinc-500">Master:</span>
            <input type="range" id="mix-master-vol" min="0" max="200" value="${masterPct}"
              oninput="document.getElementById('mix-master-label').textContent=this.value+'%'"
              class="w-20 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
            <span id="mix-master-label" class="text-xs text-zinc-400 font-mono w-8">${masterPct}%</span>
          </div>
        </div>

        <!-- Track rows -->
        <div class="space-y-1.5" id="mixer-tracks">
          ${mixerState.tracks.map((t, i) => {
            const assignLabel = getAssignmentLabel(t);
            const pct = Math.round(t.volume * 100);
            return `
          <div class="flex items-center gap-2 py-0.5">
            <div class="w-8 text-xs font-mono ${t.trackType === 'input' ? 'text-zinc-300' : 'text-zinc-500'} truncate" title="${escapeHtml(t.stem)}">${escapeHtml(t.label)}</div>
            <span class="w-16 text-xs ${assignLabel ? 'text-zinc-400' : 'text-zinc-600'} truncate">${escapeHtml(assignLabel || '\u2014')}</span>
            <button onclick="toggleTrackSolo(${i})" id="mixer-solo-${i}" class="w-6 h-6 text-[10px] font-bold rounded ${t.soloed ? 'bg-yellow-600 text-white' : 'bg-zinc-800 text-zinc-500 hover:text-yellow-400'} transition-colors" title="Solo">S</button>
            <button onclick="toggleTrackMute(${i})" id="mixer-mute-${i}" class="w-6 h-6 text-[10px] font-bold rounded ${t.muted ? 'bg-red-600 text-white' : 'bg-zinc-800 text-zinc-500 hover:text-red-400'} transition-colors" title="Mute">M</button>
            <canvas id="mixer-meter-${i}" width="200" height="16" class="rounded" style="background:#111;min-width:80px;height:16px;flex:2;"></canvas>
            <input type="range" id="mixer-vol-${i}" data-mix-stem="${escapeHtml(t.stem)}" min="0" max="300" value="${pct}"
              oninput="setMixerTrackVolume(${i},this.value)"
              class="w-20 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
            <span id="mixer-vol-label-${i}" class="text-xs text-zinc-400 w-10 text-right font-mono">${pct}%</span>
          </div>`;
          }).join('')}
        </div>

        <!-- Playback controls -->
        <div class="flex items-center gap-3 mt-3 pt-3 border-t border-zinc-800">
          <div class="flex items-center gap-2">
            <button id="mixer-load-btn" onclick="loadAudioPreviews('${episodeId}')" class="px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 rounded font-medium transition-colors">Load Audio</button>
            <button id="mixer-play-btn" onclick="toggleMixerPlayback()" disabled class="px-3 py-1.5 text-xs bg-zinc-700 rounded font-medium text-zinc-500 transition-colors">&#9654; Play</button>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-xs text-zinc-500"><span id="mixer-range">${fmtMixerTime(mixerState.previewStart)} \u2013 ${fmtMixerTime(mixerState.previewStart + mixerState.previewDuration)}</span></span>
            <button onclick="shiftMixerWindow(-30,'${episodeId}')" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors">&larr; 30s</button>
            <button onclick="shiftMixerWindow(30,'${episodeId}')" class="px-2 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded transition-colors">30s &rarr;</button>
          </div>
          <span id="mixer-status" class="text-xs text-zinc-600 ml-auto">${mixerState.loaded ? 'Ready' : 'Click "Load Audio" to preview'}</span>
        </div>

        <!-- Apply / Re-render -->
        <div class="flex items-center gap-2 mt-3 pt-3 border-t border-zinc-800">
          <button onclick="applyAudioMixFromMixer('${episodeId}')" id="mix-apply-btn" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">
            ${hasMix ? 'Update Mix' : 'Apply Mix'}
          </button>
          <button onclick="rerenderAll('${episodeId}')" id="mix-rerender-btn" class="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-xs font-medium transition-colors">
            Re-render All
          </button>
          <span id="mix-status" class="text-xs text-zinc-500">${hasMix ? 'Mix applied' : 'Set volumes, then Apply Mix to save for rendering'}</span>
        </div>
      </div>

      <!-- Speaker Cut Sensitivity -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <h3 class="text-sm font-semibold text-zinc-300 mb-3">Speaker Cut Sensitivity</h3>
        <div class="grid grid-cols-3 gap-4">
          <div>
            <label class="text-xs text-zinc-500 block mb-1">Speech Margin (dB)</label>
            <div class="flex items-center gap-1">
              <input type="range" id="cut-speech-margin" min="3" max="24" step="1" value="${speechMargin}"
                oninput="document.getElementById('cut-speech-margin-val').textContent=this.value"
                class="flex-1 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
              <span id="cut-speech-margin-val" class="text-xs text-zinc-400 font-mono w-6">${speechMargin}</span>
            </div>
            <p class="text-xs text-zinc-600 mt-0.5">Lower = more sensitive</p>
          </div>
          <div>
            <label class="text-xs text-zinc-500 block mb-1">Min Segment (s)</label>
            <div class="flex items-center gap-1">
              <input type="range" id="cut-min-segment" min="0.3" max="5" step="0.1" value="${minSegment}"
                oninput="document.getElementById('cut-min-segment-val').textContent=parseFloat(this.value).toFixed(1)"
                class="flex-1 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
              <span id="cut-min-segment-val" class="text-xs text-zinc-400 font-mono w-6">${minSegment.toFixed ? minSegment.toFixed(1) : minSegment}</span>
            </div>
            <p class="text-xs text-zinc-600 mt-0.5">Shorter = more cuts</p>
          </div>
        </div>
        <div class="flex items-center gap-2 mt-3 pt-3 border-t border-zinc-800">
          <button onclick="reanalyzeSpeakers('${episodeId}')" class="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-xs font-medium transition-colors">
            Re-analyze Speakers
          </button>
          <span class="text-xs text-zinc-600">Re-runs speaker cut + renders with new sensitivity settings</span>
        </div>
      </div>
      ` : ''}
    </div>`;

  // Load sync waveforms if on the Audio tab and we have sync data
  if (hasSync && state.activeTab === 'audio') {
    loadSyncPreview(episodeId, ep.audio_sync.offset_seconds);
  }
}

async function applyAudioMixFromMixer(episodeId) {
  const btn = document.getElementById('mix-apply-btn');
  const status = document.getElementById('mix-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }
  if (status) status.textContent = 'Generating audio mix...';

  // Collect volumes from the unified mixer state
  const tracks = mixerState.tracks.map(t => ({
    stem: t.stem,
    volume: t.volume,
  }));

  const masterEl = document.getElementById('mix-master-vol');
  const masterVolume = masterEl ? parseInt(masterEl.value) / 100 : 1.0;

  try {
    const result = await api(`/episodes/${episodeId}/audio-mix`, {
      method: 'POST',
      body: JSON.stringify({ tracks, master_volume: masterVolume }),
    });
    if (status) status.textContent = `Mix generated (${result.size_mb} MB)`;
    showToast('Audio mix generated. Click "Re-render All" to apply to video.', 'success');
  } catch (err) {
    if (status) status.textContent = 'Failed: ' + err.message;
    showToast('Audio mix failed: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Update Mix'; }
  }
}

// ════════════════════════════════════════════════════════════════════
//  AUDIO MIX PANEL (legacy collapsible — kept for crop setup page)
// ════════════════════════════════════════════════════════════════════

function renderAudioMixPanel(episodeId, ep) {
  const audioTracks = ep.audio_tracks || [];
  if (audioTracks.length === 0) return '';

  const mixCfg = ep.audio_mix || { tracks: [], master_volume: 1.0 };
  const mixMap = {};
  for (const t of mixCfg.tracks) mixMap[t.stem] = t.volume;
  const cropSpeakers = (ep.crop_config || {}).speakers || [];
  const ambientTracks = (ep.crop_config || {}).ambient_tracks || [];

  // Speaker cut config (sensitivity params)
  const cutCfg = ep.speaker_cut_config || {};
  const speechMargin = cutCfg.speech_db_margin || 12;
  const minSegment = cutCfg.min_segment_seconds || 2.0;

  const rows = audioTracks.map(t => {
    const stem = t.filename.replace(/\.[^.]+$/, '');
    const isInput = t.track_type === 'input';

    // Determine default volume
    let defaultVol = isInput ? 1.0 : 0.2;
    // Check crop_config for speaker/ambient assignment
    for (const spk of cropSpeakers) {
      if (spk.track === t.track_number) defaultVol = spk.volume || 1.0;
    }
    for (const amb of ambientTracks) {
      if (amb.track_number === t.track_number) defaultVol = amb.volume || 0.2;
    }

    const vol = stem in mixMap ? mixMap[stem] : defaultVol;
    const pct = Math.round(vol * 100);

    // Find speaker assignment
    let assignment = '';
    for (let i = 0; i < cropSpeakers.length; i++) {
      if (cropSpeakers[i].track === t.track_number) {
        assignment = cropSpeakers[i].label || `Speaker ${i}`;
        break;
      }
    }
    for (const amb of ambientTracks) {
      if (amb.track_number === t.track_number) assignment = 'Ambient';
    }

    return `
      <div class="flex items-center gap-2 py-0.5">
        <div class="w-14 text-xs font-mono ${isInput ? 'text-zinc-300' : 'text-zinc-500'} truncate" title="${escapeHtml(t.filename)}">${escapeHtml(stem.replace('audio_', ''))}</div>
        <span class="w-16 text-xs ${assignment ? 'text-zinc-400' : 'text-zinc-600'} truncate">${escapeHtml(assignment || '—')}</span>
        <input type="range" data-mix-stem="${escapeHtml(stem)}" min="0" max="300" value="${pct}"
          oninput="updateMixSliderLabel(this)"
          class="flex-1 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
        <span class="mix-vol-label text-xs text-zinc-400 w-10 text-right font-mono">${pct}%</span>
      </div>`;
  }).join('');

  const masterPct = Math.round((mixCfg.master_volume || 1.0) * 100);
  const hasMix = (ep_dir_check => {
    // We can't check file existence from frontend, but if audio_mix config exists, it was generated
    return mixCfg.tracks.length > 0;
  })();

  return `
    <div class="mb-6">
      <button onclick="toggleMixPanel()" class="w-full flex items-center justify-between bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-3 hover:bg-zinc-800/80 transition-colors">
        <div class="flex items-center gap-2">
          <svg class="w-4 h-4 text-zinc-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072M12 6v12m-3.536-8.464a5 5 0 000 7.072M18.364 5.636a9 9 0 010 12.728M5.636 5.636a9 9 0 000 12.728"/></svg>
          <span class="text-sm font-semibold text-zinc-300">Audio Mix & Speaker Settings</span>
        </div>
        <div class="flex items-center gap-2">
          ${hasMix ? '<span class="text-xs text-green-500">Mix applied</span>' : '<span class="text-xs text-zinc-600">No mix generated</span>'}
          <svg id="mix-panel-chevron" class="w-4 h-4 text-zinc-500 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
        </div>
      </button>

      <div id="mix-panel-body" class="hidden bg-zinc-900 border border-zinc-800 border-t-0 rounded-b-lg p-4">
        <!-- Track volumes -->
        <div class="mb-4">
          <div class="flex items-center justify-between mb-2">
            <h4 class="text-xs font-semibold text-zinc-400 uppercase tracking-wide">Track Volumes</h4>
            <div class="flex items-center gap-2">
              <span class="text-xs text-zinc-500">Master:</span>
              <input type="range" id="mix-master-vol" min="0" max="200" value="${masterPct}"
                oninput="document.getElementById('mix-master-label').textContent=this.value+'%'"
                class="w-16 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
              <span id="mix-master-label" class="text-xs text-zinc-400 font-mono w-8">${masterPct}%</span>
            </div>
          </div>
          <div class="space-y-0.5">${rows}</div>
          <p class="text-xs text-zinc-600 mt-2">Set volume levels for each audio track. These are applied to ALL renders (longform + shorts).</p>
        </div>

        <!-- Action buttons -->
        <div class="flex items-center gap-2 mb-4">
          <button onclick="applyAudioMix('${episodeId}')" id="mix-apply-btn" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-xs font-medium transition-colors">
            Apply Mix
          </button>
          <button onclick="rerenderAll('${episodeId}')" id="mix-rerender-btn" class="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-xs font-medium transition-colors">
            Re-render All
          </button>
          <span id="mix-status" class="text-xs text-zinc-500"></span>
        </div>

        <!-- Speaker cut sensitivity -->
        <div class="border-t border-zinc-800 pt-3">
          <h4 class="text-xs font-semibold text-zinc-400 uppercase tracking-wide mb-2">Speaker Cut Sensitivity</h4>
          <div class="grid grid-cols-3 gap-3">
            <div>
              <label class="text-xs text-zinc-500 block mb-1">Speech Margin (dB)</label>
              <div class="flex items-center gap-1">
                <input type="range" id="cut-speech-margin" min="3" max="24" step="1" value="${speechMargin}"
                  oninput="document.getElementById('cut-speech-margin-val').textContent=this.value"
                  class="flex-1 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
                <span id="cut-speech-margin-val" class="text-xs text-zinc-400 font-mono w-6">${speechMargin}</span>
              </div>
            </div>
            <div>
              <label class="text-xs text-zinc-500 block mb-1">Min Segment (s)</label>
              <div class="flex items-center gap-1">
                <input type="range" id="cut-min-segment" min="0.3" max="5" step="0.1" value="${minSegment}"
                  oninput="document.getElementById('cut-min-segment-val').textContent=parseFloat(this.value).toFixed(1)"
                  class="flex-1 h-1 bg-zinc-700 rounded-lg appearance-none cursor-pointer">
                <span id="cut-min-segment-val" class="text-xs text-zinc-400 font-mono w-6">${minSegment.toFixed ? minSegment.toFixed(1) : minSegment}</span>
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2 mt-2">
            <button onclick="reanalyzeSpeakers('${episodeId}')" class="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 rounded text-xs font-medium transition-colors">
              Re-analyze Speakers
            </button>
            <span class="text-xs text-zinc-600">Lower margin = more sensitive, shorter segments = more frequent cuts</span>
          </div>
        </div>
      </div>
    </div>`;
}

function toggleMixPanel() {
  const body = document.getElementById('mix-panel-body');
  const chevron = document.getElementById('mix-panel-chevron');
  if (!body) return;
  body.classList.toggle('hidden');
  if (chevron) chevron.style.transform = body.classList.contains('hidden') ? '' : 'rotate(180deg)';
}

function updateMixSliderLabel(slider) {
  const label = slider.nextElementSibling;
  if (label) label.textContent = slider.value + '%';
}

async function applyAudioMix(episodeId) {
  const btn = document.getElementById('mix-apply-btn');
  const status = document.getElementById('mix-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }
  if (status) status.textContent = 'Generating audio mix...';

  // Collect track volumes from sliders
  const tracks = [];
  document.querySelectorAll('[data-mix-stem]').forEach(slider => {
    tracks.push({
      stem: slider.dataset.mixStem,
      volume: parseInt(slider.value) / 100,
    });
  });

  const masterEl = document.getElementById('mix-master-vol');
  const masterVolume = masterEl ? parseInt(masterEl.value) / 100 : 1.0;

  try {
    const result = await api(`/episodes/${episodeId}/audio-mix`, {
      method: 'POST',
      body: JSON.stringify({ tracks, master_volume: masterVolume }),
    });
    if (status) status.textContent = `Mix generated (${result.size_mb} MB)`;
    showToast('Audio mix generated. Click "Re-render All" to apply.', 'success');
  } catch (err) {
    if (status) status.textContent = 'Failed: ' + err.message;
    showToast('Audio mix failed: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Apply Mix'; }
  }
}

async function rerenderAll(episodeId) {
  const btn = document.getElementById('mix-rerender-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }

  try {
    await api(`/episodes/${episodeId}/run-pipeline`, {
      method: 'POST',
      body: JSON.stringify({ agents: ['longform_render', 'shorts_render'] }),
    });
    showToast('Re-rendering longform + shorts with new audio mix...', 'success');
    // Start polling pipeline status
    checkAndShowPipeline(episodeId);
  } catch (err) {
    showToast('Re-render failed: ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Re-render All'; }
  }
}

async function reanalyzeSpeakers(episodeId) {
  // Save sensitivity config first
  const speechMargin = parseFloat(document.getElementById('cut-speech-margin')?.value || 12);
  const minSegment = parseFloat(document.getElementById('cut-min-segment')?.value || 2.0);

  try {
    await api(`/episodes/${episodeId}/speaker-cut-config`, {
      method: 'POST',
      body: JSON.stringify({
        speech_db_margin: speechMargin,
        min_segment_seconds: minSegment,
      }),
    });

    // Re-run speaker_cut + render agents
    await api(`/episodes/${episodeId}/run-pipeline`, {
      method: 'POST',
      body: JSON.stringify({ agents: ['speaker_cut', 'longform_render', 'shorts_render'] }),
    });
    showToast('Re-analyzing speakers and re-rendering...', 'success');
    checkAndShowPipeline(episodeId);
  } catch (err) {
    showToast('Re-analysis failed: ' + err.message, 'error');
  }
}

// ════════════════════════════════════════════════════════════════════
//  AUDIO SYNC VERIFICATION FUNCTIONS
// ════════════════════════════════════════════════════════════════════

async function loadSyncPreview(episodeId, currentOffset) {
  syncState.episodeId = episodeId;
  syncState.offset = currentOffset;
  syncState.autoOffset = currentOffset;

  const statusEl = document.getElementById('sync-status');

  try {
    const data = await api(`/episodes/${episodeId}/sync-preview?duration=120`);
    syncState.cameraWaveform = data.camera_waveform;
    syncState.h6eWaveform = data.h6e_waveform;
    syncState.offset = data.offset_seconds;
    syncState.duration = data.duration;
    syncState.pps = data.peaks_per_second;
    syncState.loaded = true;
    syncState.viewStart = 0;
    syncState.viewEnd = Math.min(30, data.duration);
    if (syncState.autoOffset === null) syncState.autoOffset = data.offset_seconds;

    if (statusEl) statusEl.textContent = 'Loaded. Scroll to zoom, drag to pan.';

    // Update the offset input with the loaded value
    const input = document.getElementById('sync-offset-input');
    if (input) input.value = syncState.offset.toFixed(2);

    initSyncCanvas();
    drawSyncWaveform();
    initSyncPlayhead();

    // Init H6E audio playback for sync verification
    const ep = state.currentEpisode;
    if (ep && ep.audio_tracks) {
      initSyncAudio(episodeId, ep.audio_tracks);
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = 'Failed to load: ' + err.message;
  }
}

function initSyncCanvas() {
  const canvas = document.getElementById('sync-waveform');
  if (!canvas) return;

  // Mouse wheel = zoom
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mouseX = (e.clientX - rect.left) / rect.width; // 0-1 position
    const viewSpan = syncState.viewEnd - syncState.viewStart;
    const zoomFactor = e.deltaY > 0 ? 1.3 : 0.7;
    const newSpan = Math.max(1, Math.min(syncState.duration, viewSpan * zoomFactor));

    // Zoom centered on mouse position
    const mouseTime = syncState.viewStart + mouseX * viewSpan;
    syncState.viewStart = Math.max(0, mouseTime - mouseX * newSpan);
    syncState.viewEnd = Math.min(syncState.duration, syncState.viewStart + newSpan);
    if (syncState.viewStart < 0) { syncState.viewEnd -= syncState.viewStart; syncState.viewStart = 0; }

    drawSyncWaveform();
  }, { passive: false });

  // Drag to pan
  canvas.addEventListener('mousedown', (e) => {
    syncState.isDragging = true;
    syncState.dragStartX = e.clientX;
    syncState.dragStartViewStart = syncState.viewStart;
  });
  window.addEventListener('mousemove', (e) => {
    if (!syncState.isDragging) return;
    const canvas = document.getElementById('sync-waveform');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dx = e.clientX - syncState.dragStartX;
    const viewSpan = syncState.viewEnd - syncState.viewStart;
    const timeDelta = -(dx / rect.width) * viewSpan;
    let newStart = syncState.dragStartViewStart + timeDelta;
    newStart = Math.max(0, Math.min(syncState.duration - viewSpan, newStart));
    syncState.viewStart = newStart;
    syncState.viewEnd = newStart + viewSpan;
    drawSyncWaveform();
  });
  window.addEventListener('mouseup', () => { syncState.isDragging = false; });

  // Click to seek video
  canvas.addEventListener('click', (e) => {
    if (Math.abs(e.clientX - syncState.dragStartX) > 5) return; // was a drag
    const rect = canvas.getBoundingClientRect();
    const mouseX = (e.clientX - rect.left) / rect.width;
    const clickTime = syncState.viewStart + mouseX * (syncState.viewEnd - syncState.viewStart);
    const video = syncState.videoElement;
    if (video) video.currentTime = Math.max(0, clickTime);
  });
}

function drawSyncWaveform() {
  const canvas = document.getElementById('sync-waveform');
  if (!canvas || !syncState.loaded) return;

  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;

  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const midY = h / 2;
  const pps = syncState.pps;
  const viewStart = syncState.viewStart;
  const viewEnd = syncState.viewEnd;
  const viewSpan = viewEnd - viewStart;

  // Background
  ctx.fillStyle = '#0a0a0a';
  ctx.fillRect(0, 0, w, h);

  // Center line
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, midY);
  ctx.lineTo(w, midY);
  ctx.stroke();

  // Time grid — adaptive spacing
  const gridIntervals = [0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60];
  let gridStep = gridIntervals.find(s => (viewSpan / s) <= 30) || 60;
  ctx.fillStyle = 'rgba(255,255,255,0.12)';
  ctx.font = `${10 * dpr}px system-ui`;
  const gridStart = Math.ceil(viewStart / gridStep) * gridStep;
  for (let t = gridStart; t <= viewEnd; t += gridStep) {
    const x = ((t - viewStart) / viewSpan) * w;
    ctx.fillRect(x, 0, 1, h);
    ctx.fillStyle = 'rgba(255,255,255,0.35)';
    const label = gridStep < 1 ? t.toFixed(2) + 's' : gridStep < 10 ? t.toFixed(1) + 's' : Math.round(t) + 's';
    ctx.fillText(label, x + 3 * dpr, h - 4 * dpr);
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
  }

  // Draw camera waveform (orange, top half)
  _drawWaveformHalf(ctx, syncState.cameraWaveform, 'rgba(251,146,60,0.8)', w, h, 0, pps, viewStart, viewEnd, true);

  // Draw H6E waveform (blue, bottom half) — shifted by offset
  _drawWaveformHalf(ctx, syncState.h6eWaveform, 'rgba(96,165,250,0.8)', w, h, syncState.offset, pps, viewStart, viewEnd, false);

  // Video playhead
  const video = syncState.videoElement;
  if (video && video.currentTime >= viewStart && video.currentTime <= viewEnd) {
    const x = ((video.currentTime - viewStart) / viewSpan) * w;
    ctx.strokeStyle = 'rgba(255,255,255,0.9)';
    ctx.lineWidth = 2 * dpr;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }

  // View range label
  const rangeEl = document.getElementById('sync-view-range');
  if (rangeEl) rangeEl.textContent = `${viewStart.toFixed(2)}s — ${viewEnd.toFixed(2)}s (${viewSpan.toFixed(1)}s visible)`;
}

function _drawWaveformHalf(ctx, waveform, color, w, h, offsetSeconds, pps, viewStart, viewEnd, isTop) {
  if (!waveform.length) return;
  const viewSpan = viewEnd - viewStart;
  const midY = h / 2;
  const halfH = h * 0.45;

  ctx.fillStyle = color;
  for (let px = 0; px < w; px++) {
    const time = viewStart + (px / w) * viewSpan;
    // For H6E: time in camera space → h6e sample = (time + offset) * pps
    const sampleTime = time + offsetSeconds;
    const sampleIdx = Math.floor(sampleTime * pps);
    if (sampleIdx < 0 || sampleIdx >= waveform.length) continue;

    const amp = waveform[sampleIdx];
    const barH = Math.max(0.5, amp * halfH);

    if (isTop) {
      ctx.fillRect(px, midY - barH, 1, barH);
    } else {
      ctx.fillRect(px, midY, 1, barH);
    }
  }
}

function syncZoomToFit() {
  syncState.viewStart = 0;
  syncState.viewEnd = syncState.duration;
  drawSyncWaveform();
}

function initSyncPlayhead() {
  const video = document.getElementById('sync-video');
  if (!video) return;
  syncState.videoElement = video;
  video.currentTime = 2;

  function animate() {
    drawSyncWaveform();
    syncState.animFrame = requestAnimationFrame(animate);
  }
  if (syncState.animFrame) cancelAnimationFrame(syncState.animFrame);
  animate();
}

function adjustSyncOffset(delta) {
  setSyncOffset(syncState.offset + delta);
}

function setSyncOffset(value) {
  syncState.offset = Math.round(value * 100) / 100;
  const input = document.getElementById('sync-offset-input');
  if (input) input.value = syncState.offset.toFixed(2);
  if (syncState.loaded) drawSyncWaveform();
  syncH6EPlayback();
}

async function saveSyncOffset(episodeId) {
  const btn = document.getElementById('sync-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }

  try {
    await api(`/episodes/${episodeId}/sync-offset`, {
      method: 'POST',
      body: JSON.stringify({ offset_seconds: syncState.offset }),
    });
    showToast(`Offset saved: ${syncState.offset.toFixed(2)}s`, 'success');
  } catch (err) {
    showToast('Failed to save: ' + err.message, 'error');
  }

  if (btn) { btn.disabled = false; btn.textContent = 'Save Offset'; }
}

function toggleSyncMute(which) {
  const video = document.getElementById('sync-video');
  const h6eAudio = document.getElementById('sync-h6e-audio');
  if (which === 'camera' && video) {
    video.muted = !video.muted;
    const btn = document.getElementById('sync-mute-cam');
    if (btn) {
      btn.textContent = video.muted ? 'Camera OFF' : 'Camera ON';
      btn.className = `px-2 py-1 text-xs rounded font-medium ${video.muted ? 'bg-zinc-700 text-zinc-400' : 'bg-orange-800 text-orange-200'}`;
    }
  }
  if (which === 'h6e' && h6eAudio) {
    h6eAudio.muted = !h6eAudio.muted;
    const btn = document.getElementById('sync-mute-h6e');
    if (btn) {
      btn.textContent = h6eAudio.muted ? 'H6E OFF' : 'H6E ON';
      btn.className = `px-2 py-1 text-xs rounded font-medium ${h6eAudio.muted ? 'bg-zinc-700 text-zinc-400' : 'bg-blue-800 text-blue-200'}`;
    }
  }
}

function syncH6EPlayback() {
  const video = document.getElementById('sync-video');
  const h6eAudio = document.getElementById('sync-h6e-audio');
  if (!video || !h6eAudio || !h6eAudio.src) return;
  h6eAudio.currentTime = video.currentTime + syncState.offset;
}

function initSyncAudio(episodeId, audioTracks) {
  // Find the stereo mix or builtin mic track for sync preview
  let syncTrack = audioTracks.find(t => t.track_type === 'stereo_mix')
    || audioTracks.find(t => t.track_type === 'builtin_mic')
    || audioTracks.find(t => t.track_type === 'input');
  if (!syncTrack) return;

  const stem = syncTrack.filename.replace(/\.(WAV|wav)$/, '');
  const h6eAudio = document.getElementById('sync-h6e-audio');
  const video = document.getElementById('sync-video');
  if (!h6eAudio || !video) return;

  // Load H6E audio preview (first 30 minutes — enough to verify drift)
  h6eAudio.src = `${API}/episodes/${episodeId}/audio-preview/${stem}?start=0&duration=1800`;

  // Sync H6E playback with video
  video.addEventListener('play', () => {
    syncH6EPlayback();
    h6eAudio.play().catch(() => {});
  });
  video.addEventListener('pause', () => h6eAudio.pause());
  video.addEventListener('seeked', () => syncH6EPlayback());
  video.addEventListener('timeupdate', () => {
    // Correct drift every second
    const expected = video.currentTime + syncState.offset;
    if (Math.abs(h6eAudio.currentTime - expected) > 0.3) {
      h6eAudio.currentTime = expected;
    }
  });

  // Scroll on offset input to adjust
  const input = document.getElementById('sync-offset-input');
  if (input) {
    input.addEventListener('wheel', (e) => {
      e.preventDefault();
      const step = e.shiftKey ? 1.0 : 0.1;
      setSyncOffset(syncState.offset + (e.deltaY > 0 ? -step : step));
    }, { passive: false });
  }
}

function resetSyncOffset() {
  if (syncState.autoOffset !== null) syncState.offset = syncState.autoOffset;

  const display = document.getElementById('sync-offset-display');
  if (display) display.textContent = syncState.offset.toFixed(4) + 's';

  if (syncState.loaded) drawSyncWaveform();
}

// ════════════════════════════════════════════════════════════════════
//  AUDIO MIXER FUNCTIONS
// ════════════════════════════════════════════════════════════════════

function fmtMixerTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

async function loadAudioPreviews(episodeId) {
  const btn = document.getElementById('mixer-load-btn');
  const status = document.getElementById('mixer-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Loading...'; }
  if (status) status.textContent = 'Generating audio previews...';

  if (!mixerState.audioCtx) {
    mixerState.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (mixerState.audioCtx.state === 'suspended') mixerState.audioCtx.resume();

  try {
    const promises = mixerState.tracks.map(async (track) => {
      const url = `${API}/episodes/${episodeId}/audio-preview/${track.stem}?start=${mixerState.previewStart}&duration=${mixerState.previewDuration}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`${track.label}: HTTP ${resp.status}`);
      const buf = await resp.arrayBuffer();
      track.buffer = await mixerState.audioCtx.decodeAudioData(buf);
    });
    await Promise.all(promises);
    mixerState.loaded = true;

    if (btn) { btn.textContent = 'Reload'; btn.disabled = false; }
    if (status) status.textContent = `${mixerState.tracks.length} tracks loaded. Hit Play to listen — solo (S) individual tracks to identify speakers.`;
    const playBtn = document.getElementById('mixer-play-btn');
    if (playBtn) { playBtn.disabled = false; playBtn.className = playBtn.className.replace('text-zinc-500', 'text-white').replace('bg-zinc-700', 'bg-brand-600'); }
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
    if (status) status.textContent = `Error: ${err.message}`;
  }
}

function toggleMixerPlayback() {
  if (mixerState.playing) stopMixerPlayback(); else startMixerPlayback();
}

function startMixerPlayback() {
  if (!mixerState.loaded || !mixerState.audioCtx) return;
  if (mixerState.audioCtx.state === 'suspended') mixerState.audioCtx.resume();

  const anySoloed = mixerState.tracks.some(t => t.soloed);

  for (const track of mixerState.tracks) {
    if (!track.buffer) continue;
    const source = mixerState.audioCtx.createBufferSource();
    source.buffer = track.buffer;
    const gain = mixerState.audioCtx.createGain();
    if (anySoloed) {
      gain.gain.value = (track.soloed && !track.muted) ? track.volume : 0;
    } else {
      gain.gain.value = track.muted ? 0 : track.volume;
    }
    const analyser = mixerState.audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(gain); gain.connect(analyser); analyser.connect(mixerState.audioCtx.destination);
    source.start(0);
    track.source = source; track.gain = gain; track.analyser = analyser;
    source.onended = () => { if (mixerState.playing) stopMixerPlayback(); };
  }

  mixerState.playing = true;
  const playBtn = document.getElementById('mixer-play-btn');
  if (playBtn) playBtn.innerHTML = '&#9724; Stop';
  updateMixerMeters();
}

function stopMixerPlayback() {
  for (const track of mixerState.tracks) {
    if (track.source) { try { track.source.stop(); } catch {} track.source = null; }
    track.gain = null; track.analyser = null;
  }
  mixerState.playing = false;
  if (mixerState.animFrame) { cancelAnimationFrame(mixerState.animFrame); mixerState.animFrame = null; }
  const playBtn = document.getElementById('mixer-play-btn');
  if (playBtn) playBtn.innerHTML = '&#9654; Play';
  for (let i = 0; i < mixerState.tracks.length; i++) drawMixerMeter(i, 0);
}

function updateMixerMeters() {
  for (let i = 0; i < mixerState.tracks.length; i++) {
    const track = mixerState.tracks[i];
    if (!track.analyser) { drawMixerMeter(i, 0); continue; }
    const data = new Float32Array(track.analyser.fftSize);
    track.analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (let j = 0; j < data.length; j++) sum += data[j] * data[j];
    drawMixerMeter(i, Math.sqrt(sum / data.length));
  }
  if (mixerState.playing) mixerState.animFrame = requestAnimationFrame(updateMixerMeters);
}

function drawMixerMeter(idx, level) {
  const canvas = document.getElementById(`mixer-meter-${idx}`);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, w, h);
  if (level <= 0) return;
  const db = 20 * Math.log10(Math.max(level, 1e-10));
  const norm = Math.max(0, Math.min(1, (db + 50) / 50));
  const barW = Math.round(norm * w);
  if (barW > 0) {
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, '#22c55e');
    grad.addColorStop(0.75, '#eab308');
    grad.addColorStop(1, '#ef4444');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 2, barW, h - 4);
  }
}

function setMixerTrackVolume(idx, value) {
  const vol = parseInt(value) / 100;
  mixerState.tracks[idx].volume = vol;
  const anySoloed = mixerState.tracks.some(t => t.soloed);
  if (mixerState.tracks[idx].gain && !mixerState.tracks[idx].muted) {
    if (!anySoloed || mixerState.tracks[idx].soloed) {
      mixerState.tracks[idx].gain.gain.value = vol;
    }
  }
  const label = document.getElementById(`mixer-vol-label-${idx}`);
  if (label) label.textContent = value + '%';
}

function toggleTrackMute(idx) {
  const track = mixerState.tracks[idx];
  track.muted = !track.muted;
  applyMixerGains();
  const btn = document.getElementById(`mixer-mute-${idx}`);
  if (btn) btn.className = `w-6 h-6 text-[10px] font-bold rounded transition-colors ${track.muted ? 'bg-red-600 text-white' : 'bg-zinc-800 text-zinc-500 hover:text-red-400'}`;
}

function toggleTrackSolo(idx) {
  mixerState.tracks[idx].soloed = !mixerState.tracks[idx].soloed;
  applyMixerGains();
  for (let i = 0; i < mixerState.tracks.length; i++) {
    const t = mixerState.tracks[i];
    const btn = document.getElementById(`mixer-solo-${i}`);
    if (btn) btn.className = `w-6 h-6 text-[10px] font-bold rounded transition-colors ${t.soloed ? 'bg-yellow-600 text-white' : 'bg-zinc-800 text-zinc-500 hover:text-yellow-400'}`;
  }
}

function applyMixerGains() {
  const anySoloed = mixerState.tracks.some(t => t.soloed);
  for (const t of mixerState.tracks) {
    if (!t.gain) continue;
    if (anySoloed) {
      t.gain.gain.value = (t.soloed && !t.muted) ? t.volume : 0;
    } else {
      t.gain.gain.value = t.muted ? 0 : t.volume;
    }
  }
}

function assignMixerTrack(trackIdx, value) {
  mixerState.tracks[trackIdx].assignment = value;
  const trackNum = mixerState.tracks[trackIdx].trackNumber;
  if (value.startsWith('speaker-')) {
    const si = parseInt(value.split('-')[1]);
    // Clear this track from other speakers
    for (const spk of cropState.speakers) { if (spk.track === trackNum) spk.track = null; }
    if (cropState.speakers[si]) cropState.speakers[si].track = trackNum;
    // Clear other tracks assigned to this speaker in mixer
    for (let i = 0; i < mixerState.tracks.length; i++) {
      if (i !== trackIdx && mixerState.tracks[i].assignment === value) {
        mixerState.tracks[i].assignment = '';
        const sel = document.getElementById(`mixer-assign-${i}`);
        if (sel) sel.value = '';
      }
    }
  } else {
    // Unassigned or ambient — clear from speakers
    for (const spk of cropState.speakers) { if (spk.track === trackNum) spk.track = null; }
  }
  // Sync sidebar track dropdowns
  for (let i = 0; i < cropState.speakers.length; i++) {
    const sel = document.getElementById(`crop-track-${i}`);
    if (sel) sel.value = cropState.speakers[i].track || '';
  }
}

function shiftMixerWindow(delta, episodeId) {
  if (mixerState.playing) stopMixerPlayback();
  mixerState.previewStart = Math.max(0, mixerState.previewStart + delta);
  mixerState.loaded = false;
  const range = document.getElementById('mixer-range');
  if (range) range.textContent = `${fmtMixerTime(mixerState.previewStart)} – ${fmtMixerTime(mixerState.previewStart + mixerState.previewDuration)}`;
  const playBtn = document.getElementById('mixer-play-btn');
  if (playBtn) { playBtn.disabled = true; playBtn.className = playBtn.className.replace('text-white', 'text-zinc-500').replace('bg-brand-600', 'bg-zinc-700'); }
  loadAudioPreviews(episodeId);
}


// ════════════════════════════════════════════════════════════════════
//  SCHEDULE VIEW
// ════════════════════════════════════════════════════════════════════

async function renderSchedule() {
  let data = { schedule: [] };
  try { data = await api('/schedule'); } catch {}

  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const schedule = data.schedule || [];

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <h1 class="text-2xl font-bold mb-2">Publish Schedule</h1>
    <p class="text-zinc-500 text-sm mb-8">1 clip/day Mon-Thu &middot; 2 clips/day Fri-Sun</p>

    <div class="grid grid-cols-7 gap-2">
      ${days.map(day => `
        <div class="text-center text-xs font-semibold text-zinc-500 pb-2">${day}</div>
      `).join('')}
    </div>

    <div class="grid grid-cols-7 gap-2">
      ${schedule.length > 0
        ? schedule.map(day => `
          <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-2 min-h-[100px]">
            <div class="text-xs text-zinc-500 mb-2">${day.date || ''}</div>
            ${(day.items || []).map(item => `
              <div class="text-xs mb-1 p-1 rounded ${item.type === 'longform' ? 'bg-zinc-800 text-zinc-300' : 'bg-zinc-800/50 text-zinc-400'}">
                <div class="truncate">${item.type === 'longform' ? 'Longform' : (item.title || item.id || 'Clip')}</div>
                ${item.platforms ? `<div class="flex gap-1 mt-0.5">${item.platforms.map(p => `<span class="platform-${p} text-[10px]">${p.slice(0,2).toUpperCase()}</span>`).join('')}</div>` : ''}
              </div>
            `).join('')}
          </div>
        `).join('')
        : days.map(() => `
          <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-2 min-h-[100px]">
            <div class="text-xs text-zinc-600 text-center pt-6">No schedule</div>
          </div>
        `).join('')
      }
    </div>
  </div>`;
}


// ════════════════════════════════════════════════════════════════════
//  ANALYTICS VIEW
// ════════════════════════════════════════════════════════════════════

async function renderAnalytics() {
  let data = {};
  try { data = await api('/analytics/'); } catch {}

  const clips = data.clips || [];
  const weights = data.scoring_weights || {};

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <h1 class="text-2xl font-bold mb-2">Analytics</h1>
    <p class="text-zinc-500 text-sm mb-8">Performance tracking and feedback loop status</p>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="lg:col-span-2">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-zinc-800 text-left text-xs text-zinc-500">
                <th class="px-4 py-3">Clip</th>
                <th class="px-4 py-3">Views</th>
                <th class="px-4 py-3">Engagement</th>
                <th class="px-4 py-3">Watch Time</th>
                <th class="px-4 py-3">Platform</th>
              </tr>
            </thead>
            <tbody>
              ${clips.length > 0
                ? clips.map(c => `
                  <tr class="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                    <td class="px-4 py-2.5 text-zinc-300">${escapeHtml(c.title || c.id)}</td>
                    <td class="px-4 py-2.5 text-zinc-400">${c.views != null ? c.views.toLocaleString() : '--'}</td>
                    <td class="px-4 py-2.5 text-zinc-400">${c.engagement_rate != null ? (c.engagement_rate * 100).toFixed(1) + '%' : '--'}</td>
                    <td class="px-4 py-2.5 text-zinc-400">${c.avg_watch_time != null ? c.avg_watch_time.toFixed(1) + 's' : '--'}</td>
                    <td class="px-4 py-2.5">${c.platform ? `<span class="platform-${c.platform} text-xs">${c.platform}</span>` : '--'}</td>
                  </tr>
                `).join('')
                : `<tr><td colspan="5" class="px-4 py-8 text-center text-zinc-600">No analytics data yet. Publish clips to start tracking.</td></tr>`
              }
            </tbody>
          </table>
        </div>
      </div>

      <div class="space-y-4">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Scoring Weights</h3>
          <div class="space-y-2">
            ${Object.keys(weights).length > 0
              ? Object.entries(weights).map(([k, v]) => `
                <div class="flex items-center justify-between text-xs">
                  <span class="text-zinc-500">${k.replace(/_/g, ' ')}</span>
                  <div class="flex items-center gap-2">
                    <div class="w-20 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                      <div class="h-full bg-brand-500 rounded-full" style="width: ${(v * 100)}%"></div>
                    </div>
                    <span class="text-zinc-400 w-8 text-right">${(v * 100).toFixed(0)}%</span>
                  </div>
                </div>
              `).join('')
              : '<p class="text-xs text-zinc-600">Default weights active. Analytics data needed to adjust.</p>'
            }
          </div>
        </div>

        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Feedback Loop</h3>
          <div class="text-xs text-zinc-500 space-y-1">
            <div class="flex justify-between"><span>Last collection</span><span class="text-zinc-400">${data.last_collection || 'Never'}</span></div>
            <div class="flex justify-between"><span>Episodes tracked</span><span class="text-zinc-400">${data.episodes_tracked || 0}</span></div>
            <div class="flex justify-between"><span>Weight adjustments</span><span class="text-zinc-400">${data.weight_adjustments || 0}</span></div>
          </div>
        </div>
      </div>
    </div>
  </div>`;
}
