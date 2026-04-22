/**
 * Human-language formatters. Cascade never shows raw state codes or bare
 * second counts to Sam — everything passes through here.
 */

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return '—';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
  return `${sec}s`;
}

export function formatTimecode(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return '--:--';
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  if (h > 0) return `${h}:${mm}:${ss}`;
  return `${mm}:${ss}`;
}

export function formatOffsetMs(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return '—';
  const ms = Math.round(seconds * 1000);
  if (ms === 0) return 'aligned';
  const abs = Math.abs(ms);
  const suffix = ms > 0 ? 'H6E leads camera' : 'camera leads H6E';
  return `${abs}ms — ${suffix}`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (isNaN(then)) return '—';
  const diff = Date.now() - then;
  const abs = Math.abs(diff);
  const future = diff < 0;
  const minutes = Math.round(abs / 60_000);
  const hours = Math.round(abs / 3_600_000);
  const days = Math.round(abs / 86_400_000);
  if (abs < 60_000) return future ? 'in a moment' : 'just now';
  if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`;
  if (hours < 24) return future ? `in ${hours}h` : `${hours}h ago`;
  if (days < 30) return future ? `in ${days}d` : `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

/* ---------------------- Status → plain-English mapping --------------------- */

type StatusKey =
  | 'queued'
  | 'processing'
  | 'awaiting_crop'
  | 'awaiting_longform_review'
  | 'awaiting_clip_review'
  | 'awaiting_publish'
  | 'awaiting_backup'
  | 'live'
  | 'error'
  | 'cancelled';

export type StatusTone =
  | 'working'
  | 'waiting'
  | 'success'
  | 'danger'
  | 'neutral';

export interface StatusDescriptor {
  key: StatusKey;
  tone: StatusTone;
  label: string;
  hint: string;
}

/** Descriptor shape keyed by StatusKey. */
const STATUS: Record<StatusKey, Omit<StatusDescriptor, 'key'>> = {
  processing: {
    tone: 'working',
    label: 'Processing',
    hint: 'Pipeline running — no action needed.',
  },
  awaiting_crop: {
    tone: 'waiting',
    label: 'Crop setup needed',
    hint: 'Draw speaker crops and confirm audio sync.',
  },
  awaiting_longform_review: {
    tone: 'waiting',
    label: 'Longform review',
    hint: 'Watch the cut and approve or request edits.',
  },
  awaiting_clip_review: {
    tone: 'waiting',
    label: 'Clip review',
    hint: 'Review 10 clips and per-platform metadata.',
  },
  awaiting_publish: {
    tone: 'waiting',
    label: 'Ready to publish',
    hint: 'Clips approved — confirm to go live.',
  },
  awaiting_backup: {
    tone: 'waiting',
    label: 'Backup pending',
    hint: 'Confirm backup to external drive.',
  },
  live: {
    tone: 'success',
    label: 'Live',
    hint: 'Episode is live across platforms.',
  },
  error: {
    tone: 'danger',
    label: 'Error',
    hint: 'Pipeline hit a blocker — details below.',
  },
  cancelled: {
    tone: 'neutral',
    label: 'Cancelled',
    hint: 'Pipeline was cancelled.',
  },
  queued: { tone: 'neutral', label: 'Queued', hint: 'Waiting to start.' },
};

/** Raw backend strings that map onto each canonical StatusKey. */
const STATUS_ALIASES: Record<string, StatusKey> = {
  processing: 'processing',
  running: 'processing',
  awaiting_crop_setup: 'awaiting_crop',
  awaiting_crop: 'awaiting_crop',
  needs_crop_setup: 'awaiting_crop',
  awaiting_longform_approval: 'awaiting_longform_review',
  awaiting_longform_review: 'awaiting_longform_review',
  ready_for_review: 'awaiting_clip_review',
  awaiting_clip_review: 'awaiting_clip_review',
  awaiting_approval: 'awaiting_clip_review',
  awaiting_publish: 'awaiting_publish',
  approved: 'awaiting_publish',
  awaiting_backup_approval: 'awaiting_backup',
  awaiting_backup: 'awaiting_backup',
  published: 'live',
  completed: 'live',
  live: 'live',
  error: 'error',
  failed: 'error',
  cancelled: 'cancelled',
  canceled: 'cancelled',
  queued: 'queued',
  created: 'queued',
  new: 'queued',
};

export function describeStatus(raw: string | null | undefined): StatusDescriptor {
  const key = STATUS_ALIASES[(raw ?? '').toLowerCase()];
  if (key) return { key, ...STATUS[key] };
  const label = raw
    ? raw.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
    : 'Unknown';
  return { key: 'queued', tone: 'neutral', label, hint: '' };
}

/* ---------------------- Agent → plain-English mapping --------------------- */

const AGENT_LABELS: Record<string, string> = {
  ingest: 'Reading SD card',
  stitch: 'Stitching source clips',
  audio_sync: 'Syncing H6E to camera',
  audio_analysis: 'Analyzing audio',
  audio_enhance: 'Enhancing audio',
  speaker_cut: 'Splitting by speaker',
  transcribe: 'Transcribing',
  clip_miner: 'Mining clips',
  longform_render: 'Rendering longform',
  shorts_render: 'Rendering shorts',
  metadata_gen: 'Writing metadata',
  thumbnail_gen: 'Generating thumbnails',
  qa: 'Quality check',
  podcast_feed: 'Updating RSS feed',
  publish: 'Publishing',
  backup: 'Backing up',
};

/** Canonical order of the 14-stage cascade pipeline. */
export const CANONICAL_AGENTS: string[] = [
  'ingest',
  'stitch',
  'audio_sync',
  'audio_enhance',
  'speaker_cut',
  'transcribe',
  'clip_miner',
  'longform_render',
  'shorts_render',
  'metadata_gen',
  'thumbnail_gen',
  'qa',
  'podcast_feed',
  'publish',
  'backup',
];

export function describeAgent(agent: string | null | undefined): string {
  if (!agent) return '';
  return (
    AGENT_LABELS[agent] ??
    agent.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

export function pluralize(n: number, word: string, plural?: string): string {
  if (n === 1) return `1 ${word}`;
  return `${n} ${plural ?? word + 's'}`;
}

/**
 * Distill a backend error string into a one-line summary that fits in chrome.
 * rsync timeouts become `Command timed out after 1800 seconds`; JSON parse
 * errors become a generic `Parse error — response was truncated`; anything
 * else returns its first line, truncated to `maxLen`.
 */
export function summarizeErrorText(text: string, maxLen = 100): string {
  if (/timed out/i.test(text)) {
    const m = text.match(/timed out after \d+ seconds?/i);
    if (m) return `Command ${m[0]}`;
  }
  if (/Unterminated string/i.test(text)) {
    return 'Parse error — response was truncated';
  }
  const first = text.split(/\r?\n/)[0];
  return first.length > maxLen ? first.slice(0, maxLen) + '…' : first;
}

/**
 * Pick the best human title for an episode.
 * Priority: guest_name → episode_name → title → `Untitled — <date>`.
 */
export function episodeTitle(
  ep: Record<string, unknown> | null | undefined,
  episodeId: string
): string {
  if (ep) {
    const g = (ep.guest_name as string | undefined)?.trim();
    if (g) return g;
    const en = (ep.episode_name as string | undefined)?.trim();
    if (en) return en;
    const t = (ep.title as string | undefined)?.trim();
    if (t) return t;
  }
  return `Untitled — ${episodeDateLabel(episodeId)}`;
}

/** Parse an `ep_YYYY-MM-DD_HHMMSS` episode id into a short human date. */
export function episodeDateLabel(episodeId: string): string {
  const m = episodeId.match(/^ep_(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return episodeId;
  const [, y, mo, d] = m;
  const date = new Date(Number(y), Number(mo) - 1, Number(d));
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: new Date().getFullYear() === Number(y) ? undefined : 'numeric',
  });
}
