"""Opt-in integration tests for the installed mem0/Ollama/MCP stack.

Run with ``BATON_MEMORY_LIVE=1 $BATON_HOME/venv/bin/python -m unittest
tests.test_v2_memory_live``.  The default test suite skips this module so
ordinary unit tests never download packages or touch the user's model store.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))

from baton_memory.install import find_python, install_memory
from baton_memory.ledger import MemoryLedger, memory_append_approved
from baton_memory.mem0_adapter import MODEL_NAME, Mem0Index, mem0_config, ollama_tags
from baton_memory.search import retrieve_memory


@unittest.skipUnless(os.environ.get("BATON_MEMORY_LIVE") == "1", "set BATON_MEMORY_LIVE=1 for real mem0/Ollama integration")
class LiveMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info < (3, 10):
            raise unittest.SkipTest("real mem0 2.x requires Python 3.10+; run BATON_MEMORY_LIVE=1 $BATON_HOME/venv/bin/python -m unittest tests.test_v2_memory_live")
        cls.tmp = tempfile.TemporaryDirectory(prefix="baton-memory-live-")
        cls.home = Path(cls.tmp.name) / "baton-home"
        cls.project = Path(cls.tmp.name) / "project"
        (cls.project / ".baton" / "runs" / "live").mkdir(parents=True)
        (cls.project / "src" / "data").mkdir(parents=True)
        (cls.project / "src" / "data" / "loader.py").write_text("def load_records():\n    pass\n", encoding="utf-8")
        cls.old_env = dict(os.environ)
        os.environ["BATON_HOME"] = str(cls.home)
        os.environ["BATON_TASK"] = "live"
        os.environ["BATON_LEG"] = "1"
        os.environ["BATON_LEG_STARTED"] = str(time.time())
        if not shutil.which("ollama"):
            raise unittest.SkipTest("ollama CLI unavailable")
        try:
            cls.result = install_memory(baton_home=str(cls.home), python=find_python(), scripts_dir=str(ROOT / "skills" / "baton" / "scripts"))
            cls.python = cls.result["python"]
            subprocess.run([cls.python, "-c", "import mem0, mcp, ollama"], check=True, timeout=30)
            site = subprocess.check_output([cls.python, "-c", "import site; print(site.getsitepackages()[0])"], text=True).strip()
            if site not in sys.path:
                sys.path.insert(0, site)
            if MODEL_NAME not in ollama_tags(timeout=2):
                raise unittest.SkipTest("baton-qwen3-embed:latest unavailable in Ollama")
        except unittest.SkipTest:
            cls.tmp.cleanup()
            raise
        except Exception as exc:
            cls.tmp.cleanup()
            raise unittest.SkipTest("real memory dependencies unavailable: %s" % exc)
        cls.ledger = MemoryLedger(str(cls.project))
        entries = [
            {"kind": "map", "text": "数据加载代码放在 src/data/，入口是 load_records", "source": "R-LIVE",
             "anchors": ["src/data/loader.py#load_records"]},
            {"kind": "command", "text": "集成测试使用 tests/integration，命令 pytest tests/integration", "source": "R-LIVE",
             "anchors": ["tests/integration"]},
            {"kind": "convention", "text": "配置读取集中在 src/config.py，避免散落环境变量", "source": "R-LIVE",
             "anchors": ["src/data/loader.py"]},
            {"kind": "pitfall", "text": "Qdrant collection 维度必须是 1024", "source": "R-LIVE",
             "anchors": ["src/data/loader.py"]},
        ]
        memory_append_approved(cls.ledger, entries, source="R-LIVE")

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls.old_env)
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def test_install_and_real_semantic_retrieval(self):
        result = self.result
        self.assertTrue(Path(result["venv"]).is_dir())
        self.assertEqual(MODEL_NAME, "baton-qwen3-embed:latest")
        class Semantic:
            def search(inner, query, top_k=30, filters=None):
                return Mem0Index(self.ledger).search(query, top_k=top_k, filters=filters)
        value = retrieve_memory(self.ledger, ["数据读取应该写在哪里"], semantic=Semantic(), top_k=10)
        self.assertFalse(value["fallback_reason"])
        self.assertTrue(any("src/data" in item["text"] for item in value["results"]))
        print("LIVE_SEMANTIC_RESULTS=" + json.dumps(value["results"], ensure_ascii=False, sort_keys=True))

    def test_approved_entry_is_recalled_semantically(self):
        written = memory_append_approved(self.ledger, [{
            "kind": "map",
            "text": "发布前的回归门槛记录在 docs/release-checklist.md",
            "source": "R-LIVE-APPROVED",
            "anchors": ["docs/release-checklist.md"],
        }], source="R-LIVE-APPROVED")
        self.assertEqual(len(written), 1)
        class Semantic:
            def search(inner, query, top_k=30, filters=None):
                return Mem0Index(self.ledger).search(query, top_k=top_k, filters=filters)
        value = retrieve_memory(self.ledger, ["上线前回归检查清单在哪里"], semantic=Semantic(), top_k=10)
        self.assertFalse(value["fallback_reason"])
        self.assertTrue(any("release-checklist" in item["text"] for item in value["results"]))

    def test_keyword_route_and_offline_fallback_reason(self):
        value = retrieve_memory(self.ledger, ["tests/integration 集成测试"])
        self.assertTrue(any("tests/integration" in item["text"] for item in value["results"]))
        old = os.environ.get("BATON_OLLAMA_URL")
        os.environ["BATON_OLLAMA_URL"] = "http://127.0.0.1:9"
        try:
            class Semantic:
                def search(inner, query, top_k=30, filters=None):
                    return Mem0Index(self.ledger).search(query, top_k=top_k, filters=filters)
            fallback = retrieve_memory(self.ledger, ["tests/integration"], semantic=Semantic())
            self.assertTrue(fallback["results"])
            self.assertTrue(fallback["fallback_reason"])
        finally:
            if old is None:
                os.environ.pop("BATON_OLLAMA_URL", None)
            else:
                os.environ["BATON_OLLAMA_URL"] = old

    def test_dimension_error_is_visible_and_reindex(self):
        with self.assertRaises(Exception) as ctx:
            Mem0Index(self.ledger, collection="baton_live_wrong_dim", dims=1).add(self.ledger.latest()["M001"])
        self.assertTrue("dimension" in str(ctx.exception).lower() or "aligned" in str(ctx.exception).lower())
        self.ledger.append_index_pending("M001", "test pending")
        count = Mem0Index(self.ledger).reindex()
        self.assertGreaterEqual(count, 1)
        self.assertFalse(self.ledger.latest()["M001"].get("index_pending", False))

    def test_real_mcp_stdio_tools_and_call(self):
        import baton
        baton_project = baton.Project(str(self.project))
        ready, reason = baton.memory_mcp_preflight(baton_project, "live", 2, baton_project.config)
        self.assertTrue(ready, reason)
        env = dict(os.environ, BATON_HOME=str(self.home), BATON_TASK="live", BATON_LEG="1",
                   BATON_LEG_STARTED=str(time.time()), PYTHONUNBUFFERED="1")
        proc = subprocess.Popen([self.python, "-m", "baton_memory.server"], cwd=str(self.project), env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop_server():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.addCleanup(stop_server)

        def request(value, ident):
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": value[0], "params": value[1]}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            deadline = time.time() + 15
            while time.time() < deadline:
                line = proc.stdout.readline() if proc.stdout is not None else ""
                if not line:
                    continue
                item = json.loads(line)
                if item.get("id") == ident:
                    return item
            self.fail("MCP response timeout")

        init = request(("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "baton-test", "version": "1"}}), 1)
        self.assertIn("result", init)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        tools = request(("tools/list", {}), 2)
        names = {item["name"] for item in tools["result"]["tools"]}
        self.assertEqual(names, {"memory_search", "archive_search"})
        self.assertTrue((self.project / ".baton" / "runs" / "live" / "leg-001.mcp-ready").exists())
        called = request(("tools/call", {"name": "memory_search", "arguments": {"queries": ["数据读取"]}}), 3)
        self.assertIn("result", called)


if __name__ == "__main__":
    unittest.main()

