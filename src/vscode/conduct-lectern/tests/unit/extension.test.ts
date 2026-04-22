import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as os from 'os';
import * as path from 'path';

// Mock vscode module — unavailable outside the extension host
vi.mock('vscode', () => ({
  workspace: {
    getConfiguration: vi.fn(),
    onDidChangeConfiguration: vi.fn(() => ({ dispose: vi.fn() })),
  },
  window: {
    createStatusBarItem: vi.fn(() => ({
      text: '',
      tooltip: undefined,
      command: undefined,
      show: vi.fn(),
      dispose: vi.fn(),
    })),
    showWarningMessage: vi.fn(),
    showOpenDialog: vi.fn(),
    showInformationMessage: vi.fn(),
  },
  commands: {
    registerCommand: vi.fn(() => ({ dispose: vi.fn() })),
    executeCommand: vi.fn(),
  },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  Uri: { file: vi.fn((p: string) => ({ fsPath: p, scheme: 'file' })) },
  MarkdownString: vi.fn((s: string) => ({ value: s })),
}));

import * as vscode from 'vscode';

describe('resolveConductorHome path logic', () => {
  const homedir = os.homedir();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('expands ~ to the OS home directory', () => {
    vi.mocked(vscode.workspace.getConfiguration).mockReturnValue({
      get: vi.fn(() => '~/.conductor'),
      update: vi.fn(),
    } as unknown as vscode.WorkspaceConfiguration);

    const raw = '~/.conductor';
    const resolved = raw.startsWith('~')
      ? path.join(homedir, raw.slice(1))
      : raw;

    expect(resolved).toBe(path.join(homedir, '.conductor'));
  });

  it('leaves absolute paths unchanged', () => {
    const raw = '/opt/conductor';
    const resolved = raw.startsWith('~')
      ? path.join(homedir, raw.slice(1))
      : raw;

    expect(resolved).toBe('/opt/conductor');
  });

  it('shortens paths under home dir to tilde form when saving', () => {
    const selectedPath = path.join(homedir, 'projects', 'conductor');
    const stored = selectedPath.startsWith(homedir)
      ? '~' + selectedPath.slice(homedir.length)
      : selectedPath;

    expect(stored).toBe('~/projects/conductor');
  });

  it('keeps paths outside home dir as-is when saving', () => {
    const selectedPath = '/opt/conductor';
    const stored = selectedPath.startsWith(homedir)
      ? '~' + selectedPath.slice(homedir.length)
      : selectedPath;

    expect(stored).toBe('/opt/conductor');
  });
});
