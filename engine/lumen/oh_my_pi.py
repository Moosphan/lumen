"""Inventory Oh My Pi projects from bounded local session headers."""
import json
import re
from itertools import islice

from .integrations import recorded_path


_AGENT = 'Oh My Pi'
_PROFILE_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_HASHED_BUCKET = re.compile(r'(?:home|tmp|abs)-[a-zA-Z0-9._-]{1,80}-[0-9a-f]{64}')
_SESSION_NAME = re.compile(
    r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{3}Z)'
    r'_([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})[.]jsonl'
)
_TITLE_BYTES = 256
_HEADER_BYTES = 16 * 1024


def inside_default(path, home):
    root = home / '.omp'
    return path is not None and (path == root or root in path.parents)


def _read_line(metadata, stream, limit):
    line = stream.readline(min(limit, metadata.budget))
    metadata.budget -= len(line)
    if line.endswith(b'\n'):
        return line, json.loads(line)
    return line, None


def _title_slot(line, value):
    # https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/coding-agent/src/session/session-title-slot.ts
    pad = value.get('pad')
    return (len(line) == _TITLE_BYTES and type(value.get('v')) is int and value['v'] == 1
            and isinstance(value.get('title'), str) and isinstance(value.get('updatedAt'), str)
            and isinstance(pad, str) and not pad.strip(' ')
            and value.get('source') in (None, 'auto', 'user'))


def _session_header(metadata, path, identity):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            line, value = _read_line(metadata, stream, _HEADER_BYTES)
            if isinstance(value, dict) and value.get('type') == 'title':
                if not _title_slot(line, value):
                    return None
                _, value = _read_line(metadata, stream, _HEADER_BYTES)
    except OSError as error:
        metadata.failed(path, error)
        return None
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get('type') != 'session' or value.get('id') != identity[2]:
        return None
    version, timestamp = value.get('version', 1), value.get('timestamp')
    if (type(version) is int and version in (1, 2, 3) and isinstance(timestamp, str)
            and timestamp.replace(':', '-').replace('.', '-') == identity[1]
            and value.get('origin') in (None, 'chrome')):
        return value
    return None


def _sessions(metadata, roots, add):
    remaining = metadata.limit
    for root in roots:
        buckets = (path for path in metadata.children(root, directories=True)
                   if path.name.startswith('-') or _HASHED_BUCKET.fullmatch(path.name))
        for directory in (root, *buckets):
            for session in metadata.children(directory, suffix='.jsonl'):
                identity = _SESSION_NAME.fullmatch(session.name)
                if identity is None:
                    continue
                if remaining == 0 or metadata.budget == 0:
                    metadata.issue(root, 'Oh My Pi 会话元数据达到本次读取上限')
                    return
                remaining -= 1
                header = _session_header(metadata, session, identity)
                if header is None or recorded_path(header.get('cwd')) is None:
                    continue
                add(header['cwd'], _AGENT)
                additional = header.get('additionalDirectories')
                if isinstance(additional, list):
                    if len(additional) > metadata.limit:
                        metadata.issue(session, 'Oh My Pi 工作区目录达到本次读取上限')
                    for path in islice(additional, metadata.limit):
                        add(path, _AGENT)


def discover_oh_my_pi(metadata, store, add):
    # https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/utils/src/dirs.ts
    # https://github.com/can1357/oh-my-pi/blob/v19.2.1/docs/session.md
    root = metadata.home / '.omp'
    if not store(_AGENT, root, 'Oh My Pi 配置、运行数据和共享附件'):
        return
    store(_AGENT, root / 'wt', 'Oh My Pi 管理的工作树')
    agents = [root / 'agent']
    profiles = [path for path in metadata.children(root / 'profiles', directories=True)
                if _PROFILE_NAME.fullmatch(path.name) and not path.name.endswith('.') and path.name != 'default']
    for profile in profiles:
        if store(_AGENT, profile, f'Oh My Pi 配置档 · {profile.name}'):
            agents.append(profile / 'agent')
    configured = metadata.configured('PI_CODING_AGENT_DIR')
    if inside_default(configured, metadata.home) and store(_AGENT, configured, 'Oh My Pi 自定义工具目录'):
        agents.append(configured)
    sessions = [agent / 'sessions' for agent in agents]
    override = metadata.configured('PI_CODING_AGENT_SESSION_DIR')
    if inside_default(override, metadata.home):
        sessions.append(override)
    roots = [path for path in dict.fromkeys(sessions) if store(_AGENT, path, 'Oh My Pi 会话与恢复数据')]
    _sessions(metadata, roots, add)


def protected_artifact(path):
    return '.omp' in path.parts[:-1]
