import * as vscode from 'vscode';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

const SETTING_KEY = 'conductor.home';
const STATUS_BAR_PRIORITY = 100;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveConductorHome(): string {
  const config = vscode.workspace.getConfiguration();
  const raw = config.get<string>(SETTING_KEY) ?? '~/.conductor';
  return raw.startsWith('~') ? path.join(os.homedir(), raw.slice(1)) : raw;
}

// ---------------------------------------------------------------------------
// Activity-bar tree view
// ---------------------------------------------------------------------------

class ConductorItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsible: vscode.TreeItemCollapsibleState,
    options: {
      description?: string;
      tooltip?: string | vscode.MarkdownString;
      iconPath?: vscode.ThemeIcon;
      command?: vscode.Command;
      contextValue?: string;
    } = {},
  ) {
    super(label, collapsible);
    if (options.description !== undefined) { this.description = options.description; }
    if (options.tooltip !== undefined) { this.tooltip = options.tooltip; }
    if (options.iconPath !== undefined) { this.iconPath = options.iconPath; }
    if (options.command !== undefined) { this.command = options.command; }
    if (options.contextValue !== undefined) { this.contextValue = options.contextValue; }
  }
}

class ConductorHomeProvider implements vscode.TreeDataProvider<ConductorItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<ConductorItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: ConductorItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: ConductorItem): ConductorItem[] {
    if (element) {
      return [];
    }

    const homePath = resolveConductorHome();
    const exists = fs.existsSync(homePath);

    if (!exists) {
      return [
        new ConductorItem('Home directory not found', vscode.TreeItemCollapsibleState.None, {
          description: homePath,
          iconPath: new vscode.ThemeIcon('warning'),
          tooltip: `${homePath} does not exist`,
        }),
      ];
    }

    const entries = fs.readdirSync(homePath, { withFileTypes: true });
    return entries.map((entry) => {
      const fullPath = path.join(homePath, entry.name);
      const isDir = entry.isDirectory();
      return new ConductorItem(entry.name, vscode.TreeItemCollapsibleState.None, {
        iconPath: new vscode.ThemeIcon(isDir ? 'folder' : 'file'),
        tooltip: fullPath,
        ...(isDir
          ? {}
          : {
              command: {
                command: 'vscode.open',
                title: 'Open',
                arguments: [vscode.Uri.file(fullPath)],
              },
            }),
      });
    });
  }
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
  // Status bar
  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    STATUS_BAR_PRIORITY,
  );
  updateStatusBar(statusBar);
  context.subscriptions.push(statusBar);

  // Activity-bar tree view
  const homeProvider = new ConductorHomeProvider();
  const treeView = vscode.window.createTreeView('conductor.homeView', {
    treeDataProvider: homeProvider,
    showCollapseAll: false,
  });
  context.subscriptions.push(treeView);

  // Refresh tree + status bar when config changes
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration(SETTING_KEY)) {
        updateStatusBar(statusBar);
        homeProvider.refresh();
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

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.refreshHomeView', () => {
      homeProvider.refresh();
    }),
  );
}

export function deactivate(): void {
  // nothing to clean up — subscriptions are disposed automatically
}

