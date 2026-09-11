import type { EpisodeUpdateRequest, UnknownRecord } from './api';

export interface MetadataValues {
  guest_name: string;
  guest_title: string;
  episode_name: string;
  episode_description: string;
  title: string;
  description: string;
  tags: string;
  youtube_longform_url: string;
  spotify_longform_url: string;
  link_tree_url: string;
}

export type MetadataField = keyof MetadataValues;

export function metadataValues(ep: UnknownRecord): MetadataValues {
  return {
    guest_name: (ep.guest_name as string) ?? '',
    guest_title: (ep.guest_title as string) ?? '',
    episode_name: (ep.episode_name as string) ?? '',
    episode_description: (ep.episode_description as string) ?? '',
    title: (ep.title as string) ?? '',
    description: (ep.description as string) ?? '',
    tags: ((ep.tags as string[]) ?? []).join(', '),
    youtube_longform_url: (ep.youtube_longform_url as string) ?? '',
    spotify_longform_url: (ep.spotify_longform_url as string) ?? '',
    link_tree_url: (ep.link_tree_url as string) ?? '',
  };
}

function normalized(field: MetadataField, value: string): string | string[] {
  if (field === 'tags') {
    return value
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean);
  }
  return value.trim();
}

export function buildMetadataPatch(
  saved: MetadataValues,
  draft: MetadataValues
): EpisodeUpdateRequest {
  const patch: EpisodeUpdateRequest = {};
  for (const field of Object.keys(saved) as MetadataField[]) {
    const before = normalized(field, saved[field]);
    const after = normalized(field, draft[field]);
    if (JSON.stringify(before) !== JSON.stringify(after)) {
      Object.assign(patch, { [field]: after });
    }
  }
  return patch;
}

export function acceptSavedPatch(
  saved: MetadataValues,
  submitted: MetadataValues,
  fields: MetadataField[]
): MetadataValues {
  const next = { ...saved };
  for (const field of fields) next[field] = submitted[field];
  return next;
}
