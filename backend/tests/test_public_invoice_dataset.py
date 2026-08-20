from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.invoice_tau.dataset_validation import validate_dataset


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = (
    REPO_ROOT / "benchmarks" / "invoice_tau" / "datasets" / "public_invoice_arithmetic_v1"
)


def test_public_invoice_dataset_is_complete_and_replayable() -> None:
    report = validate_dataset(DATASET_ROOT)

    assert report["case_count"] == 50
    assert report["unique_pdf_count"] == 50
    assert report["split_counts"] == {"dev": 30, "validation": 10, "holdout": 10}
    assert report["proof_status_counts"] == {"SUPPORTED": 70, "CONTRADICTED": 30}


def test_public_invoice_dataset_has_balanced_truth_and_no_oracle_prompt_leak() -> None:
    dataset = json.loads((DATASET_ROOT / "dataset.json").read_text(encoding="utf-8"))

    assert dataset["coverage"]["consistency"] == {
        "correct": 20,
        "subtotal_error": 15,
        "total_error": 15,
    }
    assert dataset["coverage"]["edge_case"] == {
        "credit_note": 5,
        "mixed_vat": 5,
        "none": 30,
        "reverse_charge": 5,
        "single_item": 5,
    }

    for entry in dataset["cases"]:
        case_dir = DATASET_ROOT / entry["path"]
        scenario_text = (case_dir / "scenario.json").read_text(encoding="utf-8")
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        assert expected["oracle_sentinel"] not in scenario_text


def test_public_invoice_dataset_rejects_a_tampered_pdf(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(DATASET_ROOT, copied)
    first_case = json.loads((copied / "dataset.json").read_text(encoding="utf-8"))["cases"][0]
    invoice = copied / first_case["path"] / "attachments" / "invoice.pdf"
    invoice.write_bytes(invoice.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="Attachment size mismatch"):
        validate_dataset(copied)


def test_pinned_invoicebenchmark_tools_match_source_manifest() -> None:
    upstream = DATASET_ROOT / "upstream"
    source = json.loads((upstream / "SOURCE.json").read_text(encoding="utf-8"))

    assert source["revision"] == "f0699d8c94468fb2fefaa22f669684b22e68c3d3"
    assert source["license"] == "MIT"
    for entry in source["files"]:
        path = upstream / entry["path"]
        assert path.stat().st_size == entry["byte_size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


def test_pinned_invoicebenchmark_generator_runs_locally(tmp_path: Path) -> None:
    generator = DATASET_ROOT / "upstream" / "invoice_generator.py"
    output = tmp_path / "generated"

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(generator),
            "--output",
            str(output),
            "--count",
            "8",
            "--seed",
            "42",
            "--verify",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "verified 8 invoices" in result.stdout
    assert len(list((output / "invoices").glob("INV-*.md"))) == 8
    assert len(list((output / "ground_truth").glob("INV-*.json"))) == 8
