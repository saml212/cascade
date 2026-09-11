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
    const detail = payload && typeof payload === 'object'
      ? (payload as Record<string, unknown>).detail
      : null;
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join('; ')
      : `${method} ${path} failed (${res.status})`;
    const err = new Error(message) as ApiError;
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
  /**
   * True iff crop_config has been saved. Needed to disambiguate the
   * overloaded `ready_for_review` status (same string for "truncated
   * pipeline done, awaiting crop" and "full pipeline done, awaiting
   * clip review"). Fall back to `false` if backend hasn't surfaced it.
   */
  has_crop_config?: boolean;
  delivery?: DeliveryStatus | null;
}

export interface NewEpisodeRequest {
  source_path?: string;
  audio_path?: string;
  speaker_count?: number;
  agents?: string[];
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

export interface DeliveryStatus extends UnknownRecord {
  status: 'not_prepared' | 'preparing' | 'ready' | 'failed';
  episode_id: string;
  error?: string;
  filename?: string;
  download_url?: string;
  size_bytes?: number;
  duration_seconds?: number;
  expected_duration_seconds?: number;
  duration_difference_seconds?: number;
  integrated_lufs?: number;
  true_peak_dbfs?: number;
  loudness_range_lu?: number;
  completed_at?: string;
  notes?: string[];
  stale?: boolean;
  video_status?: 'not_prepared' | 'preparing' | 'ready' | 'failed';
  video_progress?: number;
  video_detail?: string;
  video_error?: string;
  video_completed_at?: string;
  video_download_url?: string;
  video?: {
    filename: string;
    size_bytes: number;
    duration_seconds: number;
    width: number;
    height: number;
    video_codec: string;
    audio_codec: string;
    encoder: string;
    edit_count: number;
  };
  source_duration_seconds?: number;
  trim_start_seconds?: number;
  trim_end_seconds?: number;
}

export const api = {
  /* Episodes */
  listEpisodes: () => request<EpisodeSummary[]>('GET', '/api/episodes/'),
  getEpisode: (id: string) => request<UnknownRecord>('GET', `/api/episodes/${id}`),
  createEpisode: (req: NewEpisodeRequest) =>
    request<UnknownRecord>('POST', '/api/episodes/', req),
  runPipeline: (id: string, req: NewEpisodeRequest) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/run-pipeline`, req),
  updateEpisode: (id: string, req: EpisodeUpdateRequest) =>
    request<UnknownRecord>('PATCH', `/api/episodes/${id}`, req),

  cropFrameUrl: (id: string) => `/api/episodes/${id}/crop-frame`,

  syncPreview: (id: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/sync-preview`),
  saveSyncOffset: (id: string, offset_seconds: number) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/sync-offset`, {
      offset_seconds,
    }),

  saveCropConfig: (id: string, cfg: CropConfigRequest) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/crop-config`, cfg),

  deliveryStatus: (id: string) =>
    request<DeliveryStatus>('GET', `/api/episodes/${id}/delivery`),
  prepareDelivery: (id: string) =>
    request<DeliveryStatus>('POST', `/api/episodes/${id}/delivery/prepare`),
  prepareDeliveryVideo: (id: string) =>
    request<DeliveryStatus>('POST', `/api/episodes/${id}/delivery/video/prepare`),
  saveDeliveryTrim: (id: string, start_seconds: number, end_seconds: number) =>
    request<DeliveryStatus>('PUT', `/api/episodes/${id}/delivery/trim`, {
      start_seconds,
      end_seconds,
    }),

  /* Pipeline */
  pipelineStatus: (id: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/pipeline-status`),
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
  approveClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/approve`),
  rejectClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/reject`),
  alternativeClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/alternative`),
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

  /* Edits */
  listEdits: (id: string) =>
    request<{ edits: UnknownRecord[]; count: number }>(
      'GET',
      `/api/episodes/${id}/edits`
    ),
  addEdit: (
    id: string,
    edit: { type: 'cut'; start_seconds: number; end_seconds: number; reason?: string }
  ) => request<UnknownRecord>('POST', `/api/episodes/${id}/edits`, edit),
  removeEdit: (id: string, index: number) =>
    request<UnknownRecord>('DELETE', `/api/episodes/${id}/edits/${index}`),
  applyEdits: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/edits/apply`),

  /* Transcript */
  getTranscript: (id: string): Promise<{
    utterances: Array<{ speaker: number; start: number; end: number; text: string }>;
    speaker_map?: Array<{ index: number; label: string; track?: number }>;
  }> =>
    fetch(`/media/episodes/${id}/diarized_transcript.json`).then((r) => {
      if (!r.ok) throw new Error(`Transcript not available (status ${r.status})`);
      return r.json() as Promise<{
        utterances: Array<{ speaker: number; start: number; end: number; text: string }>;
        speaker_map?: Array<{ index: number; label: string; track?: number }>;
      }>;
    }),

  /* Schedule */
  schedule: () => request<UnknownRecord>('GET', '/api/schedule'),
};
