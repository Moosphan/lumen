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
const backend = process.argv[2] ?? 'simple';
assert(['simple', 'persistent'].includes(backend), 'Unknown Chromium code cache backend');
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
    throw new Error(`${options.label ?? command} failed: ${result.error ?? result.stderr?.slice(-1800)}`);
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
    label: cmd, input: JSON.stringify({ cmd, ...args }), maxBuffer: 64 * 1024 * 1024,
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
    `--${backend === 'persistent' ? 'enable' : 'disable'}-features=PersistentCacheForCodeCache`,
    '--v8-cache-options=code',
    '--js-flags=--wasm-tiering-budget=100 --wasm-caching-threshold=1 --wasm-caching-timeout-ms=0',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, `--disk-cache-dir=${cache}`, `--crash-dumps-dir=${lab}`, 'about:blank'], {
    cwd: lab, detached: true, stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, HOME: home, CFFIXED_USER_HOME: home, TMPDIR: lab, MAC_CHROMIUM_TMPDIR: lab },
  });
  const exited = once(browser, 'exit');
  let diagnostics = '';
  browser.stderr.setEncoding('utf8');
  browser.stderr.on('data', (text) => { diagnostics = (diagnostics + text).slice(-3500); });
  let socket;
  try {
    const url = await eventually('isolated Chrome DevTools endpoint', () => {
      if (browser.exitCode !== null || browser.signalCode !== null) throw new Error(diagnostics);
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
      assert.equal(await evaluate('window.lumenWasm'), 42);
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

const script = `let counter = 0;\n${'counter += 1;\n'.repeat(1800)}
window.lumenWasm = WebAssembly.instantiateStreaming(fetch('/fixture.wasm')).then(({ instance }) => {
  window.lumenWasmInstance = instance;
  let sum = 0;
  for (let call = 0; call < 10000; call++) sum = instance.exports.add(19, 23);
  return sum;
});
window.lumenResult = counter;\n`;
// (module (func (export "add") (param i32 i32) (result i32)
//   local.get 0 local.get 1 i32.add))
const wasm = Buffer.concat([
  Buffer.from('0061736d0100000001070160027f7f017f030201000707010361646400000a09010700200020016a0b', 'hex'),
  // A custom section keeps the wire payload above historical cache thresholds
  // without making the compiled function or its cache entry large.
  Buffer.from([0, 0x81, 0x80, 0x08, 0]),
  Buffer.alloc(128 * 1024),
]);
const server = createServer((request, response) => {
  if (request.url === '/fixture.js') {
    response.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'public, max-age=3600' });
    response.end(script);
  } else if (request.url === '/fixture.wasm') {
    response.writeHead(200, { 'Content-Type': 'application/wasm', 'Cache-Control': 'public, max-age=3600' });
    response.end(wasm);
  } else if (request.url?.startsWith('/fixture?')) {
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
    response.end('<!doctype html><title>Lumen fixture</title><script src="/fixture.js"></script><p>Browser fixture</p>');
  } else { response.writeHead(404).end(); }
});

try {
  // The hosted image can add Finder attributes to the framework. Verify the
  // signed software and publisher without imposing the package's own xattr policy.
  run('/usr/bin/codesign', ['--verify', '-R',
    '=anchor apple generic and identifier "com.google.Chrome" and certificate leaf[subject.OU] = "EQHXZ8M8AV"', app]);
  const version = run('/usr/bin/defaults', ['read', join(app, 'Contents/Info'), 'CFBundleShortVersionString']).trim();
  console.log(`Testing signed Google Chrome ${version} with the ${backend} code cache backend.`);
  mkdirSync(profile, { recursive: true });
  mkdirSync(cache, { recursive: true });
  writeFileSync(join(lab, 'chrome.sb'), `(version 1)
(allow default)
(deny network-outbound (remote ip "*:*"))
(allow network-outbound (remote ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
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
    if (backend === 'simple') {
      await eventually('Chrome to write the WebAssembly cache', () =>
        readdirSync(join(cache, 'Default/Code Cache/wasm')).some((name) => /^[0-9a-f]{16}_0$/.test(name)));
    }
  });

  const protectedPaths = ['Default/Local Storage', 'Default/Service Worker/CacheStorage']
    .map((name) => join(profile, name));
  protectedPaths.push(...['Default/Cookies', 'Default/Network/Cookies']
    .map((name) => join(profile, name)).filter(existsSync));
  const protectedBefore = protectedPaths.flatMap(contents);
  assert(protectedBefore.some(([path]) => path.endsWith('/Cookies')), 'Chrome did not create a cookies database');
  const systemExclusions = readdirSync('/Applications').map((name) => join('/Applications', name))
    .filter((path) => path !== app);
  engine('settings', { local_exclude: [...systemExclusions, '/System/Applications'] });
  engine('scan_local', { startup: true });
  const scan = engine('scan_app', { path: app });
  if (backend === 'persistent') {
    const directory = join(cache, 'Default/Code Cache/pc');
    const before = contents(directory);
    assert(before.some(([path]) => readFileSync(path).subarray(0, 16).equals(Buffer.from('SQLite format 3\0'))),
      'Chrome did not leave a SQLite-backed Persistent Cache database');
    const paths = before.map(([path]) => path);
    const rows = scan.files.filter((file) => paths.includes(file.path));
    assert.equal(rows.length, paths.length);
    assert(rows.every((file) => file.cache_backend === 'persistent' && file.decision === 'keep' &&
      file.app_path === app && !file.cleanup_kind), 'Persistent Cache files should be attributed and kept together');
    const result = engine('run', { scan_id: scan.scan_id, paths, action: 'quarantine' });
    assert.deepEqual(result.last_action.items.map((entry) => entry.status), paths.map(() => 'skipped'));
    assert.deepEqual(contents(directory), before);
  } else {
    verifySimpleCache(scan, origin, protectedPaths, protectedBefore);
  }
  await session(origin, 1, async (evaluate) => {
    const reopened = await evaluate(`(async () => {
      const offline = await caches.match('/local-only');
      return [localStorage.getItem('lumen-local'), document.cookie.includes('lumen=' + ${saved}), await offline?.text()];
    })()`);
    assert.deepEqual(reopened, [credential, true, credential]);
  });
  if (backend === 'persistent') {
    console.log(`Real Google Chrome ${version}: Persistent Cache files were attributed, excluded from isolation and byte-identical; restart preserved WebAssembly execution and site data.`);
  } else {
    console.log(`Real Google Chrome ${version}: JavaScript and WebAssembly caches quarantined and restored byte-for-byte; after restart, WebAssembly executed and cookies, local storage and offline Cache Storage remained readable.`);
  }
} finally {
  server.close();
  rmSync(lab, { recursive: true, force: true });
}

function verifySimpleCache(scan, origin, protectedPaths, protectedBefore) {
  const entries = scan.files.filter((file) =>
    file.cleanup_kind === 'compiled_code' && file.decision === 'review' && file.app_path === app);
  const inventory = scan.files.filter((file) => file.path.includes('/Code Cache/'))
    .map((file) => [file.path.slice(cache.length + 1), file.size, file.reason]);
  const jsEntry = entries.find((file) => file.cache_language === 'JavaScript' && file.size > 1024 &&
    readFileSync(file.path).includes(`${origin}/fixture.js`));
  const wasmEntry = entries.find((file) => file.cache_language === 'WebAssembly' &&
    readFileSync(file.path).includes(`${origin}/fixture.wasm`));
  assert(jsEntry && wasmEntry, `Expected real JavaScript and WebAssembly entries: ${JSON.stringify(inventory.slice(0, 25))}`);
  const targets = [jsEntry.path, wasmEntry.path];
  const prior = targets.map(digest);
  function quarantine(snapshot) {
    const result = engine('run', { scan_id: snapshot.scan_id, paths: targets, action: 'quarantine' });
    assert.deepEqual(result.last_action.items.map((entry) => entry.status), ['quarantined', 'quarantined'],
      JSON.stringify(result.last_action.items));
    assert(targets.every((path) => !existsSync(path)));
    assert.deepEqual(protectedPaths.flatMap(contents), protectedBefore);
    return result.last_action.id;
  }
  const batch = quarantine(scan);
  engine('restore', { batch_id: batch });
  assert.deepEqual(targets.map(digest), prior);
  quarantine(engine('scan_app', { path: app }));
}
