"""Reacquire an individually verified Hugging Face weight outside its cache."""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import stat
import unicodedata
import uuid
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from .actions import _directory, _eligible, _path
from .integrations import recorded_path
from .model_inventory import HUB, HUB_REVISION
from .scan import MODELS, _read, _signature


_HUB_HOST = 'huggingface.co'
_DOWNLOAD_HOSTS = frozenset({
    _HUB_HOST, 'cdn-lfs.huggingface.co', 'cdn-lfs.hf.co',
    'cdn-lfs-us-1.hf.co', 'cdn-lfs-eu-1.hf.co',
    'cas-bridge.xethub.hf.co', 'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co',
})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 4
_CHUNK_SIZE = 1024 * 1024
_SHA256 = re.compile(r'[0-9a-f]{64}')
_REPO_PART = re.compile(r'[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,94}[A-Za-z0-9_])?')
_SIGNATURE_KEYS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')
_RECIPE_KEYS = frozenset({'schema_version', 'repository', 'revision', 'filename',
                          'digest', 'bytes', 'cache_root', 'xet_id'})


class RecoveryError(ValueError):
    """The requested file could not be safely rematerialized."""


@dataclass(frozen=True)
class _SourceEntry:
    path: Path
    signature: tuple
    target: Path | None = None


@dataclass(frozen=True)
class RecoveryProof:
    repository: str
    revision: str
    filename: str
    digest: str
    byte_count: int
    cache_root: Path
    _entries: tuple[_SourceEntry, ...]
    _marker: _SourceEntry | None = None
    _xet_id: str | None = None

    def details(self):
        return dict(repository=self.repository, revision=self.revision, filename=self.filename,
                    digest='sha256:' + self.digest, bytes=self.byte_count)


def _weight(path):
    return (path.suffix.lower() in MODELS or re.fullmatch(
        r'(?:pytorch_model|model|adapter_model)(?:[-_.].*)?\.bin', path.name, re.I))


def _repository_id(value):
    if not isinstance(value, str) or len(value) > 193:
        return False
    parts = value.split('/')
    return (len(parts) <= 2 and all(_REPO_PART.fullmatch(part) and '..' not in part and '--' not in part
                                    for part in parts))


def _artifact_filename(value):
    if (not isinstance(value, str) or len(value) > 4096 or '\\' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        return False
    try:
        return len(value.encode('utf-8')) <= 4096 and bool(_weight(Path(value)))
    except UnicodeError:
        return False


def _artifact_size(value):
    return type(value) is int and 0 < value < 2 ** 63


def _row_signature(row):
    values = tuple(row.get(key) for key in _SIGNATURE_KEYS)
    return values if all(type(value) is int for value in values) else None


def _repo_blob(blob, row, shared):
    signature = _row_signature(row)
    if signature is None:
        return None
    if row.get('kind') == 'model':
        return (_SourceEntry(blob, signature),)
    if row.get('kind') == 'symlink' and blob in shared:
        payload, payload_signature = shared[blob]
        return _SourceEntry(blob, signature, payload), _SourceEntry(payload, payload_signature)
    return None


def describe(model, report):
    """Extract exact SHA-256 weight candidates from the current inventory only."""
    if model.get('tool') != HUB:
        return []
    snapshot = recorded_path(model.get('path'))
    if (snapshot is None or len(snapshot.parents) < 4 or snapshot.parent.name != 'snapshots'
            or not HUB_REVISION.fullmatch(snapshot.name) or snapshot.name != model.get('version')):
        return []
    repo = snapshot.parent.parent
    if not repo.name.startswith('models--'):
        return []
    repository = repo.name.removeprefix('models--').replace('--', '/')
    if not _repository_id(repository) or repository != model.get('name'):
        return []

    paths = set(model.get('files', ()))
    store = repo.parent / 'blobs'
    marker_path = store / '.huggingface-shared-blobs'
    rows = {row['path']: row for row in report.get('files', ())
            if (row['path'] in paths or row['path'] == str(marker_path)) and not row.get('agent_store_path')}
    marker_row = rows.get(str(marker_path), {})
    marker_signature = _row_signature(marker_row)
    marker = (_SourceEntry(marker_path, marker_signature)
              if marker_signature is not None and marker_row.get('kind') != 'symlink' else None)
    shared = {}
    if marker is not None:
        for name, row in rows.items():
            payload = recorded_path(name)
            if (payload is None or payload.parent.parent != store or row.get('kind') == 'symlink'
                    or not _SHA256.fullmatch(payload.name) or payload.parent.name != payload.name[:2]):
                continue
            signature = _row_signature(row)
            if signature is not None:
                for reference in row.get('refs', ()):
                    shared[Path(reference)] = payload, signature

    proofs = []
    for name, row in rows.items():
        blob = recorded_path(name)
        if blob is None or blob.parent != repo / 'blobs' or not _SHA256.fullmatch(blob.name):
            continue
        entries = _repo_blob(blob, row, shared)
        if entries is None:
            continue
        payload = entries[-1]
        size = payload.signature[2]
        if not _artifact_size(size):
            continue
        marker_entry = marker if payload.path != blob else None
        for reference in row.get('refs', ()):
            link = recorded_path(reference)
            link_row = rows.get(reference, {})
            if link is None or snapshot not in link.parents or link_row.get('kind') != 'symlink':
                continue
            link_signature = _row_signature(link_row)
            filename = link.relative_to(snapshot).as_posix()
            if link_signature is None or not _artifact_filename(filename):
                continue
            proofs.append(RecoveryProof(
                repository, snapshot.name, filename, blob.name, size, repo.parent,
                (_SourceEntry(link, link_signature, blob), *entries), marker_entry,
                payload.path.name if marker_entry is not None else None,
            ))
    return sorted(proofs, key=lambda proof: proof.filename)


def _recheck_source(proof):
    try:
        if proof._marker is not None:
            marker = proof._marker
            row = dict(zip(_SIGNATURE_KEYS, marker.signature))
            if _read(marker.path, row, lambda stream: stream.read(3)) != b'1\n':
                raise RecoveryError('共享模型缓存标记已变化，请重新扫描')
        for entry in proof._entries:
            with _directory(entry.path.parent) as parent:
                info = os.stat(entry.path.name, dir_fd=parent, follow_symlinks=False)
                if _signature(info) != entry.signature:
                    raise RecoveryError('模型文件身份或大小已变化，请重新扫描')
                if entry.target is None and stat.S_ISREG(info.st_mode):
                    continue
                if entry.target is not None and stat.S_ISLNK(info.st_mode):
                    expected = os.path.relpath(entry.target, entry.path.parent)
                    if os.readlink(entry.path.name, dir_fd=parent) == expected:
                        continue
                raise RecoveryError('模型快照链接已变化，请重新扫描')
    except OSError:
        raise RecoveryError('模型的本地来源证据已失效，请重新扫描') from None


def save_recipe(proof):
    """Capture fresh indexed evidence for the caller's private local store."""
    if not proof._entries:
        raise RecoveryError('保存重新获取记录需要当前扫描的本地来源证据')
    _recheck_source(proof)
    return dict(proof.details(), schema_version=1, cache_root=str(proof.cache_root), xet_id=proof._xet_id)


def read_recipe(value):
    """Load a versioned record from the caller's trusted private local store."""
    if (not isinstance(value, dict) or value.keys() != _RECIPE_KEYS
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise RecoveryError('重新获取记录的格式无效')
    repository, revision, filename = (value[key] for key in ('repository', 'revision', 'filename'))
    digest, size, xet_id = (value[key] for key in ('digest', 'bytes', 'xet_id'))
    cache_root = recorded_path(value['cache_root'])
    if (not _repository_id(repository) or not isinstance(revision, str) or not HUB_REVISION.fullmatch(revision)
            or not _artifact_filename(filename) or not _artifact_size(size)
            or not isinstance(digest, str) or not digest.startswith('sha256:')
            or not _SHA256.fullmatch(digest.removeprefix('sha256:'))
            or cache_root is None or cache_root == Path('/') or str(cache_root) != value['cache_root']
            or xet_id is not None and (not isinstance(xet_id, str) or not _SHA256.fullmatch(xet_id))):
        raise RecoveryError('重新获取记录缺少有效的仓库、修订、文件或摘要证据')
    return RecoveryProof(repository, revision, filename, digest.removeprefix('sha256:'), size,
                         cache_root, (), _xet_id=xet_id)


def _cancelled(cancel):
    if cancel is not None and cancel():
        raise RecoveryError('已取消重新获取')


def _size(value):
    return int(value) if value is not None and re.fullmatch(r'[0-9]{1,19}', value) else None


def _verify_hub_response(response, proof):
    commit = response.getheader('X-Repo-Commit')
    tag = response.getheader('X-Linked-Etag') or response.getheader('ETag')
    size = response.getheader('X-Linked-Size') or response.getheader('Content-Length')
    if (commit != proof.revision or tag not in (proof.digest, f'"{proof.digest}"')
            or _size(size) != proof.byte_count):
        raise RecoveryError('服务端修订、摘要或文件大小与扫描证据不符')
    if proof._xet_id is not None and response.getheader('X-Xet-Hash') != proof._xet_id:
        raise RecoveryError('服务端 Xet 标识与扫描证据不符')


def _url_text(value):
    return (bool(value) and len(value) <= 32768 and value.isascii() and '\\' not in value
            and all(32 < ord(char) < 127 for char in value))


def _download_url(url):
    if not _url_text(url):
        raise RecoveryError('下载地址格式无效')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise RecoveryError('下载跳转地址无效') from None
    if (parsed.scheme != 'https' or parsed.hostname not in _DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise RecoveryError('下载跳转超出 Hugging Face 官方服务范围')
    return parsed


@contextmanager
def _download(proof, cancel):
    relative = quote(proof.filename)
    url = f'https://{_HUB_HOST}/{proof.repository}/resolve/{proof.revision}/{relative}'
    for hop in range(_MAX_REDIRECTS + 1):
        _cancelled(cancel)
        endpoint = _download_url(url)
        path = endpoint.path or '/'
        if endpoint.query:
            path += '?' + endpoint.query
        # HTTPSConnection verifies certificates and does not consult proxy, Hub endpoint or token settings.
        with closing(http.client.HTTPSConnection(endpoint.hostname, timeout=15)) as connection:
            try:
                connection.request('GET', path, headers={'Accept-Encoding': 'identity'})
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                raise RecoveryError('无法连接模型服务或下载中断') from None
            with response:
                if response.status in (401, 403, 404, 410):
                    raise RecoveryError('公开模型文件不可访问，可能已下架或需要登录')
                if response.status != 200 and response.status not in _REDIRECTS:
                    raise RecoveryError(f'模型服务返回 HTTP {response.status}')
                if hop == 0:
                    _verify_hub_response(response, proof)
                if response.status == 200:
                    yield response
                    return
                location = response.getheader('Location')
                if not _url_text(location):
                    raise RecoveryError('模型服务缺少有效的下载跳转')
                try:
                    url = urljoin(url, location)
                except ValueError:
                    raise RecoveryError('下载跳转地址无效') from None
    raise RecoveryError('模型下载跳转次数超限')


def _transfer(response, stream, proof, cancel):
    content_length = response.getheader('Content-Length')
    if content_length is not None and _size(content_length) != proof.byte_count:
        raise RecoveryError('响应文件大小与扫描证据不符')
    if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
        raise RecoveryError('无法验证此下载编码')
    digest = hashlib.sha256()
    size = 0
    while True:
        _cancelled(cancel)
        try:
            chunk = response.read(min(_CHUNK_SIZE, proof.byte_count - size + 1))
        except (OSError, http.client.HTTPException):
            raise RecoveryError('模型下载中断') from None
        if not chunk:
            break
        size += len(chunk)
        if size > proof.byte_count:
            raise RecoveryError('下载文件超过已核对的大小')
        digest.update(chunk)
        stream.write(chunk)
    if size != proof.byte_count or digest.hexdigest() != proof.digest:
        raise RecoveryError('下载文件大小或摘要校验失败')


def _destination(proof, value, excluded_roots):
    path = recorded_path(os.fspath(value))
    if path is None or path == Path('/'):
        raise RecoveryError('请选择具体的恢复目录')
    directory = _path(path)
    name = Path(proof.filename).name
    target = directory / name
    folded = lambda path: Path(unicodedata.normalize('NFC', str(_path(path)).casefold()))
    destination = folded(directory)
    for root in (proof.cache_root, *excluded_roots):
        blocked = folded(root)
        if destination == blocked or blocked in destination.parents:
            raise RecoveryError('请选择模型缓存和受保护位置以外的恢复目录')
    try:
        _eligible(target, (directory,), proof.cache_root)
    except ValueError as exc:
        raise RecoveryError(str(exc)) from None
    return directory, name


def _identity(info):
    return info.st_dev, info.st_ino


def _recheck_destination(directory, identity):
    with _directory(directory) as current:
        info = os.fstat(current)
        if (_identity(info) != identity or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise RecoveryError('请选择由当前用户控制且未变化的恢复目录')


@contextmanager
def _private_stage(parent):
    name = '.lumen-model-' + uuid.uuid4().hex
    os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    identity = _identity(os.fstat(fd))
    try:
        yield fd
    finally:
        os.close(fd)
        try:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity:
                os.rmdir(name, dir_fd=parent)
        except OSError:
            pass


def recover(proof, destination, *, cancel=None, excluded_roots=(), precommit=None):
    """Verify the public artifact and atomically publish to an authorized directory.

    The caller must use describe or a trusted saved recipe and authorize the chosen directory.
    A precommit callback can return a context manager to keep its guard through publication.
    """
    _cancelled(cancel)
    if proof._entries:
        _recheck_source(proof)
    directory, name = _destination(proof, destination, excluded_roots)
    try:
        with _directory(directory) as parent:
            identity = _identity(os.fstat(parent))
            _recheck_destination(directory, identity)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RecoveryError('恢复目录中已有同名条目')
            with _private_stage(parent) as private:
                stage = 'download.part'
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                fd = os.open(stage, flags, 0o600, dir_fd=private)
                staged_id = _identity(os.fstat(fd))
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        with _download(proof, cancel) as response:
                            _transfer(response, stream, proof, cancel)
                        stream.flush()
                        os.fsync(stream.fileno())
                        staged = _signature(os.fstat(stream.fileno()))
                        guard = precommit() if precommit is not None else None
                        with guard if guard is not None else nullcontext():
                            _cancelled(cancel)
                            if proof._entries:
                                _recheck_source(proof)
                            _recheck_destination(directory, identity)
                            info = os.stat(stage, dir_fd=private, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode) or _signature(info) != staged:
                                raise RecoveryError('暂存文件已变化')
                            os.link(stage, name, src_dir_fd=private, dst_dir_fd=parent, follow_symlinks=False)
                            exported = os.stat(name, dir_fd=parent, follow_symlinks=False)
                            if not stat.S_ISREG(exported.st_mode) or _signature(exported)[:-1] != staged[:-1]:
                                if _identity(exported) == staged_id:
                                    os.unlink(name, dir_fd=parent)
                                raise RecoveryError('目标文件在发布时发生变化')
                finally:
                    try:
                        if _identity(os.stat(stage, dir_fd=private, follow_symlinks=False)) == staged_id:
                            os.unlink(stage, dir_fd=private)
                    except OSError:
                        pass
    except FileExistsError:
        raise RecoveryError('恢复目录中已有同名条目') from None
    except OSError:
        raise RecoveryError('恢复目录不可用或无法写入完整文件') from None
    return dict(proof.details(), path=str(directory / name), status='verified',
                provider_visibility='unverified')
