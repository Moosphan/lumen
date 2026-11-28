"""Snapshot-scoped MCP cleanup drafts stored under the service's workspace lock.

``config['mcp_drafts']`` maps draft IDs to private records. The persisted states
are pending, processing (claimed before quarantine), completed, interrupted,
rejected, expired, and stale. A pending record holds the exact item identities.
The status token is stored only as a hash. Terminal records omit the item list.
"""
import hashlib
import hmac
import os
import re
import secrets
import stat
import time
from pathlib import Path

from . import actions


MAX_CANDIDATES = 200
MAX_DRAFT_ITEMS = 50
MAX_PENDING_DRAFTS = 10
DRAFT_TTL_SECONDS = 600
STATUS_TTL_SECONDS = 86400
MAX_STORED_DRAFTS = 100
IDENTITY_FIELDS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')
DECISIONS = ('safe', 'review')


class ReviewError(ValueError):
    """An MCP error whose text is safe to send to the client."""


def _scan(config):
    return config['report'].get('scan_id')


def _require_enabled(config):
    if config.get('mcp_enabled') is not True:
        raise ReviewError('Lumen MCP is disabled. Enable it in the Lumen app.')


def _alias(scan, label):
    return hmac.digest(scan.encode(), os.fsencode(label), 'sha256')[:16].hex()


def _candidates(rows):
    return {row['path']: row for row in rows if row['decision'] in DECISIONS}


def _published(scan, candidates):
    ordered = sorted(candidates.values(), key=lambda row: (-row['size'], row['path']))
    return {_alias(scan, f"candidate:{row['path']}"): row for row in ordered[:MAX_CANDIDATES]}


def summary(config, candidates):
    _require_enabled(config)
    scan = _scan(config)
    eligible = _candidates(candidates) if scan else {}
    totals = {decision: {'count': 0, 'size': 0} for decision in DECISIONS}
    for row in eligible.values():
        totals[row['decision']]['count'] += 1
        totals[row['decision']]['size'] += row['size']
    return {
        'snapshot': _alias(scan, 'snapshot') if scan else None,
        'totals': totals,
        'candidates': [dict(alias=alias, size=row['size'], decision=row['decision'])
                       for alias, row in _published(scan, eligible).items()],
    }


def _state(config, draft, now):
    if draft['status'] == 'processing':
        # The service holds the workspace lock until completion. A persisted claim
        # observed by another call means the previous approval was interrupted.
        return 'interrupted'
    if draft['status'] != 'pending':
        return draft['status']
    if not draft['created_at'] <= now < draft['expires_at']:
        return 'expired'
    return 'pending' if draft['scan_id'] == _scan(config) else 'stale'


def _prune(config, now):
    drafts = config.setdefault('mcp_drafts', {})
    for key, draft in list(drafts.items()):
        if not draft['created_at'] <= now < draft['created_at'] + STATUS_TTL_SECONDS:
            del drafts[key]
        else:
            state = _state(config, draft, now)
            if state != 'pending':
                draft['status'] = state
                draft.pop('items', None)
    if len(drafts) >= MAX_STORED_DRAFTS:
        oldest = min((key for key in drafts if drafts[key]['status'] != 'pending'),
                     key=lambda key: drafts[key]['created_at'])
        del drafts[oldest]
    return drafts


def _identity(row):
    return [row[key] for key in IDENTITY_FIELDS]


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_draft(config, candidates, snapshot, aliases):
    _require_enabled(config)
    scan = _scan(config)
    if not scan or snapshot != _alias(scan, 'snapshot'):
        raise ReviewError('The scan has changed. Get a current summary before drafting.')
    if (not isinstance(aliases, list) or not 0 < len(aliases) <= MAX_DRAFT_ITEMS
            or any(not isinstance(alias, str) for alias in aliases) or len(set(aliases)) != len(aliases)):
        raise ReviewError('Select a list of distinct candidate aliases from the summary.')
    published = _published(scan, _candidates(candidates))
    if any(alias not in published for alias in aliases):
        raise ReviewError('One or more aliases are outside the current summary.')
    now = int(time.time())
    drafts = _prune(config, now)
    if sum(draft['status'] == 'pending' for draft in drafts.values()) >= MAX_PENDING_DRAFTS:
        raise ReviewError('Too many drafts are waiting for review in Lumen.')
    draft_id, token = secrets.token_hex(16), secrets.token_hex(32)
    rows = [published[alias] for alias in aliases]
    draft = {
        'scan_id': scan, 'token_hash': _token_hash(token), 'created_at': now,
        'expires_at': now + DRAFT_TTL_SECONDS, 'status': 'pending',
        'items': [dict(path=row['path'], decision=row['decision'], size=row['size'], identity=_identity(row))
                  for row in rows],
    }
    drafts[draft_id] = draft
    return dict(draft_id=draft_id, status_token=token, status='pending', expires_at=draft['expires_at'])


def _matches(item, candidates):
    row = candidates.get(item['path'])
    return (row and row['decision'] == item['decision'] and _identity(row) == item['identity']
            and _matches_file(item))


def pending(config, candidates):
    if config.get('mcp_enabled') is not True:
        return []
    eligible = _candidates(candidates)
    now = int(time.time())
    return [dict(draft_id=draft_id, expires_at=draft['expires_at'],
                 items=[{key: item[key] for key in ('path', 'size', 'decision')} for item in draft['items']])
            for draft_id, draft in config.get('mcp_drafts', {}).items()
            if _state(config, draft, now) == 'pending'
            and all(_matches(item, eligible) for item in draft['items'])]


def _matches_file(item):
    path = Path(item['path'])
    try:
        with actions._directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            and [getattr(info, f'st_{key}') for key in IDENTITY_FIELDS] == item['identity'])


def _find(config, draft_id):
    if isinstance(draft_id, str) and re.fullmatch(r'[0-9a-f]{32}', draft_id):
        return config.get('mcp_drafts', {}).get(draft_id)
    return None


def take(config, candidates, draft_id, requested_paths):
    """Claim a native approval. Persist the claim before starting the quarantine."""
    _require_enabled(config)
    draft = _find(config, draft_id)
    if not draft or _state(config, draft, int(time.time())) != 'pending':
        raise ReviewError('This draft is no longer available for approval.')
    approved = [item['path'] for item in draft['items']]
    if (not isinstance(requested_paths, list) or len(requested_paths) != len(approved)
            or any(not isinstance(path, str) for path in requested_paths) or set(requested_paths) != set(approved)):
        raise ReviewError('Approve the exact list of items in this draft.')
    eligible = _candidates(candidates)
    if any(not _matches(item, eligible) for item in draft['items']):
        raise ReviewError('A draft item has changed or left the authorized scope. Scan again before drafting.')
    draft['status'] = 'processing'
    draft.pop('items')
    return approved


def discard(config, draft_id):
    _require_enabled(config)
    draft = _find(config, draft_id)
    if not draft or _state(config, draft, int(time.time())) != 'pending':
        raise ReviewError('This draft is no longer available for review.')
    draft['status'] = 'rejected'
    draft.pop('items')


def finish(config, draft_id, result):
    draft = _find(config, draft_id)
    if not draft or draft['status'] not in ('processing', 'interrupted'):
        raise ReviewError('This draft has not been claimed for approval.')
    if result is None:
        draft['status'] = 'interrupted'
        return
    report = {}
    batch = result.get('batch_id')
    if isinstance(batch, str) and re.fullmatch(r'[0-9a-f]{32}', batch):
        report['batch_id'] = batch
    draft['result'] = report
    statuses = [item['status'] for item in result.get('items', [])]
    moved = sum(value in ('quarantined', 'restored', 'purged') for value in statuses)
    skipped = statuses.count('skipped')
    if moved + skipped != len(statuses):
        draft['status'] = 'interrupted'
        return
    draft['status'] = 'completed'
    report.update(quarantined=moved, skipped=skipped)


def status(config, draft_id, status_token):
    _require_enabled(config)
    draft = _find(config, draft_id)
    now = int(time.time())
    if (not draft or not draft['created_at'] <= now < draft['created_at'] + STATUS_TTL_SECONDS
            or not isinstance(status_token, str) or not re.fullmatch(r'[0-9a-f]{64}', status_token)
            or not hmac.compare_digest(draft['token_hash'], _token_hash(status_token))):
        raise ReviewError('Draft status is unavailable.')
    return dict(draft_id=draft_id, status=_state(config, draft, now), expires_at=draft['expires_at'],
                **draft.get('result', {}))


def disable(config):
    config.pop('mcp_drafts', None)
