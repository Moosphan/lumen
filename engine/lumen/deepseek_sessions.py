"""Read DeepSeek Harness session identity and cwd without decoding the event body."""
import json
import re

from .integrations import recorded_path
from .zstd_header import first_frame


# https://github.com/deepseek-ai/deepseek-harness/blob/4b399c39a6d1479cd84ca591b4d7c88e24d38a96/packages/session/session-persistence-jsonl/src/format.ts
_GENERATION = re.compile(r'session(?:\.v([1-9][0-9]*))?\.jsonl(\.zstd)?')
_HEAD_LIMIT = 16 * 1024
_FRAME_LIMIT = 32 * 1024
_LATEST_SUPPORTED = 4
_SAFE = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')


def _encode_segment(value):
    encoded = value.encode('utf-16-le', errors='surrogatepass')
    units = (int.from_bytes(encoded[i:i + 2], 'little') for i in range(0, len(encoded), 2))
    return ''.join(chr(unit) if chr(unit) in _SAFE else f'~{unit:04X}' for unit in units)


def _project_key(cwd):
    readable = _encode_segment(re.sub(r'[/\\:]+', '-', cwd)).lstrip('-') or 'root'
    return '--' + readable[:251] + '--'


def _header(metadata, path, compressed):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            limit = min(_FRAME_LIMIT if compressed else _HEAD_LIMIT, metadata.budget)
            payload = stream.read(limit) if compressed else stream.readline(limit)
            metadata.budget -= len(payload)
            if compressed:
                payload = first_frame(payload, _HEAD_LIMIT)
            if payload is not None and payload.find(b'\n') == len(payload) - 1 and payload.endswith(b'\n'):
                return json.loads(payload)
    except OSError as error:
        metadata.failed(path, error)
    except (ValueError, RecursionError):
        pass
    return None


def _cwd(metadata, directory, generations):
    if len({compressed for _, compressed, _ in generations}) != 1:
        return None
    version, compressed, path = max(generations)
    if version > _LATEST_SUPPORTED:
        return None
    record = _header(metadata, path, compressed)
    if not isinstance(record, dict) or record.get('type') != 'session':
        return None
    identifier, created = record.get('id'), record.get('createdAt')
    ver, cwd = record.get('version'), record.get('cwd')
    if (type(ver) is not int or ver != version or type(created) is not int or created < 0
            or not isinstance(identifier, str) or not 0 < len(identifier) <= 255):
        return None
    escaped_id = {'.': '~002E', '..': '~002E~002E'}.get(identifier) or _encode_segment(identifier)
    project = recorded_path(cwd)
    if project is not None and escaped_id == directory.name and _project_key(cwd) == directory.parent.name:
        return project
    return None


def projects(metadata, root):
    found = {}
    remaining = metadata.limit
    for bucket in metadata.children(root, directories=True, prefix='--'):
        if not bucket.name.endswith('--'):
            continue
        for directory in metadata.children(bucket, directories=True):
            if remaining == 0 or metadata.budget == 0:
                metadata.issue(root, 'DeepSeek Harness 会话元数据超过本次读取上限')
                return [path for path in found.values() if path is not None]
            remaining -= 1
            generations = []
            for path in metadata.children(directory, prefix='session', symlinks=True):
                match = _GENERATION.fullmatch(path.name)
                if match:
                    generations.append((int(match[1] or 0), bool(match[2]), path))
            if not generations:
                continue
            project = _cwd(metadata, directory, generations) if directory.name not in found else None
            if project is None:
                metadata.issue(root, '部分 DeepSeek Harness 会话的版本、编码或工作目录无法核验')
            found[directory.name] = project
    return [path for path in found.values() if path is not None]
