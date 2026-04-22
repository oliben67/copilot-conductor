import { defineConfig } from '@vscode/test-cli';

export default defineConfig({
  files: 'tests/integration/**/*.test.ts',
  version: 'stable',
  workspaceFolder: '.',
  launchArgs: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  mocha: {
    timeout: 30000,
    ui: 'tdd',
  },
});
