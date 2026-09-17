import { signal, onCleanup } from '../lib/signals';
import { api, type EpisodeSummary, type UnknownRecord } from '../lib/api';
import { coalescedRefresh } from '../lib/coalesced-refresh';

export const episodes = signal<EpisodeSummary[] | null>(null);

let episodesTimer: number | null = null;
let episodesRequestPending = false;
const EPISODES_POLL_MS = 8000;

async function refreshEpisodes(): Promise<void> {
  if (episodesRequestPending) return;
  episodesRequestPending = true;
  try {
    const next = await api.listEpisodes();
    if (JSON.stringify(next) !== JSON.stringify(episodes.peek())) episodes.set(next);
  } catch {
    // Poll will retry.
  } finally {
    episodesRequestPending = false;
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
let detailGeneration = 0;
let detailRefresh: { generation: number; run: () => Promise<void> } | null = null;
const DETAIL_POLL_MS = 4000;

function loadDetail(id: string, generation: number): Promise<void> {
  if (detailRefresh?.generation !== generation) {
    detailRefresh = {
      generation,
      run: coalescedRefresh(async () => {
        if (detailGeneration !== generation || episodeDetailId.peek() !== id) return;
        try {
          const d = await api.getEpisode(id);
          if (detailGeneration === generation && episodeDetailId.peek() === id) {
            if (JSON.stringify(d) !== JSON.stringify(episodeDetail.peek())) episodeDetail.set(d);
            episodeDetailError.set(null);
          }
        } catch (e) {
          if (detailGeneration === generation && episodeDetailId.peek() === id) {
            episodeDetailError.set((e as Error).message ?? 'Could not load episode');
          }
        }
      }),
    };
  }
  return detailRefresh.run();
}

export function refreshEpisode(id: string): Promise<void> {
  if (episodeDetailId.peek() !== id) return Promise.resolve();
  return loadDetail(id, detailGeneration);
}

export function watchEpisode(id: string | null): void {
  if (id) onCleanup(() => {
    if (episodeDetailId.peek() === id) watchEpisode(null);
  });
  if (episodeDetailId.peek() === id) return;
  const generation = ++detailGeneration;
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
  void loadDetail(id, generation);
  detailTimer = window.setInterval(() => void loadDetail(id, generation), DETAIL_POLL_MS);
}
