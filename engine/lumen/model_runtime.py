"""Read-only runtime checks for explicitly selected local model tools."""

import http.client
import json
import re
import socket
from collections import Counter, defaultdict
from contextlib import closing, suppress
from threading import Event, Timer
from time import monotonic

from .model_inventory import LM_STUDIO, OLLAMA


_ENDPOINTS = {
    'ollama': (OLLAMA, 11434, '/api/ps'),
    'lmstudio': (LM_STUDIO, 1234, '/api/v1/models'),
}
_TIMEOUT_SECONDS = 1.5
_MAX_RESPONSE_BYTES = 1024 * 1024
_DIGEST = re.compile(r'(?:sha256[:-])?([0-9a-f]{64})', re.I)
_NAME_PART = re.compile(r'[a-z0-9_][a-z0-9_.-]*', re.I | re.ASCII)
_OLLAMA_HOST = re.compile(r'[a-z0-9][a-z0-9.-]*(?::[0-9]+)?', re.I | re.ASCII)


def _fetch(port, path):
    deadline = monotonic() + _TIMEOUT_SECONDS
    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=_TIMEOUT_SECONDS)) as connection:
        connection.connect()
        stream = connection.sock
        expired = Event()

        def stop_reading():
            expired.set()
            with suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)

        # A socket timeout alone can restart when a peer slowly streams headers or body.
        watchdog = Timer(max(0, deadline - monotonic()), stop_reading)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.request('GET', path, headers={'Accept': 'application/json', 'Connection': 'close'})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ConnectionError('Runtime endpoint returned a non-200 response')
                if response.length is not None and response.length > _MAX_RESPONSE_BYTES:
                    raise ValueError('Runtime response exceeds the size limit')
                data = response.read(_MAX_RESPONSE_BYTES + 1)
                if expired.is_set() or monotonic() >= deadline:
                    raise TimeoutError('Runtime check reached its deadline')
                if len(data) > _MAX_RESPONSE_BYTES or response.length not in (None, 0):
                    raise ValueError('Runtime response is over the size limit or incomplete')
                return json.loads(data)
        finally:
            watchdog.cancel()


def _ollama_name(value):
    if not isinstance(value, str):
        return None
    parts = value.split('/')
    if len(parts) > 3:
        return None
    host = parts[0] if len(parts) == 3 else 'registry.ollama.ai'
    namespace = parts[-2] if len(parts) >= 2 else 'library'
    model, separator, tag = parts[-1].partition(':')
    tag = tag if separator else 'latest'
    if not _OLLAMA_HOST.fullmatch(host) or not all(map(_NAME_PART.fullmatch, (namespace, model, tag))):
        return None
    if host.casefold() == 'ollama.com':
        host = 'registry.ollama.ai'
    return tuple(value.casefold() for value in (host, namespace, model, tag))


def _ollama_digest(value):
    match = _DIGEST.fullmatch(value) if isinstance(value, str) else None
    return match[1].lower() if match else None


def _ollama_states(models, running):
    reported = defaultdict(set)
    for row in running:
        names = {_ollama_name(row[field]) for field in ('name', 'model') if field in row}
        digest = _ollama_digest(row.get('digest'))
        if len(names) != 1 or None in names or not digest:
            raise ValueError('Invalid Ollama running model identity')
        reported[names.pop()].add(digest)

    local = {}
    copies = Counter()
    for model in models:
        name = _ollama_name(f"{model.get('name', '')}:{model.get('version', '')}")
        candidates = (model.get('digest'), *model.get('runtime_digests', ()))
        digests = set(filter(None, map(_ollama_digest, candidates)))
        local[model['id']] = (name, digests)
        copies.update((name, digest) for digest in digests)

    running_digests = set().union(*reported.values())
    for model_id, (name, digests) in local.items():
        matches = digests.intersection(reported.get(name, ()))
        if any(copies[name, digest] == 1 for digest in matches):
            yield model_id, 'loaded'
        elif name and digests and name not in reported and digests.isdisjoint(running_digests):
            yield model_id, 'not_loaded'
        else:
            yield model_id, 'unknown'


def _lmstudio_identity(repository, model_format, quant):
    if not isinstance(repository, str) or not isinstance(quant, str):
        return None
    parts = repository.casefold().split('/')
    if (len(parts) != 2 or not all(map(_NAME_PART.fullmatch, parts))
            or model_format not in ('gguf', 'mlx') or not _NAME_PART.fullmatch(quant)):
        return None
    publisher, name = parts
    # The downloaded repository commonly adds a format suffix to the runtime key.
    return publisher, name.removesuffix('-' + model_format), model_format, quant.casefold()


def _lmstudio_states(models, available):
    reported = defaultdict(list)
    for row in available:
        key, publisher = row.get('key'), row.get('publisher')
        instances, quant = row.get('loaded_instances'), row.get('quantization')
        variants = row.get('variants', [])
        if (not isinstance(key, str) or not key or not isinstance(publisher, str)
                or not isinstance(instances, list) or not isinstance(variants, list)
                or not all(isinstance(instance, dict) and isinstance(instance.get('id'), str)
                           and instance['id'] for instance in instances)
                or not all(isinstance(variant, str) and variant for variant in variants)
                or (quant is not None and not isinstance(quant, dict))):
            raise ValueError('Invalid LM Studio model identity or instances')
        repository = key if '/' in key else f'{publisher}/{key}'
        if not repository.casefold().startswith(publisher.casefold() + '/'):
            continue
        identity = _lmstudio_identity(repository, row.get('format'), (quant or {}).get('name'))
        if identity is not None:
            # Instance IDs can be custom, so the selected variant doesn't prove which variant is loaded.
            state = 'unknown' if instances and len(variants) > 1 else 'loaded' if instances else 'not_loaded'
            reported[identity].append(state)

    local = {}
    for model in models:
        version = model.get('version', '')
        model_format = 'mlx' if version == 'MLX' else 'gguf' if version.lower().endswith('.gguf') else None
        local[model['id']] = _lmstudio_identity(model.get('name'), model_format, model.get('quantization'))
    copies = Counter(local.values())
    for model_id, identity in local.items():
        matches = reported.get(identity, ())
        yield model_id, matches[0] if copies[identity] == 1 and len(matches) == 1 else 'unknown'


def check(models, tool):
    """Query one fixed loopback endpoint and report evidence by inventory ID."""
    if not isinstance(tool, str) or tool not in _ENDPOINTS:
        return {'tool': None, 'status': 'unsupported_tool', 'models': {}}

    tool_name, port, path = _ENDPOINTS[tool]
    selected = [model for model in models if model['tool'] == tool_name]
    states = {model['id']: {'active_state': 'unknown', 'active_source': None} for model in selected}
    try:
        payload = _fetch(port, path)
        rows = payload.get('models') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('Invalid runtime model list')
        match = _ollama_states if tool == 'ollama' else _lmstudio_states
        source = f'{tool}:{path}'
        states = {model_id: {'active_state': state, 'active_source': source}
                  for model_id, state in match(selected, rows)}
        status = 'ok'
    except (OSError, http.client.HTTPException):
        status = 'unavailable'
    except (ValueError, RecursionError):
        status = 'invalid_response'
    return {'tool': tool, 'status': status, 'models': states}
