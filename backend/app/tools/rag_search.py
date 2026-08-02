from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.state.schemas import RagEvidence, RagResult


SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}
INDEX_SCHEMA_VERSION = 4
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+|[\u4e00-\u9fff]")
FIELD_PATTERNS = {
    "profile_id": re.compile(r"\bprofile_id\s*:\s*`?([A-Za-z0-9_-]+)`?", re.I),
    "invoice_id": re.compile(r"\b(?:invoice|发票)\s*(?:id|no|number|号)?[:：#\s-]*([A-Z]{2,}-?\d{2,}|\bINV[-_]?\d+)", re.I),
    "supplier": re.compile(r"\b(?:vendor|supplier|供应商)[:：\s-]+([A-Za-z0-9\u4e00-\u9fff &.,_-]{2,80})", re.I),
    "total_amount": re.compile(r"\b(?:amount|total|金额)[:：\s-]*(?:USD|CNY|RMB|\$)?\s*([0-9][0-9,]*(?:\.\d+)?)", re.I),
    "currency": re.compile(r"\b(USD|CNY|RMB|EUR|GBP)\b", re.I),
    "po_number": re.compile(r"\b(?:PO|purchase order|采购订单)[:：#\s-]*([A-Z]{1,4}[-_]?\d{2,})", re.I),
}


class RagSkill:
    def __init__(self, knowledge_roots: list[Path] | None = None, index_root: Path | None = None) -> None:
        settings = get_settings()
        self.knowledge_roots = [Path(root) for root in (knowledge_roots or settings.knowledge_roots)]
        root = Path(index_root or settings.storage_root / "rag")
        self.index_root = root
        self.index_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.index_root / "index.json"
        self.txtai_path = self.index_root / "txtai"
        self.index_mode = "sparse" if _vector_disabled() else "hybrid"

    def retrieve(
        self,
        query: str,
        *,
        intent: str = "policy_qa",
        filters: dict[str, Any] | None = None,
        top_k: int = 5,
        include_raw_snippets: bool = True,
    ) -> RagResult:
        docs = self._load_or_build_index()
        if not docs:
            return _not_found()

        query_tokens = _tokens(query)
        scored = self._txtai_scores(query, docs, top_k=max(top_k * 4, 12))
        scored = _apply_business_scoring(
            scored,
            docs,
            query=query,
            query_tokens=query_tokens,
            intent=intent,
            filters=filters or {},
        )
        scored = _dedupe_profiles(scored, docs)
        evidences = _render_evidences(
            scored[: max(1, top_k)],
            docs,
            query_tokens,
            include_raw_snippets,
            channel=f"txtai_{self.index_mode}",
        )
        if not evidences:
            return _not_found()
        return RagResult(
            status="success",
            evidences=evidences,
            answer_context=_render_context(evidences),
            unsupported_fields=[],
            reason="Retrieved local invoice payment knowledge snippets with txtai.",
        )

    def _load_or_build_index(self) -> list[dict[str, Any]]:
        root_signature = [str(root.resolve()) for root in self.knowledge_roots]
        knowledge_signature = _knowledge_signature(self.knowledge_roots)
        txtai_path = self._txtai_path(knowledge_signature)
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            docs = data.get("documents")
            manifest_txtai_path = Path(str(data.get("txtai_path") or self.txtai_path))
            if (
                isinstance(docs, list)
                and data.get("schema_version") == INDEX_SCHEMA_VERSION
                and data.get("knowledge_roots") == root_signature
                and data.get("knowledge_signature") == knowledge_signature
                and data.get("engine") == "txtai"
                and data.get("index_mode") == self.index_mode
                and manifest_txtai_path.exists()
            ):
                self.txtai_path = manifest_txtai_path
                return docs

        docs = self._build_documents()
        self.txtai_path = txtai_path
        if docs:
            embeddings = _new_embeddings(self.index_mode)
            embeddings.index([(index, str(doc.get("text") or ""), None) for index, doc in enumerate(docs)])
            embeddings.save(str(self.txtai_path))
        self.manifest_path.write_text(
            json.dumps(
                {
                    "engine": "txtai",
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "index_mode": self.index_mode,
                    "txtai_path": str(self.txtai_path),
                    "knowledge_roots": root_signature,
                    "knowledge_signature": knowledge_signature,
                    "documents": docs,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return docs

    def _txtai_path(self, knowledge_signature: str) -> Path:
        return self.index_root / f"txtai-{self.index_mode}-{knowledge_signature[:12]}"

    def _build_documents(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for root in self.knowledge_roots:
            if not root.exists():
                continue
            base = root.parent if root.name == "ERP Approval" else root
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                text = _read_text(path)
                if not text.strip():
                    continue
                rel = _safe_relative(path, base)
                current_profile = ""
                for index, chunk in enumerate(_chunks(text), start=1):
                    if chunk.lstrip().startswith("## "):
                        current_profile = ""
                    fields = _extract_fields(chunk)
                    current_profile = str(fields.get("profile_id") or current_profile)
                    if current_profile:
                        fields.setdefault("profile_id", current_profile)
                    docs.append(
                        {
                            "source_path": rel,
                            "locator": f"chunk {index}",
                            "text": chunk,
                            "tokens": _tokens(chunk),
                            "fields": fields,
                        }
                    )
        return docs

    def _txtai_scores(self, query: str, docs: list[dict[str, Any]], *, top_k: int) -> list[tuple[float, int]]:
        embeddings = _new_embeddings(self.index_mode)
        embeddings.load(str(self.txtai_path))
        results = embeddings.search(query, max(1, min(top_k, len(docs))))
        scored: list[tuple[float, int]] = []
        for item in results:
            doc_id, score = _parse_txtai_result(item)
            if doc_id is None or doc_id < 0 or doc_id >= len(docs):
                continue
            scored.append((float(score), doc_id))
        return scored


def _new_embeddings(index_mode: str) -> Any:
    from txtai import Embeddings

    if index_mode == "sparse":
        return Embeddings({"keyword": True})
    return Embeddings({"path": "sentence-transformers/all-MiniLM-L6-v2", "hybrid": True})


def _vector_disabled() -> bool:
    return os.getenv("INVOICE_AGENT_ENABLE_VECTOR", "auto").strip().lower() in {"0", "false", "no", "off"}


def _parse_txtai_result(item: Any) -> tuple[int | None, float]:
    if isinstance(item, dict):
        raw_id = item.get("id")
        raw_score = item.get("score", 0.0)
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        raw_id, raw_score = item[0], item[1]
    else:
        return None, 0.0
    try:
        return int(raw_id), float(raw_score)
    except (TypeError, ValueError):
        return None, 0.0


def _apply_business_scoring(
    scored: list[tuple[float, int]],
    docs: list[dict[str, Any]],
    *,
    query: str,
    query_tokens: list[str],
    intent: str,
    filters: dict[str, Any],
) -> list[tuple[float, int]]:
    seen = {doc_id for _, doc_id in scored}
    for index, doc in enumerate(docs):
        if index not in seen and _lexical_score(query_tokens, Counter(doc.get("tokens") or []), str(doc.get("text") or "")) > 0:
            scored.append((0.0, index))
    adjusted: list[tuple[float, int]] = []
    for base_score, doc_id in scored:
        doc = docs[doc_id]
        text = str(doc.get("text") or "")
        source_path = str(doc.get("source_path") or "")
        score = float(base_score)
        score += _lexical_score(query_tokens, Counter(doc.get("tokens") or []), text)
        score += _filter_bonus(filters, text, doc)
        profile_id = str((doc.get("fields") or {}).get("profile_id") or "")
        if profile_id and profile_id.lower() in query.lower():
            score += 5.0
        if intent == "policy_qa" and "policy" in source_path.lower():
            score += 0.8
        if "invoice" in query.lower() and "invoice" in text.lower():
            score += 0.5
        if score > 0:
            adjusted.append((score, doc_id))
    adjusted.sort(key=lambda item: item[0], reverse=True)
    return adjusted


def _render_evidences(
    scored: list[tuple[float, int]],
    docs: list[dict[str, Any]],
    query_tokens: list[str],
    include_raw_snippets: bool,
    channel: str,
) -> list[RagEvidence]:
    evidences: list[RagEvidence] = []
    for score, doc_id in scored:
        doc = docs[doc_id]
        source_path = str(doc.get("source_path") or "")
        locator = str(doc.get("locator") or "")
        snippet = _best_snippet(str(doc.get("text") or ""), query_tokens)
        profile_id = str((doc.get("fields") or {}).get("profile_id") or "")
        if profile_id and profile_id not in snippet:
            snippet = f"profile_id: `{profile_id}`\n{snippet}"
        fields = _extract_fields(snippet) or dict(doc.get("fields") or {})
        if profile_id:
            fields["profile_id"] = profile_id
        evidences.append(
            RagEvidence(
                source_id=_stable_id(source_path, locator),
                source_path=source_path,
                source_type=_source_type(source_path),
                locator=locator,
                snippet=snippet if include_raw_snippets else snippet[:280],
                fields=fields,
                score=round(float(score), 4),
                channel=channel,
            )
        )
    return evidences


def _dedupe_profiles(scored: list[tuple[float, int]], docs: list[dict[str, Any]]) -> list[tuple[float, int]]:
    result: list[tuple[float, int]] = []
    seen: set[str] = set()
    for row in scored:
        profile_id = str((docs[row[1]].get("fields") or {}).get("profile_id") or "")
        if profile_id and profile_id in seen:
            continue
        result.append(row)
        if profile_id:
            seen.add(profile_id)
    return result


def _not_found() -> RagResult:
    return RagResult(
        status="not_found",
        evidences=[],
        answer_context="",
        unsupported_fields=["query"],
        reason="No local knowledge snippets matched the query.",
    )


def _safe_relative(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _knowledge_signature(roots: list[Path]) -> str:
    rows: list[str] = []
    for root in roots:
        if not root.exists():
            rows.append(f"missing:{root.resolve()}")
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _chunks(text: str, max_chars: int = 1400) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and block.lstrip().startswith("## "):
            chunks.append(current)
            current = ""
        for start in range(0, len(block), max_chars):
            piece = block[start : start + max_chars]
            if current and len(current) + len(piece) + 2 > max_chars:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def _tokens(text: str) -> list[str]:
    return [item.lower() for item in TOKEN_RE.findall(str(text or ""))]


def _lexical_score(query_tokens: list[str], doc_counts: Counter[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    score = 0.0
    lowered = text.lower()
    for token in query_tokens:
        if len(token) <= 1 and not ("\u4e00" <= token <= "\u9fff"):
            continue
        score += min(doc_counts.get(token, 0), 4) * 1.0
        if token in lowered:
            score += 0.2
    return score / max(1.0, len(doc_counts) ** 0.5)


def _filter_bonus(filters: dict[str, Any], text: str, doc: dict[str, Any]) -> float:
    joined = f"{text}\n{json.dumps(doc.get('fields') or {}, ensure_ascii=False)}".lower()
    bonus = 0.0
    for value in filters.values():
        if value is None:
            continue
        needle = str(value).strip().lower()
        if needle and needle in joined:
            bonus += 1.2
    return bonus


def _best_snippet(text: str, query_tokens: list[str], max_chars: int = 900) -> str:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    if not paragraphs:
        return text[:max_chars]
    best = max(
        paragraphs,
        key=lambda paragraph: sum(1 for token in query_tokens if token and token in paragraph.lower()),
    )
    if len(best) > max_chars:
        lowered = best.lower()
        anchors = [lowered.find(token) for token in query_tokens if token and lowered.find(token) >= 0]
        start = max(0, min(anchors) - max_chars // 3) if anchors else 0
        start = min(start, len(best) - max_chars)
        snippet = best[start : start + max_chars]
        return f"{'...' if start else ''}{snippet.rstrip()}{'...' if start + max_chars < len(best) else ''}"
    return best


def _extract_fields(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip().rstrip(".")
        if name == "total_amount":
            try:
                fields[name] = float(value.replace(",", ""))
            except ValueError:
                fields[name] = value
        else:
            fields[name] = value
    return fields


def _source_type(source_path: str) -> str:
    lowered = source_path.lower()
    if "policy" in lowered:
        return "policy_doc"
    if "process" in lowered or "bpi" in lowered or "event" in lowered:
        return "process_log"
    if "clear_invoice" in lowered or "clear invoice" in lowered:
        return "clear_invoice_event"
    if "invoice" in lowered or "发票" in lowered:
        return "invoice_text"
    if "vendor" in lowered:
        return "vendor_record"
    return Path(source_path).suffix.lstrip(".") or "unknown"


def _stable_id(source_path: str, locator: str) -> str:
    digest = hashlib.sha256(f"{source_path}|{locator}".encode("utf-8")).hexdigest()[:12]
    return f"rag_{digest}"


def _render_context(evidences: list[RagEvidence]) -> str:
    lines = []
    for index, item in enumerate(evidences, start=1):
        lines.append(f"[{index}] {item.source_path} {item.locator}\n{item.snippet}")
    return "\n\n".join(lines)


def rag_search(**kwargs: Any) -> dict[str, Any]:
    allowed = {"query", "intent", "filters", "top_k", "include_raw_snippets"}
    result = RagSkill().retrieve(**{key: value for key, value in kwargs.items() if key in allowed})
    return result.model_dump()
