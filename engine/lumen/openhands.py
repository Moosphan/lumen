"""Inventory the standard native OpenHands Canvas and legacy CLI stores."""
import re
from uuid import UUID


CANVAS = 'OpenHands Agent Canvas'
CLI = 'OpenHands CLI'
_APP_SUPPORT = ('Library', 'Application Support', CANVAS)
_SESSION_DIR = re.compile(r'[0-9a-f]{32}')


def _projects(metadata, root, filename, agent, title, store, add):
    if not store(agent, root, title):
        return
    for directory in metadata.children(root, directories=True):
        if not _SESSION_DIR.fullmatch(directory.name):
            continue
        if not metadata.budget:
            metadata.issue(root, 'OpenHands 会话元数据达到本次读取上限')
            break
        entry = metadata.read_json(directory / filename, dict, limit=128 * 1024)
        if entry is None:
            continue
        identity = UUID(hex=directory.name)
        workspace = entry.get('workspace')
        if (entry.get('id') in (identity.hex, str(identity)) and isinstance(workspace, dict)
                and workspace.get('kind') == 'LocalWorkspace'):
            add(workspace.get('working_dir'), agent)


def discover_openhands(metadata, store, add):
    base = metadata.home / '.openhands'
    canvas = base / 'agent-canvas'
    store(CANVAS, metadata.home.joinpath(*_APP_SUPPORT), 'OpenHands Agent Canvas 桌面设置')
    store(CANVAS, canvas, 'OpenHands Agent Canvas 本地运行数据')
    store(CANVAS, canvas / 'workspaces', 'OpenHands Agent Canvas 托管工作目录')
    for name in ('settings.json', 'secrets.json', 'workspaces.json', 'projects.json'):
        store(CANVAS, base / name, 'OpenHands Agent Server 本机资料', file=True)
    # Native npm and Electron launchers use dev_conversations. Docker uses conversations.
    # https://github.com/OpenHands/OpenHands/blob/v1.46.0/scripts/dev-safe.mjs
    _projects(metadata, canvas / 'dev_conversations', 'meta.json', CANVAS, 'OpenHands Agent Canvas 本机会话',
              store, add)
    # https://github.com/OpenHands/OpenHands-CLI/blob/1.16.0/openhands_cli/setup.py
    _projects(metadata, base / 'conversations', 'base_state.json', CLI, 'OpenHands CLI 会话', store, add)
    for name in ('agent_settings.json', 'cli_config.json', 'mcp.json'):
        store(CLI, base / name, 'OpenHands CLI 用户设置', file=True)


def managed_project(path, home, agent):
    return (agent == CANVAS and path.parent == home / '.openhands/agent-canvas/workspaces'
            and _SESSION_DIR.fullmatch(path.name) is not None)


def protected_artifact(path):
    parts = path.parts[:-1]
    return '.openhands' in parts or any(
        parts[index:index + len(_APP_SUPPORT)] == _APP_SUPPORT
        for index, name in enumerate(parts) if name == 'Library')
