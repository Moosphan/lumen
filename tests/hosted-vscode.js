// Runs inside the real VS Code extension host. No external extensions or packages.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

exports.activate = context => context;

async function waitForHistory(resource, text, indexed) {
    const history = process.env.LUMEN_HISTORY;
    const limit = Date.now() + 12000;
    let diagnostic = '';
    do {
        let payloads = 0, indexes = 0;
        try {
            for (const dir of fs.readdirSync(history, {withFileTypes:true})) {
                if (!dir.isDirectory()) continue;
                const folder = path.join(history, dir.name);
                let names = fs.readdirSync(folder).filter(n => n !== 'entries.json');
                if (indexed) {
                    const index = path.join(folder, 'entries.json');
                    if (!fs.existsSync(index)) continue;
                    indexes++;
                    const data = JSON.parse(fs.readFileSync(index, 'utf8'));
                    if (data.resource !== resource.toString()) continue;
                    names = data.entries.map(entry => entry.id);
                }
                for (const name of names) {
                    payloads++;
                    if (fs.readFileSync(path.join(folder, name), 'utf8') === text) return;
                }
            }
            diagnostic = 'indexes=' + indexes + ', payloads=' + payloads;
        } catch (error) {
            diagnostic = error.message;
        }
        await new Promise(resolve => setTimeout(resolve, 150));
    } while (Date.now() < limit);
    throw new Error('Missing ' + (indexed ? 'indexed revision' : 'saved revision') +
        ' for ' + path.basename(resource.fsPath) + ' (' + diagnostic + ')');
}

async function replaceAndSave(document, text) {
    const edit = new vscode.WorkspaceEdit();
    const end = document.positionAt(document.getText().length);
    edit.replace(document.uri, new vscode.Range(new vscode.Position(0, 0), end), text);
    assert(await vscode.workspace.applyEdit(edit));
    assert(await document.save());
}

exports.run = async () => {
    const {workspace, window, Uri} = vscode;
    const context = await vscode.extensions.getExtension('lumen.local-proof').activate();
    assert.strictEqual(vscode.version, '1.95.3');
    assert.strictEqual(workspace.workspaceFolders.length, 1);
    const folder = workspace.workspaceFolders[0].uri;
    const note = Uri.joinPath(folder, 'story.txt');
    const settings = workspace.getConfiguration('editor', note);
    assert.strictEqual(settings.get('tabSize'), 3);
    assert.strictEqual(settings.get('insertSpaces'), false);
    const asset = await workspace.fs.readFile(Uri.joinPath(folder, 'assets', 'offline.bin'));
    assert.strictEqual(Buffer.from(asset).toString('hex'), '004c756d656eff');
    const previous = 'Prior local revision\nPrivate sample draft\n';
    const finalText = 'Final local revision\nPrivate sample draft\n';
    if (process.env.LUMEN_SESSION === 'before') {
        const source = Uri.joinPath(folder, 'draft.txt');
        const draft = await workspace.openTextDocument(source);
        await window.showTextDocument(draft);
        assert.strictEqual(draft.getText(), 'Private sample draft\n');
        await replaceAndSave(draft, previous);
        await waitForHistory(source, previous, false);
        await context.workspaceState.update('priorSession', 'private local workspace');
        await context.globalState.update('savedPreference', 'local only');

        // VS Code 1.95.3 WorkingCopyHistoryModel.moveEntries explicitly stores
        // the migrated index. Extension-test process exit alone may skip its flush.
        const rename = new vscode.WorkspaceEdit();
        rename.renameFile(source, note);
        assert(await workspace.applyEdit(rename));
        await waitForHistory(note, previous, true);
        const doc = await workspace.openTextDocument(note);
        await window.showTextDocument(doc);
        await replaceAndSave(doc, finalText);
    } else {
        assert.strictEqual(context.workspaceState.get('priorSession'), 'private local workspace');
        assert.strictEqual(context.globalState.get('savedPreference'), 'local only');
        await waitForHistory(note, previous, true);
    }
    const doc = await workspace.openTextDocument(note);
    await window.showTextDocument(doc);
    assert.strictEqual(doc.getText(), finalText);
    fs.writeFileSync(process.env.LUMEN_RECEIPT, JSON.stringify({session:process.env.LUMEN_SESSION, version:vscode.version}));
};
