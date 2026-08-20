#!/usr/bin/env python3
"""
run_benchmark.py — Invoice Validation Benchmark Harness
========================================================

Runs 200 synthetic invoices through language models via OpenAI-compatible APIs
(Ollama for local models, vLLM for remote) under two conditions:

  Condition B: Model extracts fields AND verifies arithmetic (engineered prompt)
  Condition C: Model extracts fields only; deterministic code verifies arithmetic

Outputs a results CSV with per-invoice scores, error bands, and metadata.

Requirements:
    pip install openai

Usage:
    # Run all invoices through one local model, both conditions
    python run_benchmark.py --models llama3.1:8b

    # Run specific models, specific condition
    python run_benchmark.py --models llama3.1:8b,qwen3:8b --conditions B

    # Test on a single invoice first (sanity check)
    python run_benchmark.py --models llama3.1:8b --invoice INV-2026-0001

    # Remote models via vLLM (prefix with vllm:)
    python run_benchmark.py --models vllm:meta-llama/Llama-3.1-8B-Instruct --vllm-url http://gpu-server:8000/v1

    # Resume an interrupted run (skips existing results)
    python run_benchmark.py --models llama3.1:8b --resume

    # Dry run — no API calls, just test scoring on synthetic responses
    python run_benchmark.py --dry-run
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package required. Run: pip install openai")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Paths (relative to this script's directory)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
INVOICE_DIR = SCRIPT_DIR / "output" / "invoices"
GROUND_TRUTH_DIR = SCRIPT_DIR / "output" / "ground_truth"
PROMPT_DIR = SCRIPT_DIR / "prompts"
RESULTS_DIR = SCRIPT_DIR / "results"
MANIFEST_PATH = SCRIPT_DIR / "output" / "manifest.csv"


# ---------------------------------------------------------------------------
# Error bands
# ---------------------------------------------------------------------------
BAND_EXACT = "exact"
BAND_GHOST = "ghost"            # <1% — undetectable without recalculation
BAND_PLAUSIBLE = "plausible"    # 1–5% — the most dangerous band
BAND_SUSPICIOUS = "suspicious"  # 5–15% — a competent reviewer catches this
BAND_OBVIOUS = "obvious"        # >15% — everyone notices
BAND_PARSE_ERROR = "parse_error"


# ---------------------------------------------------------------------------
# CSV column order
# ---------------------------------------------------------------------------
RESULT_FIELDS = [
    "invoice_id", "model", "condition", "timestamp",
    # Ground truth
    "true_total", "rendered_total", "is_consistent", "number_format",
    "vat_variant", "discount_variant", "layout", "edge_case",
    # Model output
    "model_total", "model_raw_total",
    # Scoring
    "error_amount", "error_pct", "error_band", "exact_match",
    # Condition B extras
    "model_flagged_inconsistency", "model_is_consistent",
    "model_assumptions",
    # Condition C extras
    "recomputed_total",
    # Meta
    "response_time_s", "parse_success", "raw_response_length",
    "error_message",
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_manifest() -> dict[str, dict]:
    """Load manifest.csv into a dict keyed by invoice_id."""
    manifest = {}
    with open(MANIFEST_PATH, newline="") as f:
        for row in csv.DictReader(f):
            manifest[row["id"]] = row
    return manifest


def load_invoice_text(invoice_id: str) -> str:
    """Load the markdown text of an invoice."""
    path = INVOICE_DIR / f"{invoice_id}.md"
    return path.read_text(encoding="utf-8")


def load_ground_truth(invoice_id: str) -> dict:
    """Load the ground truth JSON for an invoice."""
    path = GROUND_TRUTH_DIR / f"{invoice_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_prompt_template(condition: str) -> str:
    """Load the prompt template for a condition (B or C)."""
    filename = {
        "B": "condition_b_engineered.txt",
        "C": "condition_c_extraction.txt",
    }[condition]
    return (PROMPT_DIR / filename).read_text(encoding="utf-8")


def list_invoice_ids() -> list[str]:
    """List all invoice IDs from the manifest."""
    manifest = load_manifest()
    return sorted(manifest.keys())


def load_completed_results(results_path: Path) -> set[tuple[str, str, str]]:
    """Load (invoice_id, model, condition) tuples from an existing results CSV."""
    completed = set()
    if results_path.exists():
        with open(results_path, newline="") as f:
            for row in csv.DictReader(f):
                completed.add((row["invoice_id"], row["model"], row["condition"]))
    return completed


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def extract_json_from_response(text: str) -> Optional[dict]:
    """
    Extract a JSON object from model response text.
    Handles: raw JSON, markdown fences (```json ... ```), leading/trailing text,
    and reasoning model output wrapped in <think>...</think> tags.
    """
    if not text or not text.strip():
        return None

    # Strip reasoning model thinking traces (Qwen3, DeepSeek-R1, QwQ, etc.)
    # These wrap chain-of-thought in <think>...</think> before the actual answer.
    # The think block may contain JSON-like snippets that would confuse the parser.
    # First: strip closed <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Second: strip unclosed <think> tags (model sometimes streams thinking without closing)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()

    # Try 1: strip markdown fences
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # Try 2: find the outermost { ... } block
    # (handles models that add explanation before/after the JSON)
    brace_start = text.find("{")
    if brace_start == -1:
        return None

    # Walk forward to find matching closing brace
    depth = 0
    in_string = False
    escape_next = False
    for i in range(brace_start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start : i + 1])
                except json.JSONDecodeError:
                    return None

    # Try 3: brute force
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(error_pct: float) -> str:
    """Classify absolute error percentage into scoring bands."""
    abs_pct = abs(error_pct)
    if abs_pct == 0:
        return BAND_EXACT
    elif abs_pct < 1.0:
        return BAND_GHOST
    elif abs_pct < 5.0:
        return BAND_PLAUSIBLE
    elif abs_pct < 15.0:
        return BAND_SUSPICIOUS
    else:
        return BAND_OBVIOUS


def compute_error(model_total: Decimal, true_total: Decimal) -> tuple[Decimal, float, str]:
    """
    Returns (error_amount, error_pct, error_band).
    error_pct is relative to true_total. For zero true_total, any nonzero model_total is 100%.
    """
    error_amount = model_total - true_total
    if true_total == 0:
        error_pct = 100.0 if error_amount != 0 else 0.0
    else:
        error_pct = float((error_amount / true_total) * 100)
    band = classify_error(error_pct)
    return error_amount, error_pct, band


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------

D = Decimal

def to_decimal(value) -> Decimal:
    """Convert a value to Decimal, handling strings, ints, floats, None."""
    if value is None:
        return D("0.00")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        # Round floats to 2 decimal places to avoid float precision issues
        return D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace(" ", "")
        try:
            return D(value).quantize(D("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return D("0.00")
    return D("0.00")


def decimal_round(value: Decimal) -> Decimal:
    """Round to 2 decimal places."""
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Condition C: Recompute total from extracted fields
# ---------------------------------------------------------------------------

def recompute_total_from_extraction(extracted: dict) -> Decimal:
    """
    Compute the invoice total from model-extracted fields using Decimal arithmetic.

    Logic:
    - Sum line item amounts → computed_subtotal
    - If VAT is included in prices:
        total = computed_subtotal - discount
        (VAT is already baked into the line item amounts)
    - If VAT is excluded:
        vat = computed_subtotal * vat_rate
        total = computed_subtotal + vat - discount
    - If vat_rate is null (mixed VAT), fall back to stated_vat_amount
    - Conditional discounts (trade terms) are NOT applied
    """
    # Step 1: Sum line items
    computed_subtotal = D("0.00")
    line_items = extracted.get("line_items", [])
    for item in line_items:
        amount = item.get("amount")
        if amount is not None:
            computed_subtotal += to_decimal(amount)
        else:
            # Fall back to qty * unit_price
            qty = to_decimal(item.get("quantity", 0))
            price = to_decimal(item.get("unit_price", 0))
            computed_subtotal += decimal_round(qty * price)

    # Step 2: Determine VAT handling
    vat_included = extracted.get("vat_included_in_prices", False)
    vat_rate_raw = extracted.get("vat_rate")
    stated_vat = extracted.get("stated_vat_amount")

    # Step 3: Compute VAT
    if vat_included:
        # VAT is already in the line item amounts — don't add it again
        vat_amount = D("0.00")
    elif vat_rate_raw is not None:
        vat_rate = to_decimal(vat_rate_raw)
        vat_amount = decimal_round(computed_subtotal * vat_rate)
    elif stated_vat is not None:
        # Mixed VAT or rate not stated — use model's extracted VAT amount
        vat_amount = to_decimal(stated_vat)
    else:
        vat_amount = D("0.00")

    # Step 4: Compute discount
    discount_amount = D("0.00")
    is_conditional = extracted.get("discount_is_conditional", False)

    if not is_conditional:
        discount_raw = extracted.get("discount_amount")
        if discount_raw is not None and discount_raw != 0:
            discount_amount = to_decimal(discount_raw)
        elif extracted.get("discount_rate") is not None:
            rate = to_decimal(extracted["discount_rate"])
            discount_amount = decimal_round(computed_subtotal * rate)

    # Step 5: Compute total
    total = computed_subtotal + vat_amount - discount_amount
    return decimal_round(total)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_condition_b(parsed: dict, ground_truth: dict) -> dict:
    """
    Score a Condition B response.
    Compares model's reported total against ground truth correct total.
    Also checks whether the model detected inconsistencies.
    """
    true_total = to_decimal(ground_truth["total"])
    model_total_raw = parsed.get("total")
    model_total = to_decimal(model_total_raw)

    error_amount, error_pct, band = compute_error(model_total, true_total)

    # Did the model flag inconsistencies?
    is_consistent_gt = ground_truth["rendered_total"] == ground_truth["total"]
    model_flagged = bool(parsed.get("inconsistencies"))
    model_says_consistent = parsed.get("is_consistent", True)

    assumptions = parsed.get("assumptions", [])
    if isinstance(assumptions, list):
        assumptions = "; ".join(str(a) for a in assumptions)

    return {
        "model_total": str(model_total),
        "model_raw_total": str(model_total_raw),
        "error_amount": str(error_amount),
        "error_pct": f"{error_pct:.4f}",
        "error_band": band,
        "exact_match": str(error_amount == 0).lower(),
        "model_flagged_inconsistency": str(model_flagged).lower(),
        "model_is_consistent": str(model_says_consistent).lower(),
        "model_assumptions": assumptions,
        "recomputed_total": "",
    }


def score_condition_c(parsed: dict, ground_truth: dict) -> dict:
    """
    Score a Condition C response.
    Recomputes total from model-extracted fields, then compares against ground truth.
    """
    true_total = to_decimal(ground_truth["total"])

    # Recompute total from extracted fields
    recomputed = recompute_total_from_extraction(parsed)

    error_amount, error_pct, band = compute_error(recomputed, true_total)

    # Also capture what the model extracted as stated_total (for comparison)
    model_stated = parsed.get("stated_total")

    return {
        "model_total": str(recomputed),
        "model_raw_total": str(model_stated) if model_stated else "",
        "error_amount": str(error_amount),
        "error_pct": f"{error_pct:.4f}",
        "error_band": band,
        "exact_match": str(error_amount == 0).lower(),
        "model_flagged_inconsistency": "",
        "model_is_consistent": "",
        "model_assumptions": "",
        "recomputed_total": str(recomputed),
    }


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def get_client(model: str, ollama_url: str, vllm_url: Optional[str]) -> tuple[OpenAI, str]:
    """
    Return (OpenAI client, model_name) for a given model string.
    Models prefixed with 'vllm:' use the vLLM endpoint; others use Ollama.
    """
    if model.startswith("vllm:"):
        if not vllm_url:
            raise ValueError(f"Model {model} requires --vllm-url to be set")
        actual_model = model[5:]  # strip 'vllm:' prefix
        client = OpenAI(base_url=vllm_url, api_key="not-needed")
        return client, actual_model
    else:
        client = OpenAI(base_url=ollama_url, api_key="ollama")
        return client, model


def call_model(
    client: OpenAI,
    model_name: str,
    prompt: str,
    temperature: float = 0.0,
    max_retries: int = 2,
) -> tuple[str, float]:
    """
    Send prompt to model via OpenAI-compatible API.
    Returns (response_text, elapsed_seconds).
    Retries on transient errors.
    """
    for attempt in range(max_retries + 1):
        try:
            t0 = time.monotonic()
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=4096,
            )
            elapsed = time.monotonic() - t0
            text = response.choices[0].message.content or ""
            return text, elapsed
        except Exception as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"    Retry {attempt + 1}/{max_retries} after error: {e}")
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Single benchmark run
# ---------------------------------------------------------------------------

def run_single(
    client: OpenAI,
    model_name: str,
    model_label: str,
    invoice_id: str,
    condition: str,
    prompt_template: str,
    ground_truth: dict,
    manifest_row: dict,
    dry_run: bool = False,
) -> dict:
    """
    Run one invoice through one model under one condition.
    Returns a dict with all result fields.
    """
    # Build prompt
    invoice_text = load_invoice_text(invoice_id)
    prompt = prompt_template.replace("{invoice_text}", invoice_text)

    # Base result
    result = {
        "invoice_id": invoice_id,
        "model": model_label,
        "condition": condition,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "true_total": ground_truth["total"],
        "rendered_total": ground_truth.get("rendered_total", ground_truth["total"]),
        "is_consistent": str(ground_truth["rendered_total"] == ground_truth["total"]).lower(),
        "number_format": ground_truth["variants"]["number_format"],
        "vat_variant": ground_truth["variants"]["vat_variant"],
        "discount_variant": ground_truth["variants"]["discount_variant"],
        "layout": ground_truth["variants"]["layout"],
        "edge_case": ground_truth["variants"]["edge_case"],
    }

    if dry_run:
        # Return a placeholder result without calling the model
        result.update({
            "model_total": "", "model_raw_total": "",
            "error_amount": "", "error_pct": "", "error_band": "dry_run",
            "exact_match": "", "model_flagged_inconsistency": "",
            "model_is_consistent": "", "model_assumptions": "",
            "recomputed_total": "",
            "response_time_s": "0.0", "parse_success": "true",
            "raw_response_length": "0", "error_message": "",
        })
        return result

    # Call model
    try:
        response_text, elapsed = call_model(client, model_name, prompt)
    except Exception as e:
        result.update({
            "model_total": "", "model_raw_total": "",
            "error_amount": "", "error_pct": "", "error_band": BAND_PARSE_ERROR,
            "exact_match": "false", "model_flagged_inconsistency": "",
            "model_is_consistent": "", "model_assumptions": "",
            "recomputed_total": "",
            "response_time_s": "0.0", "parse_success": "false",
            "raw_response_length": "0",
            "error_message": f"API error: {e}",
        })
        return result

    result["response_time_s"] = f"{elapsed:.2f}"
    result["raw_response_length"] = str(len(response_text))

    # Parse response
    parsed = extract_json_from_response(response_text)
    if parsed is None:
        result.update({
            "model_total": "", "model_raw_total": "",
            "error_amount": "", "error_pct": "", "error_band": BAND_PARSE_ERROR,
            "exact_match": "false", "model_flagged_inconsistency": "",
            "model_is_consistent": "", "model_assumptions": "",
            "recomputed_total": "",
            "parse_success": "false",
            "error_message": f"JSON parse failed. First 200 chars: {response_text[:200]}",
        })
        return result

    result["parse_success"] = "true"
    result["error_message"] = ""

    # Score
    try:
        if condition == "B":
            scores = score_condition_b(parsed, ground_truth)
        else:
            scores = score_condition_c(parsed, ground_truth)
        result.update(scores)
    except Exception as e:
        result.update({
            "model_total": "", "model_raw_total": "",
            "error_amount": "", "error_pct": "", "error_band": BAND_PARSE_ERROR,
            "exact_match": "false", "model_flagged_inconsistency": "",
            "model_is_consistent": "", "model_assumptions": "",
            "recomputed_total": "",
            "error_message": f"Scoring error: {e}",
        })

    return result


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

def print_result_line(result: dict, index: int, total: int):
    """Print a compact one-line summary of a result."""
    band = result.get("error_band", "?")
    band_symbol = {
        BAND_EXACT: "  OK",
        BAND_GHOST: "  ~0",
        BAND_PLAUSIBLE: " 1-5",
        BAND_SUSPICIOUS: "5-15",
        BAND_OBVIOUS: " >15",
        BAND_PARSE_ERROR: " ERR",
        "dry_run": " DRY",
    }.get(band, "   ?")

    error_pct = result.get("error_pct", "")
    if error_pct:
        try:
            error_pct = f"{float(error_pct):+.2f}%"
        except ValueError:
            pass
    else:
        error_pct = "     "

    time_s = result.get("response_time_s", "0.0")

    print(
        f"  [{index:4d}/{total}] {result['invoice_id']}  "
        f"{result['condition']}  [{band_symbol}] {error_pct:>8s}  "
        f"{float(time_s):5.1f}s  {result['model']}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Invoice Validation Benchmark Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--models", required=True,
        help="Comma-separated model names. Prefix with 'vllm:' for remote models. "
             "E.g.: llama3.1:8b,qwen3:8b,vllm:meta-llama/Llama-3.1-8B-Instruct"
    )
    parser.add_argument(
        "--conditions", default="B,C",
        help="Comma-separated conditions to run (default: B,C)"
    )
    parser.add_argument(
        "--invoice", default=None,
        help="Run only this invoice ID (e.g. INV-2026-0001). Default: all 200."
    )
    parser.add_argument(
        "--ollama-url", default="http://localhost:11434/v1",
        help="Ollama API base URL (default: http://localhost:11434/v1)"
    )
    parser.add_argument(
        "--vllm-url", default=None,
        help="vLLM API base URL for remote models (e.g. http://gpu-server:8000/v1)"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="Sampling temperature (default: 0.0 for deterministic output)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output CSV path. Default: results/benchmark_YYYY-MM-DD_HHMMSS.csv"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip (invoice, model, condition) combos that already exist in the output file"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't call models. Just verify data loading, prompt injection, and CSV writing."
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Seconds to wait between API calls (default: 0)"
    )

    args = parser.parse_args()

    # Parse arguments
    models = [m.strip() for m in args.models.split(",")]
    conditions = [c.strip().upper() for c in args.conditions.split(",")]
    for c in conditions:
        if c not in ("B", "C"):
            print(f"ERROR: Unknown condition '{c}'. Use B or C.")
            sys.exit(1)

    # Determine invoice list
    if args.invoice:
        invoice_ids = [args.invoice]
    else:
        invoice_ids = list_invoice_ids()

    # Load manifest and prompt templates
    manifest = load_manifest()
    prompt_templates = {}
    for c in conditions:
        prompt_templates[c] = load_prompt_template(c)

    # Determine output path
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = RESULTS_DIR / f"benchmark_{ts}.csv"

    # Load existing results for resume
    completed = set()
    if args.resume and output_path.exists():
        completed = load_completed_results(output_path)
        print(f"Resume mode: {len(completed)} existing results found in {output_path}")

    # Build work list
    work = []
    for model_label in models:
        for condition in conditions:
            for inv_id in invoice_ids:
                if (inv_id, model_label, condition) in completed:
                    continue
                work.append((model_label, condition, inv_id))

    total_calls = len(work)
    print(f"\nInvoice Validation Benchmark")
    print(f"{'=' * 50}")
    print(f"Models:     {', '.join(models)}")
    print(f"Conditions: {', '.join(conditions)}")
    print(f"Invoices:   {len(invoice_ids)}")
    print(f"Total calls: {total_calls}" + (f" ({len(completed)} skipped)" if completed else ""))
    print(f"Output:     {output_path}")
    if args.dry_run:
        print(f"MODE:       DRY RUN (no API calls)")
    print(f"{'=' * 50}\n")

    if total_calls == 0:
        print("Nothing to do.")
        return

    # Open CSV (append if resuming, write header if new)
    write_header = not output_path.exists() or not args.resume
    csv_file = open(output_path, "a" if args.resume else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=RESULT_FIELDS, extrasaction="ignore")
    if write_header:
        writer.writeheader()

    # Pre-create clients per model
    clients = {}
    for model_label in models:
        client, actual_name = get_client(model_label, args.ollama_url, args.vllm_url)
        clients[model_label] = (client, actual_name)

    # Run benchmark
    start_time = time.monotonic()
    errors = 0
    band_counts = {b: 0 for b in [BAND_EXACT, BAND_GHOST, BAND_PLAUSIBLE, BAND_SUSPICIOUS, BAND_OBVIOUS, BAND_PARSE_ERROR]}

    try:
        for i, (model_label, condition, inv_id) in enumerate(work, 1):
            client, actual_name = clients[model_label]
            gt = load_ground_truth(inv_id)
            manifest_row = manifest.get(inv_id, {})

            result = run_single(
                client=client,
                model_name=actual_name,
                model_label=model_label,
                invoice_id=inv_id,
                condition=condition,
                prompt_template=prompt_templates[condition],
                ground_truth=gt,
                manifest_row=manifest_row,
                dry_run=args.dry_run,
            )

            writer.writerow(result)
            csv_file.flush()

            # Track stats
            band = result.get("error_band", "")
            if band in band_counts:
                band_counts[band] += 1
            if band == BAND_PARSE_ERROR:
                errors += 1

            print_result_line(result, i, total_calls)

            if args.delay > 0 and i < total_calls:
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted after {i} calls. Results saved to {output_path}")
        print("Re-run with --resume to continue.")
    finally:
        csv_file.close()

    elapsed_total = time.monotonic() - start_time

    # Summary
    print(f"\n{'=' * 50}")
    print(f"SUMMARY")
    print(f"{'=' * 50}")
    print(f"Total calls:    {total_calls}")
    print(f"Total time:     {elapsed_total:.1f}s ({elapsed_total/60:.1f}min)")
    print(f"Avg per call:   {elapsed_total/max(total_calls,1):.1f}s")
    print(f"Parse errors:   {errors}")
    print()
    print(f"Error band distribution:")
    for band, count in band_counts.items():
        pct = (count / max(total_calls, 1)) * 100
        bar = "#" * int(pct / 2)
        print(f"  {band:>12s}: {count:4d} ({pct:5.1f}%)  {bar}")
    print()
    print(f"Results saved to: {output_path}")


if __name__ == "__main__":
    main()
