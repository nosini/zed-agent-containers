"""Wrapper smoke tests without a live OpenCode account or model call."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

WRAPPER = Path(__file__).resolve().parents[1] / 'opencode-delegate.py'


class DelegateTest(unittest.TestCase):
    def load(self, env):
        with patch.dict(os.environ, env, clear=True):
            return runpy.run_path(str(WRAPPER))

    def test_separate_state_volumes(self):
        for env, state in [
            ({}, '/home/node/.claude'),
            ({'CLAUDE_CONFIG_DIR': '/state/claude-b'}, '/state/claude-b'),
            ({'CODEX_HOME': '/state/codex'}, '/state/codex'),
        ]:
            with self.subTest(env=env):
                wrapper = self.load(env)
                self.assertEqual(wrapper['opencode_env']()['XDG_DATA_HOME'],
                                 state + '/opencode-data')

    def test_worker_environment(self):
        wrapper = self.load({'CODEX_HOME': '/state/codex'})
        with patch.dict(os.environ, {'CLAUDECODE': '1', 'CLAUDE_CODE_ENTRYPOINT': 'cli',
                                     'PATH': '/bin'}, clear=True):
            env = wrapper['opencode_env']()
        self.assertNotIn('CLAUDECODE', env)
        self.assertNotIn('CLAUDE_CODE_ENTRYPOINT', env)
        self.assertEqual(env['PATH'], '/bin')
        self.assertEqual(env['OPENCODE_DISABLE_AUTOUPDATE'], '1')

    def test_login_and_status_use_codex_state(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.load({'CODEX_HOME': directory})
            for action, expected in [('login', ['auth', 'login', 'opencode-go']),
                                     ('status', ['auth', 'list'])]:
                # exec never returns; SystemExit models that without replacing the test process.
                with patch('sys.argv', ['opencode-delegate', action]), \
                     patch('os.execvpe', side_effect=SystemExit) as execute:
                    with self.assertRaises(SystemExit):
                        wrapper['main']()
                program, argv, env = execute.call_args.args
                self.assertEqual(program, 'opencode')
                self.assertEqual(argv, ['opencode', *expected, '--standalone'])
                self.assertEqual(env['XDG_DATA_HOME'], directory + '/opencode-data')

    def test_run_and_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = self.load({'CODEX_HOME': directory})
            spec = Path(directory) / 'task.md'
            spec.write_text('Implement the requested change.')
            for session in [None, 'ses_followup']:
                calls = []

                def run(cmd, **kwargs):
                    if cmd[0] == 'opencode':
                        calls.append((cmd, kwargs['env']))
                        kwargs['stdout'].write(json.dumps({
                            'sessionID': 'ses_result',
                            'part': {'type': 'text', 'text': 'Done; checks passed.'}
                        }) + '\n')
                    return subprocess.CompletedProcess(cmd, 0, stdout='')

                argv = ['opencode-delegate', str(spec)]
                if session:
                    argv += ['-s', session]
                output = io.StringIO()
                with patch('sys.argv', argv), patch('subprocess.run', side_effect=run), \
                     patch('os.path.expanduser', return_value=directory), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(wrapper['main'](), 0)
                cmd, env = calls[0]
                self.assertIn('opencode-go/deepseek-v4.1-flash', cmd)
                self.assertEqual('--session' in cmd, bool(session))
                if session:
                    self.assertEqual(cmd[cmd.index('--session') + 1], session)
                self.assertEqual(env['XDG_DATA_HOME'], directory + '/opencode-data')
                self.assertIn('session: ses_result', output.getvalue())
                self.assertIn('Done; checks passed.', output.getvalue())


if __name__ == '__main__':
    unittest.main()
