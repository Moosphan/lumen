// Activated by a normal VS Code development host in the disposable offline VM.
const assert = require('assert');
const crypto = require('crypto');
const fs = require('fs');
const vscode = require('vscode');

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const previous = 'Prior local revision\nPrivate sample draft\n';
const finalText = 'Final local revision\nPrivate sample draft\n';

async function waitForHistory(note, indexed, timeout) {
    const {workspace, Uri, FileType} = vscode;
    const history = Uri.file(process.env.LUMEN_HISTORY);
    const deadline = Date.now() + timeout;
    let diagnostic = '';
    do {
        let payloads = 0, indexes = 0;
        try {
            for (const [name, type] of await workspace.fs.readDirectory(history)) {
                if (type !== FileType.Directory) continue;
                const directory = Uri.joinPath(history, name);
                const entries = await workspace.fs.readDirectory(directory);
                let names = entries.filter(([n, t]) => t === FileType.File && n !== 'entries.json').map(([n]) => n);
                if (indexed) {
                    if (!entries.some(([n]) => n === 'entries.json')) continue;
                    indexes++;
                    const index = JSON.parse(Buffer.from(await workspace.fs.readFile(Uri.joinPath(directory, 'entries.json'))).toString('utf8'));
                    const resource = Uri.parse(index.resource);
                    if (resource.scheme !== 'file' || fs.realpathSync(resource.fsPath) !== fs.realpathSync(note.fsPath)) continue;
                    names = index.entries.map(e => e.id);
                }
                for (const n of names) {
                    payloads++;
                    const data = Buffer.from(await workspace.fs.readFile(Uri.joinPath(directory, n)));
                    if (data.equals(Buffer.from(previous))) {
                        return crypto.createHash('sha256').update(data).digest('hex');
                    }
                }
            }
            diagnostic = 'indexes=' + indexes + ', payloads=' + payloads;
        } catch (error) {
            diagnostic = error.message;
        }
        await pause(200);
    } while (Date.now() < deadline);
    throw new Error('Missing ' + (indexed ? 'indexed history' : 'saved history') + ' for story.txt: ' + diagnostic);
}

async function replaceAndSave(doc, text) {
    const edit = new vscode.WorkspaceEdit();
    edit.replace(doc.uri, new vscode.Range(new vscode.Position(0, 0), doc.positionAt(doc.getText().length)), text);
    assert(await vscode.workspace.applyEdit(edit));
    assert(doc.isDirty && doc.getText() === text);
    assert(await doc.save());
}

async function verify(context) {
    const {workspace, window, Uri} = vscode;
    assert.strictEqual(vscode.version, '1.95.3');
    assert.strictEqual(workspace.workspaceFolders.length, 1);
    const folder = workspace.workspaceFolders[0].uri;
    const note = Uri.joinPath(folder, 'story.txt');
    const settings = workspace.getConfiguration('editor', note);
    assert.strictEqual(settings.get('tabSize'), 3);
    assert.strictEqual(settings.get('insertSpaces'), false);
    assert.strictEqual(workspace.getConfiguration('workbench.localHistory', note).get('enabled'), true);
    const asset = await workspace.fs.readFile(Uri.joinPath(folder, 'assets', 'offline.bin'));
    assert.strictEqual(Buffer.from(asset).toString('hex'), '004c756d656eff');

    const doc = await workspace.openTextDocument(note);
    await window.showTextDocument(doc);
    let digest;
    if (process.env.LUMEN_SESSION === 'before') {
        assert.strictEqual(doc.getText(), 'Private sample draft\n');
        // The history tracker is registered asynchronously. Give it a distinct
        // save event on each retry; wait for the prior payload before editing again.
        for (let attempt = 1; attempt <= 3; attempt++) {
            await replaceAndSave(doc, 'Editing pass ' + attempt + '\n');
            await pause(350);
            await replaceAndSave(doc, previous);
            try {
                digest = await waitForHistory(note, false, 4000);
                break;
            } catch (error) {
                if (attempt === 3) throw error;
            }
        }
        await replaceAndSave(doc, finalText);
        await context.workspaceState.update('priorSession', 'private local workspace');
        await context.globalState.update('savedPreference', 'local only');
    } else {
        assert.strictEqual(process.env.LUMEN_SESSION, 'after');
        assert.strictEqual(context.workspaceState.get('priorSession'), 'private local workspace');
        assert.strictEqual(context.globalState.get('savedPreference'), 'local only');
        digest = await waitForHistory(note, true, 12000);
    }
    assert.strictEqual(doc.getText(), finalText);
    return digest;
}

exports.activate = context => {
    // Return control to the workbench before running the proof or issuing Quit.
    setImmediate(async () => {
        let report = {session: process.env.LUMEN_SESSION, version: vscode.version};
        try {
            report.history_sha = await verify(context);
            report.ok = true;
        } catch (error) {
            report.ok = false;
            report.error = error.stack || String(error);
        }
        fs.writeFileSync(process.env.LUMEN_RECEIPT, JSON.stringify(report));
        await vscode.commands.executeCommand('workbench.action.quit');
    });
};
