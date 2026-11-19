/**
 * Offline check for the browser half. No network, no running OpenChamber:
 * jsdom loads the shipped `panel/main.js` in a fake guest frame, answers the
 * SDK handshake, clicks every row, and asserts the requests the host would
 * receive.
 *
 *   npm install && npm run check
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Test the generated page, not the intermediate bundle: the shipped entry has
// the script inlined (see build.mjs), and that is what runs in the frame.
const page = readFileSync(path.join(root, 'panel/index.html'), 'utf8');
const bundle = page.match(/<script data-openchamber-bundle>([\s\S]*?)<\/script>/i);
assert.ok(bundle, 'panel/index.html carries the inlined bundle');
const script = bundle[1];
const html = page.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');

const CHANNEL = 'openchamber.sdk';
const TOKENS = {
  background: '#111111', elevated: '#1a1a1a', foreground: '#eeeeee', muted: '#999999',
  subtle: '#222222', border: '#333333', hover: '#2a2a2a', active: '#303030',
  selection: '#264f78', selectionForeground: '#ffffff', focus: '#4c8dff', primary: '#4c8dff',
  primaryForeground: '#ffffff', primaryText: '#8ab4ff', successText: '#7ee787',
  warningText: '#f0b849', errorText: '#ff7b72', infoText: '#79c0ff', success: '#2ea043',
  warning: '#d29922', error: '#f85149', info: '#58a6ff', mutedSurface: '#161616',
  elevatedForeground: '#f5f5f5', font: 'system-ui', mono: 'monospace', radius: '8px',
};

const openFrame = ({ href, sandboxed = true, answer = true }) => {
  const dom = new JSDOM(html, {
    url: href,
    runScripts: 'outside-only',
  });
  const { window } = dom;
  const sent = [];

  const post = (message) => {
    window.dispatchEvent(new window.MessageEvent('message', { data: message, source: parent }));
  };

  const parent = {
    postMessage(message) {
      sent.push(message);
      if (message.type === 'hello' && answer) {
        setTimeout(() => post({
          channel: CHANNEL,
          v: 1,
          type: 'ready',
          payload: {
            theme: { mode: 'dark', tokens: TOKENS },
            locale: 'en',
            directory: null,
            session: null,
            surface: 'panel',
            connection: { connected: true, account: null, hasClient: true, settings: {} },
            settings: {},
            item: null,
          },
        }), 0);
        return;
      }
      if (message.type === 'open-url' || message.type === 'open-surface' || message.type === 'toast') {
        setTimeout(() => post({ channel: CHANNEL, v: 1, type: 'result', id: message.id, ok: true }), 0);
      }
    },
  };

  Object.defineProperty(window, 'parent', { value: sandboxed ? parent : window, configurable: true });
  window.eval(script);

  const waitFor = async (predicate, timeoutMs = 1500) => {
    const started = Date.now();
    while (!predicate()) {
      if (Date.now() - started > timeoutMs) return false;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    return true;
  };

  return {
    window,
    sent,
    waitFor,
    flush: () => new Promise((resolve) => setTimeout(resolve, 50)),
    click: (id) => {
      const button = window.document.querySelector(`.row[data-id="${id}"]`);
      assert.ok(button, `missing row ${id}`);
      button.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    },
    requests: () => sent.filter((message) => message.type !== 'hello').map((message) => (
      message.type === 'open-url'
        ? `open-url ${message.payload.url}`
        : `${message.type} ${message.payload?.surfaceId ?? ''}`.trim()
    )),
  };
};

const cases = [
  {
    label: 'served from the app root',
    href: 'https://opencode.example.com/api/guests/app-shortcuts/panel/index.html?oc_url_token=t',
    expected: [
      'open-url https://opencode.example.com/terminal',
      'open-url https://opencode.example.com/data-manager',
      'open-url https://opencode.example.com/',
      'open-surface terminal',
      'open-surface file',
    ],
  },
  {
    label: 'served under a user path prefix',
    href: 'https://opencode.example.com/francesco-caliva/api/guests/app-shortcuts/panel/index.html?oc_url_token=t',
    expected: [
      'open-url https://opencode.example.com/francesco-caliva/terminal',
      'open-url https://opencode.example.com/francesco-caliva/data-manager',
      'open-url https://opencode.example.com/',
      'open-surface terminal',
      'open-surface file',
    ],
  },
];

for (const testCase of cases) {
  const frame = openFrame({ href: testCase.href });
  assert.equal(frame.window.document.querySelectorAll('.row').length, 5, `${testCase.label}: rows before ready`);
  const themed = await frame.waitFor(() => frame.window.document.documentElement.dataset.ocConnected === 'true');
  assert.ok(themed, `${testCase.label}: theme applied`);
  for (const id of ['terminal', 'data-manager', 'account', 'in-app-terminal', 'in-app-files']) frame.click(id);
  const answered = await frame.waitFor(() => frame.requests().length === testCase.expected.length);
  assert.ok(answered, `${testCase.label}: every click reached the host`);
  assert.deepEqual(frame.requests(), testCase.expected, `${testCase.label}: requests`);
  console.log(`ok - ${testCase.label}`);
}

// A silent host must not leave an empty panel: the rows are static, and the
// diagnostic explains the missing handshake.
const silent = openFrame({
  href: 'https://opencode.example.com/api/guests/app-shortcuts/panel/index.html',
  answer: false,
});
await new Promise((resolve) => setTimeout(resolve, 2600));
const diagnostic = silent.window.document.querySelector('#diagnostic');
assert.equal(silent.window.document.querySelectorAll('.row').length, 5, 'silent host: rows still render');
assert.equal(diagnostic.hidden, false, 'silent host: diagnostic visible');
assert.match(diagnostic.textContent, /No handshake/, 'silent host: diagnostic names the cause');
console.log('ok - rows render without the handshake, diagnostic explains it');

const standalone = openFrame({ href: 'https://opencode.example.com/api/guests/app-shortcuts/panel/index.html', sandboxed: false });
await standalone.flush();
assert.equal(standalone.window.document.querySelector('#standalone').hidden, false, 'standalone: notice visible');
assert.deepEqual(standalone.requests(), [], 'standalone: no host requests');
console.log('ok - standalone page explains it needs OpenChamber');
