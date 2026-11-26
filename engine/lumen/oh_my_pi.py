"""Inventory Oh My Pi projects from bounded local session headers."""
import json
import re
import zlib
from itertools import islice

from .integrations import recorded_path


_AGENT = 'Oh My Pi'
_PROFILE_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_HASHED_BUCKET = re.compile(r'(?:home|tmp|abs)-[a-zA-Z0-9._-]{1,80}-[0-9a-f]{64}')
_SESSION_NAME = re.compile(
    r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{3}Z)'
    r'_([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})[.]jsonl(?:[.]gz)?'
)
_TITLE_BYTES = 256
_HEADER_BYTES = 16 * 1024
_ARCHIVE_INPUT_BYTES = 64 * 1024


def inside_default(path, home):
    root = home / '.omp'
    return path is not None and (path == root or root in path.parents)


def _plain_lines(metadata, stream):
    for _ in range(2):
        line = stream.readline(min(_HEADER_BYTES, metadata.budget))
        metadata.budget -= len(line)
        yield line


def _archive_lines(metadata, stream):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    input_left = _ARCHIVE_INPUT_BYTES
    output_left = _TITLE_BYTES + _HEADER_BYTES
    pending = bytearray()
    while input_left and output_left and metadata.budget and not decoder.eof:
        chunk = stream.read(min(4096, input_left, metadata.budget))
        if not chunk:
            return
        input_left -= len(chunk)
        metadata.budget -= len(chunk)
        decoded = decoder.decompress(chunk, output_left)
        output_left -= len(decoded)
        pending.extend(decoded)
        while (end := pending.find(b'\n')) != -1:
            yield bytes(pending[:end + 1])
            del pending[:end + 1]
        if len(pending) >= _HEADER_BYTES:
            return


def _read_header_line(lines):
    line = next(lines, b'')
    return line, json.loads(line) if len(line) <= _HEADER_BYTES and line.endswith(b'\n') else None


def _title_slot(line, value):
    # https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/coding-agent/src/session/session-title-slot.ts
    pad = value.get('pad')
    return (len(line) == _TITLE_BYTES and type(value.get('v')) is int and value['v'] == 1
            and isinstance(value.get('title'), str) and isinstance(value.get('updatedAt'), str)
            and isinstance(pad, str) and not pad.strip(' ')
            and value.get('source') in (None, 'auto', 'user'))


def _session_header(metadata, path, identity, archived):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            lines = _archive_lines(metadata, stream) if archived else _plain_lines(metadata, stream)
            line, value = _read_header_line(lines)
            if isinstance(value, dict) and value.get('type') == 'title':
                if not _title_slot(line, value):
                    return None
                _, value = _read_header_line(lines)
    except OSError as error:
        metadata.failed(path, error)
        return None
    except (ValueError, RecursionError, zlib.error):
        return None
    if not isinstance(value, dict) or value.get('type') != 'session' or value.get('id') != identity[2]:
        return None
    version, timestamp = value.get('version', 1), value.get('timestamp')
    if (type(version) is int and version in (1, 2, 3) and isinstance(timestamp, str)
            and timestamp.replace(':', '-').replace('.', '-') == identity[1]
            and value.get('origin') in (None, 'chrome')):
        return value
    return None


def _sessions(metadata, live_roots, archive_roots, add):
    sources = [(root, archive, False) for root, archive in live_roots.items()]
    sources.extend((root, None, True) for root in dict.fromkeys(archive_roots))
    shadowed_archives = set()
    remaining = metadata.limit
    for root, archive, archived in sources:
        buckets = (path for path in metadata.children(root, directories=True)
                   if path.name.startswith('-') or _HASHED_BUCKET.fullmatch(path.name))
        for directory in (root, *buckets):
            suffix = '.jsonl.gz' if archived else '.jsonl'
            for session in metadata.children(directory, suffix=suffix):
                identity = _SESSION_NAME.fullmatch(session.name)
                if identity is None or session in shadowed_archives:
                    continue
                if archive is not None:
                    relative = session.relative_to(root)
                    shadowed_archives.add(archive / relative.with_suffix('.jsonl.gz'))
                if remaining == 0 or metadata.budget == 0:
                    metadata.issue(root, 'Oh My Pi 会话元数据达到本次读取上限')
                    return
                remaining -= 1
                header = _session_header(metadata, session, identity, archived)
                if header is None or recorded_path(header.get('cwd')) is None:
                    continue
                add(header['cwd'], _AGENT)
                additional = header.get('additionalDirectories')
                if isinstance(additional, list):
                    if len(additional) > metadata.limit:
                        metadata.issue(session, 'Oh My Pi 工作区目录达到本次读取上限')
                    for path in islice(additional, metadata.limit):
                        add(path, _AGENT)


def discover_oh_my_pi_sessions(metadata, store, add):
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
    live_roots = {}
    archive_roots = []
    for root in dict.fromkeys(sessions):
        # GC mirrors paths under the sibling archive/sessions tree.
        # https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/coding-agent/src/cli/gc-cli.ts
        archive = root.parent / 'archive/sessions'
        if store(_AGENT, root, 'Oh My Pi 会话与恢复数据'):
            live_roots[root] = archive
        if inside_default(archive, metadata.home) and store(_AGENT, archive, 'Oh My Pi 冷归档'):
            archive_roots.append(archive)
    _sessions(metadata, live_roots, archive_roots, add)


def protected_artifact(path):
    return '.omp' in path.parts[:-1]
