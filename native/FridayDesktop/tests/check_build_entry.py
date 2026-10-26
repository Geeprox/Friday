"""Pure packaging guards; no compiler, runtime, accounts or device access."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('friday_project_build', REPO / 'native/FridayDesktop/script/build_desktop.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildGuards(unittest.TestCase):
    def test_only_new_artifact_app_is_allowed(self):
        with tempfile.TemporaryDirectory(prefix='friday-build-', dir='/tmp') as value:
            root = Path(value).resolve()
            (root / '.artifacts').mkdir()
            target = root / '.artifacts/Friday.app'
            with patch.object(build, 'REPO', root):
                self.assertEqual(build.output_path(target), target)
                for invalid in (root / 'Friday.app', root / '.artifacts/plain-directory'):
                    with self.assertRaises(ValueError):
                        build.output_path(invalid)
                target.mkdir()
                with self.assertRaises(ValueError):
                    build.output_path(target)
                (root / '.artifacts/link').symlink_to(root / '.artifacts', target_is_directory=True)
                with self.assertRaises(ValueError):
                    build.output_path(root / '.artifacts/link/Other.app')

    def test_packaging_uses_publication_inventory(self):
        records = build.public_sources()
        self.assertIn('homeplane/api.py', records)
        self.assertIn('fixtures/homeplane/archive-simulated.mp4', records)
        self.assertFalse(any(name.startswith(('data/', '.artifacts/')) or 'node_modules' in name for name in records))


if __name__ == '__main__':
    unittest.main(verbosity=2)
