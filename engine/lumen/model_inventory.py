"""Read-only model identities and storage relationships within an existing scan."""
import hashlib
import json
import os
import re
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .integrations import recorded_path
from .actions import _directory
from .scan import MODELS, _read, _small

OLLAMA, HUB, LM_STUDIO, COMFY, UNKNOWN = 'Ollama', 'Hugging Face', 'LM Studio', 'ComfyUI', '未知'
OLLAMA_DIGEST = re.compile(r'sha256[:-][0-9a-fA-F]{64}')
HUB_DIGEST = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
HUB_REVISION = re.compile(r'[0-9a-f]{40}')
QUANTIZATION = re.compile(
    r'(?<![\w])(?:I?Q[1-8](?:_[A-Z0-9]+)*|MXFP4|BF16|FP16|F16|F32|[248]BIT)(?![\w])', re.I)
GGUF_SHARD = re.compile(r'-\d{5}-of-\d{5}(?=\.gguf$)', re.I)
COMFY_ALIASES = {'clip': 'text_encoders', 'unet': 'diffusion_models', 't2i_adapter': 'controlnet'}
COMFY_CATEGORIES = {
    'checkpoints', 'loras', 'vae', 'text_encoders', 'diffusion_models', 'clip_vision',
    'style_models', 'embeddings', 'diffusers', 'vae_approx', 'controlnet', 'gligen',
    'upscale_models', 'latent_upscale_models', 'hypernetworks', 'photomaker',
    'classifiers', 'model_patches', 'audio_encoders', 'background_removal',
    'frame_interpolation', 'geometry_estimation', 'optical_flow', 'detection',
} | COMFY_ALIASES.keys()
COMFY_EXTENSIONS = MODELS | {'.pt2', '.bin', '.pkl', '.sft'}
COMFY_LOADER_FIELDS = {
    'CheckpointLoaderSimple': ('checkpoints', ('ckpt_name',)),
    'LoraLoader': ('loras', ('lora_name',)),
    'LoraLoaderModelOnly': ('loras', ('lora_name',)),
    'VAELoader': ('vae', ('vae_name',)),
    'CLIPLoader': ('text_encoders', ('clip_name',)),
    'DualCLIPLoader': ('text_encoders', ('clip_name1', 'clip_name2')),
    'CLIPVisionLoader': ('clip_vision', ('clip_name',)),
    'UNETLoader': ('diffusion_models', ('unet_name',)),
    'ControlNetLoader': ('controlnet', ('control_net_name',)),
    'StyleModelLoader': ('style_models', ('style_model_name',)),
    'UpscaleModelLoader': ('upscale_models', ('model_name',)),
}
PARSE_ERRORS = (OSError, ValueError, RecursionError)


def locations(home, environ=None):
    """Return candidate model roots without touching the filesystem."""
    home = Path(home)
    env = environ or {}
    candidates = [
        (home / '.ollama/models', OLLAMA),
        (home / '.cache/huggingface/hub', HUB),
        (home / '.lmstudio/models', LM_STUDIO),
        (home / '.cache/lm-studio/models', LM_STUDIO),
        (home / 'Documents/ComfyUI/models', COMFY),
        (home / 'ComfyUI/models', COMFY),
    ]
    for key, subdir, tool in (
        ('OLLAMA_MODELS', '', OLLAMA), ('HF_HUB_CACHE', '', HUB),
        ('HUGGINGFACE_HUB_CACHE', '', HUB), ('HF_HOME', 'hub', HUB),
        ('XDG_CACHE_HOME', 'huggingface/hub', HUB),
    ):
        path = recorded_path(env.get(key))
        if path and path != Path('/'):
            candidates.append((path / subdir, tool))
    return list(dict.fromkeys(candidates))


def metadata_locations(home):
    home = Path(home)
    desktop = home / 'Library/Application Support/ComfyUI'
    return [
        home / 'Documents/ComfyUI/user', home / 'ComfyUI/user',
        home / 'Documents/ComfyUI/extra_model_paths.yaml',
        home / 'ComfyUI/extra_model_paths.yaml',
        desktop / 'config.json', desktop / 'extra_models_config.yaml',
    ]


def configured_comfy_locations(home, environ, within_scope, issues):
    """Read only known config files before admitting additional scoped roots."""
    rows = {}
    for path in metadata_locations(home):
        if path.suffix not in ('.json', '.yaml') or not within_scope(path):
            continue
        try:
            with _directory(path.parent) as fd:
                info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                rows[path] = {name: getattr(info, 'st_' + name)
                              for name in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            issues.add(f'{path} · ComfyUI 配置未获读取权限')
    roots = set()
    instances, categories = _comfy_paths(rows, roots, home, environ, issues)
    return roots | set(categories) | {instance / 'user' for instance in instances}


@dataclass
class _Model:
    tool: str
    name: str
    version: str
    path: Path
    source: str
    digest: str = ''
    runtime_digests: set = field(default_factory=set)
    quantization: str = ''
    files: set = field(default_factory=set)
    storage: set = field(default_factory=set)
    refs: set = field(default_factory=set)
    workflow_refs: set = field(default_factory=set)
    issues: set = field(default_factory=set)

    def add(self, path, storage):
        self.files.add(path)
        if storage is not None:
            self.files.add(storage)
            self.storage.add(storage)
        else:
            self.issues.add(f'{path.name} 的链接目标未验证')


def _json(path, rows):
    data = json.loads(_read(path, rows[path], _small))
    if not isinstance(data, dict):
        raise ValueError('需要 JSON 对象')
    return data


def _quantization(names):
    return ', '.join(sorted({match.group(0).upper() for name in names
                            for match in QUANTIZATION.finditer(name)}))


def _ollama(root, path, rows, links):
    parts = path.relative_to(root).parts
    host, namespace, name, tag = parts[1:]
    host = re.sub(r'%3a', ':', host, flags=re.IGNORECASE) if parts[0] == 'manifests-v2' else host
    public = host.casefold() in ('registry.ollama.ai', 'ollama.com')
    if namespace.casefold() != 'library' or not public:
        name = f'{namespace}/{name}'
    if not public:
        name = f'{host}/{name}'
    model = _Model(OLLAMA, name, tag, path, '本地清单 · ' + host)
    blob_dir = root / 'blobs'
    manifest = path if rows[path]['kind'] != 'symlink' else links.get(path)
    if manifest is not None and manifest != path and (
            manifest.parent != blob_dir or not OLLAMA_DIGEST.fullmatch(manifest.name)):
        manifest = None
    if manifest is not None and rows[manifest]['kind'] == 'symlink':
        manifest = None
    model.add(path, manifest)
    if manifest is None:
        return model
    try:
        raw = _read(manifest, rows[manifest], _small)
        digest = hashlib.sha256(raw).hexdigest()
        if manifest != path and manifest.name.lower() != 'sha256-' + digest:
            raise ValueError('清单与内容寻址摘要不符')
        model.digest = 'sha256:' + digest
        pending = [(json.loads(raw), None)]
        visited = set()
        configs = set()

        def blob(entry):
            digest = entry.get('digest') if isinstance(entry, dict) else None
            if not isinstance(digest, str) or not OLLAMA_DIGEST.fullmatch(digest):
                model.issues.add('清单包含无效的 Blob 摘要')
                return None
            target = blob_dir / digest.replace(':', '-')
            if target not in rows or rows[target]['kind'] == 'symlink':
                model.issues.add(f'Blob 未覆盖或为链接：{digest}')
                return None
            model.add(target, target)
            if 'size' in entry and entry['size'] != rows[target]['size']:
                model.issues.add(f'Blob 大小与清单不符：{digest}')
            return target

        while pending:
            entry, child_digest = pending.pop()
            if not isinstance(entry, dict):
                raise ValueError('清单项不是对象')
            if 'manifests' in entry:
                if not isinstance(entry['manifests'], list) or not entry['manifests']:
                    raise ValueError('清单列表为空或无效')
                pending.extend((child, None) for child in entry['manifests'])
            elif 'config' in entry and isinstance(entry.get('layers'), list):
                if child_digest is not None:
                    model.runtime_digests.add(child_digest)
                config = blob(entry['config'])
                if config is not None:
                    configs.add(config)
                for layer in entry['layers']:
                    blob(layer)
            elif 'digest' in entry:
                target = blob(entry)
                if target is not None and target not in visited:
                    visited.add(target)
                    raw = _read(target, rows[target], _small)
                    digest = hashlib.sha256(raw).hexdigest()
                    if target.name.lower() != 'sha256-' + digest:
                        raise ValueError('子清单与内容寻址摘要不符')
                    pending.append((json.loads(raw), 'sha256:' + digest))
            else:
                raise ValueError('未知的清单结构')
        quants = set()
        for config in configs:
            quant = _json(config, rows).get('file_type')
            if isinstance(quant, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', quant):
                quants.add(quant)
        model.quantization = ', '.join(sorted(quants))
    except PARSE_ERRORS as exc:
        model.issues.add(f'清单读取不完整：{exc}')
    return model


def _hub_models(repo_files, rows, links, shared_stores, issues):
    for repo, items in repo_files.items():
        snapshots = defaultdict(list)
        refs = defaultdict(set)
        name = repo.name.removeprefix('models--').replace('--', '/')
        for path, (section, *rest) in items:
            if section == 'snapshots' and len(rest) > 1 and HUB_REVISION.fullmatch(rest[0]):
                snapshots[rest[0]].append(path)
            elif section == 'refs' and rest:
                try:
                    if rows[path]['kind'] == 'symlink':
                        raise ValueError('引用文件为链接')
                    version = _read(path, rows[path], _small).decode('ascii').strip()
                    if not HUB_REVISION.fullmatch(version):
                        raise ValueError('未知的修订 ID')
                    refs[version].add('/'.join(rest))
                except PARSE_ERRORS as exc:
                    issues.add(f'{path} · 模型引用未解析：{exc}')
        for version in sorted(snapshots.keys() | refs.keys()):
            model = _Model(HUB, name, version, repo / 'snapshots' / version, '本地 Hub 缓存')
            model.refs = refs[version]
            if not snapshots.get(version):
                model.issues.add('修订目录不可见或暂无文件')
            model.quantization = _quantization(path.stem for path in snapshots.get(version, ()))
            for path in snapshots.get(version, ()):
                target = path
                if rows[path]['kind'] == 'symlink':
                    target = links.get(path)
                    if (target is None or target.parent != repo / 'blobs'
                            or not HUB_DIGEST.fullmatch(target.name)):
                        target = None
                    elif rows[target]['kind'] == 'symlink':
                        # Newer Hub caches can link the per-repo blob to a
                        # centralized store. Both links have to be in this scan.
                        resolved = links.get(target)
                        if (resolved is None or resolved.parent.parent not in shared_stores
                                or resolved.parent.parent != repo.parent / 'blobs'
                                or not _hub_shared_blob(resolved)
                                or rows[resolved]['kind'] == 'symlink'):
                            target = None
                        else:
                            model.files.add(target)
                            target = resolved
                model.add(path, target)
            yield model


def _hub_shared_blob(path):
    return len(path.name) == 64 and HUB_DIGEST.fullmatch(path.name) and path.parent.name == path.name[:2]


def _hub_shared_stores(cache_roots, rows, issues):
    stores = set()
    for root in cache_roots:
        store = root / 'blobs'
        marker = store / '.huggingface-shared-blobs'
        if marker not in rows:
            continue
        try:
            if _read(marker, rows[marker], _small) != b'1\n':
                raise ValueError('未知的共享 Blob 目录版本')
            stores.add(store)
        except PARSE_ERRORS as exc:
            issues.add(f'{marker} · 模型缓存标记未解析：{exc}')
    return stores


def _local_path(value, base, home, environ):
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 4096:
        return None
    if value == '~' or value.startswith('~/'):
        value = str(home) + value[1:]
    if value.startswith('~') or '://' in value:
        return None
    value = re.sub(r'\$\{(\w+)\}|\$(\w+)', lambda m: environ.get(
        m[1] or m[2], m[0]), value)
    if '$' in value or '\0' in value:
        return None
    path = recorded_path(os.path.normpath(base / value))
    return path if path != Path('/') else None


def _yaml_paths(raw):
    """Read Comfy's two-level string map, including literal multiline paths."""
    sections = []
    block_key, mapping, indent, values = None, None, None, []
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        padding = len(line) - len(line.lstrip(' '))
        if block_key is not None and padding > indent:
            values.append(line.strip())
            continue
        if block_key is not None:
            mapping[block_key] = '\n'.join(values)
            block_key, values = None, []
        match = re.fullmatch(r'([\w.-]+):(?:\s*(.*))?', line.strip())
        if not match or padding and mapping is None:
            raise ValueError('未识别的 YAML 路径配置')
        key, value = match[1], (match[2] or '').strip()
        if padding == 0:
            if value and not value.startswith('#'):
                raise ValueError('未识别的 YAML 路径分组')
            mapping = {}
            sections.append(mapping)
            indent = None
            continue
        if (indent is not None and padding != indent) or key in mapping:
            raise ValueError('未识别的 YAML 路径条目')
        indent = padding
        if value in ('|', '|-', '|+'):
            block_key = key
        elif value.startswith(('"', "'")) and value[0] == value[-1]:
            mapping[key] = json.loads(value) if value[0] == '"' else value[1:-1].replace("''", "'")
        elif value and not value.startswith(('&', '*', '!', '{', '[', '>')):
            mapping[key] = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        else:
            raise ValueError('未识别的 YAML 路径值')
    if block_key is not None:
        mapping[block_key] = '\n'.join(values)
    return sections


def _comfy_paths(rows, roots, home, environ, issues):
    instances = {root.parent for root in roots if root.name == 'models'}
    configs = []
    desktop = home / 'Library/Application Support/ComfyUI'
    desktop_instance = desktop
    desktop_config = desktop / 'config.json'
    if desktop_config in rows:
        try:
            data = _json(desktop_config, rows)
            desktop_instance = recorded_path(data.get('basePath'))
            if desktop_instance is None or desktop_instance == Path('/'):
                raise ValueError('无法确认基础目录')
            instances.add(desktop_instance)
            roots.add(desktop_instance / 'models')
        except PARSE_ERRORS as exc:
            desktop_instance = desktop
            issues.add(f'{desktop_config} · ComfyUI 目录未解析：{exc}')
    for path in rows:
        if path.name not in ('extra_model_paths.yaml', 'extra_models_config.yaml', 'folder_paths.py'):
            continue
        install = path.parent
        if (install not in instances and install.name.casefold() != 'comfyui'
                and not (install / 'folder_paths.py' in rows and install / 'main.py' in rows)):
            continue
        instance = desktop_instance if install == desktop else install
        if install != desktop:
            instances.add(install)
            roots.add(install / 'models')
        if path.name != 'folder_paths.py':
            configs.append((path, instance))

    category_paths = defaultdict(set)
    for path, instance in configs:
        try:
            config = _read(path, rows[path], _small).decode('utf-8-sig')
            for section in _yaml_paths(config):
                base = _local_path(section.get('base_path', '.'), path.parent, home, environ)
                if base is None:
                    raise ValueError('无法确认基础目录')
                for category in COMFY_CATEGORIES & section.keys():
                    for value in section[category].splitlines():
                        directory = _local_path(value, base, home, {})
                        if directory is None:
                            raise ValueError(f'无法确认 {category} 目录')
                        category_paths[directory].add((instance, category, str(path)))
        except PARSE_ERRORS as exc:
            issues.add(f'{path} · ComfyUI 模型路径未完整解析：{exc}')
    return instances, category_paths


def _workflow_nodes(doc):
    if isinstance(doc.get('nodes'), list):
        for node in doc['nodes']:
            if isinstance(node, dict):
                yield node.get('id', '?'), node.get('type'), node.get('widgets_values', [])
    else:
        for node_id, node in doc.items():
            if isinstance(node, dict) and 'class_type' in node:
                yield node_id, node['class_type'], node.get('inputs', {})


def _workflow_refs(workflow, rows, index, instance, issues):
    try:
        for node_id, node_type, values in _workflow_nodes(_json(workflow, rows)):
            if not isinstance(node_type, str):
                continue
            node_type = node_type[:160].encode('utf-8', errors='replace').decode('utf-8')
            node_id = str(node_id)[:80].encode('utf-8', errors='replace').decode('utf-8')
            ref = f'{workflow}#{node_id}'
            if node_type not in COMFY_LOADER_FIELDS:
                inputs = values.values() if isinstance(values, dict) else values if isinstance(values, list) else ()
                if 'loader' in node_type.lower() or any(
                    isinstance(value, str) and Path(value).suffix.lower() in COMFY_EXTENSIONS
                    for value in inputs
                ):
                    issues.add(f'{ref} · 未解析的 ComfyUI 节点：{node_type}')
                continue
            category, keys = COMFY_LOADER_FIELDS[node_type]
            if isinstance(values, dict):
                names = [values.get(key) for key in keys]
            elif isinstance(values, list):
                names = values[:len(keys)]
            else:
                names = []
            for name in names:
                relative = None
                if isinstance(name, str) and name and '\0' not in name:
                    try:
                        name.encode('utf-8')
                        relative = Path(name.replace('\\', '/'))
                    except UnicodeError:
                        pass
                if relative is None or relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
                    issues.add(f'{ref} · 无法确认 {node_type} 的静态模型路径')
                    continue
                candidates = index.get((instance, category, relative), ())
                if len(candidates) != 1:
                    issues.add(f'{ref} · 模型路径未覆盖或存在多个匹配：{relative}')
                for model in candidates:
                    model.workflow_refs.add(ref)
    except PARSE_ERRORS as exc:
        issues.add(f'{workflow} · ComfyUI 工作流未解析：{exc}')


def _storage_target(path, rows, links):
    if rows[path]['kind'] != 'symlink':
        return path
    visited = {path}
    while path in links:
        path = links[path]
        if path in visited:
            break
        if rows[path]['kind'] != 'symlink':
            return path
        visited.add(path)
    return None


def _lm_studio(root, path, rows, links, models, mlx_dirs):
    parts = path.relative_to(root).parts
    suffix = path.suffix.lower()
    if len(parts) < 3 or suffix not in ('.gguf', '.safetensors'):
        return
    name = '/'.join(parts[:2])
    if suffix == '.gguf':
        version = GGUF_SHARD.sub('', path.name)
        group = path.with_name(version)
    else:
        version = 'MLX'
        group = path.parent
        mlx_dirs.add(group)
    key = LM_STUDIO, group
    if key not in models:
        display_path = path if suffix == '.gguf' else group
        models[key] = _Model(LM_STUDIO, name, version, display_path, '本地模型目录；下载来源未验证')
    model = models[key]
    if suffix == '.gguf' and path < model.path:
        model.path = path
    model.quantization = _quantization((group.stem, name))
    model.add(path, _storage_target(path, rows, links))


def _comfy_file(path, rows, links, roots, category_paths, instances, models, index):
    suffix = path.suffix.lower()
    if suffix not in COMFY_EXTENSIONS and (suffix or not rows[path]['size']):
        return
    matches = []
    for parent in path.parents:
        if parent not in roots and parent not in category_paths:
            continue
        relative = path.relative_to(parent)
        if parent in roots and len(relative.parts) > 1 and relative.parts[0] in COMFY_CATEGORIES:
            matches.append((parent.parent, relative.parts[0], Path(*relative.parts[1:]), '默认模型目录'))
        if parent in category_paths:
            matches.extend((instance, category, relative, source)
                           for instance, category, source in sorted(category_paths[parent]))
    if not matches:
        return
    categories = {category for _, category, _, _ in matches}
    if suffix not in COMFY_EXTENSIONS and 'classifiers' not in categories:
        return
    evidence = ', '.join(sorted({item[3] for item in matches}))
    model = _Model(COMFY, path.stem, path.name, path, f'本地模型路径 · {evidence}')
    model.quantization = _quantization((path.stem,))
    model.add(path, _storage_target(path, rows, links))
    models[COMFY, path] = model
    for instance, category, relative, _ in matches:
        instances.add(instance)
        candidates = index[instance, COMFY_ALIASES.get(category, category), relative]
        if all(candidate is not model for candidate in candidates):
            candidates.append(model)


def _counts(models, rows):
    identity = {path: (row['dev'], row['ino']) for path, row in rows.items()
                if row['kind'] != 'symlink'}
    observed = Counter(identity.values())
    storage = {}
    owners = Counter()
    shared = set()
    known_paths = {path for model in models for path in model.files}
    for model in models:
        owners.update({identity[path] for path in model.storage})
    for path, key in identity.items():
        if key not in owners:
            continue
        row = rows[path]
        storage[key] = row['allocated_bytes']
        if (owners[key] > 1 or observed[key] < row['nlink'] or path not in known_paths
                or row.get('shared_container') or row.get('shared_owners')
                or any(Path(ref) not in known_paths for ref in row.get('refs', ()))):
            shared.add(key)
    return identity, storage, shared


def _serialize(model, identity, storage, shared):
    keys = {identity[path] for path in model.storage}
    shared_bytes = sum(storage[key] for key in keys & shared)
    unique_bytes = sum(storage[key] for key in keys - shared)
    return dict(
        id=f'{model.tool}:{model.path}', tool=model.tool, name=model.name, version=model.version,
        path=str(model.path), source=model.source, digest=model.digest, quantization=model.quantization,
        runtime_digests=sorted(model.runtime_digests),
        files=sorted(str(path) for path in model.files),
        shared_files=sorted(str(path) for path in model.storage if identity[path] in shared),
        refs=sorted(model.refs), workflow_refs=sorted(model.workflow_refs), issues=sorted(model.issues),
        physical_bytes=unique_bytes + shared_bytes, unique_bytes=unique_bytes, shared_bytes=shared_bytes,
        active_state='unknown', active_source='仅盘点磁盘文件，未核对运行状态', decision='keep',
    )


def _store_ollama_model(models, root, model):
    key = OLLAMA, root, model.name.casefold(), model.version.casefold()
    previous = models.get(key)
    if previous is not None:
        directory = model.path.relative_to(root).parts[0]
        if directory != 'manifests-v2':
            model, previous = previous, model
        model.files.update(previous.files)
        model.storage.update(previous.storage)
        model.issues.update(previous.issues)
        if model.digest and model.digest == previous.digest:
            model.runtime_digests.update(previous.runtime_digests)
    models[key] = model


def summarize(report, home, environ=None, *, model_roots=()):
    """Summarize known layouts using only scanned rows and bounded metadata reads.

    model_roots holds optional (absolute path, tool name) pairs for caller-scoped
    custom installs. The function never discovers files by following these paths.
    """
    home = Path(home)
    env = environ or {}
    rows = {Path(row['path']): row for row in report.get('files', ()) if not row.get('agent_store_path')}
    roots = defaultdict(set)
    for raw_root, tool in [*locations(home, env), *model_roots]:
        root = recorded_path(str(raw_root))
        if root is not None and root != Path('/'):
            roots[tool].add(root)

    issues = set()
    models = {}
    hub_repos = defaultdict(list)
    links = {}
    index = defaultdict(list)
    mlx_dirs = set()

    for target, row in rows.items():
        for reference in row.get('refs', ()):
            link = Path(reference)
            if link in rows and rows[link]['kind'] == 'symlink':
                links[link] = target
        if len(target.parents) >= 3 and target.parent.name == 'blobs' and OLLAMA_DIGEST.fullmatch(target.name):
            roots[OLLAMA].add(target.parent.parent)
        for parent in target.parents:
            if parent.name.startswith('models--') and parent.name != 'models--':
                hub_repos[parent].append((target, target.relative_to(parent).parts))
                break
            if parent.name == 'models' and parent.parent.name.casefold() == 'comfyui':
                roots[COMFY].add(parent)

    instances, category_paths = _comfy_paths(rows, roots[COMFY], home, env, issues)
    shared_stores = _hub_shared_stores(roots[HUB] | {repo.parent for repo in hub_repos}, rows, issues)
    for path in rows:
        if (len(path.parents) >= 5 and path.parts[-5] in ('manifests', 'manifests-v2')
                and (path.parents[4] in roots[OLLAMA] or path.parts[-4].casefold() in ('registry.ollama.ai', 'ollama.com'))):
            root = path.parents[4]
            _store_ollama_model(models, root, _ollama(root, path, rows, links))
            continue
        lm_root = next((parent for parent in path.parents if parent in roots[LM_STUDIO]), None)
        if lm_root is not None:
            _lm_studio(lm_root, path, rows, links, models, mlx_dirs)
        elif roots[COMFY] or category_paths:
            _comfy_file(path, rows, links, roots[COMFY], category_paths, instances, models, index)

    for model in _hub_models(hub_repos, rows, links, shared_stores, issues):
        models[HUB, model.path] = model
    for path in rows:
        if path.parent in mlx_dirs and path.name in (
                'config.json', 'generation_config.json', 'model.safetensors.index.json',
                'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json', 'chat_template.jinja'):
            model = models[LM_STUDIO, path.parent]
            target = _storage_target(path, rows, links)
            model.add(path, target)
            if path.name == 'config.json' and target is not None:
                try:
                    quant = _json(target, rows).get('quantization')
                    bits = quant.get('bits') if isinstance(quant, dict) else None
                    if isinstance(bits, int) and not isinstance(bits, bool) and 0 < bits < 32:
                        model.quantization = f'{bits}BIT'
                except PARSE_ERRORS as exc:
                    model.issues.add(f'量化配置未解析：{exc}')
        if path.suffix.lower() != '.json' or 'workflows' not in path.parts:
            continue
        instance = next((parent for parent in path.parents if parent in instances), None)
        if instance is not None and 'workflows' in path.relative_to(instance).parts[:-1]:
            _workflow_refs(path, rows, index, instance, issues)

    known_paths = {path for model in models.values() for path in model.files}
    for path, row in rows.items():
        if path in known_paths:
            continue
        tool = UNKNOWN
        source = '普通模型文件；来源未验证'
        if path.parent.name == 'blobs' and path.parent.parent in roots[OLLAMA] and OLLAMA_DIGEST.fullmatch(path.name):
            tool, source = OLLAMA, '本地 Blob；所属模型未识别'
        elif path.parent.parent in shared_stores and _hub_shared_blob(path):
            tool, source = HUB, '本地共享 Blob；所属修订未识别'
        elif row['kind'] == 'model' and path.parent.name == 'blobs' and path.parent.parent in hub_repos:
            tool, source = HUB, '本地 Blob；所属修订未识别'
        elif row['kind'] != 'model' and path.suffix.lower() not in MODELS:
            continue
        model = _Model(tool, path.name, '', path, source)
        model.quantization = _quantization((path.stem,))
        model.add(path, _storage_target(path, rows, links))
        models[tool, path] = model

    identity, storage, shared = _counts(models.values(), rows)
    models = [_serialize(model, identity, storage, shared) for model in models.values()]
    models.sort(key=lambda model: (model['tool'] == UNKNOWN, model['tool'], model['name'].casefold(),
                                   model['version'], model['path']))
    for model in models:
        issues.update(f"{model['path']} · {issue}" for issue in model['issues'])
    return dict(models=models, issues=sorted(issues), physical_bytes=sum(storage.values()),
                unique_bytes=sum(size for key, size in storage.items() if key not in shared),
                shared_bytes=sum(storage[key] for key in shared))
