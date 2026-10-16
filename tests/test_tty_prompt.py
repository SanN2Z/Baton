import os
import pty
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "baton" / "scripts"))

import baton  # noqa: E402


class TerminalPromptTests(unittest.TestCase):
    """The tty confirmation must work on a real (pseudo) terminal, which is not seekable."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.path = os.ttyname(self.slave)

    def read_master(self):
        return os.read(self.master, 4096).decode("utf-8", "replace")

    def test_prompt_opens_a_non_seekable_terminal_and_round_trips(self):
        prompt = baton._TerminalPrompt(self.path)
        try:
            prompt.write("确认？")
            prompt.flush()
            self.assertIn("确认？", self.read_master())
            os.write(self.master, "demo-task\n".encode("utf-8"))
            self.assertEqual(prompt.readline().strip(), "demo-task")
        finally:
            prompt.close()

    def test_task_confirmation_accepts_exact_name_and_rejects_others(self):
        os.write(self.master, b"demo-task\n")
        with patch.object(baton, "_open_tty", side_effect=lambda: baton._TerminalPrompt(self.path)):
            baton._require_task_confirmation("demo-task")
        self.assertIn("demo-task", self.read_master())

        os.write(self.master, b"other\n")
        with patch.object(baton, "_open_tty", side_effect=lambda: baton._TerminalPrompt(self.path)), \
                self.assertRaisesRegex(baton.ConfigError, "任务名确认不匹配"):
            baton._require_task_confirmation("demo-task")


if __name__ == "__main__":
    unittest.main()
