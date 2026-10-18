// Builds Remora's frontend into ../backend/remora/static/:
//   index.js + chunk-*.js   Remora (ESM, code-split)
//   dfl.js                  @decky/ui, unmodified, as its own module (LGPL-2.1)
//   LICENSE.decky-ui.txt    its license
//   fallback.js             the self-contained crash-loop screen
import * as esbuild from 'esbuild';
import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, '../backend/remora/static');
const pkg = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
const dflDir = join(here, 'node_modules/@decky/ui');
const dflPkg = JSON.parse(readFileSync(join(dflDir, 'package.json'), 'utf8'));

// Remove only what this build writes, so other files in static/ survive.
mkdirSync(out, { recursive: true });
for (const f of readdirSync(out)) {
  if (/^(index|fallback|dfl)\.js(\.map)?$|^chunk-.*\.js(\.map)?$|^LICENSE\.decky-ui\.txt$/.test(f)) rmSync(join(out, f));
}

const shim = (name) => join(here, 'src/shims', name);
// react, react-dom and the JSX runtime resolve to Steam's own instances (window.SP_*), the same
// mapping plugin builds use. In Remora's own code @decky/ui resolves to the separate dfl.js.
const globalsPlugin = ({ externalDfl }) => ({
  name: 'remora-globals',
  setup(b) {
    b.onResolve({ filter: /^react$/ }, () => ({ path: shim('react.ts') }));
    b.onResolve({ filter: /^react\/jsx-runtime$/ }, () => ({ path: shim('jsx-runtime.ts') }));
    b.onResolve({ filter: /^react-dom(\/client)?$/ }, () => ({ path: shim('react-dom.ts') }));
    if (externalDfl) b.onResolve({ filter: /^@decky\/ui$/ }, () => ({ path: './dfl.js', external: true }));
  },
});

const common = {
  bundle: true,
  format: 'esm',
  target: 'chrome110',
  platform: 'browser',
  logLevel: 'info',
  legalComments: 'inline',
  define: { 'process.env.NODE_ENV': '"production"', __REMORA_VERSION__: JSON.stringify(pkg.version) },
};

await esbuild.build({
  ...common,
  entryPoints: [join(dflDir, dflPkg.module ?? dflPkg.main)],
  outfile: join(out, 'dfl.js'),
  sourcemap: 'linked',
  sourcesContent: true,
  banner: {
    js: `/* @decky/ui ${dflPkg.version} (decky-frontend-lib), LGPL-2.1: see LICENSE.decky-ui.txt.
   The library's code is included unmodified from the npm package; only its react imports are
   linked to Steam's React (window.SP_REACT / SP_JSX). Source: ${dflPkg.homepage ?? 'https://github.com/SteamDeckHomebrew/decky-frontend-lib'}
   You may replace this file with your own build of @decky/ui ${dflPkg.version.split('.')[0]}.x that keeps the same exports. */`,
  },
  plugins: [globalsPlugin({ externalDfl: false })],
});
copyFileSync(join(dflDir, 'LICENSE'), join(out, 'LICENSE.decky-ui.txt'));

await esbuild.build({
  ...common,
  entryPoints: { index: join(here, 'src/index.ts') },
  outdir: out,
  splitting: true,
  chunkNames: 'chunk-[hash]',
  jsx: 'automatic',
  sourcemap: 'linked',
  sourcesContent: true,
  banner: { js: `/* Remora ${pkg.version} frontend. MIT license. */` },
  plugins: [globalsPlugin({ externalDfl: true })],
});

await esbuild.build({
  ...common,
  entryPoints: [join(here, 'src/fallback.ts')],
  outfile: join(out, 'fallback.js'),
  sourcemap: false,
});

console.log(`built into ${out}`);
