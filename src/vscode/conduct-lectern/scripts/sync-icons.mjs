import { copyFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const artefactsDir = path.resolve(__dirname, '../../../artefacts/img');
const iconsDir = path.resolve(__dirname, '../icons');

const mappings = [
  ['favicon-dark.svg', 'dark/icon.svg'],
  ['favicon-light.svg', 'light/icon.svg'],
  ['favicon.svg', 'icon.svg'],
  ['web-app-manifest-512x512-dark.png', 'dark/icon.png'],
  ['web-app-manifest-512x512-light.png', 'light/icon.png'],
  ['web-app-manifest-512x512-light.png', 'icon.png']
];

const syncIcons = async () => {
  for (const [sourceFile, targetRelativePath] of mappings) {
    const sourcePath = path.join(artefactsDir, sourceFile);
    const targetPath = path.join(iconsDir, targetRelativePath);

    await mkdir(path.dirname(targetPath), { recursive: true });
    await copyFile(sourcePath, targetPath);
    console.log(`Synced ${sourceFile} -> ${targetRelativePath}`);
  }
};

await syncIcons();
