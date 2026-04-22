import * as vscode from 'vscode';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

const SETTING_KEY = 'conductor.home';
const STATUS_BAR_PRIORITY = 100;

function resolveConductorHome(): string {
  const config = vscode.workspace.getConfiguration();
  const raw = config.get<string>(SETTING_KEY) ?? '~/.conductor';
  return raw.startsWith('~') ? path.join(os.homedir(), raw.slice(1)) : raw;
}

function updateStatusBar(item: vscode.StatusBarItem): void {
  const resolved = resolveConductorHome();
  item.text = `$(home) Conductor: ${resolved}`;
  item.tooltip = new vscode.MarkdownString(
    `**Conduct Lectern**\n\n\`${resolved}\`\n\nClick to open`,
    true,
  );
  item.command = 'conductor.openHome';
  item.show();
}

async function openConductorHome(): Promise<void> {
  const resolved = resolveConductorHome();

  if (!fs.existsSync(resolved)) {
    const choice = await vscode.window.showWarningMessage(
      `Conductor home directory does not exist: ${resolved}`,
      'Create it',
      'Change path',
    );
    if (choice === 'Create it') {
      fs.mkdirSync(resolved, { recursive: true });
    } else if (choice === 'Change path') {
      await selectConductorHome();
      return;
    } else {
      return;
    }
  }

  await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(resolved));
}

async function selectConductorHome(): Promise<void> {
  const current = resolveConductorHome();
  const openDialogOptions: vscode.OpenDialogOptions = {
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    title: 'Select Conductor Home Directory',
    openLabel: 'Set as Conductor Home',
  };
  if (fs.existsSync(current)) {
    openDialogOptions.defaultUri = vscode.Uri.file(current);
  }
  const uris = await vscode.window.showOpenDialog(openDialogOptions);

  if (!uris || uris.length === 0) {
    return;
  }

  const selected = uris[0];
  if (!selected) {
    return;
  }

  const selectedPath = selected.fsPath;

  // Store tilde-shortened path when under home dir for portability
  const homedir = os.homedir();
  const storedPath = selectedPath.startsWith(homedir)
    ? '~' + selectedPath.slice(homedir.length)
    : selectedPath;

  await vscode.workspace
    .getConfiguration()
    .update(SETTING_KEY, storedPath, vscode.ConfigurationTarget.Global);

  vscode.window.showInformationMessage(`Conductor home set to: ${selectedPath}`);
}

export function activate(context: vscode.ExtensionContext): void {
  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    STATUS_BAR_PRIORITY,
  );

  updateStatusBar(statusBar);
  context.subscriptions.push(statusBar);

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration(SETTING_KEY)) {
        updateStatusBar(statusBar);
      }
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.openHome', () => {
      void openConductorHome();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.selectHome', () => {
      void selectConductorHome();
    }),
  );
}

export function deactivate(): void {
  // nothing to clean up — subscriptions are disposed automatically
}
