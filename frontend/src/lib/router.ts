import { signal, effectScope } from './signals';

type Params = Record<string, string>;
type Handler = (params: Params) => void;
type RouteOptions = {
  screenIdentity?: (params: Params) => string;
};
type Route = {
  keys: string[];
  pattern: RegExp;
  handler: Handler;
  screenIdentity?: (params: Params) => string;
};
type Match = { handler: Handler; params: Params; screenIdentity: string | null };

const routes: Route[] = [];
let fallback: Handler | null = null;
let disposeScreen: (() => void) | null = null;
let activeScreenIdentity: string | null = null;

export const currentPath = signal<string>(readPath());

function readPath(): string {
  const h = window.location.hash.slice(1);
  return h || '/';
}

export function route(
  pattern: string,
  handler: Handler,
  options: RouteOptions = {}
): void {
  const keys: string[] = [];
  const source = pattern.replace(/:([a-zA-Z_][a-zA-Z0-9_]*)/g, (_, k) => {
    keys.push(k);
    return '([^/]+)';
  });
  routes.push({
    keys,
    pattern: new RegExp('^' + source + '/?$'),
    handler,
    screenIdentity: options.screenIdentity,
  });
}

export function setFallback(handler: Handler): void {
  fallback = handler;
}

export function navigate(path: string): void {
  if (path === readPath()) {
    dispatch();
    return;
  }
  window.location.hash = '#' + path;
}

export function link(path: string): { href: string; onclick: (e: Event) => void } {
  return {
    href: '#' + path,
    onclick: (e) => {
      e.preventDefault();
      navigate(path);
    },
  };
}

function dispatch(): void {
  const path = readPath();
  const match = matchRoute(path);
  if (match.screenIdentity && match.screenIdentity === activeScreenIdentity) {
    currentPath.set(path);
    return;
  }

  disposeScreen?.();
  disposeScreen = null;
  activeScreenIdentity = match.screenIdentity;
  currentPath.set(path);
  disposeScreen = effectScope(() => match.handler(match.params));
}

function matchRoute(path: string): Match {
  for (const r of routes) {
    const m = path.match(r.pattern);
    if (m) {
      const params: Params = {};
      r.keys.forEach((k, i) => {
        params[k] = decodeURIComponent(m[i + 1]);
      });
      return {
        handler: r.handler,
        params,
        screenIdentity: r.screenIdentity?.(params) ?? null,
      };
    }
  }
  return {
    handler: fallback ?? (() => {}),
    params: {},
    screenIdentity: null,
  };
}

export function startRouter(): void {
  window.addEventListener('hashchange', dispatch);
  dispatch();
}
