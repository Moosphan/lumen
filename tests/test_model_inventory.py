import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from engine.lumen.model_inventory import locations, summarize
from engine.lumen.scan import scan


class ModelInventoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()

    def file(self, name, contents=b'x' * 8192):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def data(self, name, contents):
        return self.file(name, json.dumps(contents).encode())

    def test_ollama_tags_share_blob_and_record_manifest_identity(self):
        root = self.home / 'volumes/models'
        weight_hash = 'a' * 64
        config_hash = 'b' * 64
        weight = self.file(root / 'blobs' / ('sha256-' + weight_hash))
        self.data(root / 'blobs' / ('sha256-' + config_hash), {'file_type': 'Q4_K_M'})
        manifest = {'schemaVersion': 2, 'config': {'digest': f'sha256:{config_hash}'},
                    'layers': [{'digest': f'sha256:{weight_hash}'}]}
        latest = self.data(root / 'manifests/registry.ollama.ai/library/demo/latest', manifest)
        self.data(root / 'manifests/registry.ollama.ai/library/demo/copy', manifest)

        env = {'OLLAMA_MODELS': str(root)}
        inventory = summarize(scan([root], deep=False), self.home, env)
        self.assertIn((root, 'Ollama'), locations(self.home, env))
        models = {m['version']: m for m in inventory['models']}
        self.assertEqual(set(models), {'latest', 'copy'})
        model = models['latest']
        self.assertEqual(model['name'], 'demo')
        self.assertEqual(model['tool'], 'Ollama')
        self.assertEqual(model['digest'], 'sha256:' + hashlib.sha256(latest.read_bytes()).hexdigest())
        self.assertEqual(model['quantization'], 'Q4_K_M')
        self.assertEqual(model['active_state'], 'unknown')
        self.assertEqual(model['decision'], 'keep')
        self.assertIn(str(weight), model['shared_files'])
        self.assertGreater(model['shared_bytes'], 0)
        self.assertGreater(model['unique_bytes'], 0)
        self.assertEqual(inventory['physical_bytes'], model['shared_bytes'] + sum(
            m['unique_bytes'] for m in models.values()))

    def test_hub_snapshots_share_blobs_and_attach_named_refs(self):
        repo = self.home / 'custom-hub/models--org--repo'
        a, b = 'a' * 40, 'b' * 40
        blob = self.file(repo / 'blobs' / ('c' * 64))
        for revision in (a, b):
            link = repo / f'snapshots/{revision}/weights.Q5_K_M.gguf'
            link.parent.mkdir(parents=True)
            link.symlink_to(Path('../../blobs') / blob.name)
        self.file(repo / 'refs/main', a.encode())
        self.file(repo / 'refs/branches/test', b.encode())
        extra = self.file(repo / f'snapshots/{a}/config.json', b'{}')
        unrelated = self.file('outside.gguf')
        outside_link = repo / f'snapshots/{a}/outside.gguf'
        outside_link.symlink_to(unrelated)

        inventory = summarize(scan([repo], deep=False), self.home)
        models = {m['version']: m for m in inventory['models']}
        self.assertEqual(set(models), {a, b})
        self.assertEqual(models[a]['name'], 'org/repo')
        self.assertEqual(models[a]['refs'], ['main'])
        self.assertEqual(models[b]['refs'], ['branches/test'])
        self.assertEqual(models[a]['quantization'], 'Q5_K_M')
        self.assertIn(str(blob), models[a]['shared_files'])
        self.assertIn(str(extra), models[a]['files'])
        self.assertNotIn(str(unrelated), models[a]['files'])
        self.assertTrue(models[a]['issues'])

    def test_hub_xet_shared_blob_resolves_across_repositories(self):
        root = self.home / '.cache/huggingface/hub'
        digest = 'd' * 64
        payload = self.file(root / f'blobs/dd/{digest}')
        self.file(root / 'blobs/.huggingface-shared-blobs', b'1\n')
        for name in ('first', 'second'):
            repo = root / f'models--org--{name}'
            blob = repo / 'blobs' / ('e' * 64)
            blob.parent.mkdir(parents=True)
            blob.symlink_to(payload)
            snapshot = repo / 'snapshots' / ('f' * 40) / 'weights.safetensors'
            snapshot.parent.mkdir(parents=True)
            snapshot.symlink_to(blob)

        inventory = summarize(scan([root], deep=False), self.home)
        self.assertEqual(inventory['physical_bytes'], payload.stat().st_blocks * 512)
        self.assertEqual(inventory['shared_bytes'], inventory['physical_bytes'])
        self.assertEqual(len(inventory['models']), 2)
        for model in inventory['models']:
            self.assertEqual(model['shared_files'], [str(payload)])
            self.assertEqual(model['issues'], [])

    def test_ollama_v2_manifest_lists_resolve_scoped_child_manifest(self):
        root = self.home / '.ollama/models'

        def write_blob(raw):
            digest = hashlib.sha256(raw).hexdigest()
            self.file(root / 'blobs' / f'sha256-{digest}', raw)
            return f'sha256:{digest}'

        weights = write_blob(b'x' * 8192)
        config = write_blob(b'{"file_type": "Q6_K"}')
        child = write_blob(json.dumps({'config': {'digest': config}, 'layers': [{'digest': weights}]}).encode())
        blob_id = write_blob(json.dumps({'manifests': [{'digest': child}]}).encode())
        manifest = root / 'manifests-v2/ollama.com/library/demo/latest'
        manifest.parent.mkdir(parents=True)
        manifest.symlink_to(root / 'blobs' / blob_id.replace(':', '-'))
        self.data(root / 'manifests/registry.ollama.ai/library/demo/latest', {
            'config': {'digest': config}, 'layers': [{'digest': weights}],
        })

        inventory = summarize(scan([root], deep=False), self.home)
        self.assertEqual(len(inventory['models']), 1)
        model = inventory['models'][0]
        self.assertEqual(model['digest'], blob_id)
        self.assertEqual(model['quantization'], 'Q6_K')
        self.assertIn(str(root / 'blobs' / weights.replace(':', '-')), model['files'])

    def test_lm_studio_groups_gguf_shards_and_keeps_other_hardlink_references(self):
        base = '.lmstudio/models/publisher/repo/'
        first = self.file(base + 'foo-Q4_K_M-00001-of-00002.gguf')
        second = self.file(base + 'foo-Q4_K_M-00002-of-00002.gguf', b'other' * 2000)
        single = self.file(base + 'foo-Q8_0.gguf')
        generic = self.home / 'Downloads/backup.gguf'
        generic.parent.mkdir()
        os.link(first, generic)

        inventory = summarize(scan([self.home], deep=False), self.home)
        rows = {m['quantization']: m for m in inventory['models'] if m['tool'] == 'LM Studio'}
        self.assertEqual(set(rows), {'Q4_K_M', 'Q8_0'})
        group = rows['Q4_K_M']
        self.assertEqual(group['name'], 'publisher/repo')
        self.assertEqual(group['version'], 'foo-Q4_K_M.gguf')
        self.assertEqual(group['path'], str(first))
        self.assertEqual(set(group['files']), {str(first), str(second)})
        self.assertEqual(group['shared_files'], [str(first)])
        unknown = next(m for m in inventory['models'] if m['tool'] == '未知')
        self.assertEqual(unknown['files'], [str(generic)])
        self.assertEqual(unknown['unique_bytes'], 0)
        self.assertEqual(rows['Q8_0']['files'], [str(single)])
        self.assertEqual(inventory['physical_bytes'], sum(p.stat().st_blocks * 512 for p in (first, second, single)))

    def test_comfy_desktop_paths_and_workflow_references_are_scoped(self):
        base = self.home / 'Workspace'
        settings = self.home / 'Library/Application Support/ComfyUI'
        self.data(settings / 'config.json', {'basePath': str(base)})
        self.file(settings / 'extra_models_config.yaml', b'''desktop:
  base_path: ../../../Shared
  checkpoints: |
    main
    alternates
''')
        default = self.file(base / 'models/loras/style.safetensors', b'default' * 1000)
        extra = self.file('Shared/main/nested/model.safetensors')
        outside = self.file('Shared/alternates/hidden.safetensors')
        workflow = self.data(base / 'user/default/workflows/ui.json', {'nodes': [
            {'id': 1, 'type': 'CheckpointLoaderSimple', 'widgets_values': ['nested/model.safetensors']},
            {'id': 2, 'type': 'CustomLoader', 'widgets_values': ['some.bin']},
        ]})
        api = self.data(base / 'user/default/workflows/api.json', {'3': {
            'class_type': 'LoraLoader', 'inputs': {'lora_name': 'style.safetensors'},
        }})
        report = scan([settings, base, extra], deep=False)

        inventory = summarize(report, self.home)
        models = {m['path']: m for m in inventory['models']}
        self.assertEqual(set(models), {str(default), str(extra)})
        self.assertEqual(models[str(extra)]['tool'], 'ComfyUI')
        self.assertEqual(models[str(extra)]['workflow_refs'], [f'{workflow}#1'])
        self.assertEqual(models[str(default)]['workflow_refs'], [f'{api}#3'])
        self.assertTrue(any('CustomLoader' in issue for issue in inventory['issues']))
        self.assertNotIn(str(outside), json.dumps(inventory))

    def test_changed_or_missing_metadata_keeps_visible_models_and_omits_missing_weights(self):
        root = self.home / '.ollama/models'
        digest = 'a' * 64
        weight = self.file(root / 'blobs' / f'sha256-{digest}')
        manifest = self.data(root / 'manifests/registry.ollama.ai/library/demo/latest', {
            'config': {'digest': f'sha256:{digest}'}, 'layers': []})
        report = scan([manifest], deep=False)
        first = summarize(report, self.home)
        self.assertEqual(first['models'][0]['tool'], 'Ollama')
        self.assertTrue(first['models'][0]['issues'])
        self.assertNotIn(str(weight), first['models'][0]['files'])

        manifest.write_text('{}')
        second = summarize(report, self.home)
        self.assertEqual(second['models'][0]['decision'], 'keep')
        self.assertTrue(second['models'][0]['issues'])
        self.assertEqual(second['models'][0]['digest'], '')

    def test_invalid_ollama_layer_preserves_remaining_known_blob_relationships(self):
        root = self.home / '.ollama/models'
        digest = 'a' * 64
        weight = self.file(root / 'blobs' / f'sha256-{digest}')
        self.data(root / 'manifests/registry.ollama.ai/library/demo/latest', {
            'config': {'digest': f'sha256:{digest}'}, 'layers': [
                {'digest': '../outside'}, {'digest': f'sha256:{digest}', 'size': weight.stat().st_size + 1},
            ],
        })
        model = summarize(scan([root], deep=False), self.home)['models'][0]
        self.assertEqual(model['decision'], 'keep')
        self.assertIn(str(weight), model['files'])
        self.assertTrue(any('摘要' in issue for issue in model['issues']))
        self.assertTrue(any('大小' in issue for issue in model['issues']))
