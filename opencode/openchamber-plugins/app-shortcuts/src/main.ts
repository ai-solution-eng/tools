import { connectHost } from '@openchamber/sdk';
import { applyHostReady } from '@openchamber/sdk/ui';

/**
 * The guest frame is served from `<app base>/api/guests/<id>/panel/index.html`.
 * Everything before `/api/guests/` is the browser-visible OpenChamber base, so
 * the sibling pages resolve the same way they do from the app itself, whatever
 * path prefix a deployment puts in front of it.
 */
const GUEST_ASSET_MARKER = '/api/guests/';

type ShortcutKind = 'page' | 'surface';

type Shortcut = {
  id: string;
  kind: ShortcutKind;
  target: string;
};

const SHORTCUTS: Shortcut[] = [
  { id: 'terminal', kind: 'page', target: 'terminal' },
  { id: 'data-manager', kind: 'page', target: 'data-manager' },
  { id: 'account', kind: 'page', target: '' },
  { id: 'in-app-terminal', kind: 'surface', target: 'terminal' },
  { id: 'in-app-files', kind: 'surface', target: 'file' },
];

/** The browser-visible OpenChamber base, or null outside a served instance. */
const appBase = (): string | null => {
  const href = window.location.href;
  const markerAt = href.indexOf(GUEST_ASSET_MARKER);
  if (markerAt > 0) {
    const base = href.slice(0, markerAt);
    return base.endsWith('/') ? base : `${base}/`;
  }
  try {
    const url = new URL(href);
    if (url.protocol === 'http:' || url.protocol === 'https:') {
      return `${url.origin}/`;
    }
  } catch {
    // about:srcdoc in relay mode has no addressable base.
  }
  return null;
};

const pageUrl = (target: string): string | null => {
  const base = appBase();
  if (!base) return null;
  try {
    // An empty target is the host root: the router always keeps `/` for the
    // login / account page, whatever path prefix the app itself is served at.
    return target === '' ? new URL('/', base).href : new URL(target, base).href;
  } catch {
    return null;
  }
};

const showDiagnostic = (message: string): void => {
  const node = document.querySelector<HTMLElement>('#diagnostic');
  if (!node) return;
  node.hidden = false;
  node.textContent = message;
};

const openStandalone = (url: string): void => {
  try {
    window.open(url, '_blank', 'noopener,noreferrer');
  } catch {
    showDiagnostic(`Open ${url} manually.`);
  }
};

const main = (): void => {
  const root = document.querySelector<HTMLElement>('#root');
  if (!root) return;

  // A sandboxed guest frame always has a distinct parent. Same-window means the
  // page was opened on its own, where the host bridge has nothing to talk to.
  const standalone = window.parent === window;
  if (standalone) {
    const notice = document.querySelector<HTMLElement>('#standalone');
    if (notice) notice.hidden = false;
  }

  let host: ReturnType<typeof connectHost> | null = null;
  if (!standalone) {
    try {
      host = connectHost();
    } catch (error) {
      showDiagnostic(error instanceof Error ? error.message : 'Could not connect to OpenChamber.');
    }
  }

  const open = (shortcut: Shortcut): void => {
    if (shortcut.kind === 'surface') {
      if (!host) {
        showDiagnostic('In-app panels are only available inside OpenChamber.');
        return;
      }
      void host.openSurface(shortcut.target).catch((error: unknown) => {
        showDiagnostic(error instanceof Error ? error.message : 'Could not open that panel.');
      });
      return;
    }

    const url = pageUrl(shortcut.target);
    if (!url) {
      showDiagnostic('Could not resolve this environment\u2019s address.');
      return;
    }
    if (!host) {
      openStandalone(url);
      return;
    }
    void host.openUrl(url).catch(() => {
      // The host refuses a popup it cannot attribute to the click; the same
      // URL is still valid as a plain navigation from here.
      openStandalone(url);
    });
  };

  for (const button of root.querySelectorAll<HTMLButtonElement>('.row')) {
    const id = button.dataset.id ?? '';
    const shortcut = SHORTCUTS.find((candidate) => candidate.id === id);
    if (!shortcut) continue;
    button.addEventListener('click', () => open(shortcut));
  }

  if (!host) return;

  let connected = false;
  host.onReady((context) => {
    connected = true;
    if (context.surface === 'background') return;
    applyHostReady(context, document.documentElement);
    document.documentElement.dataset.ocConnected = 'true';
  });

  window.setTimeout(() => {
    if (connected) return;
    showDiagnostic(`No handshake from OpenChamber after 2.5s (frame: ${window.location.href.slice(0, 160)}).`);
  }, 2500);
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main, { once: true });
} else {
  main();
}
