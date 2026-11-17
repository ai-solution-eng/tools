// Offline check of the bundle manifest and display resources: package.json
// exports/files/dsh declarations, the Loader patch, the icon, the locale files,
// and the static rules this bundle must not break (no Harness Client package
// import, no document.body write, no iframe, no file writes on the Host).
import fs from 'node:fs'
import path from 'node:path'
import { bundleRoot, createChecks, report } from './harness.mjs'

const checks = createChecks()
const read = (name) => fs.readFileSync(path.join(bundleRoot, name), 'utf8')
const manifest = JSON.parse(read('package.json'))
const patch = read('cordis.patch.yml')
const icon = read('icon.svg')
const indexSource = read('index.js')
const clientSource = read('client.js')

// --- manifest ----------------------------------------------------------------
checks.equal('manifest: package name', manifest.name, '@local/dsh-config-document')
checks.equal('manifest: private', manifest.private, true)
checks.equal('manifest: ESM package', manifest.type, 'module')
checks.check('manifest: description is written for humans', typeof manifest.description === 'string' && manifest.description.length > 40)
checks.equal('manifest: host entry export', manifest.exports['.'], './index.js')
checks.equal('manifest: client entry export', manifest.exports['./client'], './client.js')
checks.equal('manifest: locale export', manifest.exports['./locale/*.json'], './locale/*.json')
checks.equal('manifest: icon export', manifest.exports['./icon'], './icon.svg')
checks.equal('manifest: bundle patch declared', manifest.dsh?.bundle?.patch, './cordis.patch.yml')
checks.equal('manifest: client platform', manifest.dsh?.client?.platform, 'web')
checks.equal('manifest: client loads immediately', manifest.dsh?.client?.immediately, true)
checks.check(
  'manifest: client inject orders against the settings shell and locale',
  Array.isArray(manifest.dsh?.client?.inject)
    && manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-ui-settings-general')
    && manifest.dsh.client.inject.includes('@deepseek-ai/dsh-client-locale'),
)
checks.check('manifest: files lists the runtime files', ['index.js', 'client.js', 'cordis.patch.yml', 'README.md', 'icon.svg'].every((name) => manifest.files.includes(name)))
checks.check('manifest: files covers locales and tests', manifest.files.includes('locale/*.json') && manifest.files.includes('test/*.mjs'))

// Every file the manifest promises exists on disk.
for (const name of ['index.js', 'client.js', 'cordis.patch.yml', 'README.md', 'icon.svg', 'locale/en.json', 'locale/zh.json']) {
  checks.check(`files: ${name} exists`, fs.existsSync(path.join(bundleRoot, name)))
}
for (const name of fs.readdirSync(path.join(bundleRoot, 'test'))) {
  checks.check(`files: test/${name} is an .mjs check`, name.endsWith('.mjs'))
}

// --- loader patch ------------------------------------------------------------
checks.includes('patch: inserts a row', patch, '- insert:')
checks.includes('patch: row id', patch, 'id: config-document')
checks.includes('patch: row names the package', patch, "name: '@local/dsh-config-document'")
checks.check('patch: no other rows are inserted', (patch.match(/- id:/g) ?? []).length === 1)
checks.check('patch: no YAML tab characters', !/\t/.test(patch))

// --- icon --------------------------------------------------------------------
checks.check('icon: is an SVG document', icon.trimStart().startsWith('<svg'))
checks.check('icon: declares the SVG namespace', icon.includes('xmlns="http://www.w3.org/2000/svg"'))
checks.check('icon: has a viewBox', /viewBox="0 0 64 64"/.test(icon))
checks.check('icon: declares intrinsic size', /width="64"/.test(icon) && /height="64"/.test(icon))
checks.check('icon: stays under the 256 KiB limit', Buffer.byteLength(icon, 'utf8') < 256 * 1024)
checks.check('icon: carries no script', !/<script/i.test(icon))
checks.check('icon: references no external resource', !/href="(?:https?:)?\/\//.test(icon) && !/url\(\s*['"]?(?:https?:|data:|\/|\.)/.test(icon))
checks.check('icon: is original artwork with the plugin palette', icon.includes('cfgdoc-bg') && icon.includes('#2f6fed'))
checks.check('icon: is not the template artwork', !icon.includes('<circle cx="32" cy="32" r="24" fill="#247bbf"/>'))

// --- locale files ------------------------------------------------------------
for (const language of ['en', 'zh']) {
  const locale = JSON.parse(read(`locale/${language}.json`))
  checks.check(`locale/${language}: has meta.title`, typeof locale.meta?.title === 'string' && locale.meta.title.length > 0)
  checks.check(`locale/${language}: has meta.description`, typeof locale.meta?.description === 'string' && locale.meta.description.length > 20)
  checks.check(`locale/${language}: title is not the package name`, locale.meta.title !== manifest.name)
  const uiKeys = Object.keys(locale).filter((key) => key !== 'meta')
  checks.check(`locale/${language}: carries the UI strings`, uiKeys.length > 20)
  checks.check(`locale/${language}: no placeholder-only copy`, uiKeys.every((key) => typeof locale[key] === 'string' && locale[key].length > 0))
}
checks.check('locale: titles differ between languages', JSON.parse(read('locale/en.json')).meta.title !== JSON.parse(read('locale/zh.json')).meta.title)

// --- static rules ------------------------------------------------------------
checks.check('client: registers through the module loader', clientSource.includes('window.__ModuleLoader__.load('))
checks.check('client: module id is the package name', clientSource.includes("id: '@local/dsh-config-document'"))
checks.check('client: imports no Harness Client package', !/dsh-client-ui-primitives|@deepseek-ai\/dsh-client/.test(clientSource))
checks.check('client: requires only react', (clientSource.match(/require\(([^)]*)\)/g) ?? []).join() === "require('react')")
checks.check('client: never writes to document.body', !/document\.body\s*(?:\.|=)/.test(clientSource))
checks.check('client: renders no iframe', !/iframe/i.test(clientSource))
checks.check('client: injects no HTML string', !/innerHTML|dangerouslySetInnerHTML/.test(clientSource))
checks.check('client: styles with theme tokens', clientSource.includes('var(--dsw-alias-'))
checks.check('client: no literal colour outside artwork', !/(?:color|background|border(?:-color)?)\s*:\s*#[0-9a-fA-F]{3,8}/.test(clientSource))
checks.check('client: registers the shipped cell id', clientSource.includes("id: 'open-document'"))
checks.check('client: shadows the shipped cell by priority', /priority: -1/.test(clientSource))
checks.check('client: every user-visible string goes through t(', !/>[A-Za-z][A-Za-z ,.:;'()-]{3,}</.test(clientSource.replace(/<svg[\s\S]*?<\/svg>/g, '')))
checks.check('host: registers through ctx.connection.fetch', indexSource.includes('ctx.connection.fetch.register('))
checks.check('host: declares the connection inject', /export const inject = \['connection'\]/.test(indexSource))
checks.check('host: reads with readFile only', indexSource.includes('readFile(') && !/writeFile|appendFile|createWriteStream|rename\(|unlink\(|mkdir\(/.test(indexSource))
checks.check('host: never accepts a path parameter', !/searchParams|request\.url/.test(indexSource))
checks.check('host: resolves the document from the settings service', indexSource.includes("ctx.get('settings')") && indexSource.includes("ctx.get('configEditor')"))

report('manifest-check', checks.results)
