import * as assert from 'assert';
import * as vscode from 'vscode';

suite('Conductor Home – Extension Integration', () => {
  test('Extension is active after startup', async () => {
    const ext = vscode.extensions.getExtension('oliben67.conductor-home');
    assert.ok(ext, 'Extension should be registered');
    await ext?.activate();
    assert.strictEqual(ext?.isActive, true, 'Extension should be active');
  });

  test('conductor.home setting exists with default value', () => {
    const config = vscode.workspace.getConfiguration();
    const value = config.get<string>('conductor.home');
    assert.ok(typeof value === 'string', 'conductor.home should be a string');
    assert.strictEqual(value, '~/.conductor', 'Default should be ~/.conductor');
  });

  test('conductor.openHome command is registered', async () => {
    const commands = await vscode.commands.getCommands(true);
    assert.ok(
      commands.includes('conductor.openHome'),
      'conductor.openHome command should be registered',
    );
  });

  test('conductor.selectHome command is registered', async () => {
    const commands = await vscode.commands.getCommands(true);
    assert.ok(
      commands.includes('conductor.selectHome'),
      'conductor.selectHome command should be registered',
    );
  });
});
