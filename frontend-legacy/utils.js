// Cascade frontend utilities — extracted for testability.
// These functions are also defined in app.js for browser use.

function escapeHtml(str) {
  if (str == null) return '';
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
  return String(str).replace(/[&<>"']/g, c => map[c]);
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

function getRoute(hash) {
  hash = hash || '#/';
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

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    escapeHtml,
    formatTime,
    formatTimeFull,
    formatDuration,
    parseTime,
    scoreBadge,
    statusBadge,
    getRoute,
  };
}
