// Runs inside the real VS Code extension host. No external extensions or packages.
const assert = require('assert');
const fs = require('fs');
const vscode = require('vscode');

exports.activate = context => context;

exports.run = async () => {
    const { workspace, window, Uri, WorkspaceEdit, Position } = vscode;
    const extension = vscode.extensions.getExtension('lumen.local-proof');
    const context = await extension.activate();
    assert.strictEqual(vscode.version, '1.95.3');
    assert.strictEqual(workspace.workspaceFolders.length, 1);
    const folder = workspace.workspaceFolders[0].uri;
    const note = Uri.joinPath(folder, 'story.txt');
    const settings = workspace.getConfiguration('editor', note);
    assert.strictEqual(settings.get('tabSize'), 3);
    assert.strictEqual(settings.get('insertSpaces'), false);
    const asset = await workspace.fs.readFile(Uri.joinPath(folder, 'assets', 'offline.bin'));
    assert.strictEqual(Buffer.from(asset).toString('hex'), '004c756d656eff');
    const doc = await workspace.openTextDocument(note);
    await window.showTextDocument(doc);
    const original = 'Private sample draft\n';
    const finalText = 'Final local revision\n' + original;
    if (process.env.LUMEN_SESSION === 'before') {
        assert.strictEqual(doc.getText(), original);
        const edit = new WorkspaceEdit();
        edit.insert(note, new Position(0, 0), 'Final local revision\n');
        assert(await workspace.applyEdit(edit));
        assert(await doc.save());
        await context.workspaceState.update('priorSession', 'private local workspace');
        await context.globalState.update('savedPreference', 'local only');
        // The local history provider records revisions asynchronously on save.
        const history = Uri.file(process.env.LUMEN_HISTORY);
        const until = Date.now() + 12000;
        let found = false;
        while (Date.now() < until && !found) {
            try {
                for (const [name] of await workspace.fs.readDirectory(history)) {
                    // Desktop flushes entries.json at shutdown. Wait for the payload
                    // here, then the parent process checks the flushed index on disk.
                    const folder = Uri.joinPath(history, name);
                    for (const [filename, type] of await workspace.fs.readDirectory(folder)) {
                        if (type !== vscode.FileType.File || filename === 'entries.json') continue;
                        const bytes = await workspace.fs.readFile(Uri.joinPath(folder, filename));
                        if (Buffer.from(bytes).toString().includes('Private sample draft')) found = true;
                    }
                    if (found) break;
                }
            } catch (_) { /* The first save may still be flushing. */ }
            if (!found) await new Promise(resolve => setTimeout(resolve, 200));
        }
        assert(found, 'VS Code did not persist local history for the sample document');
    } else {
        assert.strictEqual(context.workspaceState.get('priorSession'), 'private local workspace');
        assert.strictEqual(context.globalState.get('savedPreference'), 'local only');
    }
    assert.strictEqual(doc.getText(), finalText);
    fs.writeFileSync(process.env.LUMEN_RECEIPT, JSON.stringify({session:process.env.LUMEN_SESSION, version:vscode.version}));
};
