import { signal, effect } from '../lib/signals';

const AGENT_PANEL_KEY = 'cascade.agent-panel.collapsed';
const compactQuery = window.matchMedia('(max-width: 1199px)');
const savedAgentPanelState = localStorage.getItem(AGENT_PANEL_KEY);

export const agentPanelCollapsed = signal<boolean>(
  savedAgentPanelState == null ? compactQuery.matches : savedAgentPanelState === '1'
);

export const compactShell = signal<boolean>(compactQuery.matches);

compactQuery.addEventListener('change', (event) => {
  compactShell.set(event.matches);
  // Entering the compact layout should restore the workspace width. The user
  // can still expand the feed, where it opens over the page instead.
  if (event.matches) agentPanelCollapsed.set(true);
});

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
