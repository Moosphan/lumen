"""Read the integrated builder's cache accounting from Docker Desktop's local socket."""
import http.client
import os
import socket
import stat

from .actions import _directory
from .local_http import fetch_json


ENDPOINT = '/v1.45/system/df?type=build-cache'
SOCKET_PATH = '.docker/run/docker.sock'
_TIMEOUT = 6
_RESPONSE_LIMIT = 8 * 1024 * 1024
_BYTE_LIMIT = (1 << 63) - 1


def _identity(path):
    with _directory(path.parent) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ConnectionError('Docker Desktop socket identity could not be verified')
    return info.st_dev, info.st_ino


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.fspath(self.path))


def _fetch(path):
    identity = _identity(path)

    class DesktopConnection(_UnixHTTPConnection):
        def connect(self):
            super().connect()
            if _identity(self.path) != identity:
                raise ConnectionError('Docker Desktop socket changed before the request')

    return fetch_json(DesktopConnection(path, _TIMEOUT), ENDPOINT, timeout=_TIMEOUT, limit=_RESPONSE_LIMIT)


def summarize(payload):
    rows = payload.get('BuildCache') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('Docker response has no supported build cache records')
    reported = shared = unused_unshared = 0
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid Docker cache record')
        key, size, in_use, is_shared = (row.get(key) for key in ('ID', 'Size', 'InUse', 'Shared'))
        if (not isinstance(key, str) or not key or key in ids or type(size) is not int
                or not 0 <= size <= _BYTE_LIMIT or type(in_use) is not bool or type(is_shared) is not bool):
            raise ValueError('Invalid or repeated Docker cache record')
        ids.add(key)
        reported += size
        if is_shared:
            shared += size
        elif not in_use:
            unused_unshared += size
    if reported > _BYTE_LIMIT:
        raise ValueError('Docker cache total exceeds the supported byte count')
    return dict(status='ok', entries=len(ids), reported_bytes=reported, shared_bytes=shared,
                unused_unshared_bytes=unused_unshared)


def check(home):
    try:
        return summarize(_fetch(home / SOCKET_PATH))
    except (OSError, http.client.HTTPException):
        return {'status': 'unavailable'}
    except (ValueError, RecursionError):
        return {'status': 'invalid_response'}
