import { signal, onCleanup } from '../lib/signals';
import { api, type EpisodeSummary, type UnknownRecord } from '../lib/api';

export const episodes = signal<EpisodeSummary[] | null>(null);

let episodesTimer: number | null = null;
const EPISODES_POLL_MS = 8000;

async function refreshEpisodes(): Promise<void> {
  try {
    const next = await api.listEpisodes();
    if (JSON.stringify(next) !== JSON.stringify(episodes.peek())) episodes.set(next);
  } catch {
    // Poll will retry.
  }
}

export function startEpisodesPoll(): void {
  if (episodesTimer != null) return;
  refreshEpisodes();
  episodesTimer = window.setInterval(refreshEpisodes, EPISODES_POLL_MS);
}

/* Per-episode detail store */

export const episodeDetail = signal<UnknownRecord | null>(null);
export const episodeDetailError = signal<string | null>(null);
export const episodeDetailId = signal<string | null>(null);

let detailTimer: number | null = null;
const DETAIL_POLL_MS = 4000;

async function loadDetail(id: string): Promise<void> {
  try {
    const d = await api.getEpisode(id);
    if (episodeDetailId.peek() === id) {
      if (JSON.stringify(d) !== JSON.stringify(episodeDetail.peek())) episodeDetail.set(d);
      episodeDetailError.set(null);
    }
  } catch (e) {
    if (episodeDetailId.peek() === id) {
      episodeDetailError.set((e as Error).message ?? 'Could not load episode');
    }
  }
}

export function watchEpisode(id: string | null): void {
  if (id) onCleanup(() => {
    if (episodeDetailId.peek() === id) watchEpisode(null);
  });
  if (episodeDetailId.peek() === id) return;
  episodeDetailId.set(id);
  episodeDetailError.set(null);
  if (detailTimer != null) {
    clearInterval(detailTimer);
    detailTimer = null;
  }
  if (!id) {
    episodeDetail.set(null);
    return;
  }
  episodeDetail.set(null);
  loadDetail(id);
  detailTimer = window.setInterval(() => loadDetail(id), DETAIL_POLL_MS);
}
