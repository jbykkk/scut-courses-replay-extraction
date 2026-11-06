import { build } from 'esbuild';
import { globalCSS } from 'markmap-view';
import { createHash } from 'node:crypto';
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const output = path.resolve(here, '../../extension');
const result = await build({
  entryPoints: [path.join(here, 'entry.mjs')], bundle: true, minify: true,
  platform: 'browser', format: 'iife', globalName: 'SCUTMarkmap', target: 'chrome116',
  outfile: path.join(output, 'markmap-vendor.min.js'), legalComments: 'inline', metafile: true,
  banner: { js: '/* Markmap 0.18.12. See MARKMAP-LICENSES.txt. Rebuild: scripts/markmap/README.md */' },
});
await writeFile(path.join(output, 'markmap-vendor.css'), globalCSS + '\n');

// Preserve notices for every installed dependency, including bundled upstream code.
const notices = ['Third-party software used by the SCUT Markmap bundle.\n'];
async function collect(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
    const folder = path.join(directory, entry.name);
    if (entry.name.startsWith('@')) { await collect(folder); continue; }
    let meta;
    try { meta = JSON.parse(await readFile(path.join(folder, 'package.json'), 'utf8')); }
    catch { continue; }
    notices.push(`\n${'='.repeat(72)}\n${meta.name} ${meta.version} (${meta.license || 'see license below'})\n`);
    for (const file of await readdir(folder)) {
      if (/^(licen[sc]e|copying|notice)(\.|$)/i.test(file)) {
        notices.push(await readFile(path.join(folder, file), 'utf8'));
      }
    }
    try { await collect(path.join(folder, 'node_modules')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  }
}
await collect(path.join(here, 'node_modules'));
await writeFile(path.join(output, 'MARKMAP-LICENSES.txt'), notices.join('\n').split('\n').map(line => line.trimEnd()).join('\n'));
const files = {};
for (const name of ['markmap-vendor.min.js', 'markmap-vendor.css', 'MARKMAP-LICENSES.txt']) {
  files[name] = createHash('sha256').update(await readFile(path.join(output, name))).digest('hex');
}
await writeFile(path.join(output, 'markmap-vendor.json'), JSON.stringify({
  markmap: '0.18.12', source: 'https://github.com/markmap/markmap', files,
  inputs: Object.keys(result.metafile.inputs).sort(),
}, null, 2) + '\n');
console.log('Offline Markmap bundle, stylesheet and third-party notices generated.');
