"""Recognize local Aider projects from their generated session header."""
import re
from collections import deque


_CHAT = '.aider.chat.history.md'
_ARTIFACTS = frozenset({
    _CHAT, '.aider.input.history', '.aider.conf.yml', '.aiderignore',
    '.aider.model.settings.yml', '.aider.model.metadata.json',
})
_TAGS = re.compile(r'\.aider\.tags\.cache\.v[0-9]+', re.ASCII)
# https://github.com/Aider-AI/aider/blob/v0.86.2/aider/io.py
_HEADER = re.compile(rb'# aider chat started at \d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d\r?\n')
_SKIP = frozenset({'applications', 'library', 'build', 'dist', 'node_modules', 'target', 'vendor'})
_HOME_SKIP = frozenset({'downloads', 'movies', 'music', 'pictures', 'public'})


def protected_artifact(parts):
    for part in parts:
        if part[:6].casefold() == '.aider':
            local = part.casefold()
            if local in _ARTIFACTS or _TAGS.fullmatch(local):
                return True
    return False


def _has_session_header(metadata, marker):
    try:
        with metadata.open(marker) as stream:
            if stream is None:
                return False
            first = stream.readline(min(64, metadata.budget))
            metadata.budget -= len(first)
            if first in (b'\n', b'\r\n'):
                first = stream.readline(min(64, metadata.budget))
                metadata.budget -= len(first)
            return _HEADER.fullmatch(first) is not None
    except OSError as error:
        metadata.failed(marker, error)
        return False


def discover_projects(metadata, store, add):
    # Aider puts the history in the Git root or the current directory.
    # https://github.com/Aider-AI/aider/blob/v0.86.2/aider/args.py
    depth_limit = 3 if metadata.quick else 5
    roots = list(dict.fromkeys(metadata.roots))
    scheduled = set(roots[:metadata.limit])
    pending = deque((root, 0) for root in roots[:metadata.limit])
    truncated = len(roots) > metadata.limit
    while pending and metadata.budget:
        directory, depth = pending.popleft()
        if not metadata.allowed(directory):
            continue
        if directory != metadata.home and _has_session_header(metadata, directory / _CHAT):
            add(str(directory), 'Aider')
            for version in (3, 4):
                store('Aider', directory / f'.aider.tags.cache.v{version}', 'Aider RepoMap 索引缓存')
        if depth == depth_limit:
            continue
        skip = _SKIP | _HOME_SKIP if directory == metadata.home else _SKIP
        for child in metadata.children(directory, directories=True):
            if (child in scheduled or child.name.startswith('.') or child.name.casefold() in skip
                    or child.suffix.casefold() in ('.app', '.bundle', '.framework')):
                continue
            if len(scheduled) >= metadata.limit:
                truncated = True
                break
            scheduled.add(child)
            pending.append((child, depth + 1))
    if pending or truncated:
        metadata.issue('Aider', '工程发现达到本次目录或读取上限')
