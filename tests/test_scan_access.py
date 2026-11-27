import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from engine import bridge


class ScanAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()

    def call(self, command, *, granted=False, environment=None, native=True):
        output = io.StringIO()
        with (patch('sys.stdin', io.StringIO(json.dumps({'cmd': command}))),
              patch.dict('os.environ', environment or {}, clear=True),
              patch.object(bridge.Path, 'home', return_value=self.home),
              patch.object(bridge, 'native_caller', return_value=native),
              patch.object(bridge, 'has_full_disk_access', return_value=granted) as preflight,
              patch.object(bridge, 'dispatch', return_value={'scan_id': 'fixture-scan'}) as dispatch,
              redirect_stdout(output)):
            bridge.main()
        return json.loads(output.getvalue()), preflight, dispatch

    def test_native_scan_commands_stop_before_dispatch_without_system_access(self):
        for command in ('scan_local', 'scan', 'scan_app', 'size_tree', 'settings', 'run'):
            with self.subTest(command=command):
                reply, preflight, dispatch = self.call(command)
                self.assertEqual(reply['code'], 'full_disk_access_required')
                self.assertFalse(reply['ok'])
                preflight.assert_called_once_with()
                dispatch.assert_not_called()
        self.assertFalse((self.home / 'Library').exists())

    def test_success_and_revocation_are_checked_for_each_native_request(self):
        reply, preflight, dispatch = self.call('scan_local', granted=True)
        self.assertEqual(reply, {'ok': True, 'data': {'scan_id': 'fixture-scan'}})
        preflight.assert_called_once_with()
        dispatch.assert_called_once()
        reply, _, dispatch = self.call('scan_local')
        self.assertFalse(reply['ok'])
        dispatch.assert_not_called()

    def test_synthetic_home_and_direct_cli_keep_their_own_execution_context(self):
        config = {'LUMEN_STATE_DIR': str(self.home / 'state'),
                  'LUMEN_INVENTORY_HOME': str(self.home)}
        for environment, native in ((config, True), ({}, False)):
            reply, preflight, dispatch = self.call('scan_local', environment=environment, native=native)
            self.assertTrue(reply['ok'])
            preflight.assert_not_called()
            dispatch.assert_called_once()

    def test_fixture_creation_is_available_without_disk_access(self):
        reply, preflight, dispatch = self.call('fixture')
        self.assertTrue(reply['ok'])
        preflight.assert_not_called()
        dispatch.assert_called_once()
