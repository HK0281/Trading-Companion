"""Exercise start.cmd with real Windows argument parsing and an isolated Python entry."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]


class StartupDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="启动 defaults & ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copyfile(ROOT / "start.cmd", self.root / "start.cmd")
        venv.EnvBuilder(with_pip=False).create(self.root / ".venv")
        assets = self.root / "src/trading_companion/web_assets"
        assets.mkdir(parents=True)
        (assets / "index.html").write_text("fixture", encoding="utf-8")
        package = self.root / "trading_companion"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        # Capture the actual Python argv without starting a server or loading user data.
        (package / "api_server.py").write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "Path('argv.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8")

    def launch(self, arguments):
        command = '.\\start.cmd ' + subprocess.list2cmdline(arguments)
        completed = subprocess.run('cmd.exe /d /s /c "' + command + '"', cwd=self.root,
            env={**os.environ, "TRADING_COMPANION_NO_PAUSE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        captured = json.loads((self.root / "argv.json").read_text(encoding="utf-8"))
        if arguments:
            self.assertEqual(captured[-len(arguments):], arguments)
        return captured, completed.stdout.decode(errors="replace")

    def test_normal_defaults_precede_unchanged_user_overrides(self):
        arguments = ["--research-documents", "off", "--personal-memory-db", "custom path.sqlite3"]
        captured, _ = self.launch(arguments)
        defaults = captured[:-len(arguments)]
        self.assertEqual(defaults[-6:], ["--memory-db", "data\\conversations.sqlite3",
            "--research-documents", "events", "--personal-memory-db", "data\\personal_memory.sqlite3"])

    def test_demo_and_synthetic_omit_only_incompatible_implicit_defaults(self):
        for arguments, history in ((["--demo"], False), (["--data", "synthetic"], True),
                                   (["--data=synthetic"], True), (["--dem"], False),
                                   (["--dat", "synthetic"], True), (["--da=synthetic"], True)):
            with self.subTest(arguments=arguments):
                captured, _ = self.launch(arguments)
                defaults = captured[:-len(arguments)]
                self.assertNotIn("--research-documents", defaults)
                self.assertNotIn("--personal-memory-db", defaults)
                self.assertEqual("--memory-db" in defaults, history)
                self.assertIn("--trace-dir", defaults)

    def test_last_data_option_selects_defaults_and_empty_values_do_not_stop_scanning(self):
        for arguments, enabled in ((["--data=tencent", "--data", "synthetic"], False),
                                   (["--data", "synthetic", "--data=tencent"], True),
                                   (["--trace-dir", "", "--demo"], False),
                                   (["--trace-dir", "", "--data=synthetic"], False)):
            with self.subTest(arguments=arguments):
                captured, _ = self.launch(arguments)
                defaults = captured[:-len(arguments)]
                self.assertEqual("--research-documents" in defaults, enabled)
                self.assertEqual("--personal-memory-db" in defaults, enabled)

    def test_offline_user_options_remain_explicit_for_server_validation(self):
        for mode in (["--demo"], ["--data", "synthetic"]):
            for option in (["--research-documents", "events"],
                           ["--personal-memory-db", "explicit.sqlite3"]):
                with self.subTest(mode=mode, option=option):
                    captured, _ = self.launch([*mode, *option])
                    self.assertEqual(captured[-len(option):], option)
                    self.assertEqual(captured.count(option[0]), 1)

    def test_help_explains_defaults_and_independent_disable_options_without_assets(self):
        (self.root / "src/trading_companion/web_assets/index.html").unlink()
        for arguments in (["--help"], ["-h"], ["--demo", "--help"]):
            with self.subTest(arguments=arguments):
                _, output = self.launch(arguments)
                self.assertIn("--research-documents off", output)
                self.assertIn("--no-personal-memory", output)
                self.assertIn("--no-memory", output)
                self.assertIn("data\\personal_memory.sqlite3", output)


if __name__ == "__main__":
    unittest.main()
