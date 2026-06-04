from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.state.schemas import RagEvidence, RagResult


SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+|[\u4e00-\u9fff]")
FIELD_PATTERNS = {
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
        self.index_root = Path(index_root or settings.storage_root / "rag")
        self.index_root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.index_root / "index.json"
        self.embedding_path = self.index_root / "embeddings.json"

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
        query_tokens = _tokens(query)
        vector_scores = self._vector_scores(query, docs)
        filters = filters or {}
        scored: list[tuple[float, dict[str, Any], str]] = []
        for index, doc in enumerate(docs):
            text = str(doc.get("text") or "")
            token_counts = Counter(doc.get("tokens") or [])
            score = _lexical_score(query_tokens, token_counts, text)
            score += vector_scores.get(index, 0.0) * 3.0
            score += _filter_bonus(filters, text, doc)
            if intent == "policy_qa" and "policy" in str(doc.get("source_path", "")).lower():
                score += 0.8
            if "invoice" in query.lower() and "invoice" in text.lower():
                score += 0.5
            if score > 0:
                scored.append((score, doc, _best_snippet(text, query_tokens)))
        scored.sort(key=lambda item: item[0], reverse=True)
        evidences: list[RagEvidence] = []
        for score, doc, snippet in scored[: max(1, top_k)]:
            source_path = str(doc.get("source_path") or "")
            evidences.append(
                RagEvidence(
                    source_id=_stable_id(source_path, str(doc.get("locator") or "")),
                    source_path=source_path,
                    source_type=_source_type(source_path),
                    locator=str(doc.get("locator") or ""),
                    snippet=snippet if include_raw_snippets else snippet[:280],
                    fields=_extract_fields(snippet),
                    score=round(float(score), 4),
                    channel="fused" if vector_scores else "bm25",
                )
            )
        status = "success" if evidences else "not_found"
        return RagResult(
            status=status,
            evidences=evidences,
            answer_context=_render_context(evidences),
            unsupported_fields=[] if evidences else ["query"],
            reason=(
                "Retrieved local invoice payment knowledge snippets."
                if evidences
                else "No local knowledge snippets matched the query."
            ),
        )

    def _load_or_build_index(self) -> list[dict[str, Any]]:
        root_signature = [str(root.resolve()) for root in self.knowledge_roots]
        knowledge_signature = _knowledge_signature(self.knowledge_roots)
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                docs = data.get("documents")
                if (
                    isinstance(docs, list)
                    and data.get("knowledge_roots") == root_signature
                    and data.get("knowledge_signature") == knowledge_signature
                ):
                    return docs
            except json.JSONDecodeError:
                pass
        docs = self._build_index()
        self.index_path.write_text(
            json.dumps(
                {
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

    def _build_index(self) -> list[dict[str, Any]]:
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
                for index, chunk in enumerate(_chunks(text), start=1):
                    docs.append(
                        {
                            "source_path": rel,
                            "locator": f"chunk {index}",
                            "text": chunk,
                            "tokens": _tokens(chunk),
                            "fields": _extract_fields(chunk),
                        }
                    )
        return docs

    def _vector_scores(self, query: str, docs: list[dict[str, Any]]) -> dict[int, float]:
        settings = get_settings()
        if os.getenv("INVOICE_AGENT_ENABLE_VECTOR", "auto").strip().lower() in {"0", "false", "no", "off"}:
            return {}
        if not settings.embedding_api_key:
            return {}
        fingerprint = _doc_fingerprint(docs)
        cache = _read_json(self.embedding_path)
        embeddings = cache.get("embeddings")
        if (
            cache.get("fingerprint") != fingerprint
            or cache.get("model") != settings.embedding_model
            or not isinstance(embeddings, list)
            or len(embeddings) != len(docs)
        ):
            texts = [str(doc.get("text") or "")[:1600] for doc in docs]
            embeddings = _embed_texts(texts, api_key=settings.embedding_api_key, base_url=settings.embedding_base_url, model=settings.embedding_model)
            self.embedding_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "model": settings.embedding_model,
                        "embeddings": embeddings,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        query_embedding = _embed_texts([query], api_key=settings.embedding_api_key, base_url=settings.embedding_base_url, model=settings.embedding_model)[0]
        scored = {
            index: _cosine(query_embedding, embedding)
            for index, embedding in enumerate(embeddings)
            if isinstance(embedding, list)
        }
        return {index: score for index, score in scored.items() if score > 0.15}


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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


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
        if len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}".strip()
        else:
            if current:
                chunks.append(current)
            current = block[:max_chars]
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
    return score / math.sqrt(max(1, len(doc_counts)))


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
        return best[: max_chars - 3].rstrip() + "..."
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


def _doc_fingerprint(docs: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for doc in docs:
        h.update(str(doc.get("source_path") or "").encode("utf-8"))
        h.update(str(doc.get("locator") or "").encode("utf-8"))
        h.update(str(doc.get("text") or "").encode("utf-8"))
    return h.hexdigest()


def _embed_texts(texts: list[str], *, api_key: str, base_url: str, model: str) -> list[list[float]]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    embeddings: list[list[float]] = []
    for index in range(0, len(texts), 10):
        batch = texts[index : index + 10]
        response = client.embeddings.create(model=model, input=batch)
        embeddings.extend(list(item.embedding) for item in response.data)
    return embeddings


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    l_norm = math.sqrt(sum(a * a for a in left))
    r_norm = math.sqrt(sum(b * b for b in right))
    if not l_norm or not r_norm:
        return 0.0
    return dot / (l_norm * r_norm)


def _render_context(evidences: list[RagEvidence]) -> str:
    lines = []
    for index, item in enumerate(evidences, start=1):
        lines.append(f"[{index}] {item.source_path} {item.locator}\n{item.snippet}")
    return "\n\n".join(lines)


def rag_search(**kwargs: Any) -> dict[str, Any]:
    allowed = {"query", "intent", "filters", "top_k", "include_raw_snippets"}
    result = RagSkill().retrieve(**{key: value for key, value in kwargs.items() if key in allowed})
    return result.model_dump()
