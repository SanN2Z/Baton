import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))

from baton_memory.ledger import MemoryLedger


class MemoryDiffPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".baton" / "runs" / "task").mkdir(parents=True)
        (self.root / "src.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
        (self.root / ".baton" / "tasks" / "task").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "task" / "brief.md").write_text("目标\n", encoding="utf-8")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"BATON_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ledger = MemoryLedger(str(self.root))

    def entry(self):
        return {"kind": "command", "text": "pytest -q tests", "source": "R001",
                "anchors": ["src.py#keep"], "task": "task", "leg": 1, "end_snapshot": "abc"}

    def test_venv_query_uses_previous_leg_paths_and_limits_to_eight(self):
        import baton
        fake_python = self.home / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True, exist_ok=True)
        fake_python.write_text("", encoding="utf-8")
        state = {"snapshot_store": "shadow", "legs": [{"diff": [3, 10, 2], "start_ref": "start", "end_ref": "end"}]}
        paths = [("100644", "100644", "M", "changed-%02d.py" % n) for n in range(10)]
        result = subprocess.CompletedProcess([], 0, stdout='{"ok":true,"context":"ctx"}\n', stderr="")
        project = baton.Project(str(self.root))
        with patch.object(baton, "diff_tree_entries", return_value=paths) as diff, \
                patch.object(baton.subprocess, "run", return_value=result) as run:
            text, _ = baton.memory_prompt_for_leg(project, "task", "指令", st=state, cfg=project.config)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertIn("ctx", text)
        self.assertEqual(payload["queries"][-8:], ["changed-%02d.py" % n for n in range(8)])
        self.assertEqual(diff.call_args.args[1:], ("start", "end"))

    def test_keyword_fallback_uses_previous_leg_paths(self):
        import baton
        from baton_memory import search
        self.ledger.append([self.entry()])
        state = {"snapshot_store": "shadow", "legs": [{"diff": [3, 10, 2], "start_ref": "start", "end_ref": "end"}]}
        queries = []

        def capture_query(ledger, query, top_k=30):
            queries.append(query)
            return []

        project = baton.Project(str(self.root))
        with patch.object(baton, "diff_tree_entries", return_value=[
                ("100644", "100644", "M", "changed.py")]), \
                patch.object(search, "keyword_search", side_effect=capture_query):
            text, meta = baton.memory_prompt_for_leg(project, "task", "指令", st=state, cfg=project.config)
        self.assertIn("指令", text)
        self.assertEqual(meta["error"], "")
        self.assertIn("changed.py", queries)

    def test_missing_rollback_refs_return_no_memory_paths(self):
        import baton
        state = {"legs": [{"diff": [3, 10, 2]}]}
        project = baton.Project(str(self.root))
        with patch.object(baton, "diff_tree_entries") as diff:
            self.assertEqual(baton._memory_diff_paths(project, "task", state), [])
        diff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
