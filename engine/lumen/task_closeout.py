"""Describe closed task registrations using current inventory and linked actions."""
from collections import defaultdict
from pathlib import Path

from .scan import _kind


def _category(roles, kind, cleanup_kind):
    if 'outputs' in roles:
        return 'output'
    if 'inputs' in roles:
        return 'input'
    if kind == 'model':
        return 'model'
    if kind == 'cache' or cleanup_kind == 'cache':
        return 'cache'
    if kind in ('render', 'video', 'image', 'audio'):
        return 'render'
    return 'temporary'


def _identity(row):
    if 'dev' in row and 'ino' in row and row.get('kind') != 'symlink':
        return row['dev'], row['ino']
    return None


def _source_location(reference, files):
    if reference in files:
        return reference, ''
    source, separator, location = reference.rpartition('#')
    if separator and location:
        return source, location
    source, separator, location = reference.rpartition(':')
    if separator and location.isdecimal():
        return source, location
    return reference, ''


def _projects_for(source, files, projects):
    memberships = set(files.get(source, {}).get('projects', ()))
    path = Path(source)
    nearest = next((str(p) for p in (path, *path.parents) if str(p) in projects), None)
    if nearest:
        memberships.add(nearest)
    return sorted(memberships)


def _references(related_paths, files, projects, static_refs, model_workflows):
    records, paths = {}, set()
    for path in sorted(related_paths):
        paths.update(files.get(path, {}).get('refs', ()))
        paths.update(model_workflows.get(path, ()))
        for evidence in static_refs.get(path, ()):
            source, location = evidence['source'], evidence.get('location', '')
            records.setdefault((source, location), dict(source=source, location=location, target=path))
    resolved = set()
    for source, location in records:
        if location:
            labels = {f'{source}#{location}', f'{source}:{location}'} & paths
            resolved.update(labels or {f'{source}#{location}'})
        else:
            resolved.add(source)
    for label in paths - resolved:
        source, location = _source_location(label, files)
        records.setdefault((source, location), dict(source=source, location=location))
    for (source, _), record in records.items():
        record['projects'] = _projects_for(source, files, projects)
    return sorted(paths | resolved), [records[key] for key in sorted(records)]


def _actions_for(task, batches, batches_by_closeout):
    batch_ids = list(dict.fromkeys(
        [*task.get('finish_batch_ids', ()), *batches_by_closeout.get(task.get('closeout_id'), ())]))
    by_path = defaultdict(list)
    for batch_id in batch_ids:
        batch = batches.get(batch_id, {})
        for item in batch.get('items', ()):
            by_path[item['path']].append(dict(item, batch_id=batch_id, action=batch.get('action', '')))
    for item in task.get('finish_skips', ()):
        by_path[item['path']].append(dict(item))
    return batch_ids, by_path


def summarize(tasks, report, history):
    """Return one closeout for each closed task, preserving registered paths.

    Inventory entries supply current verdicts and evidence. Only explicit
    finish-batch links attribute journal actions, including later restores.
    Unindexed paths remain visible and do not receive a cleanup verdict.
    """
    closed = [task for task in tasks if task.get('closed') is True]
    if not closed:
        return []
    files = {row['path']: row for row in report.get('files', ())}
    shared_paths = defaultdict(set)
    for path, row in files.items():
        identity = _identity(row)
        if identity is not None and row.get('nlink', 1) > 1:
            shared_paths[identity].add(path)
    static_refs = defaultdict(list)
    for ref in report.get('static_refs', ()):
        static_refs[ref['target']].append(ref)
    model_workflows = defaultdict(set)
    for model in report.get('model_inventory', {}).get('models', ()):
        for path in model.get('files', ()):
            model_workflows[path].update(model.get('workflow_refs', ()))
    projects = report.get('projects', {})
    batches = {batch.get('batch_id') or batch.get('id'): batch for batch in history}
    batches_by_closeout = defaultdict(list)
    for batch_id, batch in reversed(list(batches.items())):
        if batch.get('closeout_id'):
            batches_by_closeout[batch['closeout_id']].append(batch_id)
    summaries = []
    for task in closed:
        root, agent = task.get('root', ''), task.get('agent', '')
        batch_ids, action_items = _actions_for(task, batches, batches_by_closeout)
        roles_by_path = defaultdict(list)
        for role in ('inputs', 'outputs', 'temporary'):
            for path in dict.fromkeys(task.get(role, ())):
                roles_by_path[path].append(role)
        items = []
        for path, roles in roles_by_path.items():
            row = files.get(path)
            actions = action_items.get(path, [])
            if row and actions and actions[-1].get('status') in ('cleaned', 'quarantined', 'trashed', 'purged'):
                if list(_identity(row) or ()) == actions[-1].get('identity'):
                    row = None
            evidence = row or {}
            kind = 'model' if path in model_workflows else evidence.get('kind') or _kind(Path(path))
            related_paths = {path} | shared_paths.get(_identity(evidence), set())
            refs, references = _references(related_paths, files, projects, static_refs, model_workflows)
            memberships = {root} if root else set()
            for related_path in related_paths:
                memberships.update(files.get(related_path, {}).get('projects', ()))
            for reference in references:
                memberships.update(reference['projects'])
            item = dict(
                path=path, category=_category(roles, kind, evidence.get('cleanup_kind')),
                roles=roles, kind=kind, indexed=row is not None,
                decision=evidence.get('decision', 'unknown'),
                reason=evidence.get('reason') or ('当前清单未记录处置理由' if row else '当前扫描未能核对此路径'),
                refs=refs, references=references, projects=sorted(memberships), actions=actions,
            )
            measurement = evidence or next((action for action in reversed(actions) if 'size' in action), {})
            for metric in ('size', 'allocated_bytes'):
                if metric in measurement:
                    item[metric] = measurement[metric]
            items.append(item)
        summaries.append(dict(id=task.get('id') or root + '|' + agent,
                              root=root, agent=agent, items=items, batch_ids=batch_ids))
    return summaries
