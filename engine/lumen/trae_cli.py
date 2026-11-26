"""Read bounded TRAE CLI 2.0 rollout metadata."""
import json
import os
import re

from .integrations import recorded_path
from .zstd_header import first_line


_AGENT = 'Trae CLI'
_DEFAULT = '.trae/cli'
_STORES = {'sessions': 'TRAE CLI 活动会话', 'archived_sessions': 'TRAE CLI 归档会话'}
_PROTECTED_FILES = frozenset({'auth.json', 'config.toml', 'history.jsonl', 'hooks.json', 'session_index.jsonl'})
_DATABASE = re.compile(r'(?:state|thread_history)_[0-9]{1,6}\.sqlite(?:-wal|-shm|-journal)?')
_ROLLOUT = re.compile(
    r'rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-'
    r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(\.zst)?', re.ASCII,
)
_DATE_PARTS = tuple(re.compile(pattern) for pattern in (r'[0-9]{4}', r'0[1-9]|1[0-2]', r'0[1-9]|[12][0-9]|3[01]'))
_HEADER_LIMIT = 32 * 1024
_COMPRESSED_INPUT_LIMIT = 256 * 1024


def custom_home(home, setting=os.environ.get):
    # https://trae.cn/trae-cli/install_v2.sh
    direct = setting('TRAECLI_HOME')
    if direct:
        root = recorded_path(direct)
    else:
        base = recorded_path(setting('TRAE_HOME') or setting('TRAEX_HOME'))
        root = base / 'cli' if base is not None and base != home and base not in home.parents else None
    if root is not None and root not in (home, home / _DEFAULT, *home.parents):
        return root
    return None


def protected_artifact(path, custom):
    if custom is None or custom not in path.parents:
        return False
    first, *remaining = path.relative_to(custom).parts
    return (first in _STORES or not remaining and
            (first in _PROTECTED_FILES or _DATABASE.fullmatch(first) is not None))


def _rollouts(metadata, root, dated):
    pending = [(root, 0)]
    remaining = metadata.limit
    while pending:
        if remaining == 0 or metadata.budget == 0:
            metadata.issue(root, 'TRAE CLI 会话元数据超过本次读取上限')
            return
        directory, depth = pending.pop()
        remaining -= 1
        if dated and depth < len(_DATE_PARTS):
            pending.extend((path, depth + 1) for path in metadata.children(directory, directories=True)
                           if _DATE_PARTS[depth].fullmatch(path.name))
            continue
        for path in reversed(metadata.children(directory, prefix='rollout-')):
            match = _ROLLOUT.fullmatch(path.name)
            if match is None:
                continue
            if remaining == 0 or metadata.budget == 0:
                metadata.issue(root, 'TRAE CLI 会话元数据超过本次读取上限')
                return
            remaining -= 1
            yield path, match[1], bool(match[2])


def _project(metadata, path, identity, compressed):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            if compressed:
                header, spent = first_line(stream, min(metadata.budget, _COMPRESSED_INPUT_LIMIT), _HEADER_LIMIT)
            else:
                header = stream.readline(min(metadata.budget, _HEADER_LIMIT))
                spent = len(header)
            metadata.budget -= spent
        if header is None or not header.endswith(b'\n'):
            return None
        record = json.loads(header)
        payload = record.get('payload') if isinstance(record, dict) and record.get('type') == 'session_meta' else None
        if isinstance(payload, dict) and payload.get('id') == identity:
            return recorded_path(payload.get('cwd'))
    except OSError as error:
        metadata.failed(path, error)
    except (ValueError, RecursionError):
        pass
    return None


def discover_trae_cli_rollouts(metadata, store, add):
    # https://docs.trae.cn/cli_command-line-parameters
    # https://github.com/kenn-io/agentsview/tree/d92f8630a496c9237e4fa8316aa60c3387a59f59/internal/parser/testdata/data-version-corpus/traex
    default = metadata.home / _DEFAULT
    custom = custom_home(metadata.home, metadata.setting)
    homes = (default, custom) if custom is not None else (default,)
    store(_AGENT, default, 'TRAE CLI 配置、历史与运行状态')
    found = {}
    for home in homes:
        if home == custom and not any(metadata.allowed(home / name) for name in _STORES):
            metadata.issue(home, 'TRAE CLI 自定义会话位置未获读取权限')
            continue
        for name, title in _STORES.items():
            root = home / name
            if not store(_AGENT, root, title):
                continue
            for path, identity, compressed in _rollouts(metadata, root, dated=name == 'sessions'):
                project = _project(metadata, path, identity, compressed)
                if project is None:
                    subject = '压缩会话' if compressed else '会话'
                    metadata.issue(root, f'部分 TRAE CLI {subject}首行或工作目录无法核验')
                    continue
                if found.setdefault(identity, project) != project:
                    found[identity] = None
                    metadata.issue(root, 'TRAE CLI 同一会话的工作目录冲突')
    for project in found.values():
        if project is not None:
            add(str(project), _AGENT)
