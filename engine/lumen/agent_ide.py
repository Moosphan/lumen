"""Read local IDE and extension metadata within the inventory's scan scope."""
import json
import os
import re
from itertools import islice
from urllib.parse import unquote, urlsplit

from .integrations import recorded_path


_VSCODE_EDITORS = ('Code', 'Code - Insiders')
_WINDSURF_EDITORS = ('Windsurf', 'Windsurf - Next', 'Devin', 'Devin - Next')
_WINDSURF_PROFILE_NAMES = frozenset(name.casefold() for name in _WINDSURF_EDITORS)
_WORKSPACE_HASH = re.compile(r'[0-9a-f]{16}')
_UNTITLED_ID = re.compile(r'[0-9]{13}')
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_JSONC_COMMENTS = re.compile(rf'({_JSON_STRING})|//[^\r\n]*|/\*[\s\S]*?\*/')
_JSONC_TRAILING_COMMAS = re.compile(rf'{_JSON_STRING}|,(?=[ \t\r\n]*[\]}}])')
_FOREIGN_PATH = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*:')


def _copilot_cwd(metadata, path, session_id):
    try:
        with metadata.open(path) as stream:
            if stream is None:
                return None
            limit = min(16 * 1024, metadata.budget)
            line = stream.readline(limit + 1)
            metadata.budget -= min(len(line), metadata.budget)
            if len(line) > limit:
                metadata.issue(path, 'Copilot CLI 会话元数据超过本次读取上限')
                return None
        if not line.endswith(b'\n'):
            return None
        event = json.loads(line)
        if not isinstance(event, dict) or event.get('type') != 'session.start':
            return None
        data = event.get('data')
        if not isinstance(data, dict) or data.get('version') != 1 or data.get('sessionId') != session_id:
            return None
        context = data.get('context')
        if isinstance(context, dict):
            return context.get('cwd')
    except OSError as error:
        metadata.failed(path, error)
    except (ValueError, RecursionError):
        metadata.issue(path, 'Copilot CLI 会话元数据损坏或格式未受支持')
    return None


def _copilot_cli(metadata, store, add):
    # https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
    # https://github.com/microsoft/What-I-Did-Copilot/blob/main/docs/architecture.md#session-file-format
    agent = 'GitHub Copilot CLI'
    roots = [metadata.home / '.copilot']
    configured = metadata.configured('COPILOT_HOME')
    if configured is not None:
        roots.append(configured)
    for root in dict.fromkeys(roots):
        store(agent, root / 'logs', 'Copilot CLI 日志')
        sessions = root / 'session-state'
        if store(agent, sessions, 'Copilot CLI 本地会话存储'):
            for session in metadata.children(sessions, directories=True):
                add(_copilot_cwd(metadata, session / 'events.jsonl', session.name), agent)
    store(agent, metadata.home / 'Library/Caches/copilot', 'Copilot CLI macOS 默认缓存')


def _local_folder_uri(uri):
    if (not isinstance(uri, str) or not uri.startswith('file://') or len(uri) > 12288
            or any(ord(char) < 32 for char in uri)):
        return None
    try:
        parsed = urlsplit(uri)
        if (parsed.netloc.casefold() not in ('', 'localhost') or parsed.query or parsed.fragment
                or re.search(r'%(?![\da-fA-F]{2})', parsed.path)):
            return None
        return unquote(parsed.path, errors='strict')
    except (ValueError, UnicodeError):
        return None


def parse_jsonc(payload):
    source = _JSONC_COMMENTS.sub(lambda token: token[1] or ' ', payload.decode('utf-8-sig'))

    def without_trailing_comma(token):
        if token[0] == ',':
            preceding = token.start() - 1
            while preceding >= 0 and source[preceding] in ' \t\r\n':
                preceding -= 1
            if preceding >= 0 and source[preceding] not in '{[,:':
                return ''
        return token[0]

    return json.loads(_JSONC_TRAILING_COMMAS.sub(without_trailing_comma, source))


def _workspace_folder(record, parent):
    if not isinstance(record, dict) or ('path' in record and 'uri' in record):
        return None
    if 'uri' in record:
        return _local_folder_uri(record['uri'])
    path = record.get('path')
    if (isinstance(path, str) and 0 < len(path) <= 4096 and '\\' not in path
            and not path.startswith('//') and not _FOREIGN_PATH.match(path)):
        return os.path.normpath(parent / path)
    return None


def is_windsurf_untitled_workspace(path):
    if path.name.casefold() != 'workspace.json' or len(path.parts) < 6:
        return False
    library, support, editor, collection, identifier, _ = path.parts[-6:]
    return (library.casefold() == 'library' and support.casefold() == 'application support'
            and editor.casefold() in _WINDSURF_PROFILE_NAMES and collection.casefold() == 'workspaces'
            and _UNTITLED_ID.fullmatch(identifier) is not None)


def _workspace_folders(metadata, path):
    # https://code.visualstudio.com/docs/editing/workspaces/multi-root-workspaces#_workspace-file-schema
    if {'.ssh', '.aws', '.gnupg', '.kube', 'Keychains'}.intersection(path.parts):
        return
    payload = metadata.read(path, 64 * 1024)
    if payload is None:
        return
    try:
        workspace = parse_jsonc(payload)
        if not isinstance(workspace, dict) or not isinstance(workspace.get('folders'), list):
            raise ValueError('invalid workspace folders')
    except (ValueError, RecursionError):
        metadata.issue(path, 'IDE 工作区配置损坏或格式未受支持')
        return
    if workspace.get('remoteAuthority') is not None:
        return
    folders = workspace['folders']
    if len(folders) > metadata.limit:
        metadata.issue(path, 'IDE 工作区目录数超过本次读取上限')
    for record in islice(folders, metadata.limit):
        yield _workspace_folder(record, path.parent)


def _copilot_vscode(metadata, store, add):
    # https://github.com/microsoft/vscode/blob/main/src/vs/workbench/api/common/extHostStoragePaths.ts
    # https://github.com/microsoft/vscode/blob/main/src/vs/platform/storage/electron-main/storageMain.ts
    # https://github.com/microsoft/vscode-copilot-chat/blob/main/src/extension/chat/vscode-node/sessionTranscriptService.ts
    agent = 'GitHub Copilot VS Code'
    for editor in _VSCODE_EDITORS:
        user = metadata.home / 'Library/Application Support' / editor / 'User'
        store(agent, user / 'globalStorage/github.copilot-chat', f'{editor} Copilot Chat 扩展全局存储')
        for workspace in metadata.children(user / 'workspaceStorage', directories=True):
            transcripts = workspace / 'GitHub.copilot-chat/transcripts'
            if not store(agent, transcripts, f'{editor} Copilot Chat 扩展会话存储'):
                continue
            if metadata.children(transcripts, suffix='.jsonl'):
                value = metadata.read_json(workspace / 'workspace.json', dict)
                if value is not None:
                    add(_local_folder_uri(value.get('folder')), agent)


def _windsurf(metadata, store, add):
    # https://docs.devin.ai/desktop/troubleshooting/windsurf-common-issues
    # Devin 3.14.30833 keeps VS Code's workspace.json and changes nameShort from Windsurf to Devin.
    agent = 'Windsurf'
    store(agent, metadata.home / '.codeium/windsurf/cascade', 'Windsurf Cascade 会话存储')
    for editor in _WINDSURF_EDITORS:
        profile = metadata.home / 'Library/Application Support' / editor
        # https://github.com/microsoft/vscode/blob/main/src/vs/platform/workspaces/electron-main/workspacesManagementMainService.ts
        untitled = profile / 'Workspaces'
        if store(agent, untitled, f'{editor} 未保存工作区'):
            for entry in metadata.children(untitled, directories=True):
                if _UNTITLED_ID.fullmatch(entry.name):
                    for path in _workspace_folders(metadata, entry / 'workspace.json'):
                        add(path, agent)
        user = profile / 'User'
        workspaces = user / 'workspaceStorage'
        store(agent, user / 'globalStorage', f'{editor} 全局存储')
        if store(agent, workspaces, f'{editor} 工作区存储'):
            for workspace in metadata.children(workspaces, directories=True):
                value = metadata.read_json(workspace / 'workspace.json', dict)
                if value is None:
                    continue
                if 'workspace' not in value:
                    add(_local_folder_uri(value.get('folder')), agent)
                elif 'folder' not in value:
                    config = recorded_path(_local_folder_uri(value['workspace']))
                    if config is not None and config.suffix.casefold() == '.code-workspace':
                        for path in _workspace_folders(metadata, config):
                            add(path, agent)


def _roo_code(metadata, store, add):
    # Roo Code 3.53.0 persists HistoryItem.workspace in the authoritative per-task file.
    # https://github.com/RooCodeInc/Roo-Code/blob/main/src/core/task-persistence/TaskHistoryStore.ts
    # https://github.com/RooCodeInc/Roo-Code/blob/main/src/core/task-persistence/taskMetadata.ts
    for editor in _VSCODE_EDITORS:
        root = (metadata.home / 'Library/Application Support' / editor
                / 'User/globalStorage/rooveterinaryinc.roo-cline')
        if store('Roo Code', root, f'{editor} Roo Code 扩展全局存储'):
            for task in metadata.children(root / 'tasks', directories=True):
                item = metadata.read_json(task / 'history_item.json', dict)
                if item is not None and item.get('id') == task.name:
                    add(item.get('workspace'), 'Roo Code')


def _kiro(metadata, store, add):
    # https://kiro.dev/docs/ide/whats-new-v1/#sessions
    # https://github.com/kirodotdev/Kiro/issues/10534
    # https://github.com/kirodotdev/Kiro/issues/10770
    application = metadata.home / 'Library/Application Support/Kiro'
    store('Kiro', application / 'logs', 'Kiro IDE 日志')
    legacy = application / 'User/globalStorage/kiro.kiroagent/workspace-sessions'
    store('Kiro', legacy, 'Kiro IDE 旧版会话存储')
    root = metadata.home / '.kiro/sessions'
    if not store('Kiro', root, 'Kiro KAS 共享会话存储'):
        return
    sessions = (session for bucket in metadata.children(root, directories=True)
                if _WORKSPACE_HASH.fullmatch(bucket.name)
                for session in metadata.children(bucket, directories=True))
    for index, session in enumerate(islice(sessions, metadata.limit + 1)):
        if index == metadata.limit:
            metadata.issue(root, 'Kiro 会话元数据项目数超过本次读取上限')
            break
        path = session / 'session.json'
        item = metadata.read_json(path, dict)
        if item is None or item.get('id') != session.name:
            continue
        target = item.get('executionTarget')
        if target is not None and (not isinstance(target, dict) or target.get('kind') != 'local'):
            continue
        workspaces = item.get('workspacePaths')
        if isinstance(workspaces, list):
            if len(workspaces) > metadata.limit:
                metadata.issue(path, 'Kiro 工作区数量超过本次读取上限')
            for workspace in islice(workspaces, metadata.limit):
                add(workspace, 'Kiro')


ADAPTERS = (_copilot_cli, _copilot_vscode, _windsurf, _roo_code, _kiro)
