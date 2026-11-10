# SPDX-License-Identifier: MIT
import ast
import subprocess
import sys
import unittest
from pathlib import Path

from tests import helpers


class StaticRules(unittest.TestCase):
    def test_every_module_imports_without_sdks(self):
        script = '''
import importlib, pkgutil, sys
class Deny:
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in {'wandb', 'weave', 'cwsandbox', 'coreweave', 'cwevals', 'yaml', 'opentelemetry', 'wandb_workspaces'}:
   raise RuntimeError('SDK at module import: ' + fullname)
sys.meta_path.insert(0, Deny())
import mhforge
from pathlib import Path
sys.path.insert(0, str(Path(mhforge.__file__).parent / 'insandbox'))
for m in pkgutil.walk_packages(mhforge.__path__, mhforge.__name__ + '.'):
 importlib.import_module(m.name)
'''
        result = subprocess.run([sys.executable, '-c', script], env=helpers.child_env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_children_have_explicit_environments(self):
        for p in (helpers.SC / 'mhforge').rglob('*.py'):
            if 'insandbox' in p.parts:
                continue
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if node.func.value.id == 'subprocess' and node.func.attr in ('Popen', 'run', 'check_output', 'check_call'):
                        self.assertIn('env', [k.arg for k in node.keywords], f'{p.name}:{node.lineno}')

    def test_no_ambient_environment_copy_or_login(self):
        for p in (helpers.SC / 'mhforge').rglob('*.py'):
            text = p.read_text()
            self.assertNotIn('os.environ.copy()', text, p.name)
            self.assertNotIn('wandb.login(', text, p.name)

    def test_worker_lock_matches_source_and_draft(self):
        import json, hashlib
        root = helpers.SC / 'worker-image'
        self.assertEqual((root / 'uv.lock').read_bytes(), (helpers.example_dir() / 'uv.lock').read_bytes())
        m = json.loads((helpers.SC / 'manifest.draft.json').read_text())
        self.assertEqual(hashlib.sha256((root / 'requirements.lock.txt').read_bytes()).hexdigest(),
                         m['sandbox']['wheelhouse']['requirements_sha256'])
