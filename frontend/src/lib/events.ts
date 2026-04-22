/**
 * Pipeline event stream. Poll-backed today; SSE-ready when the backend
 * exposes /api/episodes/:id/events.
 *
 * Callers use subscribe(episodeId, onEvent). The implementation behind the
 * interface can be swapped file-local without touching call sites.
 */

import { api } from './api';
import { describeAgent } from './format';

export interface PipelineEvent {
  at: number;
  kind: 'status' | 'agent_start' | 'agent_done' | 'agent_error' | 'tick' | 'idle';
  label: string;
  detail?: string;
  status?: string;
  agent?: string | null;
}

type Unsubscribe = () => void;

const POLL_INTERVAL_MS = 3000;

interface Subscription {
  episodeId: string;
  listeners: Set<(e: PipelineEvent) => void>;
  timer: number | null;
  lastAgent: string | null;
  lastStatus: string | null;
  lastCompletedCount: number;
}

const subs = new Map<string, Subscription>();

export function subscribe(
  episodeId: string,
  onEvent: (e: PipelineEvent) => void
): Unsubscribe {
  let sub = subs.get(episodeId);
  if (!sub) {
    sub = {
      episodeId,
      listeners: new Set(),
      timer: null,
      lastAgent: null,
      lastStatus: null,
      lastCompletedCount: -1,
    };
    subs.set(episodeId, sub);
    startPolling(sub);
  }
  sub.listeners.add(onEvent);
  return () => {
    sub!.listeners.delete(onEvent);
    if (sub!.listeners.size === 0) {
      if (sub!.timer != null) clearInterval(sub!.timer);
      subs.delete(episodeId);
    }
  };
}

function emit(sub: Subscription, e: PipelineEvent): void {
  for (const fn of sub.listeners) fn(e);
}

async function tick(sub: Subscription): Promise<void> {
  let s: Record<string, unknown>;
  try {
    s = await api.pipelineStatus(sub.episodeId);
  } catch {
    return;
  }
  const status = (s.status as string) ?? null;
  const currentAgent = (s.current_agent as string | null) ?? null;
  const completed = (s.agents_completed as string[]) ?? [];
  const errors = (s.errors as Record<string, string>) ?? {};

  if (status && status !== sub.lastStatus) {
    emit(sub, {
      at: Date.now(),
      kind: 'status',
      label: `Status: ${status}`,
      status,
    });
    sub.lastStatus = status;
  }

  if (currentAgent !== sub.lastAgent) {
    if (sub.lastAgent) {
      emit(sub, {
        at: Date.now(),
        kind: 'agent_done',
        label: `${describeAgent(sub.lastAgent)} — done`,
        agent: sub.lastAgent,
      });
    }
    if (currentAgent) {
      emit(sub, {
        at: Date.now(),
        kind: 'agent_start',
        label: `${describeAgent(currentAgent)}`,
        agent: currentAgent,
      });
    }
    sub.lastAgent = currentAgent;
  }

  if (completed.length !== sub.lastCompletedCount) {
    sub.lastCompletedCount = completed.length;
  }

  for (const [agent, msg] of Object.entries(errors)) {
    emit(sub, {
      at: Date.now(),
      kind: 'agent_error',
      label: `${describeAgent(agent)} failed`,
      detail: msg,
      agent,
    });
  }
}

function startPolling(sub: Subscription): void {
  tick(sub);
  sub.timer = window.setInterval(() => tick(sub), POLL_INTERVAL_MS);
}
