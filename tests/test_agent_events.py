import tempfile
import unittest
from pathlib import Path

from engine.lumen import agent_events


class AgentEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'projects' / 'example'
        self.project.mkdir(parents=True)
        self.roots = [self.project.parent]

    def codex_stop(self, **fields):
        event = dict(hook_event_name='Stop', cwd=str(self.project), session_id='thr_ABC-123',
                     turn_id='turn_001', model='gpt-6', permission_mode='default',
                     stop_hook_active=False, transcript_path='/private/codex.jsonl',
                     last_assistant_message='private response')
        return {**event, **fields}

    def gemini_after_agent(self, **fields):
        event = dict(hook_event_name='AfterAgent', cwd=str(self.project),
                     session_id='90ca692d-780a-462e-9651-e71a43196155',
                     timestamp='2026-11-25T03:00:00.123Z',
                     transcript_path='/private/gemini.json', prompt='private prompt',
                     prompt_response='private response', stop_hook_active=False)
        return {**event, **fields}

    def test_codex_stop_records_only_observation_metadata(self):
        event = self.codex_stop(closed=True, temporary=['/private/file'], event_id='fake')
        record = agent_events.parse('Codex', event, self.roots)
        self.assertEqual(len(record['event_id']), 64)
        self.assertEqual(record, dict(event_id=record['event_id'], agent='Codex',
                                      session_id=event['session_id'], root=str(self.project),
                                      stage='turn_finished', hook_event_name='Stop'))
        retry = self.codex_stop(stop_hook_active=True, last_assistant_message='a different response')
        self.assertEqual(agent_events.parse('Codex', retry, self.roots), record)
        later = self.codex_stop(turn_id='turn_002')
        self.assertNotEqual(agent_events.parse('Codex', later, self.roots)['event_id'], record['event_id'])

    def test_codex_session_end_and_interrupt_are_distinct_from_stop(self):
        stop = self.codex_stop()
        common = {key: stop[key] for key in ('session_id', 'cwd', 'transcript_path')}
        end = dict(**common, hook_event_name='SessionEnd', reason='other')
        interrupt = dict(**common, hook_event_name='Interrupt', turn_id=stop['turn_id'],
                         model='gpt-6', permission_mode='default')
        stage_by_event = {'SessionEnd': 'session_ended', 'Interrupt': 'interrupted', 'Stop': 'turn_finished'}
        identifiers = set()
        for event in (end, interrupt, stop, end):
            record = agent_events.parse('Codex', event, self.roots)
            self.assertEqual(record['stage'], stage_by_event[event['hook_event_name']])
            self.assertEqual(record['session_id'], stop['session_id'])
            identifiers.add(record['event_id'])
        self.assertEqual(len(identifiers), 3)

    def test_gemini_uses_timestamp_and_omits_prompt_and_transcript_fields(self):
        event = self.gemini_after_agent(turn_id='unrecognized', closed=True)
        record = agent_events.parse('Gemini CLI', event, self.roots)
        self.assertEqual(record, dict(event_id=record['event_id'], agent='Gemini CLI',
                                      session_id=event['session_id'], root=str(self.project),
                                      stage='turn_finished', hook_event_name='AfterAgent'))
        replay = self.gemini_after_agent(prompt='different prompt', transcript_path='')
        self.assertEqual(agent_events.parse('Gemini CLI', replay, self.roots), record)
        later = self.gemini_after_agent(timestamp='2026-11-25T03:00:00.456Z')
        self.assertNotEqual(agent_events.parse('Gemini CLI', later, self.roots)['event_id'], record['event_id'])
        end_fields = {key: event[key] for key in ('session_id', 'cwd', 'timestamp', 'transcript_path')}
        end = dict(**end_fields, hook_event_name='SessionEnd', reason='clear')
        end_record = agent_events.parse('Gemini CLI', end, self.roots)
        self.assertEqual(end_record['stage'], 'session_ended')
        self.assertNotEqual(end_record['event_id'], record['event_id'])

    def test_rejects_invalid_event_identities(self):
        invalid = (
            ('Unknown', self.codex_stop()),
            ([], self.codex_stop()),
            ('Codex', []),
            ('Codex', self.codex_stop(hook_event_name=[])),
            ('Codex', self.codex_stop(hook_event_name='SubagentStop')),
            ('Codex', self.codex_stop(session_id=' thr_ABC-123')),
            ('Codex', self.codex_stop(session_id='thr_ABC-123\n')),
            ('Codex', self.codex_stop(session_id='thr_ABC/123')),
            ('Codex', self.codex_stop(session_id=True)),
            ('Codex', self.codex_stop(turn_id=None)),
            ('Codex', self.gemini_after_agent()),
            ('Gemini CLI', self.codex_stop()),
            ('Gemini CLI', self.gemini_after_agent(timestamp='2026-02-30T03:00:00.123Z')),
            ('Gemini CLI', self.gemini_after_agent(timestamp='private prompt')),
            ('Gemini CLI', self.gemini_after_agent(timestamp=None)),
        )
        for agent, event in invalid:
            with self.subTest(agent=agent, event=event), self.assertRaises(ValueError):
                agent_events.parse(agent, event, self.roots)

    def test_rejects_out_of_scope_missing_and_noncanonical_working_directories(self):
        outside = self.root / 'projects-other'
        outside.mkdir()
        links = self.project.parent / 'link-out', self.project.parent / 'link-in'
        links[0].symlink_to(outside, target_is_directory=True)
        links[1].symlink_to(self.project, target_is_directory=True)
        file = self.project / 'file'
        file.touch()
        invalid = [str(outside), *map(str, links), str(file), str(self.project / 'missing'),
                   'projects/example', self.project.as_uri(), str(self.project) + '/',
                   f'{self.project}/../example', f'{self.project}/.', f'{self.project}\0']
        for cwd in invalid:
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                agent_events.parse('Codex', self.codex_stop(cwd=cwd), self.roots)
        with self.assertRaises(ValueError):
            agent_events.parse('Codex', self.codex_stop(), [])


if __name__ == '__main__':
    unittest.main()
