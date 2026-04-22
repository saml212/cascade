import { signal, effect } from '../lib/signals';

const AGENT_PANEL_KEY = 'cascade.agent-panel.collapsed';

export const agentPanelCollapsed = signal<boolean>(
  localStorage.getItem(AGENT_PANEL_KEY) === '1'
);

effect(() => {
  localStorage.setItem(AGENT_PANEL_KEY, agentPanelCollapsed() ? '1' : '0');
});

export function toggleAgentPanel(): void {
  agentPanelCollapsed.set((v) => !v);
}

export const toast = signal<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
  null
);

let toastTimer: number | null = null;

export function showToast(
  message: string,
  tone: 'info' | 'error' | 'success' = 'info'
): void {
  toast.set({ message, tone });
  if (toastTimer != null) clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.set(null), 4500);
}
