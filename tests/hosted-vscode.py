#!/usr/bin/env python3
"""Live VS Code 1.95.3 workflow. Execute only in a GitHub-hosted ARM macOS VM."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit


ARCHIVE_URL = 'https://update.code.visualstudio.com/1.95.3/darwin-arm64/stable'
ARCHIVE_SHA = 'fe01f564777afb75703e748ada00f1204f0495be54947d6ffb513e5a5eb16a08'
ROOT = Path(__file__).resolve().parents[1]
DEADLINE = time.monotonic() + 225


def run(args, env=None, cwd=None, payload=None, timeout=60):
    budget = min(timeout, DEADLINE - time.monotonic())
    if budget <= 0:
        raise RuntimeError('Four-minute verification budget exhausted')
    with subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            out, err = process.communicate(payload, timeout=budget)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise RuntimeError(f'{Path(args[0]).name} exceeded {budget:.0f}s')
        if process.returncode:
            detail = (out[-2000:] + b'\n' + err[-2000:]).decode(errors='replace')
            raise RuntimeError(f'{Path(args[0]).name} exited {process.returncode}. {detail}')
        return out


def summary(message):
    print(message, flush=True)
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a', encoding='utf-8') as stream:
            print(message, file=stream)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def contents(folder):
    return {str(p.relative_to(folder)): sha(p) for p in folder.rglob('*') if p.is_file()}


def verify(lab):
    home = lab / 'home'
    apps = home / 'Applications'
    work = home / 'project'
    profile = home / 'Library/Application Support/Code'
    ext = lab / 'proof-extension'
    history = profile / 'User/History'
    for folder in [apps, work / '.vscode', work / 'assets', profile / 'User', ext,
                   lab / 'extensions', lab / 'temp']:
        folder.mkdir(parents=True)
    env = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'HOME':str(home), 'TMPDIR':str(lab / 'temp'),
           'LANG':'en_US.UTF-8', 'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':'/dev/null',
           'PYTHONDONTWRITEBYTECODE':'1', 'LUMEN_STATE_DIR':str(lab / 'state'),
           'LUMEN_HISTORY':str(history)}
    archive = lab / 'archive.zip'
    run(['/usr/bin/curl','-q','--fail','--silent','--show-error','--location','--proto','=https',
         '--proto-redir','=https','--connect-timeout','12','--max-time','75','-o',str(archive),ARCHIVE_URL], env, lab, timeout=78)
    if sha(archive) != ARCHIVE_SHA:
        raise ValueError('Official VS Code archive SHA-256 mismatch')
    run(['/usr/bin/ditto','-x','-k',str(archive),str(apps)], env, lab)
    app = apps / 'Visual Studio Code.app'
    run(['/usr/bin/codesign','--verify','--strict','-R',
         '=anchor apple generic and identifier "com.microsoft.VSCode" and certificate leaf[subject.OU] = "UBF8T346G9"',
         str(app)], env, lab)
    (work / 'story.txt').write_text('Private sample draft\n')
    (work / '.vscode/settings.json').write_text('{"editor.tabSize":3}')
    (work / 'assets/offline.bin').write_bytes(bytes.fromhex('004c756d656eff'))
    user_settings = profile / 'User/settings.json'
    user_settings.write_text(json.dumps({'editor.insertSpaces':False, 'telemetry.telemetryLevel':'off',
        'update.mode':'none','extensions.autoUpdate':False, 'workbench.localHistory.enabled':True,
        'workbench.localHistory.mergeWindow':0, 'git.enabled':False, 'workbench.startupEditor':'none'}))
    (ext / 'package.json').write_text(json.dumps({'name':'local-proof','publisher':'lumen','version':'0.0.1',
        'engines':{'vscode':'^1.95.0'}, 'main':'./proof.js','activationEvents':['onStartupFinished']}))
    shutil.copyfile(ROOT / 'tests/hosted-vscode.js', ext / 'proof.js')
    policy = lab / 'offline.sb'
    policy.write_text('''(version 1)
(allow default)
(deny network-outbound (remote ip "*:*"))
(allow network-outbound (remote ip "localhost:*"))
(deny file-read-data (subpath (param "RUNNER_HOME")))
(allow file-read-data (subpath (param "LAB")) (subpath (param "ENGINE")))
''')
    isolation = ['/usr/bin/sandbox-exec','-D','RUNNER_HOME='+str(Path.home()),
                 '-D','LAB='+str(lab),'-D','ENGINE='+str(ROOT/'engine'),'-f',str(policy)]
    def session(phase):
        receipt = lab / (phase + '.json')
        code = app / 'Contents/MacOS/Electron'
        run(isolation + [str(code), '--no-sandbox','--disable-gpu','--use-mock-keychain',
            '--password-store=basic','--disable-workspace-trust','--disable-updates','--disable-telemetry',
            '--skip-welcome','--skip-release-notes','--disable-extension','vscode.github-authentication',
            '--disable-extension','vscode.microsoft-authentication','--disable-extension','vscode.git',
            '--user-data-dir='+str(profile),'--extensions-dir='+str(lab/'extensions'),
            '--extensionDevelopmentPath='+str(ext),str(work)],
            dict(env, LUMEN_SESSION=phase, LUMEN_RECEIPT=str(receipt)), lab, timeout=55)
        if not receipt.is_file():
            raise RuntimeError(f'{phase} editor quit without a verification receipt')
        report = json.loads(receipt.read_text())
        if report.get('ok') is not True:
            raise RuntimeError(report.get('error') or 'Extension verification failed')
        if (report.get('session'), report.get('version')) != (phase, '1.95.3'):
            raise RuntimeError('Invalid in-editor verification receipt')
        return report['history_sha']
    def bridge(request):
        raw = run(isolation + ['/usr/bin/python3','-B',str(ROOT/'engine/bridge.py')], env, lab,
                  json.dumps(request).encode(), timeout=75)
        response = json.loads(raw)
        if not response['ok']:
            raise RuntimeError(f"Local engine {request['cmd']} failed: {response['error']}")
        return response['data']
    before_digest = session('before')
    print('First offline editor session completed and saved a genuine history revision.', flush=True)
    logs = profile / 'logs'
    real = [p for p in logs.iterdir() if re.fullmatch(r'[0-9]{8}T[0-9]{6}',p.name)
            and (p/'main.log').is_file() and (p/'main.log').stat().st_size]
    if len(real) != 1:
        raise ValueError('Expected one real editor session with a non-empty main.log')
    # Age the real log and model ten newer retention slots without eleven launches.
    then = datetime.datetime.now() - datetime.timedelta(days=45)
    expired = logs / then.strftime('%Y%m%dT%H%M%S')
    real[0].rename(expired)
    target = expired / 'main.log'
    os.utime(target, (then.timestamp(),then.timestamp()))
    for n in range(1, 11):
        newer = logs / (then + datetime.timedelta(days=n)).strftime('%Y%m%dT%H%M%S')
        newer.mkdir()
        (newer/'main.log').write_text('retained control ' + str(n))
    before_logs = contents(logs)
    del before_logs[str(target.relative_to(logs))]
    before_work, before_history, settings_sha = contents(work), contents(history), sha(user_settings)
    history_items = []
    for index in history.rglob('entries.json'):
        entry = json.loads(index.read_text())
        uri = urlsplit(entry['resource'])
        if uri.scheme == 'file' and not uri.netloc and Path(unquote(uri.path)).resolve() == work/'story.txt':
            history_items.extend(index.parent/item['id'] for item in entry['entries'])
    previous = b'Prior local revision\nPrivate sample draft\n'
    expected_digest = hashlib.sha256(previous).hexdigest()
    if before_digest != expected_digest:
        raise ValueError('Editor reported the wrong prior revision hash')
    if not any(item.is_file() and sha(item) == expected_digest for item in history_items):
        raise ValueError(f'Missing indexed prior document text ({len(history_items)} matching entries)')
    preview = bridge({'cmd':'scan', 'roots':list(map(str,[app,logs,work])), 'deep':True})
    row = next(r for r in preview['files'] if r['path'] == str(target))
    if (row['decision'], row.get('rule_id')) != ('safe','vscode.1.95.3.main-log.v1'):
        raise RuntimeError('Real log failed scan preflight: ' + row['reason'])
    result = bridge({'cmd':'run','scan_id':preview['scan_id'],'paths':[str(target)],'action':'clean'})
    if [i['status'] for i in result['last_action']['items']] != ['cleaned'] or target.exists():
        raise RuntimeError('Production clean action did not remove exactly the selected log')
    if (contents(logs),contents(work),contents(history),sha(user_settings)) != (before_logs,before_work,before_history,settings_sha):
        raise RuntimeError('A retained log, sample asset, local history entry, or setting changed during cleanup')
    if session('after') != before_digest:
        raise RuntimeError('Reopened editor could not verify the original indexed revision hash')
    if contents(work) != before_work or sha(user_settings) != settings_sha:
        raise RuntimeError('Project or preferences changed on reopening')
    for name, digest in before_history.items():
        if sha(history/name) != digest:
            raise RuntimeError('Editor history changed on reopening')
    # Reuse this verified archive for the optional uninstall/restore regression.
    # The normal suite establishes its own kernel-enforced temporary write scope.
    run([str(ROOT / 'scripts/test-isolated.sh')], dict(os.environ, LUMEN_VSCODE_SIGNED_ZIP=str(archive)),
        ROOT, timeout=100)
    summary('The full isolated filesystem suite passed with the signed archive, including bundle uninstall and restore.')
    summary('VS Code 1.95.3 ARM64 verified on the hosted VM. Two real offline sessions passed. '
            'The production action removed one aged real main.log and preserved ten synthetic retention slots, '
            'the document, settings, offline asset, extension state and local history. No signed-in account was used.')


def main():
    if not (os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('LUMEN_RUNNER_ENVIRONMENT', os.environ.get('RUNNER_ENVIRONMENT')) == 'github-hosted'
            and os.environ.get('RUNNER_OS') == 'macOS' and platform.system() == 'Darwin' and platform.machine() == 'arm64'):
        raise SystemExit('Requires an ARM64 GitHub-hosted macOS runner')
    gui = subprocess.run(['/bin/launchctl','print','gui/'+str(os.getuid())], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, timeout=5)
    if gui.returncode:
        summary('Unverified — the runner has no GUI bootstrap session. Editor launch, live cleanup and reopen were not tested.')
        return 78
    try:
        # Secure directory-fd traversal must open every ancestor. RUNNER_TEMP is
        # inside the private runner home, which the editor/engine sandbox denies.
        with tempfile.TemporaryDirectory(prefix='hosted-vscode-', dir='/private/tmp') as temp:
            verify(Path(temp).resolve())
    except Exception as exc:
        summary('Live VS Code verification incomplete. ' + str(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
