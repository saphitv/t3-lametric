import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ServiceTests(unittest.TestCase):
    def test_generated_service_uses_its_virtual_environment_without_enabling_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text(f'[bridge]\nstate_file="{root / "state.json"}"\n')
            output = root / "local.t3-lametric.plist"
            command = [
                sys.executable,
                "-m",
                "t3_lametric",
                "--config",
                str(config),
                "service-file",
                "--output",
                str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn("has not been enabled", result.stdout)
            with output.open("rb") as handle:
                service = plistlib.load(handle)
            self.assertEqual(service["ProgramArguments"][0], str(Path(sys.executable).absolute()))
            self.assertEqual(service["ProgramArguments"][-1], "run")
            self.assertFalse((root / "state.json").exists())
            original = output.read_bytes()
            repeated = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
