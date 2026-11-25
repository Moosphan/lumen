"""Offline adapters. Inputs are caller-selected records or inventoried files."""
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


FIXTURE_LOG = 'fixture.rotated-log.v1'
VSCODE_LOG = 'vscode.1.95.3.main-log.v1'
TASK_TEMPORARY = 'task.declared-temporary.quarantine.v1'
SAFE_RULE_IDS = frozenset({FIXTURE_LOG, VSCODE_LOG})


def recorded_path(value):
    """Validate an absolute local path from tool metadata without opening it."""
    if not isinstance(value, str) or len(value) > 4096 or '\0' in value:
        return None
    try:
        value.encode('utf-8')
    except UnicodeError:
        return None
    path = Path(value)
    if path.anchor == '/' and '..' not in path.parts:
        return path
    return None


def claude_session_end(event, roots):
    """Adapt an explicitly supplied Claude Code SessionEnd hook payload to a task."""
    if not isinstance(event, dict) or event.get('hook_event_name') != 'SessionEnd':
        raise ValueError('只支持 Claude Code SessionEnd 事件')
    session = event.get('session_id')
    if not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', session):
        raise ValueError('无效的 Claude 会话标识')
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError('工作目录须为已授权的绝对路径')
    path = Path(os.path.normpath(cwd))
    if not any(path == Path(r) or Path(r) in path.parents for r in roots):
        raise ValueError('事件工程不在已授权范围')
    if path.resolve() != path or not path.is_dir():
        raise ValueError('事件工作目录缺失或含符号链接')
    return dict(id='claude-session:' + session, agent='Claude Code', root=str(path),
                closed=True, inputs=[], outputs=[], temporary=[])


def automatic_paths(report, enabled_rules):
    """Preview only. The service still performs a fresh scan and action preflight."""
    allowed = SAFE_RULE_IDS.intersection(enabled_rules or ())
    return [r['path'] for r in report.get('files', ())
            if r.get('decision') == 'safe' and r.get('rule_id') in allowed]


def vite_outputs(path, data):
    """Parse Vite's outDir/.vite/manifest.json without reading referenced files."""
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise ValueError('Vite manifest 应为 JSON 对象')
    out_dir = path.parent.parent
    for name, chunk in manifest.items():
        if not isinstance(chunk, dict) or not isinstance(chunk.get('file'), str):
            raise ValueError('Vite chunk 缺少 file 字段')
        urls = [chunk['file']]
        for key in ('css', 'assets'):
            items = chunk.get(key, [])
            if not isinstance(items, list):
                raise ValueError(f'Vite {key} 应为数组')
            urls.extend(items)
        for value in urls:
            if not isinstance(value, str) or not value or '\x00' in value:
                raise ValueError('Vite 产物路径无效')
            relative = Path(value)
            if relative.is_absolute() or '..' in relative.parts or urlsplit(value).scheme:
                raise ValueError('Vite 产物路径超出输出目录')
            yield out_dir / relative, name


def fcpxml_sources(path, data):
    """Only parse local static asset sources, without evaluating entities or DTDs."""
    xml = data.decode('utf-8-sig')
    if '\x00' in xml or re.search(r'<!\s*ENTITY|<!DOCTYPE[^>]*\[', xml, re.I):
        raise ValueError('包含实体或内部 DTD 的工程未解析')
    doc = ElementTree.fromstring(xml)
    if doc.tag != 'fcpxml':
        raise ValueError('缺少 fcpxml 根节点')
    for asset in doc.findall('.//resources/asset'):
        for element in [asset] + list(asset.findall('media-rep')):
            value = element.get('src')
            if not value:
                continue
            parts = urlsplit(value)
            if parts.scheme not in ('', 'file') or parts.netloc not in ('', 'localhost'):
                raise ValueError('外部素材地址未解析')
            target = unquote(parts.path)
            if not target or '\x00' in target:
                raise ValueError('素材路径无效')
            yield Path(os.path.normpath(path.parent / target)), asset.get('id', 'asset')


def hub_ref(path):
    """Return the Hub model repo and named ref only inside its refs subtree."""
    for parent in path.parents:
        if parent.name == 'refs' and parent.parent.name.startswith('models--'):
            return parent.parent, path.relative_to(parent).as_posix()
    return None
