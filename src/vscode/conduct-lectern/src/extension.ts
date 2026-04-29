import * as vscode from 'vscode';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';

const SETTING_KEY = 'conductor.home';
const SETTING_API_URL = 'conductor.apiUrl';
const STATUS_BAR_PRIORITY = 100;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveConductorHome(): string {
  const config = vscode.workspace.getConfiguration();
  const raw = config.get<string>(SETTING_KEY) ?? '~/.conductor';
  return raw.startsWith('~') ? path.join(os.homedir(), raw.slice(1)) : raw;
}

function resolveApiUrl(): string {
  const config = vscode.workspace.getConfiguration();
  return config.get<string>(SETTING_API_URL) ?? 'http://127.0.0.1:8000/api/v1';
}

// ---------------------------------------------------------------------------
// Agent types
// ---------------------------------------------------------------------------

interface AgentInfo {
  role: string;
  name: string;
  scope: 'system' | 'project';
  project: string | null;
  running: boolean;
  file_path: string | null;
  file_exists: boolean;
  description: string | null;
}

interface AgentListResponse {
  system_agents: AgentInfo[];
  project_agents: AgentInfo[];
}

interface AgentTask {
  name: string;
  description: string | null;
  cron: string | null;
}

interface AgentInstances {
  min: number;
  max: number | null;
}

interface AgentDetailResponse extends AgentInfo {
  active: boolean;
  sidekick: boolean;
  augmenting: boolean;
  model: string | null;
  permissions: string[];
  tasks: AgentTask[];
  cron: string | null;
  instances: AgentInstances | null;
  instructions: string | null;
}

// ---------------------------------------------------------------------------
// Agents tree – node discriminators
// ---------------------------------------------------------------------------

type AgentsNode =
  | { kind: 'root'; conductor: AgentInfo | null }
  | { kind: 'group'; scope: 'system' | 'projects' }
  | { kind: 'project'; name: string }
  | { kind: 'agent'; agent: AgentInfo };

// ---------------------------------------------------------------------------
// Agents tree item
// ---------------------------------------------------------------------------

class AgentsItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsible: vscode.TreeItemCollapsibleState,
    public readonly node: AgentsNode,
    options: {
      description?: string;
      tooltip?: string | vscode.MarkdownString;
      iconPath?: vscode.ThemeIcon | vscode.Uri | { light: vscode.Uri; dark: vscode.Uri };
      contextValue?: string;
      command?: vscode.Command;
    } = {},
  ) {
    super(label, collapsible);
    if (options.description !== undefined) { this.description = options.description; }
    if (options.tooltip !== undefined) { this.tooltip = options.tooltip; }
    if (options.iconPath !== undefined) { this.iconPath = options.iconPath; }
    if (options.contextValue !== undefined) { this.contextValue = options.contextValue; }
    if (options.command !== undefined) { this.command = options.command; }
  }
}

// ---------------------------------------------------------------------------
// Agents tree provider
// ---------------------------------------------------------------------------

async function fetchAgents(apiUrl: string): Promise<AgentListResponse> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(`${apiUrl}/agents`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return (await res.json()) as AgentListResponse;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function fetchAgentDetail(apiUrl: string, role: string): Promise<AgentDetailResponse> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(`${apiUrl}/agents/${encodeURIComponent(role)}`, {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) { throw new Error(`HTTP ${res.status}`); }
    return (await res.json()) as AgentDetailResponse;
  } finally {
    clearTimeout(timeoutId);
  }
}

class ConductorAgentsProvider implements vscode.TreeDataProvider<AgentsItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<AgentsItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private _cached: AgentListResponse | null = null;
  private _error: string | null = null;
  private _pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly _extensionUri: vscode.Uri) {}

  startPolling(): vscode.Disposable {
    this._pollTimer = setInterval(() => { void this._poll(); }, 5000);
    return { dispose: () => this._stopPolling() };
  }

  private _stopPolling(): void {
    if (this._pollTimer !== null) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  private async _poll(): Promise<void> {
    try {
      const next = await fetchAgents(resolveApiUrl());
      if (this._hasChanged(next)) {
        this._cached = next;
        this._error = null;
        this._onDidChangeTreeData.fire();
      }
    } catch {
      // Swallow poll errors silently; the tree shows the error state on next manual refresh
    }
  }

  private _hasChanged(next: AgentListResponse): boolean {
    if (!this._cached) { return true; }
    const allOld = [...this._cached.system_agents, ...this._cached.project_agents];
    const allNew = [...next.system_agents, ...next.project_agents];
    if (allOld.length !== allNew.length) { return true; }
    const oldMap = new Map(allOld.map((a) => [`${a.scope}:${a.role}:${a.project ?? ''}`, a.running]));
    for (const agent of allNew) {
      if (oldMap.get(`${agent.scope}:${agent.role}:${agent.project ?? ''}`) !== agent.running) {
        return true;
      }
    }
    return false;
  }

  refresh(): void {
    this._cached = null;
    this._error = null;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: AgentsItem): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: AgentsItem): Promise<AgentsItem[]> {
    if (!element) {
      if (!this._cached && !this._error) {
        try {
          this._cached = await fetchAgents(resolveApiUrl());
        } catch (e: unknown) {
          this._error = e instanceof Error ? e.message : String(e);
        }
      }

      if (this._error) {
        return [
          new AgentsItem(
            'con-pilot not reachable',
            vscode.TreeItemCollapsibleState.None,
            { kind: 'root', conductor: null },
            {
              description: this._error,
              iconPath: new vscode.ThemeIcon('warning'),
              tooltip: `Could not connect to con-pilot: ${this._error}`,
              contextValue: 'error',
            },
          ),
        ];
      }

      if (!this._cached) {
        return [];
      }

      const conductor = (this._cached.system_agents ?? []).find((a) => a.role === 'conductor') ?? null;
      return [
        new AgentsItem(
          'conductor',
          vscode.TreeItemCollapsibleState.Expanded,
          { kind: 'root', conductor },
          {
            iconPath: {
              light: vscode.Uri.joinPath(this._extensionUri, 'icons', 'light', 'icon.svg'),
              dark: vscode.Uri.joinPath(this._extensionUri, 'icons', 'dark', 'icon.svg'),
            },
            contextValue: 'root',
            tooltip: new vscode.MarkdownString(
              conductor
                ? `**conductor** \u2014 ${conductor.name}\n\n*Read-only: the conductor cannot be edited here.*`
                : '**conductor**',
            ),
            ...(conductor
              ? {
                  command: {
                    command: 'conductor.showAgentProperties',
                    title: 'Show Agent Properties',
                    arguments: [conductor],
                  },
                }
              : {}),
          },
        ),
      ];
    }

    const { node } = element;

    if (node.kind === 'root') {
      return [
        new AgentsItem(
          'system',
          vscode.TreeItemCollapsibleState.Expanded,
          { kind: 'group', scope: 'system' },
          { iconPath: new vscode.ThemeIcon('server'), contextValue: 'group' },
        ),
        new AgentsItem(
          'projects',
          vscode.TreeItemCollapsibleState.Expanded,
          { kind: 'group', scope: 'projects' },
          { iconPath: new vscode.ThemeIcon('folder-library'), contextValue: 'group' },
        ),
      ];
    }

    if (node.kind === 'group' && node.scope === 'system') {
      return [...(this._cached?.system_agents ?? [])]
        .filter((a) => a.role !== 'conductor')
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((agent) => this._makeAgentItem(agent));
    }

    if (node.kind === 'group' && node.scope === 'projects') {
      const names = [
        ...new Set(
          (this._cached?.project_agents ?? [])
            .map((a) => a.project)
            .filter((p): p is string => p !== null),
        ),
      ].sort();
      return names.map((name) =>
        new AgentsItem(
          name,
          vscode.TreeItemCollapsibleState.Expanded,
          { kind: 'project', name },
          { iconPath: new vscode.ThemeIcon('repo'), contextValue: 'project' },
        ),
      );
    }

    if (node.kind === 'project') {
      return [...(this._cached?.project_agents ?? [])]
        .filter((a) => a.project === node.name)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((agent) => this._makeAgentItem(agent));
    }

    return [];
  }

  private _agentIcon(agent: AgentInfo): { light: vscode.Uri; dark: vscode.Uri } {
    const prefix = agent.scope === 'system' ? 'system' : 'project';
    const state = agent.running ? 'on' : 'off';
    const uri = vscode.Uri.joinPath(this._extensionUri, 'icons', `${prefix}-${state}.svg`);
    return { light: uri, dark: uri };
  }

  private _makeAgentItem(agent: AgentInfo): AgentsItem {
    return new AgentsItem(
      agent.name,
      vscode.TreeItemCollapsibleState.None,
      { kind: 'agent', agent },
      {
        description: agent.role,
        iconPath: this._agentIcon(agent),
        tooltip: new vscode.MarkdownString(
          `**${agent.name}** (${agent.role})\n\nStatus: ${agent.running ? '🟢 running' : '⚪ idle'}` +
          (agent.description ? `\n\n${agent.description}` : ''),
        ),
        contextValue: 'agent',
        command: {
          command: 'conductor.showAgentProperties',
          title: 'Show Agent Properties',
          arguments: [agent],
        },
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Agent properties panel
// ---------------------------------------------------------------------------

class AgentPropertiesPanel {
  private static _current: AgentPropertiesPanel | undefined;
  private readonly _panel: vscode.WebviewPanel;

  static show(agent: AgentInfo, extensionUri: vscode.Uri): void {
    if (AgentPropertiesPanel._current) {
      AgentPropertiesPanel._current._panel.title = `Agent: ${agent.name}`;
      AgentPropertiesPanel._current._panel.webview.html = AgentPropertiesPanel._buildHtml(agent);
      AgentPropertiesPanel._current._panel.reveal(vscode.ViewColumn.Beside, true);
    } else {
      const panel = vscode.window.createWebviewPanel(
        'conductorAgentProperties',
        `Agent: ${agent.name}`,
        { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
        { enableScripts: false, retainContextWhenHidden: true },
      );
      AgentPropertiesPanel._current = new AgentPropertiesPanel(panel);
      panel.webview.html = AgentPropertiesPanel._buildHtml(agent);
    }
    void AgentPropertiesPanel._current._fetchDetail(agent);
  }

  private constructor(panel: vscode.WebviewPanel) {
    this._panel = panel;
    panel.onDidDispose(() => { AgentPropertiesPanel._current = undefined; });
  }

  private async _fetchDetail(agent: AgentInfo): Promise<void> {
    try {
      const detail = await fetchAgentDetail(resolveApiUrl(), agent.role);
      if (AgentPropertiesPanel._current) {
        this._panel.title = `Agent: ${detail.name}`;
        this._panel.webview.html = AgentPropertiesPanel._buildHtml(detail);
      }
    } catch {
      // keep the basic info already rendered
    }
  }

  private static _esc(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  private static _row(label: string, value: string): string {
    return `<tr><td class="lbl">${label}</td><td>${value}</td></tr>`;
  }

  private static _buildHtml(agent: AgentInfo | AgentDetailResponse): string {
    const d = agent as AgentDetailResponse;
    const esc = AgentPropertiesPanel._esc;
    const row = AgentPropertiesPanel._row;
    const running = agent.running;
    const isReadOnly = agent.role === 'conductor';
    const statusColor = running ? '#4ec9b0' : '#858585';
    const statusLabel = running ? '● running' : '○ idle';

    const permissionsHtml = d.permissions?.length
      ? d.permissions.map((p) => `<span class="tag">${esc(p)}</span>`).join(' ')
      : '<em>none</em>';

    const tasksHtml = d.tasks?.length
      ? `<ul>${d.tasks.map((t) =>
          `<li><strong>${esc(t.name)}</strong>` +
          (t.cron ? ` <span class="tag">${esc(t.cron)}</span>` : ' <span class="tag">manual</span>') +
          (t.description ? `<br><small>${esc(t.description)}</small>` : '') +
          `</li>`
        ).join('')}</ul>`
      : '<em>none</em>';

    const instructionsHtml = d.instructions
      ? `<pre class="instructions">${esc(d.instructions)}</pre>`
      : '<em>none</em>';

    const fileCell = agent.file_exists && agent.file_path
      ? `<span class="path">${esc(agent.file_path)}</span>`
      : `<span class="missing">missing</span>`;

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 16px 20px; }
  h1 { font-size: 1.2em; margin: 0 0 4px; }
  .status { font-size: 0.9em; color: ${statusColor}; margin-bottom: 16px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 8px; }
  td { padding: 3px 8px 3px 0; vertical-align: top; }
  td.lbl { color: var(--vscode-descriptionForeground); white-space: nowrap; min-width: 110px; }
  .tag { display: inline-block; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); border-radius: 3px; padding: 1px 6px; font-size: 0.82em; margin: 1px 2px; }
  h2 { font-size: 0.95em; text-transform: uppercase; letter-spacing: 0.05em; margin: 16px 0 6px; color: var(--vscode-descriptionForeground); }
  hr { border: none; border-top: 1px solid var(--vscode-panel-border); margin: 12px 0; }
  ul { margin: 0; padding-left: 18px; }
  li { margin: 4px 0; }
  pre.instructions { white-space: pre-wrap; word-break: break-word; background: var(--vscode-textCodeBlock-background); padding: 8px 10px; border-radius: 4px; font-size: 0.85em; margin: 0; }
  .missing { color: var(--vscode-errorForeground); }
  .path { font-family: var(--vscode-editor-font-family, monospace); font-size: 0.85em; word-break: break-all; }
  .readonly-banner { background: var(--vscode-inputValidation-warningBackground, #6c4f1e); color: var(--vscode-inputValidation-warningForeground, #fff); border: 1px solid var(--vscode-inputValidation-warningBorder, #b89500); border-radius: 4px; padding: 6px 10px; margin-bottom: 14px; font-size: 0.9em; }
</style>
</head>
<body>
${isReadOnly ? '<div class="readonly-banner">🔒 Read-only — the conductor agent cannot be edited through Conduct Lectern.</div>' : ''}
<h1>${esc(agent.name)}</h1>
<div class="status">${statusLabel}</div>
<table>
  ${row('Role', esc(agent.role))}
  ${row('Scope', esc(agent.scope))}
  ${agent.project ? row('Project', esc(agent.project)) : ''}
  ${d.active !== undefined ? row('Active', d.active ? 'yes' : 'no') : ''}
  ${d.sidekick ? row('Sidekick', 'yes') : ''}
  ${d.augmenting ? row('Augmenting', 'yes') : ''}
  ${d.model ? row('Model', esc(d.model)) : ''}
  ${d.instances ? row('Instances', `min ${d.instances.min}${d.instances.max ? `, max ${d.instances.max}` : ''}`) : ''}
  ${row('File', fileCell)}
</table>
${agent.description ? `<h2>Description</h2><p>${esc(agent.description)}</p>` : ''}
${d.permissions !== undefined ? `<h2>Permissions</h2><div>${permissionsHtml}</div>` : ''}
${d.tasks !== undefined ? `<h2>Tasks</h2>${tasksHtml}` : ''}
${d.instructions !== undefined ? `<h2>Instructions</h2>${instructionsHtml}` : ''}
</body>
</html>`;
  }
}

// ---------------------------------------------------------------------------
// Activity-bar tree view — .github browser
// ---------------------------------------------------------------------------

class ConductorItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsible: vscode.TreeItemCollapsibleState,
    public readonly resourcePath: string | undefined,
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
    const githubPath = path.join(resolveConductorHome(), '.github');
    const dirPath = element?.resourcePath ?? githubPath;

    if (!fs.existsSync(dirPath)) {
      if (element) { return []; }
      return [
        new ConductorItem('.github not found', vscode.TreeItemCollapsibleState.None, undefined, {
          description: githubPath,
          iconPath: new vscode.ThemeIcon('warning'),
          tooltip: `${githubPath} does not exist`,
        }),
      ];
    }

    return fs.readdirSync(dirPath, { withFileTypes: true }).map((entry) => {
      const fullPath = path.join(dirPath, entry.name);
      const isDir = entry.isDirectory();
      return new ConductorItem(
        entry.name,
        isDir ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None,
        isDir ? fullPath : undefined,
        {
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
        },
      );
    });
  }
}

// ---------------------------------------------------------------------------
// Agent file opener
// ---------------------------------------------------------------------------

async function openAgentFile(agent: AgentInfo): Promise<void> {
  if (!agent.file_path || !agent.file_exists) {
    vscode.window.showWarningMessage(`Agent file not found for "${agent.name}".`);
    return;
  }

  const uri = vscode.Uri.file(agent.file_path);
  await vscode.window.showTextDocument(uri, { preview: false });

  if (agent.role === 'conductor') {
    vscode.window.showInformationMessage(
      'The conductor agent is read-only and cannot be edited through Conduct Lectern.',
    );
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

  if (vscode.env.remoteName) {
    // Over SSH / containers: revealFileInOS targets the local machine, not the remote.
    // Open an integrated terminal at the conductor home instead.
    const terminal = vscode.window.createTerminal({
      name: 'Conductor Home',
      cwd: resolved,
    });
    terminal.show();
  } else {
    await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(resolved));
  }
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

  // Agents tree view (top)
  const agentsProvider = new ConductorAgentsProvider(context.extensionUri);
  const agentsView = vscode.window.createTreeView('conductor.agentsView', {
    treeDataProvider: agentsProvider,
    showCollapseAll: true,
  });
  context.subscriptions.push(agentsView);

  // .github tree view (bottom)
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
      if (event.affectsConfiguration(SETTING_API_URL)) {
        agentsProvider.refresh();
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

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.refreshAgentsView', () => {
      agentsProvider.refresh();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.openAgent', (agent: AgentInfo) => {
      void openAgentFile(agent);
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('conductor.showAgentProperties', (agent: AgentInfo) => {
      AgentPropertiesPanel.show(agent, context.extensionUri);
    }),
  );

  context.subscriptions.push(agentsProvider.startPolling());
}

export function deactivate(): void {
  // nothing to clean up — subscriptions are disposed automatically
}
