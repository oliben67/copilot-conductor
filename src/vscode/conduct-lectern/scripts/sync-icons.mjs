import { copyFile, mkdir } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// All icons are derived from the clean conductor.svg.
const cleanDir = path.resolve(__dirname, '../../../../.dev-artefacts/img/clean');
const iconsDir = path.resolve(__dirname, '../icons');
const conductorSvg = path.join(cleanDir, 'conductor.svg');

const svgTargets = [
  'icon.svg',
  'dark/icon.svg',
  'light/icon.svg',
  'activitybar.svg',
];

const pngTargets = [
  'icon.png',
  'dark/icon.png',
  'light/icon.png',
];

const syncIcons = async () => {
  // Copy SVG to all SVG icon slots
  for (const rel of svgTargets) {
    const target = path.join(iconsDir, rel);
    await mkdir(path.dirname(target), { recursive: true });
    await copyFile(conductorSvg, target);
    console.log(`Copied conductor.svg -> ${rel}`);
  }

  // Generate PNGs at 512x512 using rsvg-convert
  for (const rel of pngTargets) {
    const target = path.join(iconsDir, rel);
    await mkdir(path.dirname(target), { recursive: true });
    execFileSync('rsvg-convert', ['-w', '512', '-h', '512', conductorSvg, '-o', target]);
    console.log(`Generated PNG -> ${rel}`);
  }
};

await syncIcons();
