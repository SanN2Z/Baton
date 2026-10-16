"""Venv-side computation for Baton轮首 memory prompt injection."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

from .ledger import MemoryLedger
from .search import render_pinned_block, retrieve_memory


def _withdrawals(ledger: MemoryLedger, previous_ids: List[str]) -> List[str]:
    latest = ledger.latest()
    result = []
    for ident in dict.fromkeys(previous_ids):
        item = latest.get(ident) or {}
        status = item.get("status", "active")
        if status == "superseded":
            result.append("撤回：%s 已由 %s 取代，忽略此前轮次推送的 %s" %
                          (ident, item.get("replaced_by", "未知条目"), ident))
        elif status in {"retired", "suspended"}:
            result.append("撤回：%s 已%s，忽略此前轮次推送的 %s" % (ident, status, ident))
    return result


def compute(value: Dict[str, Any]) -> Dict[str, Any]:
    project_root = str(value["project_root"])
    ledger = MemoryLedger(project_root, baton_home=value.get("baton_home") or None)
    if not os.path.exists(ledger.ledger_path):
        return {"ok": True, "pushed": [], "core": "", "context": "", "withdrawals": []}
    good, error = ledger.verify(repair=True)
    if not good:
        return {"ok": False, "error": "记忆台账校验失败：%s" % error}
    queries = [str(x) for x in value.get("queries", []) if str(x).strip()]
    memory_cfg = value.get("memory") or {}
    top_k = int(memory_cfg.get("inject_top_k", 10))
    core_max_chars = int(memory_cfg.get("core_max_chars", 2200))
    previous_ids = [str(x) for x in value.get("previous_ids", [])]
    withdrawals = _withdrawals(ledger, previous_ids)
    _, _, core_ids = render_pinned_block(ledger, budget=core_max_chars)
    semantic = None
    try:
        from .mem0_adapter import Mem0Index
        index = Mem0Index(ledger)

        class Semantic:
            def search(self, query, top_k=30, filters=None):
                return index.search(query, top_k=top_k, filters=filters)

        semantic = Semantic()
    except Exception:
        semantic = None
    result = retrieve_memory(ledger, queries, semantic=semantic, top_k=top_k,
                             core_ids=core_ids, withdrawals=withdrawals,
                             core_max_chars=core_max_chars)
    return {"ok": True, "pushed": [str(x.get("ledger_id")) for x in result.get("results", [])],
            "core": result.get("pinned", ""), "context": result.get("context", ""),
            "withdrawals": withdrawals, "core_truncated": bool(result.get("core_truncated")),
            "fallback_reason": result.get("fallback_reason", "")}


def main() -> None:
    try:
        value = json.loads(sys.stdin.read() or "{}")
        result = compute(value)
    except Exception as exc:  # parent converts this to keyword fallback
        result = {"ok": False, "error": str(exc)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

