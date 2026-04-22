/**
 * Thin DOM builder. Not a framework — just ergonomic element creation with
 * property setting, event binding, and child composition. Paired with the
 * signals primitive, this gives us a reactive UI in <200 lines total.
 */

type Child = Node | string | number | null | undefined | false | Child[];
type Props = Record<string, unknown>;

function applyProp(el: Element, key: string, value: unknown): void {
  if (value == null || value === false) return;

  if (key === 'class' || key === 'className') {
    el.setAttribute('class', String(value));
    return;
  }
  if (key === 'style' && typeof value === 'object') {
    Object.assign((el as HTMLElement).style, value as Record<string, string>);
    return;
  }
  if (key === 'dataset' && typeof value === 'object') {
    Object.assign((el as HTMLElement).dataset, value as Record<string, string>);
    return;
  }
  if (key === 'ref' && typeof value === 'function') {
    (value as (el: Element) => void)(el);
    return;
  }
  if (key.startsWith('on') && typeof value === 'function') {
    el.addEventListener(
      key.slice(2).toLowerCase(),
      value as EventListener
    );
    return;
  }
  if (key === 'html') {
    (el as HTMLElement).innerHTML = String(value);
    return;
  }

  // Boolean attributes (disabled, hidden, etc.) via DOM property when possible
  if (typeof value === 'boolean') {
    if (value) el.setAttribute(key, '');
    else el.removeAttribute(key);
    return;
  }
  el.setAttribute(key, String(value));
}

function appendChild(parent: Node, child: Child): void {
  if (child == null || child === false) return;
  if (Array.isArray(child)) {
    for (const c of child) appendChild(parent, c);
    return;
  }
  if (child instanceof Node) {
    parent.appendChild(child);
    return;
  }
  parent.appendChild(document.createTextNode(String(child)));
}

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props?: Props | null,
  ...children: Child[]
): HTMLElementTagNameMap[K];
export function h(tag: string, props?: Props | null, ...children: Child[]): HTMLElement;
export function h(tag: string, props?: Props | null, ...children: Child[]): HTMLElement {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, val] of Object.entries(props)) applyProp(el, key, val);
  }
  for (const c of children) appendChild(el, c);
  return el;
}

export function svg(tag: string, props?: Props | null, ...children: Child[]): SVGElement {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  if (props) {
    for (const [key, val] of Object.entries(props)) {
      if (val == null || val === false) continue;
      if (key.startsWith('on') && typeof val === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), val as EventListener);
      } else {
        el.setAttribute(key, String(val));
      }
    }
  }
  for (const c of children) appendChild(el, c);
  return el;
}

export function mount(target: Element, node: Node): void {
  target.replaceChildren(node);
}

export function empty(el: Element): void {
  el.replaceChildren();
}
