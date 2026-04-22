/**
 * Typed fetch wrapper for the cascade backend. One function per backend route.
 *
 * Shapes mirror server/routes Pydantic models. Where the backend returns a
 * raw dict, we type it loosely (Record<string, unknown>) and let screens
 * narrow as needed.
 */

export type UnknownRecord = Record<string, unknown>;

export interface ApiError extends Error {
  status: number;
  body: unknown;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      /* ignore */
    }
    const err = new Error(
      `${method} ${path} failed (${res.status})`
    ) as ApiError;
    err.status = res.status;
    err.body = payload;
    throw err;
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* ---------------------------------- Episodes --------------------------------- */

export interface EpisodeSummary {
  episode_id: string;
  title: string | null;
  status: string;
  duration_seconds: number | null;
  created_at: string;
  /** Backend returns the full clips array in the list response, not a count. */
  clips?: UnknownRecord[];
  guest_name?: string | null;
  guest_title?: string | null;
  episode_name?: string | null;
  episode_description?: string | null;
}

export interface NewEpisodeRequest {
  source_path?: string;
  audio_path?: string;
  speaker_count?: number;
}

export interface EpisodeUpdateRequest {
  title?: string;
  description?: string;
  tags?: string[];
  guest_name?: string;
  guest_title?: string;
  episode_name?: string;
  episode_description?: string;
  youtube_longform_url?: string;
  spotify_longform_url?: string;
  link_tree_url?: string;
}

export interface SpeakerCropConfig {
  label: string;
  center_x: number;
  center_y: number;
  zoom: number;
  longform_center_x?: number;
  longform_center_y?: number;
  longform_zoom: number;
  track?: number;
  volume: number;
}

export interface AmbientTrackConfig {
  track_number?: number;
  stem?: string;
  volume: number;
}

export interface CropConfigRequest {
  speakers?: SpeakerCropConfig[];
  ambient_tracks?: AmbientTrackConfig[];
  wide_center_x?: number;
  wide_center_y?: number;
  wide_zoom?: number;
  source_width?: number;
  source_height?: number;
}

export const api = {
  /* Episodes */
  listEpisodes: () => request<EpisodeSummary[]>('GET', '/api/episodes/'),
  getEpisode: (id: string) => request<UnknownRecord>('GET', `/api/episodes/${id}`),
  createEpisode: (req: NewEpisodeRequest) =>
    request<UnknownRecord>('POST', '/api/episodes/', req),
  updateEpisode: (id: string, req: EpisodeUpdateRequest) =>
    request<UnknownRecord>('PATCH', `/api/episodes/${id}`, req),
  deleteEpisode: (id: string) => request<void>('DELETE', `/api/episodes/${id}`),
  approveEpisode: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve`),

  cropFrameUrl: (id: string) => `/api/episodes/${id}/crop-frame`,
  videoPreviewUrl: (id: string) => `/api/episodes/${id}/video-preview`,
  audioPreviewUrl: (id: string, track: string) =>
    `/api/episodes/${id}/audio-preview/${track}`,
  channelPreviewUrl: (id: string, channel: 'left' | 'right') =>
    `/api/episodes/${id}/channel-preview/${channel}`,

  syncPreview: (id: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/sync-preview`),
  saveSyncOffset: (id: string, offset_seconds: number) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/sync-offset`, {
      offset_seconds,
    }),

  saveCropConfig: (id: string, cfg: CropConfigRequest) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/crop-config`, cfg),

  /* Pipeline */
  runPipeline: (id: string, body: { source_path?: string; audio_path?: string; agents?: string[] } = {}) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/run-pipeline`, body),
  runAgent: (id: string, agent: string, body: { source_path?: string } = {}) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/run-agent/${agent}`, body),
  pipelineStatus: (id: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/pipeline-status`),
  cancelPipeline: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/cancel-pipeline`),
  resumePipeline: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/resume-pipeline`),
  autoApprove: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/auto-approve`),
  approveLongform: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve-longform`),
  approvePublish: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve-publish`),
  approveBackup: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve-backup`),

  /* Clips */
  listClips: (id: string) =>
    request<UnknownRecord[]>('GET', `/api/episodes/${id}/clips/`),
  getClip: (id: string, clipId: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/clips/${clipId}`),
  approveClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/approve`),
  rejectClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/reject`),
  alternativeClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/alternative`),
  addManualClip: (id: string, body: { start_seconds: number; end_seconds: number }) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/manual`, body),
  updateClip: (
    id: string,
    clipId: string,
    body: {
      title?: string;
      description?: string;
      hashtags?: string[];
      start_seconds?: number;
      end_seconds?: number;
      metadata?: UnknownRecord;
    }
  ) =>
    request<UnknownRecord>(
      'PATCH',
      `/api/episodes/${id}/clips/${clipId}/metadata`,
      body
    ),

  /* Chat */
  chatHistory: (id: string) =>
    request<UnknownRecord[]>('GET', `/api/episodes/${id}/chat/history`),
  chat: (id: string, message: string) =>
    request<{ response: string; actions_taken: UnknownRecord[] }>(
      'POST',
      `/api/episodes/${id}/chat`,
      { message }
    ),
  completeMetadata: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/complete-metadata`),

  /* Trim */
  trim: (id: string, body: { trim_start_seconds?: number; trim_end_seconds?: number }) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/trim`, body),

  /* Edits */
  listEdits: (id: string) =>
    request<UnknownRecord[]>('GET', `/api/episodes/${id}/edits/`),
  addEdit: (id: string, body: UnknownRecord) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/edits/`, body),
  removeEdit: (id: string, index: number) =>
    request<UnknownRecord>('DELETE', `/api/episodes/${id}/edits/${index}`),
  clearEdits: (id: string) =>
    request<UnknownRecord>('DELETE', `/api/episodes/${id}/edits/`),
  findEdits: (id: string, body: { query: string; max_results?: number }) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/edits/find`, body),
  applyEdits: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/edits/apply`),

  /* Schedule */
  schedule: () => request<UnknownRecord>('GET', '/api/schedule'),
};
