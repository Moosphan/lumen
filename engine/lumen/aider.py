"""Recognize local Aider projects from their generated session header."""
import re


_CHAT = '.aider.chat.history.md'
_ARTIFACTS = frozenset({
    _CHAT, '.aider.input.history', '.aider.conf.yml', '.aiderignore',
    '.aider.model.settings.yml', '.aider.model.metadata.json',
})
_TAGS = re.compile(r'\.aider\.tags\.cache\.v[0-9]+', re.ASCII)
# https://github.com/Aider-AI/aider/blob/v0.86.2/aider/io.py
_HEADER = re.compile(rb'# aider chat started at \d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d\r?\n')


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


def discover_project_history(metadata, directory, store, add):
    # Aider puts the history in the Git root or the current directory.
    # https://github.com/Aider-AI/aider/blob/v0.86.2/aider/args.py
    if _has_session_header(metadata, directory / _CHAT):
        add(str(directory), 'Aider')
        for version in (3, 4):
            store('Aider', directory / f'.aider.tags.cache.v{version}', 'Aider RepoMap 索引缓存')
