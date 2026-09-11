/**
 * Episode metadata editor — guest info, episode title/description, tags,
 * longform platform URLs. PATCHes to /api/episodes/:id with the
 * EpisodeUpdateRequest shape.
 */

import { h } from '../../lib/dom';
import { signal, effect, type Signal } from '../../lib/signals';
import { api } from '../../lib/api';
import { Button } from '../../components/Button';
import { showToast } from '../../state/ui';
import {
  acceptSavedPatch,
  buildMetadataPatch,
  metadataValues,
  type MetadataField,
  type MetadataValues,
} from '../../lib/metadata-draft';

interface DraftState extends MetadataValues {
  saving: boolean;
  dirty: boolean;
  revision: number;
}

export function renderMetadata(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  let saved = metadataValues(ep);
  const draft = signal<DraftState>({
    ...saved,
    saving: false,
    dirty: false,
    revision: 0,
  });

  async function save(): Promise<void> {
    const d = draft.peek();
    const submitted = valuesOf(d);
    const payload = buildMetadataPatch(saved, submitted);
    const fields = Object.keys(payload) as MetadataField[];
    if (fields.length === 0) {
      draft.set({ ...d, dirty: false });
      return;
    }
    const submittedVersion = d.revision;
    draft.set({ ...d, saving: true });
    try {
      await api.updateEpisode(episodeId, payload);
      saved = acceptSavedPatch(saved, submitted, fields);
      const current = draft.peek();
      const changedDuringSave = current.revision !== submittedVersion;
      const stillChanged = Object.keys(buildMetadataPatch(saved, valuesOf(current))).length > 0;
      draft.set({
        ...current,
        saving: false,
        dirty: changedDuringSave || stillChanged,
      });
      showToast('Metadata saved.', 'success');
    } catch (e) {
      draft.set({ ...draft.peek(), saving: false });
      showToast((e as Error).message, 'error');
    }
  }

  const saveBar = h('div');
  effect(() => {
    const d = draft();
    saveBar.replaceChildren(
      d.dirty
        ? h(
            'p',
            {
              class: 'text-body-sm text-status-warning',
            },
            'Unsaved changes.'
          )
        : h(
            'p',
            { class: 'text-body-sm text-ink-tertiary' },
            'Changes save only when you hit Save.'
          ),
      h('div', { class: 'flex-1' }),
      Button({
        variant: 'primary',
        size: 'md',
        label: d.saving ? 'Saving…' : 'Save metadata',
        loading: d.saving,
        disabled: !d.dirty || d.saving,
        onClick: save,
      })
    );
  });

  target.replaceChildren(
    h(
      'div',
      { class: 'grid grid-cols-2 gap-6 pb-32' },
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 min-w-0' },
        sectionHeader('Guest'),
        fieldText(draft, 'guest_name', 'Name'),
        fieldText(draft, 'guest_title', 'Title / role')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 min-w-0' },
        sectionHeader('Episode'),
        fieldText(draft, 'episode_name', 'Short name'),
        fieldTextarea(draft, 'episode_description', 'Short description')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 col-span-2 min-w-0' },
        sectionHeader('Longform'),
        fieldText(draft, 'title', 'Full title (YouTube / Spotify)'),
        fieldTextarea(draft, 'description', 'Full description', 6),
        fieldText(draft, 'tags', 'Tags', 'comma-separated')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 col-span-2 min-w-0' },
        sectionHeader('Links'),
        fieldText(
          draft,
          'youtube_longform_url',
          'YouTube longform URL',
          'Pasted automatically after YouTube processing'
        ),
        fieldText(
          draft,
          'spotify_longform_url',
          'Spotify longform URL'
        ),
        fieldText(draft, 'link_tree_url', 'Link tree')
      )
    ),
    h(
      'div',
      {
        class:
          'sticky bottom-0 -mx-10 px-10 py-4 bg-canvas/95 backdrop-blur-md border-t border-border-subtle flex items-center gap-4',
      },
      saveBar
    )
  );
}

function sectionHeader(label: string): HTMLElement {
  return h(
    'h3',
    { class: 'text-heading-sm uppercase text-ink-tertiary' },
    label
  );
}

function fieldText(
  draft: Signal<DraftState>,
  name: MetadataField,
  label: string,
  hint?: string
): HTMLElement {
  const initial = (draft.peek() as unknown as Record<string, unknown>)[name] as string;
  const input = h('input', {
    id: `metadata-${name}`,
    type: 'text',
    value: initial,
    class:
      'w-full h-10 bg-surface-2 border border-border rounded-md px-3 text-body text-ink-primary focus:border-accent focus:outline-none',
    oninput: (e: Event) => {
      const v = (e.target as HTMLInputElement).value;
      draft.set({
        ...draft.peek(),
        [name]: v,
        dirty: true,
        revision: draft.peek().revision + 1,
      } as DraftState);
    },
  }) as HTMLInputElement;
  return h(
    'div',
    { class: 'flex flex-col gap-1.5 min-w-0' },
    h(
      'label',
      { for: `metadata-${name}`, class: 'text-body-sm text-ink-secondary font-medium' },
      label
    ),
    input,
    hint ? h('p', { class: 'text-body-sm text-ink-tertiary' }, hint) : null
  );
}

function fieldTextarea(
  draft: Signal<DraftState>,
  name: MetadataField,
  label: string,
  rows = 3
): HTMLElement {
  const initial = (draft.peek() as unknown as Record<string, unknown>)[name] as string;
  const input = h('textarea', {
    id: `metadata-${name}`,
    class:
      'w-full bg-surface-2 border border-border rounded-md px-3 py-2 text-body text-ink-primary leading-relaxed focus:border-accent focus:outline-none resize-vertical',
    rows: String(rows),
    value: initial,
    oninput: (e: Event) => {
      const v = (e.target as HTMLTextAreaElement).value;
      draft.set({
        ...draft.peek(),
        [name]: v,
        dirty: true,
        revision: draft.peek().revision + 1,
      } as DraftState);
    },
  }) as HTMLTextAreaElement;
  return h(
    'div',
    { class: 'flex flex-col gap-1.5' },
    h(
      'label',
      { for: `metadata-${name}`, class: 'text-body-sm text-ink-secondary font-medium' },
      label
    ),
    input
  );
}

function valuesOf(draft: DraftState): MetadataValues {
  const { saving: _saving, dirty: _dirty, revision: _revision, ...values } = draft;
  return values;
}
