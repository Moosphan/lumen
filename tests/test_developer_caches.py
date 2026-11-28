import os
import plistlib
import tempfile
import time
import unittest
from pathlib import Path

from engine.lumen import app_cleanup
from engine.lumen.scan import scan


class DeveloperCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'home'

    def file(self, relative, data=b'x' * 8192):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def classify(self, *, roots=(), keep=(), excluded=(), env=None):
        from engine.lumen import developer_caches

        locations = developer_caches.locations(self.home, excluded, authorized=roots, env=env)
        report = scan([*roots, *(row['path'] for row in locations)], deep=False, keep=keep, exclude=excluded)
        app_cleanup.classify(report, self.home)
        groups = developer_caches.classify(report, self.home, discovered=locations)
        return groups, {Path(row['path']): row for row in report['files']}

    def test_npm_only_reviews_complete_content_entries_from_its_known_store(self):
        blob = self.file('.npm/_cacache/content-v2/sha512/aa/bb/' + 'c' * 124)
        linked = self.file('.npm/_cacache/content-v2/sha512/aa/bb/' + 'd' * 124)
        index = self.file('.npm/_cacache/index-v5/aa/bb/metadata')
        temporary = self.file('.npm/_cacache/tmp/download.partial')
        lookalike = self.file('project/.npm/_cacache/content-v2/sha512/aa/bb/' + 'c' * 124)
        os.link(linked, self.home / 'project/hard-link')
        groups, rows = self.classify(roots=(self.home / 'project',))
        self.assertEqual([group['tool'] for group in groups], ['npm'])
        self.assertEqual(groups[0]['review_paths'], [str(blob)])
        self.assertEqual(groups[0]['review_bytes'], rows[blob]['allocated_bytes'])
        self.assertEqual(rows[blob]['decision'], 'review')
        self.assertEqual(rows[linked]['decision'], 'keep')
        self.assertEqual(rows[index]['decision'], 'keep')
        self.assertEqual(rows[temporary]['decision'], 'defer')
        self.assertNotIn('developer_cache', rows[lookalike])

    def test_pip_legacy_http_entries_can_be_reviewed_and_v2_pairs_stay_together(self):
        digest = 'abcde' + 'f' * 51
        legacy = self.file('Library/Caches/pip/http/a/b/c/d/e/' + digest)
        metadata = self.file('Library/Caches/pip/http-v2/a/b/c/d/e/' + digest)
        body = self.file('Library/Caches/pip/http-v2/a/b/c/d/e/' + digest + '.body')
        wheel = self.file('Library/Caches/pip/wheels/a/pkg.whl')
        groups, rows = self.classify(roots=(self.home / 'Library/Caches',))
        self.assertEqual([p for g in groups for p in g['review_paths']], [str(legacy)])
        self.assertEqual(rows[metadata]['decision'], 'keep')
        self.assertEqual(rows[body]['decision'], 'keep')
        self.assertNotIn('developer_cache', rows[wheel])

    def test_pnpm_and_uv_attribution_preserves_linked_package_storage(self):
        object_path = self.file('Library/pnpm/store/v10/files/ab/content')
        linked = object_path.with_name('linked')
        os.link(object_path, linked)
        uv = self.file('.cache/uv/archive-v0/id/package.py')
        elsewhere = self.file('.local/share/uv/tools/tool/site.py')
        groups, rows = self.classify(roots=(self.home / '.local/share',))
        self.assertEqual({g['tool'] for g in groups}, {'pnpm', 'uv'})
        pnpm = next(group for group in groups if group['tool'] == 'pnpm')
        self.assertEqual(pnpm['allocated_bytes'], rows[object_path]['allocated_bytes'])
        self.assertEqual(pnpm['files'], 2)
        self.assertFalse(pnpm['review_paths'])
        self.assertEqual(rows[uv]['decision'], 'keep')
        self.assertNotIn('developer_cache', rows[elsewhere])

    def test_docker_reports_allocated_vm_bytes_with_unknown_build_cache_size(self):
        image = self.file('Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw')
        with image.open('r+b') as stream:
            stream.truncate(128 * 1024 * 1024)
        settings = self.file('Library/Containers/com.docker.docker/Data/settings.json')
        groups, rows = self.classify()
        self.assertNotIn(settings, rows)
        self.assertEqual(rows[image]['decision'], 'keep')
        self.assertEqual(groups[0]['sizing'], 'vm_image')
        self.assertEqual(groups[0]['allocated_bytes'], image.stat().st_blocks * 512)
        self.assertEqual(groups[0]['logical_bytes'], 128 * 1024 * 1024)
        self.assertIsNone(groups[0]['isolated_build_cache_bytes'])
        self.assertEqual(groups[0]['review_paths'], [])

    def test_homebrew_reviews_old_complete_unreferenced_downloads(self):
        prefix = 'Library/Caches/Homebrew/downloads/'
        old = self.file(prefix + 'a' * 64 + '--tool-2.tar.gz')
        protected = self.file(prefix + 'b' * 64 + '--protected-2.tar.gz')
        linked = self.file(prefix + 'c' * 64 + '--linked-2.tar.gz')
        recent = self.file(prefix + 'd' * 64 + '--recent-2.tar.gz')
        junk = self.file(prefix + 'unrecognized.tar.gz')
        shortcut = self.home / 'Library/Caches/Homebrew/linked-2.tar.gz'
        shortcut.symlink_to(linked)
        now = time.time() - 121 * 86400
        for path in (old, protected, linked, junk):
            os.utime(path, (now, now))
        groups, rows = self.classify(roots=(self.home / 'Library/Caches',), keep=(protected,))
        self.assertEqual(groups[0]['review_paths'], [str(old)])
        self.assertEqual(rows[protected]['reason'], '用户已标记保留')
        for path in (linked, recent, junk):
            self.assertEqual(rows[path]['decision'], 'keep')

    def test_xcode_uses_existing_app_review_rule_for_safe_intermediates(self):
        marker = self.file('Applications/Xcode.app/Contents/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'com.apple.dt.Xcode', 'CFBundleShortVersionString': '16.2',
        }))
        base = 'Library/Developer/Xcode/DerivedData/MyProject/'
        intermediate = self.file(base + 'Build/Intermediates.noindex/compile.o')
        product = self.file(base + 'Build/Products/Release/MyProject')
        package = self.file(base + 'SourcePackages/checkouts/lib/readme')
        groups, rows = self.classify(roots=(marker.parents[2],))
        self.assertEqual(groups[0]['tool'], 'Xcode')
        self.assertEqual(groups[0]['review_paths'], [str(intermediate)])
        self.assertEqual(groups[0]['files'], 1)
        self.assertEqual(rows[intermediate]['cleanup_kind'], 'build')
        self.assertEqual(rows[product]['decision'], 'keep')
        self.assertEqual(rows[package]['decision'], 'keep')

    def test_env_overrides_require_a_grant_and_links_or_exclusions_disallow_discovery(self):
        from engine.lumen import developer_caches

        external = self.base / 'other'
        custom = external / '_cacache/content-v2'
        custom.mkdir(parents=True)
        (self.home / '.npm').mkdir(parents=True)
        (self.home / '.npm/_cacache').symlink_to(custom.parent)
        uv = self.file('.cache/uv/archive-v0/id/package.py').parents[2]
        env = {'npm_config_cache': str(external), 'UV_CACHE_DIR': str(self.home)}
        self.assertEqual(developer_caches.locations(self.home, [uv], env=env), [])
        locations = developer_caches.locations(self.home, [uv], authorized=[external], env=env)
        self.assertEqual([row['path'] for row in locations], [str(custom.parent)])

    def test_changed_cache_identity_disallows_review_from_old_location(self):
        from engine.lumen import developer_caches

        blob = self.file('.npm/_cacache/content-v2/sha512/aa/bb/' + 'c' * 124)
        locations = developer_caches.locations(self.home, [])
        root = blob.parents[4]
        root.rename(root.with_name('previous'))
        self.file(blob.relative_to(self.home))
        report = scan([root], deep=False)
        groups = developer_caches.classify(report, self.home, discovered=locations)
        self.assertFalse(groups[0]['review_paths'])
        self.assertEqual(report['files'][0]['decision'], 'keep')

    def test_manual_scan_discovers_only_cache_roots_intersecting_a_grant(self):
        from engine.lumen import developer_caches

        self.file('.npm/_cacache/content-v2/sha512/aa/bb/' + 'c' * 124)
        uv = self.file('.cache/uv/archive-v0/id/package.py')
        image = self.file('Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw')
        self.assertEqual(developer_caches.locations(self.home, scoped_only=True), [])
        locations = developer_caches.locations(
            self.home, authorized=[uv.parent, image.parent], scoped_only=True)
        self.assertEqual({row['path'] for row in locations}, {str(uv.parents[2]), str(image)})

    def test_manual_scan_protects_docker_disk_under_a_case_alias(self):
        from engine.lumen import developer_caches

        image = self.file('Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw')
        grant = self.home / 'library/containers/com.docker.docker/data/vms/0/data'
        if not grant.is_dir():
            self.skipTest('The test volume treats path case as significant')
        alias = grant / image.name
        locations = developer_caches.locations(self.home, authorized=[grant], scoped_only=True)
        self.assertEqual([(location['path'], location['aliases']) for location in locations],
                         [(str(image), [str(alias)])])
        report = scan([grant, image], deep=False)
        groups = developer_caches.classify(report, self.home, discovered=locations)
        rows = {row['path']: row for row in report['files']}
        self.assertEqual(rows[str(alias)]['decision'], 'keep')
        self.assertEqual(rows[str(image)]['decision'], 'keep')
        self.assertEqual(groups[0]['allocated_bytes'], image.stat().st_blocks * 512)

    def test_manual_case_alias_does_not_create_duplicate_npm_candidates(self):
        from engine.lumen import developer_caches

        blob = self.file('.npm/_cacache/content-v2/sha512/aa/bb/' + 'c' * 124)
        root = self.home / '.npm/_cacache'
        grant = self.home / '.NPM'
        if not grant.is_dir():
            self.skipTest('The test volume treats path case as significant')
        locations = developer_caches.locations(self.home, authorized=[grant])
        report = scan([grant, root], deep=False)
        groups = developer_caches.classify(report, self.home, discovered=locations)
        self.assertEqual(groups[0]['review_paths'], [])
        self.assertEqual(groups[0]['allocated_bytes'], blob.stat().st_blocks * 512)
        self.assertTrue(all(row['decision'] == 'keep' for row in report['files']))

    def test_distinct_case_variant_is_outside_the_known_cache(self):
        from engine.lumen import developer_caches

        self.file('Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw')
        grant = self.home / 'library/containers/com.docker.docker/data/vms/0/data'
        if grant.is_dir():
            self.skipTest('The test volume treats path case as insignificant')
        self.file(grant.relative_to(self.home) / 'Docker.raw')
        self.assertEqual(developer_caches.locations(self.home, authorized=[grant], scoped_only=True), [])

    def test_configured_cache_can_match_a_verified_case_alias_grant(self):
        from engine.lumen import developer_caches

        configured = self.base / 'PackageCache'
        root = configured / '_cacache'
        root.mkdir(parents=True)
        grant = self.base / 'packagecache'
        if not grant.is_dir():
            self.skipTest('The test volume treats path case as significant')
        locations = developer_caches.locations(self.home, authorized=[grant], scoped_only=True,
                                               env={'NPM_CONFIG_CACHE': str(configured)})
        self.assertEqual([(location['path'], location['aliases']) for location in locations],
                         [(str(root), [str(grant / root.name)])])
