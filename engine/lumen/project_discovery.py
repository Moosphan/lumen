"""Share the bounded discovery pass for IDE and CLI project markers."""
from collections import deque

from . import aider, amp, crush


_SKIP = frozenset({'applications', 'library', 'build', 'dist', 'node_modules', 'target', 'vendor'})
_HOME_SKIP = frozenset({'downloads', 'movies', 'music', 'pictures', 'public'})


def discover_workspace_markers(metadata, store, add):
    depth_limit = 3 if metadata.quick else 5
    roots = list(dict.fromkeys(metadata.roots))
    scheduled = set(roots[:metadata.limit])
    pending = deque((root, 0) for root in roots[:metadata.limit])
    truncated = len(roots) > metadata.limit
    while pending and metadata.budget:
        directory, depth = pending.popleft()
        if not metadata.allowed(directory):
            continue
        if directory != metadata.home:
            aider.discover_project_history(metadata, directory, store, add)
            amp.discover_project_config(metadata, directory, store, add)
            crush.discover_project_database(metadata, directory, store, add)
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
        metadata.issue('工程标记', '本机工程发现达到本次目录或读取上限')
