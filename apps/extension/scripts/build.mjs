import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { build } from 'esbuild'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = resolve(root, 'dist')
const developmentOrigin = 'http://127.0.0.1:8000'
const portalOrigin = process.env.WORKFLOW_PORTAL_ORIGIN ?? developmentOrigin
const parsedPortalOrigin = new URL(portalOrigin)

if (parsedPortalOrigin.origin !== portalOrigin) {
  throw new Error('WORKFLOW_PORTAL_ORIGIN must be an origin without path, query, or fragment')
}
if (parsedPortalOrigin.protocol !== 'https:' && portalOrigin !== developmentOrigin) {
  throw new Error('Non-development portal origins must use HTTPS')
}

await rm(output, { force: true, recursive: true })
await mkdir(output, { recursive: true })

await build({
  bundle: true,
  entryPoints: [resolve(root, 'src/background.ts')],
  format: 'esm',
  outfile: resolve(output, 'background.js'),
  platform: 'browser',
  sourcemap: false,
  target: 'chrome120',
  define: {
    __WORKFLOW_PORTAL_ORIGIN__: JSON.stringify(portalOrigin),
  },
})

await build({
  bundle: true,
  entryPoints: [resolve(root, 'src/content.ts')],
  format: 'iife',
  outfile: resolve(output, 'content.js'),
  platform: 'browser',
  sourcemap: false,
  target: 'chrome120',
})

const manifest = JSON.parse(await readFile(resolve(root, 'manifest.json'), 'utf8'))
const portalPattern = `${portalOrigin}/*`
manifest.host_permissions = [...new Set([...manifest.host_permissions, portalPattern])]
manifest.externally_connectable.matches = [
  ...new Set([...manifest.externally_connectable.matches, portalPattern]),
]
await writeFile(
  resolve(output, 'manifest.json'),
  `${JSON.stringify(manifest, null, 2)}\n`,
  'utf8',
)