from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main


class ConfigPathTests(unittest.TestCase):
    def test_source_execution_uses_repository_root_config(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                main.get_config_path(),
                Path(main.__file__).resolve().parent.parent / "Config.json",
            )

    def test_frozen_execution_uses_config_next_to_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "llama-server-switch.exe"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertEqual(
                    main.get_config_path(),
                    executable.resolve().parent / "Config.json",
                )


if __name__ == "__main__":
    unittest.main()

