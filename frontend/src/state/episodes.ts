import { signal, effect } from '../lib/signals';
import { api, type EpisodeSummary, type UnknownRecord } from '../lib/api';

export const episodes = signal<EpisodeSummary[] | null>(null);
export const episodesError = signal<string | null>(null);
export const episodesLoading = signal<boolean>(false);

let episodesTimer: number | null = null;
const EPISODES_POLL_MS = 8000;

export async function refreshEpisodes(): Promise<void> {
  episodesLoading.set(true);
  try {
    const list = await api.listEpisodes();
    episodes.set(list);
    episodesError.set(null);
  } catch (e) {
    episodesError.set((e as Error).message ?? 'Could not load episodes');
  } finally {
    episodesLoading.set(false);
  }
}

export function startEpisodesPoll(): void {
  if (episodesTimer != null) return;
  refreshEpisodes();
  episodesTimer = window.setInterval(refreshEpisodes, EPISODES_POLL_MS);
}

export function stopEpisodesPoll(): void {
  if (episodesTimer != null) {
    clearInterval(episodesTimer);
    episodesTimer = null;
  }
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
      episodeDetail.set(d);
      episodeDetailError.set(null);
    }
  } catch (e) {
    if (episodeDetailId.peek() === id) {
      episodeDetailError.set((e as Error).message ?? 'Could not load episode');
    }
  }
}

export function watchEpisode(id: string | null): void {
  episodeDetailId.set(id);
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

// Auto-stop detail polling when nothing's watching
effect(() => {
  if (!episodeDetailId()) return;
});
