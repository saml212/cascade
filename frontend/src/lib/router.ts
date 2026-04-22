import { signal } from './signals';

type Params = Record<string, string>;
type Handler = (params: Params) => void;
type Route = { keys: string[]; pattern: RegExp; handler: Handler };

const routes: Route[] = [];
let fallback: Handler | null = null;

export const currentPath = signal<string>(readPath());

function readPath(): string {
  const h = window.location.hash.slice(1);
  return h || '/';
}

export function route(pattern: string, handler: Handler): void {
  const keys: string[] = [];
  const source = pattern.replace(/:([a-zA-Z_][a-zA-Z0-9_]*)/g, (_, k) => {
    keys.push(k);
    return '([^/]+)';
  });
  routes.push({ keys, pattern: new RegExp('^' + source + '/?$'), handler });
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
  currentPath.set(path);
  for (const r of routes) {
    const m = path.match(r.pattern);
    if (m) {
      const params: Params = {};
      r.keys.forEach((k, i) => {
        params[k] = decodeURIComponent(m[i + 1]);
      });
      r.handler(params);
      return;
    }
  }
  fallback?.({});
}

export function startRouter(): void {
  window.addEventListener('hashchange', dispatch);
  dispatch();
}
