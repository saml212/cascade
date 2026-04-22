import { h, mount } from '../lib/dom';
import { signal, effect } from '../lib/signals';
import { Button } from '../components/Button';
import { api } from '../lib/api';
import { navigate } from '../lib/router';
import { showToast } from '../state/ui';

export function NewEpisode(target: HTMLElement): void {
  const sourcePath = signal<string>('');
  const audioPath = signal<string>('');
  const speakerCount = signal<number>(2);
  const submitting = signal<boolean>(false);
  const error = signal<string | null>(null);

  async function submit(): Promise<void> {
    if (!sourcePath().trim()) {
      error.set('Give cascade a source path (DJI SD card or archive folder).');
      return;
    }
    submitting.set(true);
    error.set(null);
    try {
      const ep = (await api.createEpisode({
        source_path: sourcePath().trim(),
        audio_path: audioPath().trim() || undefined,
        speaker_count: speakerCount(),
      })) as Record<string, unknown>;
      const id = ep.episode_id as string;
      showToast('Episode created — pipeline starting.', 'success');
      navigate(`/episodes/${id}`);
    } catch (e) {
      error.set((e as Error).message);
    } finally {
      submitting.set(false);
    }
  }

  const sourceInput = h('input', {
    type: 'text',
    placeholder: '/Volumes/CAMERA/DCIM/DJI_001',
    class: inputClass,
    value: sourcePath(),
    oninput: (e: Event) => sourcePath.set((e.target as HTMLInputElement).value),
  }) as HTMLInputElement;

  const audioInput = h('input', {
    type: 'text',
    placeholder: '/Volumes/ZOOM_H6E/…',
    class: inputClass,
    value: audioPath(),
    oninput: (e: Event) => audioPath.set((e.target as HTMLInputElement).value),
  }) as HTMLInputElement;

  const speakerControls = h('div', {
    class: 'flex gap-2',
  });

  effect(() => {
    const sel = speakerCount();
    speakerControls.replaceChildren(
      ...[2, 3, 4].map((n) =>
        h(
          'button',
          {
            onclick: () => speakerCount.set(n),
            class: [
              'px-4 h-11 rounded-md border text-body font-medium transition-colors duration-[120ms]',
              sel === n
                ? 'bg-accent text-ink-on-accent border-transparent'
                : 'bg-surface-2 text-ink-primary border-border hover:bg-surface-3',
            ].join(' '),
          },
          `${n}`
        )
      )
    );
  });

  const actions = h('div', { class: 'flex gap-3 mt-2' });
  const errorEl = h('div', {
    class: 'text-body-sm text-status-danger mt-2 hidden',
  });

  effect(() => {
    const e = error();
    if (e) {
      errorEl.textContent = e;
      errorEl.classList.remove('hidden');
    } else {
      errorEl.classList.add('hidden');
    }
  });

  effect(() => {
    actions.replaceChildren(
      Button({
        variant: 'primary',
        size: 'lg',
        label: submitting() ? 'Starting pipeline…' : 'Start pipeline',
        loading: submitting(),
        onClick: submit,
      }),
      Button({
        variant: 'ghost',
        size: 'lg',
        label: 'Cancel',
        onClick: () => navigate('/'),
      })
    );
  });

  mount(
    target,
    h(
      'div',
      { class: 'max-w-[720px] mx-auto px-10 py-16' },
      h(
        'h1',
        { class: 'font-display text-display-xl text-ink-primary mb-3' },
        'New episode.'
      ),
      h(
        'p',
        { class: 'text-body-lg text-ink-secondary mb-10' },
        'Point cascade at a source folder. Add H6E audio separately if you recorded 3+ speakers. Pipeline runs until it reaches crop setup.'
      ),
      formRow(
        'Source path',
        'Where the DJI footage lives — SD card or archive folder.',
        sourceInput
      ),
      formRow(
        'Audio path (optional)',
        'Zoom H6E folder. Leave blank for 2-speaker Canon episodes.',
        audioInput
      ),
      formRow('Speaker count', 'Includes the host.', speakerControls),
      actions,
      errorEl
    )
  );
}

const inputClass =
  'w-full h-11 bg-surface-2 border border-border rounded-md px-4 text-body text-ink-primary placeholder:text-ink-disabled focus:border-accent focus:outline-none font-mono tabular';

function formRow(label: string, hint: string, control: HTMLElement): HTMLElement {
  return h(
    'div',
    { class: 'mb-8' },
    h(
      'label',
      { class: 'block text-heading-sm uppercase text-ink-tertiary mb-2' },
      label
    ),
    control,
    h('p', { class: 'text-body-sm text-ink-tertiary mt-2' }, hint)
  );
}
