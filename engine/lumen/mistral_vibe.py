"""Read bounded project evidence from Mistral Vibe's local session stores."""
from hashlib import sha256
import json
import os
import re

from .integrations import recorded_path


_AGENT = 'Mistral Vibe'
_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
_LEGACY = re.compile(r'session_[0-9]{8}_[0-9]{6}_([0-9a-f]{8})')
_GENERATION = re.compile(r'[0-9]{16}')
_UNIFIED_FORMAT = 'mistral.vibe.unified-session-store/v1'
_HOME_FILES = ('config.toml', 'hooks.toml', '.env', 'vibehistory', 'trusted_folders.toml',
               'local_projects.toml', 'projects.toml')
_HOME_STORES = {
    'worktrees': 'Mistral Vibe 管理的工作树',
    'scratch': 'Mistral Vibe 临时工作目录',
}


def _unified_generation(metadata, folder):
    # https://github.com/mistralai/mistral-vibe/blob/v3.1.5/harness/runtimes/python/python/mistralai_vibe_local_harness/vibe/_storage.py
    pointer_path = folder / 'CURRENT'
    pointer = metadata.read_json(pointer_path, dict, limit=8192)
    if pointer is None or pointer.get('store_format') != _UNIFIED_FORMAT or pointer.get('session_id') != folder.name:
        return False
    minor = pointer.get('store_format_minor', 1)
    if type(minor) is not int or not 1 <= minor <= 11:
        metadata.issue(pointer_path, 'Mistral Vibe 会话存储版本未受支持')
        return False
    generation = pointer.get('generation')
    sequence = pointer.get('snapshot_sequence')
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation) or type(sequence) is not int or sequence < 0:
        return False
    path = folder / 'generations' / generation / 'manifest.json'
    payload = metadata.read(path, 64 * 1024)
    if payload is None:
        return False
    # Vibe writes a final newline after the canonical bytes used for the digest.
    if payload.endswith(b'\n') and sha256(payload[:-1]).hexdigest() == pointer.get('manifest_sha256'):
        try:
            manifest = json.loads(payload)
        except (ValueError, RecursionError):
            manifest = None
        if (isinstance(manifest, dict) and type(manifest.get('manifest_version')) is int
                and manifest['manifest_version'] == 1 and manifest.get('session_id') == folder.name
                and manifest.get('generation') == generation
                and type(manifest.get('snapshot_sequence')) is int and manifest['snapshot_sequence'] == sequence):
            return True
    metadata.issue(path, 'Mistral Vibe 当前会话清单与指针不一致')
    return False


def _legacy_session(metadata, folder, entry, short_id):
    # https://github.com/mistralai/mistral-vibe/blob/v2.26.1/vibe/core/session/session_logger.py
    identity = entry.get('session_id')
    if not isinstance(identity, str) or not _ID.fullmatch(identity) or not identity.startswith(short_id):
        return False
    messages = folder / 'messages.jsonl'
    try:
        with metadata.open(messages) as stream:
            return stream is not None and (os.fstat(stream.fileno()).st_size > 0 or entry.get('total_messages') == 0)
    except OSError as error:
        metadata.failed(messages, error)
        return False


def _session_projects(entry, add):
    # https://github.com/mistralai/mistral-vibe/blob/v3.1.5/vibe/core/session/session_index.py
    if entry.get('runtime') not in (None, 'local'):
        return
    environment = entry.get('environment')
    working = recorded_path(environment.get('working_directory')) if isinstance(environment, dict) else None
    if working is None:
        return
    add(working)
    origin = recorded_path(entry.get('origin_directory'))
    if origin is not None and origin != working:
        add(origin)


def _sessions(metadata, root, store, add, *, dedicated):
    if dedicated and not store(_AGENT, root, 'Mistral Vibe 会话与恢复数据'):
        return
    for folder in metadata.children(root / 'unified', directories=True):
        if not _ID.fullmatch(folder.name) or not _unified_generation(metadata, folder):
            continue
        if not dedicated:
            store(_AGENT, folder, 'Mistral Vibe 自定义统一会话')
        entry = metadata.read_json(folder / 'meta.json', dict, limit=128 * 1024)
        if entry and entry.get('session_id') == folder.name:
            _session_projects(entry, add)
    for folder in metadata.children(root, directories=True, prefix='session_'):
        match = _LEGACY.fullmatch(folder.name)
        if match is None:
            continue
        entry = metadata.read_json(folder / 'meta.json', dict, limit=128 * 1024)
        if entry and _legacy_session(metadata, folder, entry, match[1]):
            if not dedicated:
                store(_AGENT, folder, 'Mistral Vibe 自定义历史会话')
            _session_projects(entry, add)


def discover_mistral_vibe(metadata, store, add):
    # https://github.com/mistralai/mistral-vibe/blob/v3.1.5/vibe/core/paths/_vibe_home.py
    # https://github.com/mistralai/mistral-vibe/blob/v3.1.5/vibe/core/config/layers/environment.py
    default = metadata.home / '.vibe'
    configured = metadata.configured('VIBE_HOME')
    homes = tuple(dict.fromkeys(path for path in (default, configured) if path is not None))
    managed = {home / name for home in homes for name in _HOME_STORES}

    def add_project(path):
        if path not in managed and not managed.intersection(path.parents):
            add(str(path), _AGENT)

    store(_AGENT, default, 'Mistral Vibe 用户配置与工具数据')
    roots = {home / 'logs/session': home == default for home in homes}
    custom_sessions = metadata.configured('VIBE_SESSION_LOGGING__SAVE_DIR')
    if custom_sessions is not None:
        roots.setdefault(custom_sessions, False)
    for home in homes:
        for name, evidence in _HOME_STORES.items():
            store(_AGENT, home / name, evidence)
        if home != default:
            for name in _HOME_FILES:
                store(_AGENT, home / name, 'Mistral Vibe 自定义配置与历史', file=True)
    for root, dedicated in roots.items():
        _sessions(metadata, root, store, add_project, dedicated=dedicated)


def protected_artifact(path):
    return '.vibe' in path.parts[:-1]
