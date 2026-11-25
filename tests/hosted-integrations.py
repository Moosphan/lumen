"""Real vendor integrations on disposable GitHub-hosted runners."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.lumen import docker_cache, model_recovery, service
from engine.lumen.local_http import fetch_json


def docker(root):
    (root / 'Dockerfile').write_text('FROM scratch\nCOPY payload /payload\n')
    (root / 'payload').write_bytes(os.urandom(65536))
    subprocess.run(['/usr/bin/docker', 'build', '--builder', 'default', '--no-cache', str(root)], timeout=50, check=True)

    # The Linux runner supplies a system daemon. On a Mac, the app additionally
    # validates ownership and identity of Docker Desktop's per-user socket.
    connection = docker_cache._UnixHTTPConnection(Path('/var/run/docker.sock'), timeout=10)
    result = docker_cache.summarize(fetch_json(connection, docker_cache.ENDPOINT, timeout=10, limit=8 * 1024 * 1024))
    if result['entries'] < 1 or result['reported_bytes'] < 65536:
        raise RuntimeError(f'The real Docker engine did not report the built cache: {result}')
    print(f"Real Docker Engine: {result['entries']} integrated-builder records, {result['reported_bytes']} B.")


def hugging_face(root):
    # Public LFS-compatible Xet artifact pinned to the official pointer and commit.
    repository = 'peft-internal-testing/tiny-random-gpt2'
    revision = '2f18a2874922d4cc4777cdf9fbf66cfa057a691a'
    filename = 'model.safetensors'
    digest = '8111d5afb0715dbf5a31396d31432cb56370ba23f6650a035ea0fc8a20b4e500'
    size = 453864
    xet = 'f8accece953fd366d4ce30597b97acc1ccedc3c785187a5ef6ecb4a8e1755122'

    home, state, seed = root / 'home', root / 'state', root / 'seed'
    destination = home / 'Downloads'
    destination.mkdir(parents=True)
    seed.mkdir()
    hub = home / '.cache/huggingface/hub'
    proof = model_recovery.RecoveryProof(repository, revision, filename, digest, size, hub, (), _xet_id=xet)
    source = Path(model_recovery.recover(proof, seed)['path'])

    repo = hub / ('models--' + repository.replace('/', '--'))
    blobs = repo / 'blobs'
    blobs.mkdir(parents=True)
    blob = blobs / digest
    source.rename(blob)
    snapshot = repo / 'snapshots' / revision / filename
    snapshot.parent.mkdir(parents=True)
    snapshot.symlink_to(os.path.relpath(blob, snapshot.parent))

    def call(command, **arguments):
        return service.dispatch(state, {'cmd': command, **arguments}, local_home=home)

    scan = call('scan_local', startup=True)
    model, = [item for item in scan['model_inventory']['models'] if item['version'] == revision]
    pin = call('model_pin', model_id=model['id'], scan_id=scan['scan_id'], filename=filename,
               digest='sha256:' + digest, revision=revision, bytes=size)
    blob.unlink()
    call('scan_local', startup=True)
    result = call('model_recover', recipe_id=pin['recipe']['id'], destination=str(destination))

    restored = (destination / filename).read_bytes()
    if result['status'] != 'verified' or len(restored) != size or hashlib.sha256(restored).hexdigest() != digest:
        raise RuntimeError('Hugging Face recovery did not restore the pinned artifact')
    header_size = int.from_bytes(restored[:8], 'little')
    tensors = json.loads(restored[8:8 + header_size])
    if not any(name != '__metadata__' for name in tensors):
        raise RuntimeError('Restored artifact has no Safetensors header entries')
    print(f'Real Hugging Face: restored and verified {size} B from revision {revision[:12]}.')


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Run this integration check only on a disposable GitHub-hosted runner.')
    checks = {'docker': docker, 'hugging-face': hugging_face}
    if len(sys.argv) != 2 or sys.argv[1] not in checks:
        raise SystemExit('Usage: hosted-integrations.py docker|hugging-face')
    with tempfile.TemporaryDirectory(prefix='lumen-vendor-') as work:
        checks[sys.argv[1]](Path(work).resolve())
