import { publicationEvidenceStatusLabel } from '../lib/clip-review-surface';
import { h } from '../lib/dom';
import type { EpisodePublicationEvidence } from '../lib/episode-release';

export function PublicationEvidenceLink(
  record: EpisodePublicationEvidence,
  content: string
): HTMLElement {
  const destinations =
    record.destinations ??
    (record.destination ? [record.destination] : ['unknown destination']);
  const destinationLabel = destinations
    .map((value) => value.replaceAll('_', ' '))
    .join(', ');
  const status = publicationEvidenceStatusLabel(
    record.status,
    record.scheduled
  );
  const label = h(
    'span',
    { class: 'text-body-sm text-ink-secondary' },
    `${content} · ${destinationLabel} · ${status}${
      record.job_id
        ? ` · Job ${record.job_id}`
        : record.request_id
          ? ` · Request ${record.request_id}`
          : ''
    }${record.error ? ` · ${record.error}` : ''}`
  );
  const safeUrl =
    record.url?.startsWith('https://') || record.url?.startsWith('http://')
      ? record.url
      : null;
  return safeUrl
    ? h(
        'a',
        {
          href: safeUrl,
          target: '_blank',
          rel: 'noreferrer',
          class: 'hover:text-accent',
        },
        label
      )
    : label;
}
