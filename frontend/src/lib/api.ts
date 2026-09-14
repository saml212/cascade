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
      : detail && typeof detail === 'object' && typeof (detail as Record<string, unknown>).message === 'string'
        ? String((detail as Record<string, unknown>).message)
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
  /** Canonical clips from clips.json, retained for status resolution. */
  clips?: UnknownRecord[];
  /** All mined clip candidates, including rejected candidates. */
  clip_count?: number;
  /** Clips selected for local rendering and review. */
  selected_clip_count?: number;
  /** Candidates that have not been rejected. */
  nonrejected_clip_count?: number;
  rejected_clip_count?: number;
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
  quality?: QualitySnapshot | null;
}

export interface QualityBlocker extends UnknownRecord {
  code: string;
  severity: string;
  message: string;
  clip_ids?: string[];
}

export interface QualityFinding extends UnknownRecord {
  id: string;
  kind: string;
  classification?: string;
  severity: string;
  confidence?: number;
  channel?: number;
  source_time?: {
    start_seconds: number;
    end_seconds: number;
    duration_seconds: number;
  };
  edited_time?: {
    status: string;
    ranges: Array<{
      start_seconds: number;
      end_seconds: number;
      source_start_seconds: number;
      source_end_seconds: number;
    }>;
  };
  evidence?: UnknownRecord;
  preview?: {
    source?: string;
    grounded_fallback?: string;
  };
  resolution?: {
    status?: string;
    reviewed_by?: string;
    reviewed_at?: string;
    evidence?: { note?: string; output_revision?: string };
  };
  review?: AudioFindingReviewContext;
}

export interface InspectionRequest {
  method: 'GET';
  endpoint: string;
  query: {
    target: 'source' | 'longform' | 'short' | 'short_variant';
    clock: 'source' | 'output';
    seconds: number;
    duration_seconds: number;
    clip_id?: string;
    variant_id?: string;
  };
}

export interface ReviewOutputIdentity extends UnknownRecord {
  revision: string;
  render_fingerprint?: string;
  output_stat: { size_bytes: number; mtime_ns: number };
  completed_at?: string;
}

export interface AudioFindingReviewContext extends UnknownRecord {
  allowed: boolean;
  reason?: string | null;
  report_fingerprint: string;
  finding_fingerprint: string;
  output_revision?: string | null;
  inspection_request?: InspectionRequest | null;
  decision_endpoint: string;
}

export interface OutputContinuityFinding extends UnknownRecord {
  id: string;
  fingerprint?: string;
  revision?: string;
  role: string;
  clip_id?: string;
  kind?: string;
  severity?: string;
  artifact_time?: {
    clock: 'source' | 'output';
    start_seconds: number;
    end_seconds: number;
    duration_seconds?: number;
  };
  evidence?: { transcript_excerpt?: string } & UnknownRecord;
  inspection_request?: InspectionRequest;
}

export interface OutputFindingReviewEvent extends UnknownRecord {
  id: string;
  fingerprint: string;
  binding: {
    classification: string;
    source_ranges: Array<{ start_seconds: number; end_seconds: number }>;
    transcript_evidence: {
      speech_overlap_seconds: number;
      required_speech_overlap_seconds: number;
      transcript_word_count: number;
      transcript_excerpt: string;
    } & UnknownRecord;
  };
  members: Array<{
    id: string;
    fingerprint: string;
    revision: string;
    role: string;
    clip_id?: string;
  }>;
  resolution?: QualityFinding['resolution'];
  review: {
    allowed: boolean;
    reason?: string | null;
    report_fingerprint: string;
    event_fingerprint: string;
    output_revision?: string | null;
    inspection_request?: InspectionRequest | null;
    decision_endpoint: string;
  };
}

export interface OutputContinuityReport extends UnknownRecord {
  current?: boolean;
  status?: string;
  safe?: boolean;
  reviewable?: boolean;
  detail?: string;
  artifacts?: Array<{
    role?: string;
    clip_id?: string;
    status?: string;
    detail?: string;
    mechanically_verified?: boolean;
  } & UnknownRecord>;
  findings?: OutputContinuityFinding[];
  review_events?: OutputFindingReviewEvent[];
}

export interface MediaInspection extends UnknownRecord {
  target: 'source' | 'longform' | 'short' | 'short_variant';
  clip_id?: string | null;
  variant_id?: string | null;
  artifact: { current: true; fingerprint: string; duration_seconds: number };
  asset: {
    url: string;
    media_type: string;
    duration_seconds?: number;
    cached?: boolean;
  };
}

export interface AudioRepairCandidate extends UnknownRecord {
  status?: string;
  current: boolean;
  fingerprint?: string;
  verification_status?: string;
  repaired_finding_count: number;
  unresolved_finding_count: number;
  perceptual_review?: { status?: string; claim?: string };
  audio_url: string;
}

export interface QualitySnapshot extends UnknownRecord {
  episode_id: string;
  quality: {
    status: 'missing' | 'stale' | 'blocked' | 'passed';
    current_revision: string;
    report_revision?: string;
    generated_at?: string;
    overall?: string;
    checks: UnknownRecord[];
  };
  release_gate: {
    status: 'blocked' | 'awaiting_publish_approval' | 'ready';
    safe: boolean;
    can_approve_publish: boolean;
    revision: string;
    blockers: QualityBlocker[];
  };
  artifacts: {
    release_video: {
      ready: boolean;
      detail: string;
      download_url: string;
      review_output?: ReviewOutputIdentity | null;
    };
    legacy_longform: {
      available: boolean;
      review_url: string;
      release_candidate: false;
    };
    approved_short_count: number;
    candidate_count: number;
    rendered_short_count: number;
    rendered_short_ids: string[];
    pending_clip_count: number;
    missing_short_ids: string[];
  };
  audio_quality: {
    report_fingerprint?: string;
    release_gate: UnknownRecord;
    analysis: UnknownRecord;
    finding_count: number;
    findings: QualityFinding[];
    repair_candidate?: AudioRepairCandidate | null;
    repair_selection?: {
      status?: string;
      fingerprint?: string;
      release_safe?: boolean;
      detail?: string;
      repair_binding_status?: 'current' | 'partial' | 'stale' | null;
      stale_repaired_finding_count?: number;
    } | null;
    selected_master_output_continuity?: OutputContinuityReport;
  };
}

export interface ReviewArtifact extends UnknownRecord {
  status: 'missing' | 'untracked' | 'stale' | 'current';
  current: boolean;
  playable: boolean;
  path: string;
  url: string | null;
  download_url: string | null;
  reason_code: string | null;
  detail: string;
  fingerprint?: string;
  completed_at?: string;
}

export interface ReviewDestination extends UnknownRecord {
  key: string;
  label: string;
  required_fields: string[];
}

export interface ClipReviewState extends UnknownRecord {
  selection: { status: 'selected' | 'unselected' | 'rejected' };
  render: ReviewArtifact;
  approval: { status: string; current: boolean; revision: string };
  distribution: ClipDistributionState;
  metadata: {
    complete: boolean;
    enabled_destination_count: number;
    complete_destination_count: number;
    destinations: Array<
      ReviewDestination & { complete: boolean; missing_fields: string[] }
    >;
  };
  render_job: {
    status: 'idle' | 'rendering' | 'succeeded' | 'failed' | 'interrupted';
    started_at?: string;
    completed_at?: string;
    error?: string;
  };
  variants?: Record<string, ShortVariantReview>;
}

export interface ShortVariantReview extends UnknownRecord {
  id: string;
  label: string;
  asset_id: string;
  render: ReviewArtifact;
  approval: { status: string; current: boolean; revision: string };
  render_job: ClipReviewState['render_job'];
}

export interface ClipDistributionState extends UnknownRecord {
  version: 'base' | 'background_motion_v1' | 'invalid';
  variant_id: null | 'background_motion_v1';
  label: string;
  current: boolean;
  approval_current: boolean;
  revision: string;
}

export interface EpisodeReviewState extends UnknownRecord {
  schema: 'cascade.review/v1';
  episode_id: string;
  clock: 'source';
  enabled_destinations: ReviewDestination[];
  clip_summary: {
    candidate_count: number;
    selected_count: number;
    unselected_count: number;
    rejected_count: number;
  };
  longform: {
    render: ReviewArtifact;
    canonical_render: ReviewArtifact;
    legacy_render: ReviewArtifact;
    approval: { status: string; current: boolean; revision: string };
    source_preview_url: string;
  };
  clips: Array<UnknownRecord & { review: ClipReviewState }>;
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
  shorts_three_person_stack?: boolean;
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

export interface TranscriptSpeakerMapEntry extends UnknownRecord {
  index: number;
  label?: string;
  person?: string | null;
  crop_speaker_index?: number | null;
  target_speaker?: string;
  mapping_method?: string;
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
    render_mode?: string;
  };
  source_duration_seconds?: number;
  trim_start_seconds?: number;
  trim_end_seconds?: number;
  quality?: QualitySnapshot;
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
  quality: (id: string) =>
    request<QualitySnapshot>('GET', `/api/episodes/${id}/quality`),
  review: (id: string) =>
    request<EpisodeReviewState>('GET', `/api/episodes/${id}/review`),
  runQuality: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/run-agent/qa`, {}),
  inspectionPreview: (id: string, query: InspectionRequest['query']) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
    return request<MediaInspection>(
      'GET',
      `/api/episodes/${id}/inspection/preview?${params.toString()}`
    );
  },
  selectAudioRepairCandidate: (id: string) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/audio-qc/repair-candidate/select`
    ),
  clearAudioRepairSelection: (id: string) =>
    request<UnknownRecord>(
      'DELETE',
      `/api/episodes/${id}/audio-qc/repair-selection`
    ),
  reviewAudioFinding: (
    id: string,
    findingId: string,
    body: {
      decision: 'accepted' | 'false_positive';
      reviewer: string;
      evidence_note: string;
      expected_report_fingerprint: string;
      expected_finding_fingerprint: string;
      expected_output_revision: string;
    }
  ) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/audio-qc/findings/${findingId}/review`,
      body
    ),
  reviewAudioOutputFinding: (
    id: string,
    eventId: string,
    body: {
      decision: 'accepted' | 'false_positive';
      reviewer: string;
      evidence_note: string;
      expected_report_fingerprint: string;
      expected_event_fingerprint: string;
      expected_output_revision: string;
    }
  ) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/audio-qc/output-findings/${eventId}/review`,
      body
    ),

  /* Pipeline */
  pipelineStatus: (id: string) =>
    request<UnknownRecord>('GET', `/api/episodes/${id}/pipeline-status`),
  resumePipeline: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/resume-pipeline`),
  approveLongform: (
    id: string,
    body?: { continue_production?: boolean }
  ) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/approve-longform`,
      body
    ),
  approvePublish: (id: string, body?: { start_publication?: boolean }) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve-publish`, body),
  approveBackup: (id: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/approve-backup`),

  /* Clips */
  listClips: (id: string) =>
    request<UnknownRecord[]>('GET', `/api/episodes/${id}/clips/`),
  approveClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/approve`),
  selectClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/select`),
  renderClip: (id: string, clipId: string) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/${clipId}/render`),
  renderClipVariant: (
    id: string,
    clipId: string,
    variantId: string,
    assetId?: string
  ) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/clips/${clipId}/variants/${variantId}/render`,
      assetId ? { asset_id: assetId } : {}
    ),
  approveClipVariant: (
    id: string,
    clipId: string,
    variantId: string,
    expectedRevision: string
  ) =>
    request<UnknownRecord>(
      'POST',
      `/api/episodes/${id}/clips/${clipId}/variants/${variantId}/approve`,
      { expected_revision: expectedRevision }
    ),
  selectClipDistribution: (
    id: string,
    clipId: string,
    variantId: null | 'background_motion_v1',
    expectedRevision: string
  ) =>
    request<UnknownRecord>(
      'PUT',
      `/api/episodes/${id}/clips/${clipId}/distribution`,
      { variant_id: variantId, expected_revision: expectedRevision }
    ),
  approveClips: (id: string, clipIds: string[]) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/bulk/approve`, {
      clip_ids: clipIds,
    }),
  rejectClips: (id: string, clipIds: string[]) =>
    request<UnknownRecord>('POST', `/api/episodes/${id}/clips/bulk/reject`, {
      clip_ids: clipIds,
    }),
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
    speaker_map?: TranscriptSpeakerMapEntry[] | Record<string, string>;
  }> => request('GET', `/media/episodes/${id}/diarized_transcript.json`),

  /* Schedule */
  schedule: () => request<UnknownRecord>('GET', '/api/schedule'),
};
