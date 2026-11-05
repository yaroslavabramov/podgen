"""Offline utility boundaries: state is data, extra commands cannot be supplied, keys stay out."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from preview_runtime import CONTRACT, PreviewState, offline_environment
import preview_stage


class PreviewToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('shared.json', 'profile.json'):
            (self.root / name).write_text('{}')
        self.payload = dict(contract=CONTRACT, project=str(uuid4()), actor=str(uuid4()),
                            run_id=str(uuid4()), case='gps-clock', objects=str(self.root / 'objects'),
                            shared_ref_file=str(self.root / 'shared.json'),
                            profile_ref_file=str(self.root / 'profile.json'), previews={}, endpoint_called=False)
        self.state = self.root / 'state.json'

    def write_state(self):
        self.state.write_text(json.dumps(self.payload))

    def test_command_is_built_from_data_and_current_interpreter(self):
        state = PreviewState.from_payload(self.payload)
        command = state.preview_command()
        self.assertEqual(command[:3], [sys.executable, '-m', 'podgen.writing_cli'])
        self.assertEqual(command[9], 'preview')
        self.assertNotIn('run', command)
        self.assertNotIn('--env-file', command)

    def test_executable_arrays_and_invalid_state_are_rejected(self):
        for bad in (dict(cli=['arbitrary-command']), dict(preview=['run']),
                    dict(project='invalid'), dict(objects='relative'),
                    dict(shared_ref_file=str(self.root / 'missing.json'))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PreviewState.from_payload({**self.payload, **bad})

    def test_preview_subprocess_has_no_provider_credentials(self):
        self.write_state()
        argv = ['preview_stage.py', str(self.state), '--stage', 'plan', '--out', str(self.root / 'result.json')]
        with patch.dict(os.environ, {'PODGEN_DATABASE_URL': 'local-test-database',
                                    'OPENAI_API_KEY': 'test-only', 'AZURE_OPENAI_API_KEY': 'test-only'}, clear=True), \
             patch.object(sys, 'argv', argv), patch('preview_stage.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(preview_stage.main(), 0)
            command = run.call_args.args[0]
            environment = run.call_args.kwargs['env']
            self.assertEqual(command[9], 'preview')
            self.assertEqual(environment['PODGEN_DATABASE_URL'], 'local-test-database')
            self.assertNotIn('OPENAI_API_KEY', environment)
            self.assertNotIn('AZURE_OPENAI_API_KEY', environment)
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'test-only')

    def test_extra_cli_options_cannot_load_an_environment_or_run(self):
        self.write_state()
        for extra in (['--env-file', 'credentials.env'], ['run'], ['--stage', 'run']):
            argv = ['preview_stage.py', str(self.state), '--stage', 'plan', '--out', 'result.json', *extra]
            with self.subTest(extra=extra), patch.object(sys, 'argv', argv), \
                 patch('preview_stage.subprocess.run') as run, contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                preview_stage.main()
            run.assert_not_called()

    def test_database_must_be_explicit(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            offline_environment()


if __name__ == '__main__':
    unittest.main()
