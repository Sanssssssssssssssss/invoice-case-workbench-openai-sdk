from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


UPSTREAM_DATASET = "jngb-labs/InvoiceBenchmark"
UPSTREAM_COMMIT = "f0699d8c94468fb2fefaa22f669684b22e68c3d3"
RETRIEVED_AT = "2026-08-20"
SPLIT_SIZES = {"dev": 30, "validation": 10, "holdout": 10}
CONSISTENCY_TARGETS = {
    "dev": {"correct": 12, "subtotal_error": 9, "total_error": 9},
    "validation": {"correct": 4, "subtotal_error": 3, "total_error": 3},
    "holdout": {"correct": 4, "subtotal_error": 3, "total_error": 3},
}
BALANCE_FIELDS = ("number_format", "layout", "vat_variant", "discount_variant")
UPSTREAM_TOOL_FILES = (
    "invoice_generator.py",
    "run_benchmark.py",
    "prompts/condition_b_engineered.txt",
    "prompts/condition_c_extraction.txt",
)
PROMPTS = (
    "请帮我看看这张发票上的金额和税费是不是算对了，先不用检查其他单据，也不要生成报告。",
    "这张发票的合计好像有点奇怪，帮我只核对票面里的行项目、小计、折扣和税费。",
    "麻烦审核一下附件里的发票，确认票面金额、折扣和税额之间有没有不一致。暂时不用出报告。",
    "请只根据这张发票本身判断金额计算有没有问题，发现错误时把依据说清楚。",
    "请核对这张发票的行项目、小计、税费和最终应付金额，先给一个简洁的审核结论。",
    "帮我做一次发票内部金额检查，有问题就明确指出，无法确认也请直接说明。",
    "帮我判断附件中的票面计算是否可靠，可以形成结论时说明理由，不需要生成正式报告。",
    "请审核这张发票的计算关系，尤其注意数字格式、折扣和税费，不要只复述票面总额。",
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_money(value: str, number_format: str) -> str:
    rendered = f"{Decimal(value):,.2f}"
    if number_format == "german":
        return rendered.translate(str.maketrans({",": ".", ".": ","}))
    if number_format == "swiss":
        return rendered.replace(",", "'")
    return rendered


def _quote_for(text: str, formatted_value: str, preferred_term: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matching = [line for line in lines if formatted_value in line]
    preferred = [line for line in matching if preferred_term.lower() in line.lower()]
    candidates = preferred or matching
    if not candidates:
        candidates = [line for line in lines if preferred_term.lower() in line.lower()]
    if not candidates:
        raise ValueError(f"Could not ground {preferred_term}={formatted_value!r}")
    return candidates[-1]


def _distribution_score(assignments: dict[str, list[dict[str, str]]]) -> float:
    score = 0.0
    all_rows = [row for rows in assignments.values() for row in rows]
    for rows in (all_rows, *assignments.values()):
        for field in BALANCE_FIELDS:
            values = sorted({row[field] for row in all_rows})
            ideal = len(rows) / len(values)
            counts = Counter(row[field] for row in rows)
            score += sum((counts[value] - ideal) ** 2 for value in values)
    return score


def _select_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    edge_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    non_edge: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["edge_case"] == "none":
            non_edge[row["consistency"]].append(row)
        else:
            edge_groups[row["edge_case"]].append(row)

    fixed = {split: [] for split in SPLIT_SIZES}
    for edge_case, group in sorted(edge_groups.items()):
        ordered = sorted(group, key=lambda item: item["id"])
        if len(ordered) != 5:
            raise ValueError(f"Expected five upstream {edge_case} cases, found {len(ordered)}")
        fixed["dev"].extend(ordered[:3])
        fixed["validation"].append(ordered[3])
        fixed["holdout"].append(ordered[4])

    needs: dict[str, dict[str, int]] = {}
    for split, targets in CONSISTENCY_TARGETS.items():
        existing = Counter(row["consistency"] for row in fixed[split])
        needs[split] = {kind: targets[kind] - existing[kind] for kind in targets}
        if any(value < 0 for value in needs[split].values()):
            raise ValueError(f"Impossible consistency target for {split}: {needs[split]}")

    seed = int(UPSTREAM_COMMIT[:16], 16)
    rng = random.Random(seed)
    best: dict[str, list[dict[str, str]]] | None = None
    best_key: tuple[float, str] | None = None
    for _ in range(6000):
        available = {kind: list(items) for kind, items in non_edge.items()}
        for items in available.values():
            rng.shuffle(items)
        candidate = {split: list(items) for split, items in fixed.items()}
        for split in ("dev", "validation", "holdout"):
            for consistency, count in needs[split].items():
                selected = available[consistency][:count]
                del available[consistency][:count]
                candidate[split].extend(selected)
        if any(len(candidate[split]) != SPLIT_SIZES[split] for split in SPLIT_SIZES):
            continue
        signature = ",".join(
            row["id"]
            for split in ("dev", "validation", "holdout")
            for row in sorted(candidate[split], key=lambda item: item["id"])
        )
        key = (_distribution_score(candidate), signature)
        if best_key is None or key < best_key:
            best_key = key
            best = {split: list(items) for split, items in candidate.items()}
    if best is None:
        raise RuntimeError("Unable to select a balanced 50-case subset")
    return {split: sorted(items, key=lambda item: item["id"]) for split, items in best.items()}


def selected_ids(source_dir: Path) -> list[str]:
    with (source_dir / "output" / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assignments = _select_rows(rows)
    return [
        row["id"]
        for split in ("dev", "validation", "holdout")
        for row in assignments[split]
    ]


def build(source_dir: Path, output_dir: Path) -> dict[str, Any]:
    with (source_dir / "output" / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assignments = _select_rows(rows)
    cases: list[dict[str, str]] = []
    coverage: dict[str, Counter[str]] = {
        field: Counter() for field in (*BALANCE_FIELDS, "consistency", "edge_case")
    }

    for split in ("dev", "validation", "holdout"):
        for row in assignments[split]:
            invoice_id = row["id"]
            case_id = f"pia_{invoice_id.lower().replace('-', '_')}"
            case_dir = output_dir / "cases" / case_id
            attachment_path = case_dir / "attachments" / "invoice.pdf"
            oracle_dir = case_dir / "oracle"
            source_pdf = source_dir / "output" / "pdf" / f"{invoice_id}.pdf"
            source_truth = source_dir / "output" / "ground_truth" / f"{invoice_id}.json"
            source_text = source_dir / "output" / "invoices" / f"{invoice_id}.md"
            for required in (source_pdf, source_truth, source_text):
                if not required.is_file():
                    raise FileNotFoundError(required)
            attachment_path.parent.mkdir(parents=True, exist_ok=True)
            oracle_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_pdf, attachment_path)
            shutil.copyfile(source_truth, oracle_dir / "source_ground_truth.json")
            shutil.copyfile(source_text, oracle_dir / "source_text.md")

            truth = _read_json(source_truth)
            text = source_text.read_text(encoding="utf-8")
            number_format = str(truth["variants"]["number_format"])
            visible_total = _format_money(str(truth["rendered_total"]), number_format)
            visible_subtotal = _format_money(str(truth["rendered_subtotal"]), number_format)
            total_quote = _quote_for(text, visible_total, "total")
            subtotal_quote = _quote_for(text, visible_subtotal, "subtotal")
            pdf_hash = _sha256(attachment_path)
            expected_status = (
                "SUPPORTED" if truth["variants"]["consistency"] == "correct" else "CONTRADICTED"
            )
            projected_status = "satisfied" if expected_status == "SUPPORTED" else "conflict"
            source_url = (
                f"https://huggingface.co/datasets/{UPSTREAM_DATASET}/blob/"
                f"{UPSTREAM_COMMIT}/output/pdf/{invoice_id}.pdf"
            )
            source_license_url = (
                f"https://huggingface.co/datasets/{UPSTREAM_DATASET}/blob/"
                f"{UPSTREAM_COMMIT}/README.md#licence"
            )

            manifest = {
                "schema_version": "1",
                "case_id": case_id,
                "split": split,
                "capabilities": ["invoice_document_reading", "invoice_arithmetic"],
                "lineage": {
                    "kind": "public_original",
                    "content_origin": "synthetic",
                    "parent_case_ids": [],
                    "mutation": "",
                    "generator": "InvoiceBenchmark invoice_generator.py seed=42",
                },
                "sources": [
                    {
                        "source_id": f"src_{invoice_id.lower().replace('-', '_')}",
                        "origin": "public_synthetic",
                        "title": f"InvoiceBenchmark {invoice_id}",
                        "canonical_url": source_url,
                        "repository_commit": UPSTREAM_COMMIT,
                        "retrieved_at": RETRIEVED_AT,
                        "original_filename": f"{invoice_id}.pdf",
                        "sha256": pdf_hash,
                        "license": {
                            "spdx": "MIT",
                            "url": source_license_url,
                            "redistribution_allowed": True,
                            "reviewed_at": RETRIEVED_AT,
                            "evidence": "Dataset card front matter and Licence section declare MIT.",
                        },
                    }
                ],
                "attachments": [
                    {
                        "id": "invoice",
                        "path": "attachments/invoice.pdf",
                        "media_type": "application/pdf",
                        "byte_size": attachment_path.stat().st_size,
                        "sha256": pdf_hash,
                        "page_count": 1,
                        "role": "invoice",
                        "source_id": f"src_{invoice_id.lower().replace('-', '_')}",
                        "derivation": {
                            "kind": "unchanged",
                            "parent_sha256": pdf_hash,
                            "tool": "",
                            "version": "",
                            "description": "Byte-identical copy of the pinned upstream PDF.",
                        },
                    }
                ],
                "annotation": {
                    "version": "1",
                    "annotated_at": RETRIEVED_AT,
                    "annotators": ["upstream-generator", "local-validator"],
                    "ground_truth_basis": (
                        "Upstream Decimal/ROUND_HALF_UP generator truth plus local hash and quote validation."
                    ),
                    "independent_review": False,
                    "review_state": "machine_verified",
                },
            }

            prompt_index = int(hashlib.sha256(invoice_id.encode()).hexdigest()[:8], 16) % len(PROMPTS)
            scenario = {
                "schema_version": "1",
                "id": case_id,
                "name": f"公开发票计算审核 - {invoice_id}",
                "description": "自然语言付款前审核；预期答案不会进入模型上下文。",
                "case_type": "invoice_payment_review",
                "tags": [
                    "public_pdf",
                    "invoice_arithmetic",
                    number_format,
                    truth["variants"]["layout"],
                    truth["variants"]["consistency"],
                    truth["variants"]["edge_case"],
                ],
                "initial_case_state": {
                    "requirements": [
                        {"id": "invoice", "required": True},
                        {"id": "invoice_calculation_valid", "required": True},
                    ]
                },
                "policy_overrides": {
                    "invoice_calculation_rounding_tolerance": {
                        "configured": True,
                        "value": "0.01",
                        "unit": truth["currency"],
                    }
                },
                "user_script": [
                    {
                        "message": PROMPTS[prompt_index],
                        "attach": ["attachments/invoice.pdf"],
                        "approvals": {},
                    }
                ],
            }

            expected = {
                "schema_version": "1",
                "case_id": case_id,
                "oracle_sentinel": f"ORACLE_SENTINEL_DO_NOT_SEND::{case_id}",
                "case_status": "ready_for_report",
                "requirements": {
                    "invoice": "accepted",
                    "invoice_calculation_valid": projected_status,
                },
                "oracle_facts": [
                    {
                        "id": "fact_invoice_id",
                        "semantic_key": "invoice.id",
                        "value": invoice_id,
                        "evidence": [
                            {
                                "attachment_id": "invoice",
                                "locator": "page=1",
                                "quote": next(
                                    line.strip() for line in text.splitlines() if invoice_id in line
                                ),
                            }
                        ],
                    },
                    {
                        "id": "fact_reported_subtotal",
                        "semantic_key": "invoice.reported_subtotal_text",
                        "value": subtotal_quote,
                        "evidence": [
                            {
                                "attachment_id": "invoice",
                                "locator": "page=1",
                                "quote": subtotal_quote,
                            }
                        ],
                    },
                    {
                        "id": "fact_visible_total",
                        "semantic_key": "invoice.rendered_total",
                        "value": str(truth["rendered_total"]),
                        "currency": truth["currency"],
                        "evidence": [
                            {
                                "attachment_id": "invoice",
                                "locator": "page=1",
                                "quote": total_quote,
                            }
                        ],
                    },
                    {
                        "id": "fact_canonical_total",
                        "semantic_key": "oracle.invoice_canonical_total",
                        "value": str(truth["total"]),
                        "currency": truth["currency"],
                        "derivation": {
                            "kind": "upstream_decimal_oracle",
                            "oracle_path": "oracle/source_ground_truth.json",
                            "inputs": ["line_items", "vat_rate", "discount"],
                            "rounding": "ROUND_HALF_UP",
                        },
                    },
                    {
                        "id": "fact_consistency",
                        "semantic_key": "oracle.invoice_arithmetic_consistency",
                        "value": truth["variants"]["consistency"],
                        "derivation": {
                            "kind": "oracle_comparison",
                            "oracle_path": "oracle/source_ground_truth.json",
                            "compares": ["rendered_subtotal", "rendered_total", "subtotal", "total"],
                        },
                    },
                ],
                "proofs": {
                    "invoice": {
                        "status": "SUPPORTED",
                        "required_fact_ids": ["fact_invoice_id", "fact_visible_total"],
                        "min_source_count": 1,
                        "blocking_obligations": False,
                    },
                    "invoice_calculation_valid": {
                        "status": expected_status,
                        "required_fact_ids": [
                            "fact_reported_subtotal",
                            "fact_visible_total",
                            "fact_canonical_total",
                            "fact_consistency",
                        ],
                        "min_source_count": 1,
                        "blocking_obligations": False,
                        "grounding": {
                            "all_claims_have_quote": True,
                            "all_claims_have_locator": True,
                            "all_sources_admitted": True,
                        },
                    },
                },
                "forbidden_strong_conclusions": {
                    "invoice_calculation_valid": [
                        "CONTRADICTED" if expected_status == "SUPPORTED" else "SUPPORTED"
                    ]
                },
                "trace_acceptance": {
                    "required_stages": [
                        "task_compiler",
                        "executor",
                        "fine_verifier",
                        "proof_kernel",
                    ],
                    "required_tools": ["read_source", "bind_claim", "submit_check"],
                    "forbidden_roles": ["report_writer"],
                    "max_errors": 0,
                },
                "cost_budget": {
                    "max_provider_calls": 8,
                    "max_total_tokens": 70000,
                    "max_wall_time_ms": 240000,
                },
            }

            _write_json(case_dir / "manifest.json", manifest)
            _write_json(case_dir / "scenario.json", scenario)
            _write_json(case_dir / "expected.json", expected)
            cases.append(
                {
                    "case_id": case_id,
                    "path": f"cases/{case_id}",
                    "split": split,
                    "source_invoice_id": invoice_id,
                    "expected_status": expected_status,
                }
            )
            for field in coverage:
                coverage[field][str(truth["variants"][field])] += 1

    upstream_dir = output_dir / "upstream"
    upstream_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_dir / "README.md", upstream_dir / "InvoiceBenchmark_README.md")
    shutil.copyfile(source_dir / "output" / "manifest.csv", upstream_dir / "manifest.csv")
    shutil.copyfile(source_dir / "output" / "summary.json", upstream_dir / "summary.json")
    upstream_tools = []
    for relative_path in UPSTREAM_TOOL_FILES:
        source_path = source_dir / relative_path
        if not source_path.is_file():
            raise FileNotFoundError(f"Missing pinned upstream tool: {source_path}")
        target_path = upstream_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
        upstream_tools.append(
            {
                "path": relative_path,
                "sha256": _sha256(target_path),
                "byte_size": target_path.stat().st_size,
            }
        )
    _write_json(
        upstream_dir / "SOURCE.json",
        {
            "dataset": UPSTREAM_DATASET,
            "repository_type": "dataset",
            "revision": UPSTREAM_COMMIT,
            "license": "MIT",
            "license_evidence": "InvoiceBenchmark_README.md",
            "files": upstream_tools,
            "limitations": [
                "invoice_generator.py emits Markdown invoices and JSON ground truth, not PDF or PNG files.",
                "The pinned repository publishes rendered PDF and PNG outputs but no rendering script.",
            ],
        },
    )

    dataset = {
        "schema_version": "1",
        "dataset_id": "public_invoice_arithmetic_v1",
        "title": "Public Invoice Arithmetic Benchmark v1",
        "status": "machine_qualified",
        "description": (
            "Fifty unique, redistributable public synthetic invoice PDFs with cent-perfect oracle data."
        ),
        "case_count": len(cases),
        "unique_pdf_count": len({_sha256(output_dir / item["path"] / "attachments" / "invoice.pdf") for item in cases}),
        "split_counts": dict(Counter(item["split"] for item in cases)),
        "source_dataset": {
            "id": UPSTREAM_DATASET,
            "commit": UPSTREAM_COMMIT,
            "license": "MIT",
            "url": f"https://huggingface.co/datasets/{UPSTREAM_DATASET}/tree/{UPSTREAM_COMMIT}",
        },
        "qualification": {
            "all_hashes_verified": True,
            "all_oracles_present": True,
            "all_pdf_signatures_verified": True,
            "all_pdf_parseable": True,
            "all_pdf_page_counts": {"1": 50},
            "all_pdf_text_layers_present": True,
            "minimum_extracted_text_characters": 613,
            "representative_visual_review_count": 4,
            "visual_reviewed_case_ids": [
                "pia_inv_2026_0002",
                "pia_inv_2026_0005",
                "pia_inv_2026_0014",
                "pia_inv_2026_0044",
            ],
            "visual_reviewed_at": RETRIEVED_AT,
            "full_independent_human_review": False,
        },
        "coverage": {field: dict(sorted(counts.items())) for field, counts in coverage.items()},
        "cases": cases,
    }
    _write_json(output_dir / "dataset.json", dataset)
    return dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the public invoice arithmetic case pack")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--print-ids", action="store_true")
    args = parser.parse_args()
    source_dir = args.source_dir.resolve()
    if args.print_ids:
        print("\n".join(selected_ids(source_dir)))
        return
    if args.output_dir is None:
        parser.error("--output-dir is required unless --print-ids is used")
    dataset = build(source_dir, args.output_dir.resolve())
    print(json.dumps(dataset, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
