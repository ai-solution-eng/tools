import { build } from 'esbuild';
import { readFileSync, writeFileSync } from 'node:fs';

/**
 * OpenChamber never compiles guest code: the panel script must ship as one
 * classic (IIFE) file. Run `npm install && npm run build`, then commit the
 * regenerated `panel/main.js` and `panel/index.html`.
 */
await build({
  entryPoints: ['src/main.ts'],
  outfile: 'panel/main.js',
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['chrome110', 'firefox110', 'safari16'],
  minify: true,
  legalComments: 'none',
});

/**
 * The page inlines the bundle instead of loading it with `<script src>`.
 *
 * A guest frame is `sandbox="allow-scripts"` (and the host adds a matching
 * `Content-Security-Policy: sandbox allow-scripts`), so its document has an
 * opaque origin. Classic subresource requests from such a document are
 * cross-origin and carry no cookies, so behind a cookie-gated proxy the
 * `<script src>` is redirected to the login page and the browser refuses it
 * (`X-Content-Type-Options: nosniff`). The HTML itself is a navigation and
 * does send cookies, so inlining the script keeps the panel at zero
 * subresource requests and working behind that proxy.
 */
const script = readFileSync('panel/main.js', 'utf8').replace(/<\/script/gi, '<\\/script');
const template = readFileSync('panel/index.template.html', 'utf8');
const marker = '<script src="main.js"></script>';
if (!template.includes(marker)) {
  throw new Error('panel/index.template.html is missing the main.js script tag');
}
writeFileSync(
  'panel/index.html',
  template.replace(marker, `<script data-openchamber-bundle>\n${script}\n</script>`),
);

console.log('built panel/main.js and panel/index.html');
