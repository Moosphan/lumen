"""Identify self-contained generated-code entries in standard Chromium caches."""
import os
import re
import struct
from collections import Counter, defaultdict
from pathlib import Path

from .actions import _directory


PRODUCTS = {
    'com.google.Chrome': ('Google', 'Chrome'),
    'org.chromium.Chromium': ('Chromium',),
}
_FOLDED_PRODUCTS = {tuple(part.casefold() for part in name): identifier for identifier, name in PRODUCTS.items()}
_PROFILE = re.compile(r'(?:Default|Profile [1-9][0-9]{0,5})', re.ASCII)
_ENTRY = re.compile(r'[0-9a-f]{16}_0', re.ASCII)
_LANGUAGE = {'js': 'JavaScript', 'wasm': 'WebAssembly'}
_MAX_PROBES = 1000
# Chromium Simple disk-cache header and EOF record layouts on macOS.
# https://chromium.googlesource.com/chromium/src/+/refs/heads/main/net/disk_cache/simple/simple_entry_format.h
_RECORD = struct.Struct('<QIIII')
_START, _END = 0xfcfb6d1ba7725c30, 0xf4fa6f45970d41d8


def identify(path, home):
    """Recognize the standard Code Cache subtree, even via a case alias."""
    root = home / 'Library/Caches'
    parts = path.parts
    if tuple(part.casefold() for part in parts[:len(root.parts)]) != tuple(part.casefold() for part in root.parts):
        return None
    relative = parts[len(root.parts):]
    folded = tuple(part.casefold() for part in relative)
    for product, identifier in _FOLDED_PRODUCTS.items():
        offset = len(product)
        if folded[:offset] != product or folded[offset + 1:offset + 2] != ('code cache',):
            continue
        profile = relative[offset]
        tail = relative[offset + 2:]
        canonical = parts[:len(root.parts)] == root.parts and relative[:offset] == PRODUCTS[identifier]
        language = _LANGUAGE.get(tail[0]) if len(tail) == 2 else None
        eligible = bool(canonical and relative[offset + 1] == 'Code Cache' and _PROFILE.fullmatch(profile)
                        and language and _ENTRY.fullmatch(tail[1]))
        return dict(bundle_id=identifier, profile=profile, language=language, eligible=eligible)
    return None


def _file_format(path, row):
    from .scan import _read

    def format_matches(stream):
        size = _RECORD.size
        header = stream.read(size)
        if len(header) != size:
            return False
        magic, version, key_length, _, _ = _RECORD.unpack(header)
        if magic != _START or not version or not 0 < key_length < row['size'] - 3 * size:
            return False
        stream.seek(-size, os.SEEK_END)
        footer = stream.read(size)
        return len(footer) == size and _RECORD.unpack(footer)[0] == _END

    try:
        with _directory(path.parent) as directory:
            for suffix in ('1', 's'):
                try:
                    os.stat(path.name[:-1] + suffix, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                return False
        return _read(path, row, format_matches)
    except (OSError, ValueError):
        return False


def classify(report, requested):
    matches = defaultdict(list)
    for app in report['apps']:
        if app['bundle_id'] in PRODUCTS:
            matches[app['bundle_id']].append(app)
    entries = [row for row in report['files'] if row.get('browser_code_cache')]
    if requested:
        entries.sort(key=lambda item: (-item['allocated_bytes'], item['path']))
    alias_count = Counter((row['dev'], row['ino']) for row in entries) if requested else {}
    remaining = {identifier: _MAX_PROBES for identifier in matches}
    limited = set()
    for row in entries:
        evidence = row['browser_code_cache']
        identifier = evidence['bundle_id']
        apps = matches.get(identifier, ())
        app = apps[0] if len(apps) == 1 and row['app_path'] in ('', apps[0]['path']) else None
        if app is not None and not row.get('shared_owners'):
            row.update(owner=app['name'], app_path=app['path'], bundle_id=identifier,
                       app_version=app['version'], cache_profile=evidence['profile'])
        if row['decision'] not in ('safe', 'review'):
            continue
        selected = app is not None and app['path'] in requested
        review = (selected and evidence['eligible'] and row['decision'] == 'review'
                  and row['nlink'] == 1 and alias_count[row['dev'], row['ino']] == 1 and not row.get('refs')
                  and not row.get('shared_owners'))
        if review and remaining[identifier]:
            remaining[identifier] -= 1
            if _file_format(Path(row['path']), row):
                row.update(cleanup_kind='compiled_code', cache_language=evidence['language'],
                           reason=f"{evidence['language']} 生成代码缓存，后续访问时可重新编译。")
                continue
        elif review:
            limited.add(identifier)
        reason = '保留浏览器缓存索引或未核验的文件。' if selected else '请在对应浏览器的应用详情中分析缓存。'
        row.update(decision='keep', reason=reason)
    for identifier in sorted(limited):
        report['issues'].append(f"{matches[identifier][0]['name']} · 生成代码缓存优先核验最大的 {_MAX_PROBES} 项")
