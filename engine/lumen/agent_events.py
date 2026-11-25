"""Normalize explicitly supplied hook payloads into advisory observations."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .integrations import recorded_path


_STAGES = {
    'Codex': {
        'Stop': 'turn_finished',
        'Interrupt': 'interrupted',
        'SessionEnd': 'session_ended',
    },
    'Gemini CLI': {
        'AfterAgent': 'turn_finished',
        'SessionEnd': 'session_ended',
    },
}
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')
_GEMINI_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')


def _occurrence(agent, name, event):
    if agent == 'Codex':
        if name == 'SessionEnd':
            return ''
        turn = event.get('turn_id')
        if isinstance(turn, str) and _IDENTIFIER.fullmatch(turn):
            return turn
        raise ValueError('无效的 Codex 回复标识')

    # Gemini emits Date.toISOString() and supplies no turn ID.
    timestamp = event.get('timestamp')
    if not isinstance(timestamp, str) or not _GEMINI_TIMESTAMP.fullmatch(timestamp):
        raise ValueError('无效的 Gemini CLI 事件时间')
    try:
        datetime.fromisoformat(timestamp[:-1] + '+00:00')
    except ValueError:
        raise ValueError('无效的 Gemini CLI 事件时间') from None
    return timestamp


def parse(agent_label, event, roots):
    """Return stable event metadata scoped to a currently authorized directory.

    Hook payloads supply observations only. Copy no transcript path, prompt,
    response, or task registration fields into the result.
    """
    if not isinstance(agent_label, str) or agent_label not in _STAGES:
        raise ValueError('不支持此 Agent 的任务事件')
    name = event.get('hook_event_name') if isinstance(event, dict) else None
    if not isinstance(name, str) or name not in _STAGES[agent_label]:
        raise ValueError('不支持此 Agent 事件类型')
    stage = _STAGES[agent_label][name]
    session = event.get('session_id')
    if not isinstance(session, str) or not _IDENTIFIER.fullmatch(session):
        raise ValueError('无效的 Agent 会话标识')
    occurrence = _occurrence(agent_label, name, event)

    cwd = event.get('cwd')
    path = recorded_path(cwd)
    if path is None or str(path) != cwd:
        raise ValueError('事件工作目录须为规范的绝对路径')
    authorized = (Path(root) for root in roots)
    if not any(path == root or root in path.parents for root in authorized):
        raise ValueError('事件工程不在已授权范围')
    try:
        canonical_directory = path.resolve(strict=True) == path and path.is_dir()
    except (OSError, RuntimeError):
        canonical_directory = False
    if not canonical_directory:
        raise ValueError('事件工作目录缺失或含符号链接')

    identity = [agent_label, session, cwd, stage, occurrence]
    event_id = hashlib.sha256(json.dumps(identity).encode('ascii')).hexdigest()
    return dict(event_id=event_id, agent=agent_label, session_id=session, root=cwd,
                stage=stage, hook_event_name=name)
