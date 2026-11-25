// Real Chrome exercise restricted to a disposable GitHub-hosted Mac.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

if (process.env.GITHUB_ACTIONS !== 'true' || process.env.RUNNER_ENVIRONMENT !== 'github-hosted' ||
    process.platform !== 'darwin' || process.arch !== 'arm64') {
  throw new Error('Requires an ARM64 GitHub-hosted macOS runner');
}

const app = '/Applications/Google Chrome.app';
const executable = join(app, 'Contents/MacOS/Google Chrome');
const lab = mkdtempSync('/private/tmp/lumen-chrome-');
const home = join(lab, 'fixture-home');
const state = join(lab, 'state');
const profile = join(home, 'Library/Application Support/Google/Chrome');
const cache = join(home, 'Library/Caches/Google/Chrome');
const credential = 'isolated-browser-fixture';

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 60_000, ...options });
  if (result.error || result.status !== 0) {
    throw new Error(`${command} failed: ${result.error ?? result.stderr?.slice(-1800)}`);
  }
  return result.stdout;
}

function engine(cmd, args = {}) {
  const invoke = [
    'import json, sys',
    'from engine.lumen.service import dispatch',
    'print(json.dumps(dispatch(sys.argv[1], json.load(sys.stdin), local_home=sys.argv[2], include_system=True)))',
  ].join('\n');
  return JSON.parse(run('/usr/bin/python3', ['-B', '-c', invoke, state, home], {
    input: JSON.stringify({ cmd, ...args }), maxBuffer: 64 * 1024 * 1024,
  }));
}

function digest(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function contents(path) {
  if (statSync(path).isFile()) return [[path, digest(path)]];
  const files = [];
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    const child = join(path, entry.name);
    if (entry.isDirectory()) files.push(...contents(child));
    else if (entry.isFile()) files.push([child, digest(child)]);
  }
  return files.sort((a, b) => a[0].localeCompare(b[0]));
}

function command(socket, method, params = {}, sessionId) {
  const id = ++command.serial;
  return new Promise((resolve, reject) => {
    function finish(error, result) {
      clearTimeout(timer);
      socket.removeEventListener('message', receive);
      socket.removeEventListener('close', closed);
      if (error) reject(error);
      else resolve(result);
    }
    function receive(event) {
      const message = JSON.parse(event.data);
      if (message.id === id) finish(message.error && new Error(`${method}: ${message.error.message}`), message.result);
    }
    function closed() { finish(new Error(`Chrome closed during ${method}`)); }
    const timer = setTimeout(() => finish(new Error(`${method} timed out`)), 6000);
    socket.addEventListener('message', receive);
    socket.addEventListener('close', closed);
    socket.send(JSON.stringify({ id, method, params, sessionId }));
  });
}
command.serial = 0;

async function eventually(description, attempt) {
  const until = Date.now() + 18_000;
  let error;
  do {
    try {
      const result = await attempt();
      if (result) return result;
    } catch (caught) { error = caught; }
    await sleep(150);
  } while (Date.now() < until);
  throw new Error(`Timed out waiting for ${description}${error ? `: ${error.message}` : ''}`);
}

async function session(origin, count, verifyContent) {
  const marker = join(profile, 'DevToolsActivePort');
  rmSync(marker, { force: true });
  const isolation = join(lab, 'chrome.sb');
  const browser = spawn('/usr/bin/sandbox-exec', ['-D', `LAB=${lab}`, '-f', isolation, executable,
    '--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking', '--disable-component-update',
    '--disable-sync', '--disable-crash-reporter', '--no-first-run', '--no-default-browser-check', '--use-mock-keychain',
    '--disable-features=PersistentCacheForCodeCache', '--v8-cache-options=code',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, `--disk-cache-dir=${cache}`, `--crash-dumps-dir=${lab}`, 'about:blank'], {
    cwd: lab, detached: true, stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, TMPDIR: lab },
  });
  const exited = once(browser, 'exit');
  let diagnostics = '';
  browser.stderr.setEncoding('utf8');
  browser.stderr.on('data', (text) => { diagnostics = (diagnostics + text).slice(-3500); });
  let socket;
  try {
    const url = await eventually('isolated Chrome DevTools endpoint', () => {
      if (browser.exitCode !== null) throw new Error(diagnostics);
      const [port, route] = readFileSync(marker, 'utf8').trim().split('\n');
      return /^\d+$/.test(port) && /^\/devtools\/browser\/[\w-]+$/.test(route)
        ? `ws://127.0.0.1:${port}${route}` : null;
    });
    socket = new WebSocket(url);
    await once(socket, 'open', { signal: AbortSignal.timeout(7000) });
    const { targetId } = await command(socket, 'Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await command(socket, 'Target.attachToTarget', { targetId, flatten: true });
    async function evaluate(expression) {
      const value = await command(socket, 'Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
      if (value.exceptionDetails) throw new Error(value.exceptionDetails.text);
      return value.result.value;
    }
    for (let page = 0; page < count; page++) {
      const url = `${origin}/fixture?pass=${page}`;
      const navigation = await command(socket, 'Page.navigate', { url }, sessionId);
      assert(!navigation.errorText, navigation.errorText);
      await eventually('fixture script to execute', () => evaluate(`location.href === ${JSON.stringify(url)} && window.lumenResult === 1800`));
      await sleep(300);
    }
    await verifyContent(evaluate);
    await command(socket, 'Browser.close').catch(() => {});
    const [code, signal] = await Promise.race([exited, sleep(8000, undefined, { ref: false }).then(() => [null, 'timeout'])]);
    assert.equal(code, 0, `Chrome did not exit normally (${signal}): ${diagnostics}`);
  } finally {
    socket?.close();
    if (browser.exitCode === null && browser.signalCode === null) {
      try { process.kill(-browser.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      await exited;
    }
  }
}

const script = `let counter = 0;\n${'counter += 1;\n'.repeat(1800)}window.lumenResult = counter;\n`;
const server = createServer((request, response) => {
  if (request.url === '/fixture.js') {
    response.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'public, max-age=3600' });
    response.end(script);
  } else if (request.url?.startsWith('/fixture?')) {
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
    response.end('<!doctype html><title>Lumen fixture</title><script src="/fixture.js"></script><p>Browser fixture</p>');
  } else { response.writeHead(404).end(); }
});

try {
  run('/usr/bin/codesign', ['--verify', '--strict', '-R', '=anchor apple generic and identifier "com.google.Chrome"', app]);
  const version = run('/usr/bin/defaults', ['read', join(app, 'Contents/Info'), 'CFBundleShortVersionString']).trim();
  console.log(`Testing signed Google Chrome ${version} with the Chromium Simple backend.`);
  mkdirSync(profile, { recursive: true });
  mkdirSync(cache, { recursive: true });
  writeFileSync(join(lab, 'chrome.sb'), `(version 1)
(allow default)
(deny network-outbound (remote ip "*:*"))
(allow network-outbound (remote ip "localhost:*"))
(deny file-write*)
(allow file-write* (subpath (param "LAB")) (literal "/dev/null"))
`);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  const saved = JSON.stringify(credential);
  await session(origin, 4, async (evaluate) => {
    assert(await evaluate(`(async () => {
      localStorage.setItem('lumen-local', ${saved});
      document.cookie = 'lumen=' + ${saved} + '; Max-Age=3600; Path=/';
      const store = await caches.open('lumen-offline');
      await store.put('/local-only', new Response(${saved}));
      return true;
    })()`));
  });

  const protectedPaths = ['Default/Local Storage', 'Default/Service Worker/CacheStorage']
    .map((name) => join(profile, name));
  protectedPaths.push(...['Default/Cookies', 'Default/Network/Cookies']
    .map((name) => join(profile, name)).filter(existsSync));
  const protectedBefore = protectedPaths.flatMap(contents);
  assert(protectedBefore.some(([path]) => path.endsWith('/Cookies')), 'Chrome did not create a cookies database');
  engine('scan_local', { startup: true });
  let scan = engine('scan_app', { path: app });
  const item = scan.files.find((file) => file.cleanup_kind === 'compiled_code' && file.decision === 'review' && file.app_path === app);
  if (!item) {
    const candidates = scan.files.filter((file) => file.path.includes('/Code Cache/')).map((file) => [file.path, file.reason]);
    throw new Error(`No real Chromium Simple generated-code entry passed verification: ${JSON.stringify(candidates.slice(0, 15))}`);
  }
  const target = item.path;
  const prior = digest(target);
  function quarantine(snapshot) {
    const result = engine('run', { scan_id: snapshot.scan_id, paths: [target], action: 'quarantine' });
    assert.deepEqual(result.last_action.items.map((entry) => entry.status), ['quarantined'], JSON.stringify(result.last_action.items));
    assert(!existsSync(target));
    assert.deepEqual(protectedPaths.flatMap(contents), protectedBefore);
    return result.last_action.id;
  }
  const batch = quarantine(scan);
  engine('restore', { batch_id: batch });
  assert.equal(digest(target), prior);
  scan = engine('scan_app', { path: app });
  quarantine(scan);
  await session(origin, 1, async (evaluate) => {
    const reopened = await evaluate(`(async () => {
      const offline = await caches.match('/local-only');
      return [localStorage.getItem('lumen-local'), document.cookie.includes('lumen=' + ${saved}), await offline?.text()];
    })()`);
    assert.deepEqual(reopened, [credential, true, credential]);
  });
  console.log(`Real Google Chrome ${version}: generated cache quarantined and restored byte-for-byte; restart preserved cookies, local storage and offline Cache Storage.`);
} finally {
  server.close();
  rmSync(lab, { recursive: true, force: true });
}
