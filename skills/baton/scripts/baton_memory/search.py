"""Keyword/semantic memory retrieval and rendering."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .ledger import KINDS, MemoryLedger


RULE_KINDS = {"command", "convention", "pitfall", "precedent"}
INSTRUCT = "Instruct: 根据问题检索相关的项目记忆条目"


def _latest_active(ledger: MemoryLedger) -> Dict[str, Dict[str, Any]]:
    latest = ledger.latest()
    return {key: value for key, value in latest.items()
            if value.get("status", "active") == "active"}


def _tokens(query: str) -> List[str]:
    raw = re.findall(r"[A-Za-z0-9_./:-]+", str(query or "").lower())
    # Chinese n-grams make short Chinese labels useful without depending on a
    # tokenizer; single characters are deliberately ignored.
    chars = [c for c in str(query or "") if "\u3400" <= c <= "\u9fff"]
    for size in range(2, min(4, len(chars)) + 1):
        raw.extend("".join(chars[i:i + size]) for i in range(len(chars) - size + 1))
    return [x for x in raw if x]


def keyword_search(ledger: MemoryLedger, query: str, top_k: int = 30) -> List[Dict[str, Any]]:
    """Search the authoritative ledger by literal identifiers and n-grams."""
    terms = _tokens(query)
    if not terms:
        return []
    out: List[Dict[str, Any]] = []
    for ident, record in _latest_active(ledger).items():
        hay = " ".join([str(record.get("text", "")), str(record.get("source", "")),
                        " ".join(str(x) for x in record.get("anchors", [])),
                        str(record.get("task", ""))]).lower()
        exact = sum(1 for term in terms if term in hay)
        if not exact:
            continue
        score = exact / max(1, len(terms))
        # Identifiers and paths are more reliable than generic prose matches.
        if any(re.search(r"[./_:-]", term) and term in hay for term in terms):
            score += 0.35
        result = dict(record)
        result["ledger_id"] = ident
        result["score"] = score
        result["route"] = "keyword"
        out.append(result)
    out.sort(key=lambda x: (-float(x.get("score", 0)), str(x.get("ledger_id", ""))))
    return out[:top_k]


def _semantic_results(semantic: Any, query: str, kinds: Sequence[str]) -> List[Dict[str, Any]]:
    prompt = "%s\nQuery: %s" % (INSTRUCT, query)
    filters = {"agent_id": "baton", "kind": {"in": list(kinds)}}
    # The top_k spelling is intentional: mem0 v2 silently ignores limit.
    result = semantic.search(prompt, top_k=30, filters=filters)
    if isinstance(result, dict):
        result = result.get("results", result.get("memories", []))
    return list(result or [])


def semantic_search(ledger: MemoryLedger, queries: Sequence[str], semantic: Any,
                    timeout_seconds: float = 10.0) -> List[Dict[str, Any]]:
    """Use a supplied mem0 adapter; callers may pass a fake in tests."""
    latest = _latest_active(ledger)
    out: List[Dict[str, Any]] = []
    seen = set()
    for query in queries:
        for kinds in (sorted(RULE_KINDS), sorted(KINDS - RULE_KINDS)):
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(_semantic_results, semantic, query, kinds)
            try:
                values = future.result(timeout=timeout_seconds)
            except FutureTimeout as exc:
                future.cancel()
                raise TimeoutError("Ollama 嵌入超过 10 秒") from exc
            finally:
                executor.shutdown(wait=False)
            for item in values:
                metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
                ident = metadata.get("ledger_id") or item.get("ledger_id") if isinstance(item, dict) else None
                if ident not in latest or ident in seen:
                    continue
                seen.add(ident)
                value = dict(latest[ident])
                value["ledger_id"] = ident
                value["score"] = float(item.get("score", item.get("distance", 0))) if isinstance(item, dict) else 0.0
                value["route"] = "semantic"
                out.append(value)
    return out


def fuse_results(ledger: MemoryLedger, keyword: Iterable[Dict[str, Any]],
                 semantic: Iterable[Dict[str, Any]], top_k: int = 10,
                 core_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Merge routes, cap event/map, and omit entries already in the core block."""
    core = set(core_ids or ())
    merged: Dict[str, Dict[str, Any]] = {}
    for route, values in (("keyword", keyword), ("semantic", semantic)):
        for item in values:
            ident = item.get("ledger_id") or item.get("id")
            if not ident or ident in core:
                continue
            if ident not in merged or float(item.get("score", 0)) > float(merged[ident].get("score", 0)):
                value = dict(item)
                value["route"] = route if ident not in merged else "both"
                merged[ident] = value
    values = sorted(merged.values(), key=lambda x: (-float(x.get("score", 0)), str(x.get("ledger_id", ""))))
    selected: List[Dict[str, Any]] = []
    counts = {"event": 0, "map": 0}
    for item in values:
        kind = item.get("kind")
        if kind in counts and counts[kind] >= 2:
            continue
        selected.append(item)
        if kind in counts:
            counts[kind] += 1
        if len(selected) >= top_k:
            break
    return selected


def _anchor_status(ledger: MemoryLedger, record: Dict[str, Any]) -> str:
    for anchor in record.get("anchors", [])[:3]:
        raw = str(anchor)
        if "#" in raw:
            path, _, identifier = raw.partition("#")
        elif ":" in raw and not re.match(r"^[A-Za-z]:[\\/]", raw):
            path, _, identifier = raw.partition(":")
        else:
            path, identifier = raw, ""
        full = path if os.path.isabs(path) else os.path.join(ledger.project_root, path)
        if not os.path.exists(full):
            return "文件已不存在"
        if identifier and identifier not in _read_limited(full):
            return "相关代码已改动，使用前核对"
    return ""


def _read_limited(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read(2_000_000)
    except OSError:
        return ""


def render_memory_context(ledger: MemoryLedger, results: Sequence[Dict[str, Any]],
                          withdrawals: Optional[Sequence[str]] = None) -> str:
    lines = ["## 相关记忆（检索结果，供参考；与章程或本轮指令冲突时以后者为准）"]
    for old in withdrawals or []:
        lines.append(str(old) if str(old).startswith("撤回：") else
                     "撤回：%s 已失效，忽略此前轮次推送的 %s" % (old, old))
    for item in results:
        ident = item.get("ledger_id", item.get("id", "?"))
        status = _anchor_status(ledger, item)
        marker = status or ("已核实" if item.get("verified") else
                            ("executor 自述，未核实" if item.get("verification") == "claimed"
                             or item.get("verification_status") == "claimed"
                             or (item.get("source_actor") == "executor" and item.get("verified") is False) else ""))
        fields = [str(ident), str(item.get("kind", "")), str(item.get("source", ""))]
        if marker:
            fields.append(marker)
        lines.append("- [%s] %s" % (" · ".join(fields), str(item.get("text", ""))))
    return "\n".join(lines)


def render_pinned_block(ledger: MemoryLedger, budget: int = 2200) -> Tuple[str, bool, List[str]]:
    latest = _latest_active(ledger)
    pinned = [x for x in latest.values() if x.get("pinned")]
    pinned.sort(key=lambda x: int(x.get("pin_seq", x.get("seq", 0))))
    lines = ["## 项目常驻记忆"]
    included: List[str] = []
    truncated = False
    for item in pinned:
        line = "- [%s · %s · %s] %s" % (item.get("id"), item.get("kind"), item.get("source", ""), item.get("text", ""))
        candidate = "\n".join(lines + [line])
        if len(candidate) > budget:
            truncated = True
            break
        lines.append(line)
        included.append(str(item.get("id")))
    return "\n".join(lines), truncated, included


def retrieve_memory(ledger: MemoryLedger, queries: Sequence[str], semantic: Any = None,
                    top_k: int = 10, core_ids: Optional[Iterable[str]] = None,
                    withdrawals: Optional[Sequence[str]] = None, core_max_chars: int = 2200) -> Dict[str, Any]:
    """Retrieve using both routes; semantic failures remain keyword-only."""
    keyword: List[Dict[str, Any]] = []
    for query in queries:
        keyword.extend(keyword_search(ledger, query, top_k=30))
    semantic_values: List[Dict[str, Any]] = []
    reason = ""
    if semantic is not None:
        try:
            semantic_values = semantic_search(ledger, queries, semantic)
        except Exception as exc:  # only availability/model/timeout failures may fall back
            from .mem0_adapter import is_expected_fallback_error
            if not is_expected_fallback_error(exc):
                raise
            reason = str(exc)
    results = fuse_results(ledger, keyword, semantic_values, top_k=top_k, core_ids=core_ids)
    context = render_memory_context(ledger, results, withdrawals=withdrawals)
    pinned, truncated, core = render_pinned_block(ledger, budget=core_max_chars)
    return {"results": results, "context": context, "pinned": pinned, "core_ids": core,
            "core_truncated": truncated, "fallback_reason": reason,
            "withdrawals": list(withdrawals or [])}


__all__ = ["keyword_search", "semantic_search", "fuse_results", "render_memory_context", "render_pinned_block", "retrieve_memory"]

