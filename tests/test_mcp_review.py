import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.lumen import mcp_review as review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.file = Path(scratch.name).resolve() / 'private-filename.tmp'
        self.file.write_bytes(b'cleanup candidate')
        info = self.file.stat()
        self.candidate = dict(path=str(self.file), decision='safe', size=info.st_size,
                              dev=info.st_dev, ino=info.st_ino, mtime_ns=info.st_mtime_ns,
                              ctime_ns=info.st_ctime_ns, reason='private text should stay private')
        self.config = dict(mcp_enabled=True, report={'scan_id': 'existing-scan-id'})

    def create_draft(self):
        summary = review.summary(self.config, [self.candidate])
        return review.create_draft(self.config, [self.candidate], summary['snapshot'],
                                   [row['alias'] for row in summary['candidates']])

    def test_summary_alias_is_redacted_and_bound_to_scan(self):
        summary = review.summary(self.config, [self.candidate, {**self.candidate, 'decision': 'keep'}])
        candidate, = summary['candidates']
        self.assertEqual(candidate, dict(alias=candidate['alias'], decision='safe', size=self.file.stat().st_size))
        self.assertNotIn(self.file.name, json.dumps(summary))
        self.assertNotIn('private text', json.dumps(summary))
        self.assertNotIn('existing-scan-id', json.dumps(summary))
        self.assertEqual(review.summary(self.config, [self.candidate])['candidates'], [candidate])
        self.config['report']['scan_id'] = 'new-scan-id'
        changed = review.summary(self.config, [self.candidate])
        self.assertNotEqual(changed['snapshot'], summary['snapshot'])
        self.assertNotEqual(changed['candidates'][0]['alias'], candidate['alias'])

    def test_create_requires_snapshot_alias_and_current_scope(self):
        summary = review.summary(self.config, [self.candidate])
        alias = summary['candidates'][0]['alias']
        with self.assertRaises(ValueError):
            review.create_draft(self.config, [], summary['snapshot'], [alias])
        with self.assertRaises(ValueError):
            review.create_draft(self.config, [self.candidate], summary['snapshot'], [str(self.file)])
        with self.assertRaises(ValueError):
            review.create_draft(self.config, [self.candidate], summary['snapshot'], [alias, alias])
        self.config['report']['scan_id'] = 'another-id'
        with self.assertRaises(ValueError):
            review.create_draft(self.config, [self.candidate], summary['snapshot'], [alias])

    def test_draft_does_not_change_file_and_needs_exact_native_approval(self):
        draft = self.create_draft()
        draft_id = draft['draft_id']
        queued, = review.pending(self.config, [self.candidate])
        self.assertEqual(queued['draft_id'], draft_id)
        self.assertEqual(queued['items'][0]['path'], str(self.file))
        self.assertNotIn(self.file.name, json.dumps(draft))
        with self.assertRaises(ValueError):
            review.take(self.config, [self.candidate], draft_id, [str(self.file), '/outside'])
        self.assertEqual(review.take(self.config, [self.candidate], draft_id, [str(self.file)]), [str(self.file)])
        self.assertEqual(review.pending(self.config, [self.candidate]), [])
        with self.assertRaises(ValueError):
            review.take(self.config, [self.candidate], draft_id, [str(self.file)])
        self.assertTrue(self.file.exists())

    def test_new_generation_changed_scope_and_changed_identity_invalidate_draft(self):
        draft = self.create_draft()
        draft_id = draft['draft_id']
        next_generation = copy.deepcopy(self.config)
        next_generation['report']['scan_id'] = 'replaced-scan-id'
        with self.assertRaises(ValueError):
            review.take(next_generation, [self.candidate], draft_id, [str(self.file)])
        with self.assertRaises(ValueError):
            review.take(self.config, [], draft_id, [str(self.file)])
        self.file.write_bytes(b'candidate changed after the draft')
        self.assertEqual(review.pending(self.config, [self.candidate]), [])
        with self.assertRaises(ValueError):
            review.take(self.config, [self.candidate], draft_id, [str(self.file)])

    def test_expired_draft_cannot_be_approved(self):
        with patch.object(review.time, 'time', return_value=100):
            draft = self.create_draft()
        with patch.object(review.time, 'time', return_value=100 + review.DRAFT_TTL_SECONDS):
            self.assertEqual(review.status(self.config, draft['draft_id'], draft['status_token'])['status'], 'expired')
            self.assertEqual(review.pending(self.config, [self.candidate]), [])
            with self.assertRaises(ValueError):
                review.take(self.config, [self.candidate], draft['draft_id'], [str(self.file)])

    def test_native_discard_keeps_only_rejected_status_and_prevents_approval(self):
        draft = self.create_draft()
        draft_id = draft['draft_id']
        review.discard(self.config, draft_id)
        self.assertEqual(review.status(self.config, draft_id, draft['status_token'])['status'], 'rejected')
        self.assertEqual(review.pending(self.config, [self.candidate]), [])
        self.assertNotIn(str(self.file), json.dumps(self.config['mcp_drafts']))
        with self.assertRaises(ValueError):
            review.take(self.config, [self.candidate], draft_id, [str(self.file)])

    def test_only_own_status_token_can_read_redacted_batch_result(self):
        draft = self.create_draft()
        draft_id = draft['draft_id']
        other = self.create_draft()
        with self.assertRaises(ValueError):
            review.status(self.config, draft_id, other['status_token'])
        review.take(self.config, [self.candidate], draft_id, [str(self.file)])
        review.finish(self.config, draft_id, {'batch_id': 'a' * 32, 'items': [
            {'path': str(self.file), 'stored': '/secret-location', 'status': 'quarantined'}
        ]})
        result = review.status(self.config, draft_id, draft['status_token'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['batch_id'], 'a' * 32)
        self.assertEqual(result['quarantined'], 1)
        self.assertNotIn(self.file.name, json.dumps(result))
        self.assertNotIn('secret-location', json.dumps(result))
        self.assertNotIn(draft['status_token'], json.dumps(result))

    def test_recovery_leaves_unverified_moves_interrupted(self):
        draft = self.create_draft()
        draft_id = draft['draft_id']
        token = draft['status_token']
        review.take(self.config, [self.candidate], draft_id, [str(self.file)])
        review.finish(self.config, draft_id, {'batch_id': 'a' * 32, 'items': [{'status': 'moving'}]})
        result = review.status(self.config, draft_id, token)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['batch_id'], 'a' * 32)
        self.assertNotIn('skipped', result)
        review.finish(self.config, draft_id, {'batch_id': 'a' * 32, 'items': [{'status': 'quarantined'}]})
        result = review.status(self.config, draft_id, token)
        self.assertEqual((result['status'], result['quarantined']), ('completed', 1))

    def test_disabled_review_has_no_public_data_and_revokes_drafts(self):
        draft = self.create_draft()
        review.disable(self.config)
        self.config['mcp_enabled'] = False
        with self.assertRaises(ValueError):
            review.summary(self.config, [self.candidate])
        self.assertEqual(review.pending(self.config, [self.candidate]), [])
        self.config['mcp_enabled'] = True
        with self.assertRaises(ValueError):
            review.status(self.config, draft['draft_id'], draft['status_token'])


if __name__ == '__main__':
    unittest.main()
