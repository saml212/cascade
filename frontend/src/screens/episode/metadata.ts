/**
 * Episode metadata editor — guest info, episode title/description, tags,
 * longform platform URLs. PATCHes to /api/episodes/:id with the
 * EpisodeUpdateRequest shape.
 */

import { h } from '../../lib/dom';
import { signal, effect, type Signal } from '../../lib/signals';
import { api, type EpisodeUpdateRequest } from '../../lib/api';
import { Button } from '../../components/Button';
import { showToast } from '../../state/ui';

interface DraftState {
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
  saving: boolean;
  dirty: boolean;
}

export function renderMetadata(
  target: HTMLElement,
  ep: Record<string, unknown>,
  episodeId: string
): void {
  const draft = signal<DraftState>({
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
    saving: false,
    dirty: false,
  });

  async function save(): Promise<void> {
    const d = draft.peek();
    draft.set({ ...d, saving: true });
    const payload: EpisodeUpdateRequest = {
      guest_name: d.guest_name.trim(),
      guest_title: d.guest_title.trim(),
      episode_name: d.episode_name.trim(),
      episode_description: d.episode_description.trim(),
      title: d.title.trim(),
      description: d.description.trim(),
      tags: d.tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
      youtube_longform_url: d.youtube_longform_url.trim(),
      spotify_longform_url: d.spotify_longform_url.trim(),
      link_tree_url: d.link_tree_url.trim(),
    };
    try {
      await api.updateEpisode(episodeId, payload);
      draft.set({ ...draft.peek(), saving: false, dirty: false });
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
      { class: 'grid grid-cols-2 gap-6 pb-24' },
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5' },
        sectionHeader('Guest'),
        fieldText(draft, 'guest_name', 'Name'),
        fieldText(draft, 'guest_title', 'Title / role')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5' },
        sectionHeader('Episode'),
        fieldText(draft, 'episode_name', 'Short name'),
        fieldTextarea(draft, 'episode_description', 'Short description')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 col-span-2' },
        sectionHeader('Longform'),
        fieldText(draft, 'title', 'Full title (YouTube / Spotify)'),
        fieldTextarea(draft, 'description', 'Full description', 6),
        fieldText(draft, 'tags', 'Tags', 'comma-separated')
      ),
      h(
        'section',
        { class: 'panel p-6 flex flex-col gap-5 col-span-2' },
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
  name: keyof DraftState,
  label: string,
  hint?: string
): HTMLElement {
  const initial = (draft.peek() as unknown as Record<string, unknown>)[name] as string;
  const input = h('input', {
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
      } as DraftState);
    },
  }) as HTMLInputElement;
  return h(
    'div',
    { class: 'flex flex-col gap-1.5' },
    h(
      'label',
      { class: 'text-body-sm text-ink-secondary font-medium' },
      label
    ),
    input,
    hint ? h('p', { class: 'text-body-sm text-ink-tertiary' }, hint) : null
  );
}

function fieldTextarea(
  draft: Signal<DraftState>,
  name: keyof DraftState,
  label: string,
  rows = 3
): HTMLElement {
  const initial = (draft.peek() as unknown as Record<string, unknown>)[name] as string;
  const input = h('textarea', {
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
      } as DraftState);
    },
  }) as HTMLTextAreaElement;
  return h(
    'div',
    { class: 'flex flex-col gap-1.5' },
    h(
      'label',
      { class: 'text-body-sm text-ink-secondary font-medium' },
      label
    ),
    input
  );
}
