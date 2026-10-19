"""Offline macOS regressions: real process locks, discovery, paths and launchers."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qa.test_coordinator import A, C
import backends
from desktop import SingleInstance

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer', REPO / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS integration tests')
class MacTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='群聊 mac test ')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finder_discovers_clis_with_minimal_path(self):
        program = self.root / 'claude'
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o755)
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}), \
             patch.object(backends, 'configured', return_value=None), \
             patch.object(backends, 'desktop_path', return_value='/usr/bin:/bin'), \
             patch.object(backends, 'SEARCH', {'claude': {'names': ('claude',), 'globs': [str(program)]}}):
            self.assertEqual(backends.find('claude', fresh=True), str(program))
            program.chmod(0o644)
            self.assertIsNone(backends.find('claude', fresh=True))

    def test_configured_path_expands_home(self):
        program = self.root / 'claude'
        program.write_text('#!/bin/sh\nexit 0\n')
        program.chmod(0o755)
        with patch.dict(os.environ, {'HOME': str(self.root), 'GROUPCHAT_CLAUDE_PATH': '~/claude'}):
            self.assertEqual(backends.find('claude', fresh=True), str(program))

    def test_desktop_environment_includes_node_and_local_tools(self):
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            path = A.child_env()['PATH'].split(os.pathsep)
        self.assertIn('/opt/homebrew/bin', path)
        self.assertIn('/usr/local/bin', path)
        self.assertIn(str(REPO / '.tools/node_modules/.bin'), path)

    def test_file_uri_spaces_unicode_and_line_suffix(self):
        target = self.root / '说明 file.md'
        target.touch()
        room = {'root': str(self.root), 'dir': str(self.root)}
        for value in (str(target), target.as_uri(), target.as_uri()+'#L12', str(target)+':12:3', target.name):
            self.assertEqual(A.resolve_path(value, room), str(target), value)

    def test_open_documents_and_reveal_programs(self):
        folder = self.root / 'Test.app'
        folder.mkdir()
        script = self.root / 'run.command'
        script.touch()
        pdf = self.root / 'report.pdf'
        pdf.touch()
        with patch.object(A.subprocess, 'Popen') as popen:
            for path in (folder, script):
                A.open_path(str(path))
                self.assertEqual(popen.call_args.args[0], ['/usr/bin/open', '-R', str(path)])
            A.open_path(str(pdf))
            self.assertEqual(popen.call_args.args[0], ['/usr/bin/open', str(pdf)])

    def test_scripts_open_in_editor(self):
        script = self.root / 'script.sh'
        script.touch()
        script.chmod(0o755)
        with patch('shutil.which', return_value='/usr/local/bin/code'), patch.object(A.subprocess, 'Popen') as popen:
            A.open_path(str(script))
            self.assertEqual(popen.call_args.args[0], ['/usr/local/bin/code', '--', str(script)])

    def test_single_instance_and_release_after_crash(self):
        first = SingleInstance(self.root)
        self.assertTrue(first.acquire())
        second = SingleInstance(self.root)
        self.assertFalse(second.acquire())
        self.assertTrue(first.request.exists())
        first.close()
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'from desktop import SingleInstance; import sys,time; '
            'x=SingleInstance(sys.argv[1]); print(x.acquire(), flush=True); time.sleep(30)', str(self.root)],
            cwd=str(REPO/'app2'), stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'True\n')
            self.assertFalse(second.acquire())
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertTrue(second.acquire())
        second.close()

    def test_concurrent_file_lock_prevents_lost_updates(self):
        counter = self.root / 'counter'
        counter.write_text('0')
        code = ('import sys; from common import Lock; from pathlib import Path; p=Path(sys.argv[1]); '
                '\nfor _ in range(30):\n with Lock(str(p)):\n  p.write_text(str(int(p.read_text())+1))\n')
        children = [subprocess.Popen([sys.executable, '-c', code, str(counter)], cwd=str(REPO)) for _ in range(3)]
        for child in children:
            self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(counter.read_text(), '90')

    def test_subprocess_flags_and_pid(self):
        result = subprocess.run([sys.executable, '-c', 'print(42)'], capture_output=True,
                                creationflags=A.NO_WINDOW, check=True, text=True)
        self.assertEqual(result.stdout.strip(), '42')
        self.assertTrue(C.pid_alive(os.getpid()))
        self.assertFalse(C.pid_alive(-1))

    def test_bundle_paths_are_shell_quoted(self):
        repo = self.root / "a project's source"
        repo.mkdir()
        (repo / 'app2').mkdir()
        script = repo / 'app2/app.py'
        result = self.root / 'launched.txt'
        script.write_text('from pathlib import Path; Path('+repr(str(result))+').write_text("ok")')
        with patch.object(installer, 'HERE', str(repo)):
            app = installer.mac_shortcut(destination=self.root/'群聊.app', python=sys.executable)
        info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
        self.assertEqual(info['CFBundleExecutable'], 'GroupChat')
        env = dict(os.environ, HOME=str(self.root))
        subprocess.run([str(app/'Contents/MacOS/GroupChat')], env=env, check=True)
        self.assertEqual(result.read_text(), 'ok')
        self.assertFalse((repo / '.claude').exists())


if __name__ == '__main__':
    unittest.main()
