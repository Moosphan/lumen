"""Bounded, local Amp inventory and workspace evidence."""
import re
from itertools import islice

from .agent_ide import _local_folder_uri, parse_jsonc


_NAME = 'Amp'
_THREAD_ID = re.compile(r'T-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.ASCII)
_SETTINGS = ('settings.json', 'settings.jsonc')
_DESKTOP_ID = 'com.ampcode.amp.macos'
_DATA_ROOTS = {('.local', 'share', 'amp'), ('.config', 'amp'),
               ('library', 'application support', _DESKTOP_ID), ('library', 'caches', _DESKTOP_ID)}


def protected_artifact(path):
    parts = tuple(part.casefold() for part in path.parts)
    return (parts[-2:] in {('.amp', name) for name in _SETTINGS}
            or any(parts[index:index + length] in _DATA_ROOTS
                   for index in range(len(parts)) for length in (2, 3))
            or any(parts[index:index + 2] == ('.amp', 'file-changes') for index in range(len(parts))))


def _thread_folders(metadata, path):
    value = metadata.read_json(path, dict, limit=256 * 1024)
    if (value is None or value.get('id') != path.stem or type(value.get('v')) is not int or value['v'] < 0
            or type(value.get('created')) is not int or value['created'] < 0
            or value.get('executor') not in (None, 'local')):
        return
    env = value.get('env')
    initial = env.get('initial') if isinstance(env, dict) else None
    if not isinstance(initial, dict) or initial.get('executor') not in (None, 'local'):
        return
    platform = initial.get('platform')
    trees = initial.get('trees')
    if not isinstance(platform, dict) or platform.get('client') != 'CLI' or not isinstance(trees, list):
        return
    if len(trees) > metadata.limit:
        metadata.issue(path, 'Amp 会话的工程位置超过本次读取上限')
    for tree in islice(trees, metadata.limit):
        if isinstance(tree, dict):
            yield _local_folder_uri(tree.get('uri'))


def discover_amp(metadata, store, add):
    # The January 2026 CLI persisted thread JSON. The modern client uses server-owned threads.
    # https://registry.npmjs.org/@sourcegraph%2famp/0.0.1768176116-ged0d88
    # https://ampcode.com/security
    home = metadata.home
    store(_NAME, home / '.amp/file-changes', 'Amp 旧版文件变动备份')
    store(_NAME, home / '.config/amp', 'Amp CLI 配置')
    store(_NAME, home / 'Library/Application Support' / _DESKTOP_ID, 'Amp 桌面应用数据')
    store(_NAME, home / 'Library/Caches' / _DESKTOP_ID, 'Amp 桌面缓存')
    cli_cache = metadata.configured('XDG_CACHE_HOME') or home / '.cache'
    store(_NAME, cli_cache / 'amp', 'Amp CLI 缓存')
    defaults = home / '.local/share'
    legacy = metadata.configured('XDG_DATA_HOME')
    for base in dict.fromkeys((defaults, legacy)):
        if base is None:
            continue
        root = base / 'amp'
        if store(_NAME, root, 'Amp CLI 会话、凭证和本地数据'):
            for path in metadata.children(root / 'threads', suffix='.json', prefix='T-'):
                if _THREAD_ID.fullmatch(path.stem):
                    for folder in _thread_folders(metadata, path):
                        add(folder, _NAME)


def discover_project_config(metadata, directory, store, add):
    # https://ampcode.com/docs/cli/settings
    root = directory / '.amp'
    if not metadata.directory(root):
        return
    for name in _SETTINGS:
        path = root / name
        payload = metadata.read(path, 64 * 1024)
        if payload is None:
            continue
        try:
            value = parse_jsonc(payload)
        except (ValueError, RecursionError):
            metadata.issue(path, 'Amp 工程配置损坏或格式未受支持')
            continue
        if isinstance(value, dict) and any(key.startswith('amp.') for key in value):
            add(str(directory), _NAME)
            store(_NAME, root, 'Amp 工程配置')
            return
