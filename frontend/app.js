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
  analytics: null,
};

// ── Router ─────────────────────────────────────────────────────────

function getRoute() {
  const hash = window.location.hash || '#/';
  const parts = hash.replace('#/', '').split('/').filter(Boolean);

  if (parts[0] === 'episodes' && parts[2] === 'clips' && parts[3]) {
    return { view: 'clip-review', episodeId: parts[1], clipId: parts[3] };
  }
  if (parts[0] === 'episodes' && parts[1]) {
    return { view: 'episode-detail', episodeId: parts[1] };
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

  updateNav();

  const app = document.getElementById('app');
  app.innerHTML = '<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8"><div class="text-zinc-500 text-center py-20">Loading...</div></div>';

  try {
    switch (route.view) {
      case 'dashboard': await renderDashboard(); break;
      case 'episode-detail': await renderEpisodeDetail(route.episodeId); break;
      case 'clip-review': await renderClipReview(route.episodeId, route.clipId); break;
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
      (nav === 'dashboard' && (state.currentView === 'dashboard' || state.currentView === 'episode-detail' || state.currentView === 'clip-review')) ||
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
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function formatTime(seconds) {
  if (seconds == null) return '--:--';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatDuration(seconds) {
  if (seconds == null) return '--';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function scoreBadge(score) {
  const cls = score >= 7 ? 'score-high' : score >= 4 ? 'score-mid' : 'score-low';
  return `<span class="score-badge ${cls}">${score}</span>`;
}

function statusBadge(status) {
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

// ── Dashboard View ─────────────────────────────────────────────────

async function renderDashboard() {
  let episodes = [];
  try { episodes = await api('/episodes'); } catch { episodes = []; }
  state.episodes = episodes;

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-2xl font-bold">Dashboard</h1>
        <p class="text-zinc-500 text-sm mt-1">Manage your podcast episodes</p>
      </div>
      <button onclick="triggerNewEpisode()" class="px-4 py-2 bg-brand-600 hover:bg-brand-700 rounded-lg text-sm font-medium transition-colors">
        + New Episode
      </button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Episode list -->
      <div class="lg:col-span-2 space-y-3" id="episode-list">
        ${episodes.length === 0
          ? '<div class="text-zinc-600 text-center py-16 border border-dashed border-zinc-800 rounded-xl">No episodes yet. Click "New Episode" to get started.</div>'
          : episodes.map(ep => episodeCard(ep)).join('')
        }
      </div>

      <!-- Sidebar: upcoming schedule -->
      <div class="space-y-4">
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-3">Upcoming Schedule</h3>
          <div id="schedule-sidebar" class="space-y-2 text-sm text-zinc-500">
            Loading...
          </div>
        </div>
      </div>
    </div>
  </div>`;

  loadScheduleSidebar();
}

function episodeCard(ep) {
  const clipCount = ep.clips ? ep.clips.length : 0;
  const approved = ep.clips ? ep.clips.filter(c => c.status === 'approved').length : 0;
  return `
    <a href="#/episodes/${ep.episode_id || ep.id}" class="clip-card block bg-zinc-900 border border-zinc-800 rounded-xl p-4 hover:border-zinc-700 transition-colors">
      <div class="flex items-start justify-between">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2 mb-1">
            <h3 class="text-sm font-semibold truncate">${escapeHtml(ep.title || ep.episode_id || 'Untitled')}</h3>
            ${statusBadge(ep.status || 'processing')}
          </div>
          <p class="text-xs text-zinc-500">${formatDuration(ep.duration_seconds)} &middot; ${clipCount} clips &middot; ${approved} approved</p>
          <p class="text-xs text-zinc-600 mt-1">${ep.created_at ? new Date(ep.created_at).toLocaleDateString() : ''}</p>
        </div>
        <svg class="w-4 h-4 text-zinc-600 mt-1 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
      </div>
    </a>`;
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

async function triggerNewEpisode() {
  const path = prompt('Source video path (or leave blank for auto-ingest):');
  try {
    const ep = await api('/episodes', {
      method: 'POST',
      body: JSON.stringify({ source_path: path || null }),
    });
    window.location.hash = `#/episodes/${ep.episode_id || ep.id}`;
  } catch (err) {
    alert('Failed to create episode: ' + err.message);
  }
}

// ── Episode Detail View ────────────────────────────────────────────

async function renderEpisodeDetail(episodeId) {
  const ep = await api(`/episodes/${episodeId}`);
  let clips = [];
  try { clips = await api(`/episodes/${episodeId}/clips`); } catch { clips = ep.clips || []; }
  state.clips = clips;

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <!-- Breadcrumb -->
    <div class="flex items-center gap-2 text-sm text-zinc-500 mb-6">
      <a href="#/" class="hover:text-white transition-colors">Dashboard</a>
      <span>/</span>
      <span class="text-zinc-300">${escapeHtml(ep.title || episodeId)}</span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Main content -->
      <div class="lg:col-span-2 space-y-6">
        <!-- Longform preview -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
          <div class="video-container aspect-video">
            <video controls preload="metadata" class="w-full">
              <source src="${MEDIA}/episodes/${episodeId}/longform.mp4" type="video/mp4">
              <p class="text-zinc-500 p-4">Longform video not yet rendered.</p>
            </video>
          </div>
        </div>

        <!-- Clip grid -->
        <div>
          <div class="flex items-center justify-between mb-3">
            <h2 class="text-lg font-semibold">Clip Candidates</h2>
            <span class="text-xs text-zinc-500">${clips.filter(c => c.status === 'approved').length}/${clips.length} approved</span>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            ${clips.map(clip => clipCard(episodeId, clip)).join('')}
          </div>
        </div>
      </div>

      <!-- Sidebar -->
      <div class="space-y-4">
        <!-- Episode info -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Episode Info</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between"><span class="text-zinc-500">Status</span>${statusBadge(ep.status || 'processing')}</div>
            <div class="flex justify-between"><span class="text-zinc-500">Duration</span><span>${formatDuration(ep.duration_seconds)}</span></div>
            <div class="flex justify-between"><span class="text-zinc-500">Created</span><span class="text-zinc-400">${ep.created_at ? new Date(ep.created_at).toLocaleDateString() : '--'}</span></div>
          </div>
        </div>

        <!-- Longform metadata -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Longform Metadata</h3>
          <label class="block">
            <span class="text-xs text-zinc-500">Title</span>
            <input type="text" id="lf-title" value="${escapeHtml(ep.title || '')}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
          </label>
          <label class="block">
            <span class="text-xs text-zinc-500">Description</span>
            <textarea id="lf-desc" rows="3" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">${escapeHtml(ep.description || '')}</textarea>
          </label>
        </div>

        <!-- Actions -->
        <button onclick="approveAll('${episodeId}')" class="w-full px-4 py-2.5 bg-green-700 hover:bg-green-600 rounded-lg text-sm font-medium transition-colors">
          Approve All &amp; Schedule
        </button>
      </div>
    </div>
  </div>`;
}

function clipCard(episodeId, clip) {
  return `
    <a href="#/episodes/${episodeId}/clips/${clip.id}" class="clip-card bg-zinc-900 border border-zinc-800 rounded-lg p-3 block">
      <div class="flex items-start justify-between mb-2">
        <div class="flex items-center gap-2">
          ${scoreBadge(Math.round(clip.virality_score || 0))}
          <span class="text-sm font-medium truncate max-w-[160px]">${escapeHtml(clip.title || clip.id)}</span>
        </div>
        ${statusBadge(clip.status || 'pending')}
      </div>
      <div class="flex items-center gap-3 text-xs text-zinc-500">
        <span>${formatTime(clip.start)} – ${formatTime(clip.end)}</span>
        <span>${formatDuration(clip.duration || (clip.end - clip.start))}</span>
        ${speakerLabel(clip.speaker)}
      </div>
      ${clip.hook_text ? `<p class="text-xs text-zinc-600 mt-2 line-clamp-2">"${escapeHtml(clip.hook_text)}"</p>` : ''}
    </a>`;
}

async function approveAll(episodeId) {
  if (!confirm('Approve all pending clips and schedule for publishing?')) return;
  try {
    await api(`/episodes/${episodeId}/approve`, { method: 'POST' });
    await renderEpisodeDetail(episodeId);
  } catch (err) {
    alert('Failed: ' + err.message);
  }
}

// ── Clip Review View ───────────────────────────────────────────────

async function renderClipReview(episodeId, clipId) {
  const clip = await api(`/episodes/${episodeId}/clips/${clipId}`);
  const allClips = state.clips.length ? state.clips : (await api(`/episodes/${episodeId}/clips`));
  state.clips = allClips;

  const idx = allClips.findIndex(c => c.id === clipId);
  const prevClip = idx > 0 ? allClips[idx - 1] : null;
  const nextClip = idx < allClips.length - 1 ? allClips[idx + 1] : null;

  const platforms = ['youtube', 'tiktok', 'instagram'];
  const metadata = clip.metadata || {};

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <!-- Breadcrumb -->
    <div class="flex items-center gap-2 text-sm text-zinc-500 mb-6">
      <a href="#/" class="hover:text-white transition-colors">Dashboard</a>
      <span>/</span>
      <a href="#/episodes/${episodeId}" class="hover:text-white transition-colors">Episode</a>
      <span>/</span>
      <span class="text-zinc-300">${escapeHtml(clip.title || clipId)}</span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Main content -->
      <div class="lg:col-span-2 space-y-4">
        <!-- Video player -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
          <div class="video-container" style="max-height: 70vh;">
            <video id="clip-video" controls preload="metadata" class="w-full">
              <source src="${MEDIA}/episodes/${episodeId}/shorts/${clipId}.mp4" type="video/mp4">
              <p class="text-zinc-500 p-4">Clip not yet rendered.</p>
            </video>
          </div>
        </div>

        <!-- Timeline position bar -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
          <div class="flex items-center justify-between text-xs text-zinc-500 mb-1">
            <span>${formatTime(clip.start)}</span>
            <span class="text-zinc-400">${formatDuration(clip.duration || (clip.end - clip.start))}</span>
            <span>${formatTime(clip.end)}</span>
          </div>
          <div class="timeline-bar">
            <div class="clip-region" style="left: 0%; width: 100%;"></div>
          </div>
        </div>

        <!-- Transcript excerpt -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-zinc-300 mb-2">Transcript</h3>
          <div class="max-h-48 overflow-y-auto space-y-1 text-sm text-zinc-400">
            ${clip.transcript_excerpt
              ? clip.transcript_excerpt.split('\n').map(line => `<p class="transcript-line py-0.5">${escapeHtml(line)}</p>`).join('')
              : '<p class="text-zinc-600">Transcript not available for this clip.</p>'
            }
          </div>
        </div>

        <!-- Platform metadata tabs -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
          <div class="flex gap-1 mb-4">
            ${platforms.map((p, i) => `
              <button onclick="switchTab('${p}')" class="tab-btn platform-${p} ${i === 0 ? 'active' : ''}" data-tab="${p}">
                ${p.charAt(0).toUpperCase() + p.slice(1)}
              </button>
            `).join('')}
          </div>
          ${platforms.map((p, i) => `
            <div id="tab-${p}" class="tab-content space-y-3 ${i > 0 ? 'hidden' : ''}">
              <label class="block">
                <span class="text-xs text-zinc-500">${p === 'youtube' ? 'Title' : 'Caption'}</span>
                <input type="text" data-platform="${p}" data-field="title" value="${escapeHtml((metadata[p] && metadata[p].title) || clip.title || '')}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">
              </label>
              <label class="block">
                <span class="text-xs text-zinc-500">Description</span>
                <textarea data-platform="${p}" data-field="description" rows="2" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500">${escapeHtml((metadata[p] && metadata[p].description) || '')}</textarea>
              </label>
              <label class="block">
                <span class="text-xs text-zinc-500">Hashtags</span>
                <input type="text" data-platform="${p}" data-field="hashtags" value="${escapeHtml((metadata[p] && metadata[p].hashtags) || '')}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-brand-500" placeholder="#podcast #clips">
              </label>
            </div>
          `).join('')}
          <button onclick="saveMetadata('${episodeId}', '${clipId}')" class="mt-3 px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 rounded-md text-xs font-medium transition-colors">
            Save Metadata
          </button>
        </div>
      </div>

      <!-- Sidebar -->
      <div class="space-y-4">
        <!-- Clip info -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-2">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-semibold text-zinc-300">Clip Info</h3>
            ${statusBadge(clip.status || 'pending')}
          </div>
          <div class="flex items-center gap-2">
            ${scoreBadge(Math.round(clip.virality_score || 0))}
            <span class="text-sm text-zinc-400">Virality Score</span>
          </div>
          <div class="text-xs text-zinc-500 space-y-1">
            <div>Speaker: ${speakerLabel(clip.speaker)}</div>
            <div>Rank: #${clip.rank || '?'}</div>
            ${clip.compelling_reason ? `<div class="mt-2 text-zinc-600">"${escapeHtml(clip.compelling_reason)}"</div>` : ''}
          </div>
        </div>

        <!-- Actions -->
        <div class="space-y-2">
          <button onclick="approveClip('${episodeId}', '${clipId}')" class="w-full px-4 py-2.5 bg-green-700 hover:bg-green-600 rounded-lg text-sm font-medium transition-colors">
            Approve
          </button>
          <button onclick="rejectClip('${episodeId}', '${clipId}')" class="w-full px-4 py-2.5 bg-red-900 hover:bg-red-800 rounded-lg text-sm font-medium transition-colors">
            Reject
          </button>
          <button onclick="requestAlternative('${episodeId}', '${clipId}')" class="w-full px-4 py-2.5 bg-amber-800 hover:bg-amber-700 rounded-lg text-sm font-medium transition-colors">
            Request Alternative
          </button>
        </div>

        <!-- Edit time range -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Edit Time Range</h3>
          <div class="grid grid-cols-2 gap-2">
            <label class="block">
              <span class="text-xs text-zinc-500">Start</span>
              <input type="text" id="edit-start" value="${formatTime(clip.start)}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 text-center focus:outline-none focus:border-brand-500" placeholder="MM:SS">
            </label>
            <label class="block">
              <span class="text-xs text-zinc-500">End</span>
              <input type="text" id="edit-end" value="${formatTime(clip.end)}" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 text-center focus:outline-none focus:border-brand-500" placeholder="MM:SS">
            </label>
          </div>
          <button onclick="updateTimeRange('${episodeId}', '${clipId}')" class="w-full px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 rounded-md text-xs font-medium transition-colors">
            Update Range
          </button>
        </div>

        <!-- Add custom clip -->
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
          <h3 class="text-sm font-semibold text-zinc-300">Add Custom Clip</h3>
          <div class="grid grid-cols-2 gap-2">
            <label class="block">
              <span class="text-xs text-zinc-500">Start</span>
              <input type="text" id="custom-start" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 text-center focus:outline-none focus:border-brand-500" placeholder="MM:SS">
            </label>
            <label class="block">
              <span class="text-xs text-zinc-500">End</span>
              <input type="text" id="custom-end" class="mt-1 block w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 text-center focus:outline-none focus:border-brand-500" placeholder="MM:SS">
            </label>
          </div>
          <button onclick="addCustomClip('${episodeId}')" class="w-full px-3 py-1.5 bg-brand-600 hover:bg-brand-700 rounded-md text-xs font-medium transition-colors">
            Add Clip
          </button>
        </div>

        <!-- Nav: prev/next -->
        <div class="flex gap-2">
          ${prevClip
            ? `<a href="#/episodes/${episodeId}/clips/${prevClip.id}" class="flex-1 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 rounded-md text-xs font-medium text-center transition-colors">← Previous</a>`
            : '<span class="flex-1"></span>'
          }
          ${nextClip
            ? `<a href="#/episodes/${episodeId}/clips/${nextClip.id}" class="flex-1 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 rounded-md text-xs font-medium text-center transition-colors">Next →</a>`
            : '<span class="flex-1"></span>'
          }
        </div>
      </div>
    </div>
  </div>`;
}

function switchTab(platform) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === platform));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('hidden', c.id !== `tab-${platform}`));
}

async function approveClip(episodeId, clipId) {
  await api(`/episodes/${episodeId}/clips/${clipId}/approve`, { method: 'POST' });
  await renderClipReview(episodeId, clipId);
}

async function rejectClip(episodeId, clipId) {
  await api(`/episodes/${episodeId}/clips/${clipId}/reject`, { method: 'POST' });
  await renderClipReview(episodeId, clipId);
}

async function requestAlternative(episodeId, clipId) {
  try {
    const result = await api(`/episodes/${episodeId}/clips/${clipId}/alternative`, { method: 'POST' });
    alert(result.message || 'Alternative requested. Reload to see the new clip.');
    await renderClipReview(episodeId, clipId);
  } catch (err) {
    alert('Failed: ' + err.message);
  }
}

function parseTime(str) {
  const parts = str.split(':').map(Number);
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return NaN;
}

async function updateTimeRange(episodeId, clipId) {
  const start = parseTime(document.getElementById('edit-start').value);
  const end = parseTime(document.getElementById('edit-end').value);
  if (isNaN(start) || isNaN(end) || end <= start) {
    alert('Invalid time range.');
    return;
  }
  await api(`/episodes/${episodeId}/clips/${clipId}/metadata`, {
    method: 'PATCH',
    body: JSON.stringify({ start_seconds: start, end_seconds: end }),
  });
  await renderClipReview(episodeId, clipId);
}

async function addCustomClip(episodeId) {
  const start = parseTime(document.getElementById('custom-start').value);
  const end = parseTime(document.getElementById('custom-end').value);
  if (isNaN(start) || isNaN(end) || end <= start) {
    alert('Invalid time range.');
    return;
  }
  const result = await api(`/episodes/${episodeId}/clips/manual`, {
    method: 'POST',
    body: JSON.stringify({ start_seconds: start, end_seconds: end }),
  });
  window.location.hash = `#/episodes/${episodeId}/clips/${result.id}`;
}

async function saveMetadata(episodeId, clipId) {
  const meta = {};
  document.querySelectorAll('[data-platform]').forEach(el => {
    const p = el.dataset.platform;
    const f = el.dataset.field;
    if (!meta[p]) meta[p] = {};
    meta[p][f] = el.value;
  });
  await api(`/episodes/${episodeId}/clips/${clipId}/metadata`, {
    method: 'PATCH',
    body: JSON.stringify({ metadata: meta }),
  });
  alert('Metadata saved.');
}

// ── Schedule View ──────────────────────────────────────────────────

async function renderSchedule() {
  let data = { schedule: [] };
  try { data = await api('/schedule'); } catch {}

  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const schedule = data.schedule || [];

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <h1 class="text-2xl font-bold mb-2">Publish Schedule</h1>
    <p class="text-zinc-500 text-sm mb-8">1 clip/day Mon–Thu &middot; 2 clips/day Fri–Sun</p>

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

// ── Analytics View ─────────────────────────────────────────────────

async function renderAnalytics() {
  let data = {};
  try { data = await api('/analytics'); } catch {}

  const clips = data.clips || [];
  const weights = data.scoring_weights || {};

  const app = document.getElementById('app');
  app.innerHTML = `${container()}
    <h1 class="text-2xl font-bold mb-2">Analytics</h1>
    <p class="text-zinc-500 text-sm mb-8">Performance tracking and feedback loop status</p>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Clip performance table -->
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

      <!-- Feedback loop status -->
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
