from __future__ import annotations

import hashlib
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-not-found]


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ROOT = ROOT / "benchmarks" / "invoice_tau" / "datasets" / "public_invoice_arithmetic_v1" / "cases"
OUTPUT_ROOT = ROOT / "evals" / "business_v1" / "cases"
TOLERANCE = Decimal("0.01")


# Values below are the amounts printed in the PDFs.  They intentionally do not
# copy hidden net values from the upstream oracle when the invoice prints gross
# (VAT-inclusive) line amounts.
CASES: dict[str, dict[str, Any]] = {
    "invoice_arithmetic_conflict_001": {
        "source": "pia_inv_2026_0002",
        "preserve_case_json": True,
        "title": "发票内部总额存在明确错误",
        "holes": ["invoice_arithmetic"],
        "status": "CONTRADICTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "CONTRADICTED",
        "lines": [
            ("2", "23279.90", "46559.80", "Travel expenses (London–Vienna) 2 23'279.90 46'559.80"),
            ("3", "7734.30", "23202.90", "Executive workshop: 'AI Strategy' 3 7'734.30 23'202.90"),
            ("1", "47421.00", "47421.00", "Travel expenses (Frankfurt–Zürich) 1 47'421.00 47'421.00"),
            ("10", "457.30", "4573.00", "Retainer fee — ongoing counsel 10 457.30 4'573.00"),
            ("1", "944.00", "944.00", "Cloud infrastructure migration — Phase 2 of 7 (estimated) 1 944.00 944.00"),
            ("100", "326.50", "32650.00", "Board meeting facilitation 100 326.50 32'650.00"),
        ],
        "subtotal": ("155350.70", "Subtotal: 155'350.70 EUR"),
        "vat": [("31070.14", "VAT: 31'070.14 EUR")],
        "adjustments": [("-3107.01", "Discount (2% early payment discount): -3'107.01 EUR")],
        "total": ("188813.24", "TOTAL: 188'813.24 EUR"),
        "component_checks": [
            {
                "id": "vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "NOT_FOUND",
                "missing": "The invoice states a VAT amount but no VAT rate.",
            },
            {
                "id": "discount_rate_base",
                "component_fact_id": "adjustment_1",
                "status": "SUPPORTED",
                "rate_factor": "0.02",
                "rate_quote": "Discount (2% early payment discount): -3'107.01 EUR",
                "base_fact_id": "printed_subtotal",
                "magnitude": "3107.01",
            },
        ],
    },
    "invoice_arithmetic_supported_0005": {
        "source": "pia_inv_2026_0005",
        "title": "单行德式发票内部计算一致",
        "holes": ["invoice_arithmetic", "single_item", "german_number_format"],
        "status": "SUPPORTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("2", "30201.70", "60403.40", "Quarterly newsletter design: 2 × 30.201,70 EUR = 60.403,40 EUR."),
        ],
        "subtotal": ("60403.40", "Subtotal: 60.403,40 EUR"),
        "vat": [("12080.68", "Statutory VAT (20%): 12.080,68 EUR")],
        "adjustments": [("-1812.10", "Less: rebate per agreement: -1.812,10 EUR")],
        "total": ("70671.98", "TOTAL: 70.671,98 EUR"),
        "component_checks": [
            {
                "id": "vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "SUPPORTED",
                "rate_factor": "0.20",
                "rate_quote": "Statutory VAT (20%): 12.080,68 EUR",
                "base_fact_id": "printed_subtotal",
            }
        ],
    },
    "invoice_subtotal_conflict_0006": {
        "framework": True,
        "source": "pia_inv_2026_0006",
        "title": "含税行金额正确但票面小计错误",
        "holes": ["invoice_arithmetic", "subtotal_integrity", "tax_inclusive_prices"],
        "status": "CONTRADICTED",
        "subtotal_status": "CONTRADICTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("1", "6404.64", "6404.64", "Marketing collateral refresh 1 6,404.64 6,404.64"),
            ("5", "1819.56", "9097.80", "Board meeting facilitation 5 1,819.56 9,097.80"),
        ],
        "subtotal": ("15507.44", "Subtotal (incl. VAT): EUR 15,507.44"),
        "vat": [],
        "adjustments": [("-645.94", "Discount (5% early payment discount): -EUR 645.94")],
        "total": ("14856.50", "TOTAL: EUR 14,856.50"),
        "context": ("tax_inclusive", "All prices include 20% VAT.", "All prices include 20% VAT."),
        "component_checks": [
            {
                "id": "discount_rate_base",
                "component_fact_id": "adjustment_1",
                "status": "NOT_FOUND",
                "rate_factor": "0.05",
                "rate_quote": "Discount (5% early payment discount): -EUR 645.94",
                "missing": "The stated discount base is not visible in the invoice.",
            }
        ],
    },
    "credit_note_arithmetic_supported_0014": {
        "source": "pia_inv_2026_0014",
        "title": "贷项通知单负数计算一致",
        "holes": ["invoice_arithmetic", "credit_note_sign"],
        "status": "SUPPORTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("10", "-1506.80", "-15068.00", "SEO audit (Q1 2026): 10 × EUR -1,506.80 = EUR -15,068.00."),
            ("4", "-736.90", "-2947.60", "Executive workshop: 'AI Strategy': 4 × EUR -736.90 = EUR -2,947.60."),
            (None, None, "-48335.50", "For Professional indemnity insurance — pro rata, we invoice EUR -48,335.50."),
        ],
        "subtotal": ("-66351.10", "Subtotal: EUR -66,351.10"),
        "vat": [("-13270.22", "VAT (20%): EUR -13,270.22")],
        "adjustments": [],
        "total": ("-79621.32", "TOTAL: EUR -79,621.32"),
        "context": ("document_type", "credit", "This document is issued as a credit note."),
        "semantic_milestone": "credit_note",
        "component_checks": [
            {
                "id": "vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "SUPPORTED",
                "rate_factor": "0.20",
                "rate_quote": "VAT (20%): EUR -13,270.22",
                "base_fact_id": "printed_subtotal",
            }
        ],
    },
    "credit_note_total_conflict_0016": {
        "source": "pia_inv_2026_0016",
        "title": "贷项通知单最终金额错误",
        "holes": ["invoice_arithmetic", "credit_note_sign", "total_reconciliation"],
        "status": "CONTRADICTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "CONTRADICTED",
        "lines": [
            ("2", "-42070.90", "-84141.80", "Software licence renewal — 50 seats 2 -42'070.90 -84'141.80"),
            ("100", "-184.10", "-18410.00", "Legal opinion — cross-border financing 100 -184.10 -18'410.00"),
            ("5", "-1999.40", "-9997.00", "Due-diligence memorandum — Project Thornbury 5 -1'999.40 -9'997.00"),
            ("2", "-4633.60", "-9267.20", "Consulting hours — Senior Partner 2 -4'633.60 -9'267.20"),
        ],
        "subtotal": ("-121816.00", "Subtotal: -121'816.00 EUR"),
        "vat": [("-24363.20", "VAT (20%): -24'363.20 EUR")],
        "adjustments": [],
        "total": ("-144717.41", "TOTAL: -144'717.41 EUR"),
        "context": ("document_type", "credit", "This document is issued as a credit note."),
        "semantic_milestone": "credit_note",
        "component_checks": [
            {
                "id": "vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "SUPPORTED",
                "rate_factor": "0.20",
                "rate_quote": "VAT (20%): -24'363.20 EUR",
                "base_fact_id": "printed_subtotal",
            }
        ],
    },
    "reverse_charge_arithmetic_supported_0020": {
        "source": "pia_inv_2026_0020",
        "title": "反向征税发票内部计算一致",
        "holes": ["invoice_arithmetic", "reverse_charge"],
        "status": "SUPPORTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("1", "19961.00", "19961.00", "Travel expenses (Frankfurt–Zürich) 1 19,961.00 19,961.00"),
            ("5", "793.50", "3967.50", "Database optimisation retainer — March 5 793.50 3,967.50"),
            ("4", "37716.10", "150864.40", "Retainer fee — ongoing counsel 4 37,716.10 150,864.40"),
            ("4", "33219.10", "132876.40", "Server maintenance — March 2026 4 33,219.10 132,876.40"),
            ("1", "12345.40", "12345.40", "Consulting hours — Senior Partner 1 12,345.40 12,345.40"),
            ("5", "203.10", "1015.50", "Annual subscription — enterprise tier (unused features included) 5 203.10 1,015.50"),
        ],
        "subtotal": ("321030.20", "Subtotal: EUR 321,030.20"),
        "vat": [],
        "adjustments": [],
        "total": ("321030.20", "TOTAL: EUR 321,030.20"),
        "context": (
            "tax_treatment",
            "reverse charge",
            "VAT reverse charge mechanism applies per Article 196 Council Directive 2006/112/EC.",
        ),
        "semantic_milestone": "reverse_charge",
        "component_checks": [],
    },
    "invoice_total_conflict_0025": {
        "framework": True,
        "source": "pia_inv_2026_0025",
        "title": "单行瑞士数值发票最终金额错误",
        "holes": ["invoice_arithmetic", "single_item", "total_reconciliation", "swiss_number_format"],
        "status": "CONTRADICTED",
        "subtotal_status": "SUPPORTED",
        "total_status": "CONTRADICTED",
        "lines": [
            ("1", "11303.20", "11303.20", "Legal opinion — cross-border financing 1 11'303.20 11'303.20"),
        ],
        "subtotal": ("11303.20", "Subtotal: 11'303.20 EUR"),
        "vat": [("2260.64", "Statutory VAT (20%): 2'260.64 EUR")],
        "adjustments": [],
        "total": ("13156.92", "TOTAL: 13'156.92 EUR"),
        "component_checks": [
            {
                "id": "vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "SUPPORTED",
                "rate_factor": "0.20",
                "rate_quote": "Statutory VAT (20%): 2'260.64 EUR",
                "base_fact_id": "printed_subtotal",
            }
        ],
    },
    "mixed_vat_subtotal_conflict_0044": {
        "source": "pia_inv_2026_0044",
        "title": "混合税率发票票面小计错误",
        "holes": ["invoice_arithmetic", "mixed_vat", "subtotal_integrity"],
        "status": "CONTRADICTED",
        "subtotal_status": "CONTRADICTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("2", "334.60", "669.20", "Data room hosting — February 2026: 2 × 334.60 EUR = 669.20 EUR."),
            (None, None, "46049.70", "For Penetration testing — scope per SOW 2026-03, we invoice 46'049.70 EUR."),
            ("10", "281.20", "2812.00", "Consulting hours — Associate: 10 × 281.20 EUR = 2'812.00 EUR."),
            ("3", "16585.80", "49757.40", "Training materials — onboarding programme: 3 × 16'585.80 EUR = 49'757.40 EUR."),
            (None, None, "18889.60", "For Backup verification (annual), we invoice 18'889.60 EUR."),
        ],
        "subtotal": ("118167.90", "Subtotal: 118'167.90 EUR"),
        "vat": [
            ("13863.24", "VAT (20%): 13'863.24 EUR"),
            ("4886.17", "VAT (10%): 4'886.17 EUR"),
        ],
        "adjustments": [
            ("-8863.34", "adjustment per framework agreement ref. FA-2021-856 (-7.5%): -8'863.34 EUR")
        ],
        "total": ("128063.97", "TOTAL: 128'063.97 EUR"),
        "component_checks": [
            {
                "id": "vat_20_rate_base",
                "component_fact_id": "vat_1",
                "status": "NOT_FOUND",
                "rate_factor": "0.20",
                "rate_quote": "VAT (20%): 13'863.24 EUR",
                "missing": "The 20% VAT allocation and taxable base are not visible in the invoice.",
            },
            {
                "id": "vat_10_rate_base",
                "component_fact_id": "vat_2",
                "status": "NOT_FOUND",
                "rate_factor": "0.10",
                "rate_quote": "VAT (10%): 4'886.17 EUR",
                "missing": "The 10% VAT allocation and taxable base are not visible in the invoice.",
            },
            {
                "id": "adjustment_rate_base",
                "component_fact_id": "adjustment_1",
                "status": "NOT_FOUND",
                "rate_factor": "0.075",
                "rate_quote": "adjustment per framework agreement ref. FA-2021-856 (-7.5%): -8'863.34 EUR",
                "missing": "The framework adjustment base is not visible in the invoice.",
            },
        ],
    },
    "tax_inclusive_arithmetic_supported_0053": {
        "framework": True,
        "source": "pia_inv_2026_0053",
        "title": "含税德式行金额与框架调整计算一致",
        "holes": ["invoice_arithmetic", "tax_inclusive_prices", "german_number_format"],
        "status": "NOT_FOUND",
        "subtotal_status": "SUPPORTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("1", "57708.12", "57708.12", "Executive workshop (2 days) 1 57.708,12 57.708,12"),
            ("3", "22092.36", "66277.08", "Executive workshop: 'AI Strategy' 3 22.092,36 66.277,08"),
            ("20", "562.44", "11248.80", "Penetration testing — scope per SOW 2026-03 20 562,44 11.248,80"),
        ],
        "subtotal": ("135234.00", "The subtotal including VAT is 135.234,00 EUR."),
        "vat": [],
        "adjustments": [
            ("-2817.38", "adjustment per framework agreement ref. FA-2024-136: -2.5% (2.817,38 EUR).")
        ],
        "total": ("132416.62", "The total amount due is 132.416,62 EUR."),
        "context": ("tax_inclusive", "including VAT", "The subtotal including VAT is 135.234,00 EUR."),
        "component_checks": [
            {
                "id": "adjustment_rate_base",
                "component_fact_id": "adjustment_1",
                "status": "NOT_FOUND",
                "rate_factor": "0.025",
                "rate_quote": "adjustment per framework agreement ref. FA-2024-136: -2.5% (2.817,38 EUR).",
                "missing": "The framework adjustment base is not visible in the invoice.",
            }
        ],
    },
    "mixed_vat_arithmetic_supported_0062": {
        "source": "pia_inv_2026_0062",
        "title": "混合税率汇总发票内部计算一致",
        "holes": ["invoice_arithmetic", "mixed_vat", "swiss_number_format"],
        "status": "NOT_FOUND",
        "subtotal_status": "SUPPORTED",
        "total_status": "SUPPORTED",
        "lines": [
            ("1", "6603.80", "6603.80", "Documentation cleanup (Sisyphean endeavour) 1 6'603.80 6'603.80"),
            ("1", "25458.00", "25458.00", "Data room hosting — February 2026 1 25'458.00 25'458.00"),
            ("1", "38752.20", "38752.20", "Training materials — onboarding programme 1 38'752.20 38'752.20"),
            ("10", "1167.80", "11678.00", "Regulatory filing — Companies House 10 1'167.80 11'678.00"),
            ("2", "1802.60", "3605.20", "Translation services (EN ↔ DE, 12k words) 2 1'802.60 3'605.20"),
            ("100", "176.10", "17610.00", "Consulting hours — Associate 100 176.10 17'610.00"),
            ("1", "26462.80", "26462.80", "Hardware procurement — 4 workstations 1 26'462.80 26'462.80"),
        ],
        "subtotal": ("130170.00", "The subtotal is 130'170.00 EUR."),
        "vat": [("23027.20", "Statutory VAT at mixed rates is applied, totalling 23'027.20 EUR.")],
        "adjustments": [],
        "total": ("153197.20", "The total amount due is 153'197.20 EUR."),
        "component_checks": [
            {
                "id": "mixed_vat_rate_base",
                "component_fact_id": "vat_1",
                "status": "NOT_FOUND",
                "missing": "The mixed VAT rates, allocations and taxable bases are not visible in the invoice.",
            }
        ],
    },
}


def _fact(
    fact_id: str,
    value: str,
    *,
    origin: str,
    quote: str = "",
    currency: str = "",
    policy_ref: str = "",
    kind: str = "decimal",
    required_in: list[str] | None = None,
    tolerance: str = "0",
    predicate_options: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": fact_id,
        "origin": origin,
        "kind": kind,
        "value": value,
        "required_in": required_in or [],
    }
    if tolerance != "0":
        item["tolerance"] = tolerance
    if currency:
        item["currency"] = currency
    if origin == "source":
        options = predicate_options or _predicate_options(fact_id)
        if not options:
            raise ValueError(f"source fact {fact_id!r} requires predicate options")
        item.update(source_role="invoice", source_quote=quote, predicate_options=options)
    if origin == "policy":
        item["policy_ref"] = policy_ref
    return item


def _predicate_options(fact_id: str) -> list[str]:
    if fact_id.endswith("_quantity"):
        return ["quantity", "qty", "line quantity", "数量"]
    if fact_id.endswith("_unit_price"):
        return ["unit price", "price per unit", "单价"]
    if fact_id.endswith("_extension"):
        return [
            "line extension",
            "line item extension",
            "extended amount",
            "line amount",
            "line total",
            "行金额",
        ]
    if fact_id == "printed_subtotal":
        return ["printed subtotal", "stated subtotal", "subtotal", "票面小计", "小计"]
    if fact_id.startswith("vat_") and fact_id.endswith("_rate_factor"):
        return ["VAT rate", "tax rate", "增值税率", "税率"]
    if fact_id.startswith("vat_"):
        return ["VAT amount", "tax amount", "VAT", "增值税额", "税额"]
    if fact_id.startswith("adjustment_") and fact_id.endswith("_rate_factor"):
        return ["adjustment rate", "discount rate", "调整比例", "折扣率"]
    if fact_id.startswith("adjustment_"):
        return [
            "adjustment amount",
            "discount amount",
            "rebate amount",
            "adjustment",
            "discount",
            "rebate",
            "调整金额",
            "折扣金额",
        ]
    if fact_id == "printed_total":
        return [
            "printed total",
            "final total",
            "total amount",
            "amount due",
            "票面总额",
            "最终金额",
            "应付金额",
        ]
    if fact_id == "document_type":
        return ["document type", "credit note type", "单据类型", "贷项类型"]
    if fact_id == "tax_treatment":
        return ["tax treatment", "reverse charge treatment", "税务处理", "反向征税处理"]
    if fact_id == "tax_inclusive":
        return ["tax inclusive treatment", "VAT inclusive prices", "含税口径", "含税价格"]
    return [fact_id.replace("_", " ")]


def _money(value: Decimal) -> str:
    return format(value, ".2f")


def _meaning(*groups: list[str]) -> dict[str, list[list[str]]]:
    return {"all_of": list(groups)}


def _build_oracle(case_id: str, spec: dict[str, Any], visible_text: str) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    line_fact_ids: list[str] = []
    line_amount_ids: list[str] = []
    line_relation_ids: list[str] = []

    for index, (quantity, unit_price, amount, quote) in enumerate(spec["lines"], start=1):
        amount_id = f"line_{index}_extension"
        if quantity is not None and unit_price is not None:
            quantity_id = f"line_{index}_quantity"
            unit_id = f"line_{index}_unit_price"
            facts.append(_fact(quantity_id, quantity, origin="source", quote=quote, required_in=["evidence"]))
            facts.append(
                _fact(unit_id, unit_price, origin="source", quote=quote, currency="EUR", required_in=["evidence"])
            )
            line_fact_ids.extend([quantity_id, unit_id])
            relation_id = f"line_{index}_extension_math"
            relations.append(
                {
                    "id": relation_id,
                    "operation": "multiply",
                    "input_fact_ids": [quantity_id, unit_id],
                    "output_fact_id": amount_id,
                }
            )
            line_relation_ids.append(relation_id)
        facts.append(
            _fact(amount_id, amount, origin="source", quote=quote, currency="EUR", required_in=["evidence"])
        )
        line_fact_ids.append(amount_id)
        line_amount_ids.append(amount_id)

    printed_subtotal, subtotal_quote = spec["subtotal"]
    subtotal_required = ["evidence", "reasoning", "report"]
    if spec["subtotal_status"] == "CONTRADICTED":
        subtotal_required.append("reply")
    facts.append(
        _fact(
            "printed_subtotal",
            printed_subtotal,
            origin="source",
            quote=subtotal_quote,
            currency="EUR",
            required_in=subtotal_required,
        )
    )

    source_line_sum = sum((Decimal(item[2]) for item in spec["lines"]), Decimal("0"))
    subtotal_relation_ids: list[str] = []
    subtotal_fact_ids = [*line_amount_ids, "printed_subtotal"]
    if len(line_amount_ids) > 1:
        if spec["subtotal_status"] == "SUPPORTED":
            relations.append(
                {
                    "id": "subtotal_math",
                    "operation": "sum",
                    "input_fact_ids": line_amount_ids,
                    "output_fact_id": "printed_subtotal",
                }
            )
            subtotal_relation_ids.append("subtotal_math")
            trusted_subtotal_id = "printed_subtotal"
        else:
            facts.append(
                _fact(
                    "line_sum",
                    _money(source_line_sum),
                    origin="derived",
                    currency="EUR",
                    required_in=["reasoning", "reply", "report"],
                )
            )
            relations.append(
                {
                    "id": "line_sum_math",
                    "operation": "sum",
                    "input_fact_ids": line_amount_ids,
                    "output_fact_id": "line_sum",
                }
            )
            subtotal_relation_ids.append("line_sum_math")
            subtotal_fact_ids.append("line_sum")
            trusted_subtotal_id = "line_sum"
    else:
        trusted_subtotal_id = line_amount_ids[0]

    subtotal_difference = abs(source_line_sum - Decimal(printed_subtotal))
    if len(line_amount_ids) == 1 or spec["subtotal_status"] == "CONTRADICTED":
        facts.append(
            _fact(
                "subtotal_difference",
                _money(subtotal_difference),
                origin="derived",
                currency="EUR",
                required_in=(
                    ["reasoning", "reply", "report"]
                    if spec["subtotal_status"] == "CONTRADICTED"
                    else ["reasoning"]
                ),
            )
        )
        relations.append(
            {
                "id": "subtotal_difference_math",
                "operation": "absolute_difference",
                "input_fact_ids": [trusted_subtotal_id, "printed_subtotal"],
                "output_fact_id": "subtotal_difference",
            }
        )
        subtotal_relation_ids.append("subtotal_difference_math")
        subtotal_fact_ids.append("subtotal_difference")

    needs_tolerance = spec["subtotal_status"] == "CONTRADICTED" or spec["total_status"] == "CONTRADICTED"
    if needs_tolerance:
        facts.append(
            _fact(
                "rounding_tolerance",
                "0.01",
                origin="policy",
                currency="EUR",
                policy_ref="invoice_calculation_rounding_tolerance",
                required_in=["reasoning"],
            )
        )
    if spec["subtotal_status"] == "CONTRADICTED":
        relations.append(
            {
                "id": "subtotal_difference_exceeds_tolerance",
                "operation": "greater_than",
                "input_fact_ids": ["subtotal_difference", "rounding_tolerance"],
                "expected_boolean": True,
            }
        )
        subtotal_relation_ids.append("subtotal_difference_exceeds_tolerance")
        subtotal_fact_ids.append("rounding_tolerance")

    component_ids: list[str] = []
    component_values: list[Decimal] = []
    for index, (value, quote) in enumerate(spec["vat"], start=1):
        fact_id = f"vat_{index}"
        facts.append(
            _fact(
                fact_id,
                value,
                origin="source",
                quote=quote,
                currency="EUR",
                required_in=["evidence", "reasoning", "report"],
            )
        )
        component_ids.append(fact_id)
        component_values.append(Decimal(value))
    for index, (value, quote) in enumerate(spec["adjustments"], start=1):
        fact_id = f"adjustment_{index}"
        facts.append(
            _fact(
                fact_id,
                value,
                origin="source",
                quote=quote,
                currency="EUR",
                required_in=["evidence", "reasoning", "report"],
            )
        )
        component_ids.append(fact_id)
        component_values.append(Decimal(value))

    component_fact_ids: list[str] = []
    component_relation_ids: list[str] = []
    component_statuses: list[str] = []
    for check in spec.get("component_checks", []):
        component_fact_id = check["component_fact_id"]
        if component_fact_id not in component_ids:
            raise ValueError(f"{case_id}: unknown component fact in check: {component_fact_id}")
        component_fact_ids.append(component_fact_id)
        component_statuses.append(check["status"])
        rate_factor = check.get("rate_factor")
        rate_fact_id = ""
        if rate_factor:
            rate_fact_id = f"{component_fact_id}_rate_factor"
            facts.append(
                _fact(
                    rate_fact_id,
                    rate_factor,
                    origin="source",
                    quote=check["rate_quote"],
                    required_in=["evidence", "reasoning"],
                )
            )
            component_fact_ids.append(rate_fact_id)
        base_fact_id = check.get("base_fact_id", "")
        if base_fact_id:
            component_fact_ids.append(base_fact_id)
        if check["status"] != "SUPPORTED":
            continue
        if not rate_fact_id or not base_fact_id:
            raise ValueError(f"{case_id}: supported component check requires a visible rate and base")
        output_fact_id = component_fact_id
        component_value = next(
            value for key, value in zip(component_ids, component_values) if key == component_fact_id
        )
        if component_fact_id.startswith("adjustment_") and component_value < 0:
            output_fact_id = f"{component_fact_id}_expected_magnitude"
            magnitude = check.get("magnitude", _money(abs(component_value)))
            facts.append(
                _fact(
                    output_fact_id,
                    magnitude,
                    origin="derived",
                    currency="EUR",
                    tolerance="0.01",
                    required_in=["reasoning"],
                )
            )
            component_fact_ids.append(output_fact_id)
        relation_id = f"{check['id']}_math"
        relations.append(
            {
                "id": relation_id,
                "operation": "multiply",
                "input_fact_ids": [base_fact_id, rate_fact_id],
                "output_fact_id": output_fact_id,
            }
        )
        component_relation_ids.append(relation_id)

    component_status = ""
    if component_statuses:
        component_status = (
            "CONTRADICTED"
            if "CONTRADICTED" in component_statuses
            else "NOT_FOUND"
            if "NOT_FOUND" in component_statuses
            else "SUPPORTED"
        )

    printed_total, total_quote = spec["total"]
    facts.append(
        _fact(
            "printed_total",
            printed_total,
            origin="source",
            quote=total_quote,
            currency="EUR",
            required_in=["evidence", "reasoning", "reply", "report"],
        )
    )
    total_inputs = [trusted_subtotal_id, *component_ids]
    recomputed_total = source_line_sum + sum(component_values, Decimal("0"))
    final_fact_ids = [trusted_subtotal_id, *component_ids, "printed_total"]
    final_relation_ids: list[str] = []
    printed_path_fact_ids: list[str] = []
    printed_path_relation_ids: list[str] = []
    if spec["subtotal_status"] == "CONTRADICTED":
        if spec["total_status"] != "SUPPORTED":
            raise ValueError(f"{case_id}: subtotal-conflict fixture must isolate the downstream total path")
        relations.append(
            {
                "id": "line_derived_final_total_math",
                "operation": "sum",
                "input_fact_ids": total_inputs,
                "output_fact_id": "printed_total",
            }
        )
        final_relation_ids.append("line_derived_final_total_math")

        printed_path_total = Decimal(printed_subtotal) + sum(component_values, Decimal("0"))
        facts.extend(
            [
                _fact(
                    "printed_subtotal_path_total",
                    _money(printed_path_total),
                    origin="derived",
                    currency="EUR",
                    required_in=["reasoning"],
                ),
                _fact(
                    "printed_subtotal_path_difference",
                    _money(abs(printed_path_total - Decimal(printed_total))),
                    origin="derived",
                    currency="EUR",
                    required_in=["reasoning"],
                ),
            ]
        )
        relations.extend(
            [
                {
                    "id": "printed_subtotal_path_math",
                    "operation": "sum",
                    "input_fact_ids": ["printed_subtotal", *component_ids],
                    "output_fact_id": "printed_subtotal_path_total",
                },
                {
                    "id": "printed_subtotal_path_difference_math",
                    "operation": "absolute_difference",
                    "input_fact_ids": ["printed_subtotal_path_total", "printed_total"],
                    "output_fact_id": "printed_subtotal_path_difference",
                },
                {
                    "id": "printed_subtotal_path_difference_exceeds_tolerance",
                    "operation": "greater_than",
                    "input_fact_ids": ["printed_subtotal_path_difference", "rounding_tolerance"],
                    "expected_boolean": True,
                },
            ]
        )
        printed_path_fact_ids = [
            "printed_subtotal",
            *component_ids,
            "printed_subtotal_path_total",
            "printed_total",
            "printed_subtotal_path_difference",
            "rounding_tolerance",
        ]
        printed_path_relation_ids = [
            "printed_subtotal_path_math",
            "printed_subtotal_path_difference_math",
            "printed_subtotal_path_difference_exceeds_tolerance",
        ]
    elif spec["total_status"] == "SUPPORTED":
        if len(total_inputs) > 1:
            relations.append(
                {
                    "id": "final_total_math",
                    "operation": "sum",
                    "input_fact_ids": total_inputs,
                    "output_fact_id": "printed_total",
                }
            )
            final_relation_ids.append("final_total_math")
        else:
            facts.append(
                _fact("final_difference", "0.00", origin="derived", currency="EUR", required_in=["reasoning"])
            )
            relations.append(
                {
                    "id": "final_difference_math",
                    "operation": "absolute_difference",
                    "input_fact_ids": [trusted_subtotal_id, "printed_total"],
                    "output_fact_id": "final_difference",
                }
            )
            final_fact_ids.append("final_difference")
            final_relation_ids.append("final_difference_math")
    else:
        facts.append(
            _fact(
                "recomputed_total",
                _money(recomputed_total),
                origin="derived",
                currency="EUR",
                required_in=["reasoning", "reply", "report"],
            )
        )
        relations.append(
            {
                "id": "recomputed_total_math",
                "operation": "sum",
                "input_fact_ids": total_inputs,
                "output_fact_id": "recomputed_total",
            }
        )
        total_difference = abs(Decimal(printed_total) - recomputed_total)
        facts.append(
            _fact(
                "total_difference",
                _money(total_difference),
                origin="derived",
                currency="EUR",
                required_in=["reasoning", "reply", "report"],
            )
        )
        relations.extend(
            [
                {
                    "id": "total_difference_math",
                    "operation": "absolute_difference",
                    "input_fact_ids": ["printed_total", "recomputed_total"],
                    "output_fact_id": "total_difference",
                },
                {
                    "id": "total_difference_exceeds_tolerance",
                    "operation": "greater_than",
                    "input_fact_ids": ["total_difference", "rounding_tolerance"],
                    "expected_boolean": True,
                },
            ]
        )
        final_fact_ids.extend(["recomputed_total", "total_difference", "rounding_tolerance"])
        final_relation_ids.extend(
            ["recomputed_total_math", "total_difference_math", "total_difference_exceeds_tolerance"]
        )

    context_id = ""
    if spec.get("context"):
        context_id, value, quote = spec["context"]
        facts.append(
            _fact(context_id, value, origin="source", quote=quote, kind="text", required_in=["evidence"])
        )
        final_fact_ids.append(context_id)

    milestones: list[dict[str, Any]] = [
        {
            "id": "line_extensions",
            "facet_ref": "line_extensions",
            "statement_meaning": _meaning(
                ["line item", "line items", "行项目"],
                ["quantity", "qty", "数量"],
                ["unit price", "单价"],
                [
                    "line extension",
                    "line extensions",
                    "line item extension",
                    "line item extensions",
                    "extended amount",
                    "extended amounts",
                    "extension amount",
                    "extension amounts",
                    "line amount",
                    "line amounts",
                    "line total",
                    "line totals",
                    "stated line",
                    "行金额",
                ],
                ["multiply", "multiplied", "product", "times", "×", "乘", "复算"],
            ),
            "expected_status": "SUPPORTED",
            "fact_ids": line_fact_ids,
            "relation_ids": line_relation_ids,
        },
        {
            "id": "subtotal_aggregation",
            "facet_ref": "subtotal_aggregation",
            "statement_meaning": _meaning(
                [
                    "line extension",
                    "line extensions",
                    "line item extension",
                    "line item extensions",
                    "extended amount",
                    "extended amounts",
                    "extension amount",
                    "extension amounts",
                    "line amount",
                    "line amounts",
                    "line total",
                    "line totals",
                    "行金额",
                ],
                ["sum", "total", "add", "added", "plus", "aggregate", "合计", "求和", "加总"],
                ["subtotal", "小计"],
                ["equal", "equals", "match", "一致", "核对"],
            ),
            "expected_status": spec["subtotal_status"],
            "fact_ids": list(dict.fromkeys(subtotal_fact_ids)),
            "relation_ids": subtotal_relation_ids,
        },
    ]
    if component_status:
        milestones.append(
            {
                "id": "stated_component_rate_base_validation",
                "facet_ref": "stated_components",
                "statement_meaning": _meaning(
                    ["tax", "VAT", "discount", "adjustment", "component", "税费", "增值税", "折扣", "调整"],
                    [
                        "calculated",
                        "calculation",
                        "correctly",
                        "multiply",
                        "times",
                        "×",
                        "apply",
                        "recompute",
                        "计算",
                        "乘",
                        "适用",
                        "复算",
                    ],
                ),
                "expected_status": component_status,
                "fact_ids": list(dict.fromkeys(component_fact_ids)),
                "relation_ids": component_relation_ids,
            }
        )
        if spec["status"] == "NOT_FOUND" and component_status == "NOT_FOUND":
            milestones[-1]["missing_meaning"] = _meaning(
                ["tax", "VAT", "discount", "adjustment", "component", "税", "增值税", "折扣", "调整"],
                ["rate", "base", "allocation", "calculation basis", "税率", "基数", "分摊", "计算依据"],
                [
                    "missing",
                    "not visible",
                    "not provided",
                    "cannot verify",
                    "insufficient",
                    "缺失",
                    "未显示",
                    "未提供",
                    "无法核验",
                    "证据不足",
                ],
            )

    total_meaning = _meaning(
        ["printed final total", "printed total", "final total", "amount due", "票面总额", "最终金额", "应付金额"],
        ["subtotal", "line-derived", "line item", "小计", "行项目汇总"],
        ["tax", "VAT", "discount", "adjustment", "reverse charge", "component", "税", "折扣", "调整", "反向征税"],
        ["equal", "equals", "reconcile", "match", "plus", "add", "一致", "吻合", "核对", "重算", "加总"],
    )
    if spec["subtotal_status"] == "CONTRADICTED":
        milestones.extend(
            [
                {
                    "id": "printed_subtotal_total_reconciliation",
                    "facet_ref": "final_total",
                    "statement_meaning": _meaning(
                        ["printed subtotal", "stated subtotal", "票面小计"],
                        ["tax", "VAT", "discount", "adjustment", "component", "税", "折扣", "调整"],
                        ["printed total", "final total", "amount due", "票面总额", "最终金额", "应付金额"],
                        ["reconcile", "match", "plus", "add", "一致", "吻合", "核对", "加总"],
                    ),
                    "expected_status": "CONTRADICTED",
                    "fact_ids": list(dict.fromkeys(printed_path_fact_ids)),
                    "relation_ids": printed_path_relation_ids,
                },
                {
                    "id": "line_derived_total_reconciliation",
                    "facet_ref": "final_total",
                    "statement_meaning": total_meaning,
                    "expected_status": "SUPPORTED",
                    "fact_ids": list(dict.fromkeys(final_fact_ids)),
                    "relation_ids": final_relation_ids,
                },
            ]
        )
    else:
        milestones.append(
            {
                "id": "final_total_reconciliation",
                "facet_ref": "final_total",
                "statement_meaning": total_meaning,
                "expected_status": spec["total_status"],
                "fact_ids": list(dict.fromkeys(final_fact_ids)),
                "relation_ids": final_relation_ids,
            }
        )
    if spec.get("semantic_milestone") == "credit_note":
        milestones.append(
            {
                "id": "credit_note_sign_semantics",
                "facet_ref": "final_total",
                "statement_meaning": _meaning(
                    ["credit note", "贷项", "贷记"],
                    ["negative", "sign", "reversal", "负数", "符号", "冲销"],
                    ["calculation", "treatment", "interpret", "计算", "处理", "理解"],
                ),
                "expected_status": "SUPPORTED",
                "fact_ids": [context_id, "printed_subtotal", "printed_total"],
                "relation_ids": [],
            }
        )
    if spec.get("semantic_milestone") == "reverse_charge":
        milestones.append(
            {
                "id": "reverse_charge_arithmetic_treatment",
                "facet_ref": "stated_components",
                "statement_meaning": _meaning(
                    ["reverse charge", "反向征税"],
                    ["VAT", "tax", "增值税", "税"],
                    ["subtotal", "total", "小计", "总额"],
                    ["calculation", "treatment", "计算", "处理"],
                ),
                "expected_status": "SUPPORTED",
                "fact_ids": [context_id, "printed_subtotal", "printed_total"],
                "relation_ids": [],
            }
        )

    for fact in facts:
        if fact["origin"] == "source":
            quote = fact["source_quote"]
            if _normalize(quote) not in visible_text:
                raise ValueError(f"{case_id}: source quote is not visible in PDF: {quote!r}")
            if fact["kind"] == "decimal" and not _quote_contains_decimal(quote, Decimal(fact["value"])):
                raise ValueError(f"{case_id}: source value {fact['value']} is not present in quote: {quote!r}")

    observed_subtotal_conflict = subtotal_difference > TOLERANCE
    observed_total_conflict = abs(Decimal(printed_total) - recomputed_total) > TOLERANCE
    if observed_subtotal_conflict != (spec["subtotal_status"] == "CONTRADICTED"):
        raise ValueError(f"{case_id}: subtotal status does not match visible arithmetic")
    if observed_total_conflict != (spec["total_status"] == "CONTRADICTED"):
        raise ValueError(f"{case_id}: total status does not match visible arithmetic")
    observed_root = (
        "CONTRADICTED"
        if observed_subtotal_conflict or observed_total_conflict
        else "NOT_FOUND"
        if component_status == "NOT_FOUND"
        else "SUPPORTED"
    )
    if observed_root != spec["status"]:
        raise ValueError(f"{case_id}: root status does not match visible evidence boundary")

    contradicted = spec["status"] == "CONTRADICTED"
    not_found = spec["status"] == "NOT_FOUND"
    epistemic_boundaries = [
        {
            "id": "tax_or_discount_policy_applicability_not_proven",
            "statement_meaning": _meaning(
                ["tax", "VAT", "discount", "reverse charge", "税", "折扣", "反向征税"],
                ["legally applicable", "policy basis", "jurisdiction", "法定适用", "政策依据", "适用辖区"],
                ["correct", "approved", "valid", "正确", "批准", "有效"],
            ),
            "allowed_statuses": ["NOT_FOUND"],
        },
        {
            "id": "expected_invoice_template_not_provided",
            "statement_meaning": _meaning(
                ["template", "layout", "format", "模板", "版式", "格式"],
                ["expected", "required", "baseline", "标准", "预期", "基准"],
                ["match", "comply", "correct", "匹配", "符合", "正确"],
            ),
            "allowed_statuses": ["NOT_FOUND"],
        },
    ]
    if component_status == "NOT_FOUND":
        epistemic_boundaries.append(
            {
                "id": "component_rate_or_base_not_proven",
                "statement_meaning": _meaning(
                    ["tax", "VAT", "discount", "adjustment", "component", "税", "折扣", "调整"],
                    [
                        "rate",
                        "base",
                        "allocation",
                        "subtotal",
                        "applied to",
                        "比例",
                        "税率",
                        "基数",
                        "分摊",
                        "小计",
                    ],
                    ["calculated correctly", "validated", "correct", "计算正确", "已核验", "正确"],
                ),
                "allowed_statuses": ["NOT_FOUND"],
            }
        )

    if contradicted:
        required_conclusion_meanings = [
            "金额不一致",
            "最终金额存在不一致",
            "存在不一致",
            "存在矛盾",
            "票面金额与重算结果不符",
            "总额错误",
            "小计错误",
            "计算错误",
            "计算不一致",
            "金额冲突",
            "勾稽异常",
        ]
        opposite_conclusions = []
        if spec["subtotal_status"] == "CONTRADICTED":
            opposite_conclusions.append(
                _meaning(
                    [
                        "printed subtotal is correct",
                        "printed subtotal matches",
                        "stated subtotal is correct",
                        "票面小计正确",
                        "票面小计一致",
                        "小计核对通过",
                    ],
                )
            )
        if spec["total_status"] == "CONTRADICTED":
            opposite_conclusions.append(
                _meaning(
                    [
                        "printed final total is correct",
                        "final total matches",
                        "final total is correct",
                        "票面总额正确",
                        "票面总额一致",
                        "最终总额正确",
                        "最终金额正确",
                        "最终金额一致",
                        "总额核对通过",
                    ],
                )
            )
        forbidden_phrases = [
            "总额计算正确",
            "发票总额计算正确",
            "票面总额与重算总额一致",
            "未发现异常",
            "没有发现问题",
            "未发现总额错误",
            "不存在总额错误",
            "没有总额错误",
            "未发现计算错误",
            "不存在计算错误",
        ]
    elif not_found:
        required_conclusion_meanings = [
            "关键计算依据缺失",
            "无法完整核验",
            "现有材料不足以确认",
            "税率或计算基数缺失",
            "需要补充计算依据",
        ]
        opposite_conclusions = [
            _meaning(
                [
                    "invoice calculation is correct",
                    "internal calculation is valid",
                    "发票内部计算正确",
                    "发票计算有效性通过",
                    "整体计算正确",
                    "整体金额核对通过",
                ],
            ),
            _meaning(
                [
                    "invoice calculation is incorrect",
                    "internal calculation is contradicted",
                    "发票内部计算错误",
                    "发票计算有效性冲突",
                    "整体计算错误",
                    "整体金额核对失败",
                ],
            ),
        ]
        # NOT_FOUND is a root-level epistemic state.  Local verified or
        # contradicted leaves remain reportable, so global phrase bans would
        # incorrectly veto a faithful partial report.  Root overclaims are
        # caught by the structured opposite conclusions instead.
        forbidden_phrases = []
    else:
        required_conclusion_meanings = [
            "内部计算一致",
            "内部计算验证通过",
            "金额核对通过",
            "算术一致",
            "未发现计算不一致",
            "内部金额可勾稽",
            "各层金额能够勾稽",
            "票面金额与重算结果吻合",
        ]
        opposite_conclusions = [
            _meaning(
                [
                    "invoice calculation is incorrect",
                    "internal calculation is contradicted",
                    "发票内部计算错误",
                    "发票计算有效性冲突",
                    "整体计算错误",
                    "整体金额核对失败",
                ],
            )
        ]
        forbidden_phrases = ["结论：金额核对未通过", "结论：发票计算错误"]

    projected_status = "conflict" if contradicted else "weak" if not_found else "satisfied"
    oracle = {
        "schema_version": "2",
        "case_id": case_id,
        "oracle_version": "4",
        "sentinel": f"ORACLE_SENTINEL_DO_NOT_SEND::{case_id}",
        "facts": facts,
        "intent": {
            "objective": _meaning(
                ["invoice", "credit note", "付款材料", "发票", "贷项"],
                ["verify", "validate", "review", "核验", "审核"],
                [
                    "internal arithmetic",
                    "calculation",
                    "calculated",
                    "recompute",
                    "内部计算",
                    "算术",
                    "重算",
                ],
            ),
            "required_milestone_ids": [item["id"] for item in milestones],
        },
        "milestones": milestones,
        "relations": relations,
        "epistemic_boundaries": epistemic_boundaries,
        "requirement": {
            "requirement_id": "invoice_calculation_valid",
            "decision_status": spec["status"],
            "projected_status": projected_status,
            "blocking_obligations": not_found,
        },
        "communication": {
            "required_meanings": [
                required_conclusion_meanings,
                ["报告", "PDF"],
            ],
            "opposite_conclusions": opposite_conclusions,
            "forbidden_phrases": forbidden_phrases,
            "require_chinese": True,
            "require_report_links": True,
        },
    }
    if spec.get("framework"):
        oracle["framework"] = {
            "required_tools": [
                {"name": name, "min_calls": 1}
                for name in (
                    "read_attachment",
                    "read_source",
                    "bind_claim",
                    "compute_witness",
                    "submit_check",
                    "write_case_file",
                    "render_pdf",
                )
            ],
            "required_roles": [{"name": "report_writer", "min_calls": 1}],
            "required_approved_tools": ["write_case_file", "render_pdf"],
            "max_tool_errors": 0,
            "ordered_milestones": [
                ["read_attachment"],
                ["read_source"],
                ["bind_claim"],
                ["compute_witness"],
                ["submit_check"],
                ["role:report_writer"],
                ["approval:write_case_file"],
                ["write_case_file"],
                ["approval:render_pdf"],
                ["render_pdf"],
            ],
        }
    return oracle


def _normalize(value: str) -> str:
    return " ".join(value.split())


_NUMBER_TOKEN = re.compile(r"[-+]?\d[\d.,'’]*")


def _quote_contains_decimal(quote: str, expected: Decimal) -> bool:
    candidates: list[Decimal] = []
    for match in _NUMBER_TOKEN.finditer(quote):
        token = match.group(0).rstrip(".,").replace("'", "").replace("’", "")
        if "," in token and "." in token:
            if token.rfind(",") > token.rfind("."):
                token = token.replace(".", "").replace(",", ".")
            else:
                token = token.replace(",", "")
        elif "," in token:
            parts = token.split(",")
            token = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) == 2 else "".join(parts)
        try:
            candidates.append(Decimal(token))
        except Exception:
            continue
    if expected in candidates or (expected < 0 and "-" in quote and abs(expected) in candidates):
        return True
    percentage_points = abs(expected) * Decimal("100")
    return "%" in quote and percentage_points in {abs(candidate) for candidate in candidates}


def _pdf_visible_text(path: Path) -> str:
    with fitz.open(str(path)) as document:
        return _normalize("\n".join(page.get_text("text") for page in document))


def build() -> list[Path]:
    written: list[Path] = []
    for case_id, spec in CASES.items():
        source_dir = PUBLIC_ROOT / spec["source"]
        source_pdf = source_dir / "attachments" / "invoice.pdf"
        manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        source = manifest["sources"][0]
        attachment = manifest["attachments"][0]
        digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
        if digest != attachment["sha256"]:
            raise ValueError(f"{case_id}: public PDF hash mismatch")

        case_dir = OUTPUT_ROOT / case_id
        attachments_dir = case_dir / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_pdf, attachments_dir / "invoice.pdf")
        case = {
            "schema_version": "1",
            "case_id": case_id,
            "case_version": "1",
            "title": spec["title"],
            "suite": "atomic_dev",
            "business_holes": spec["holes"],
            "user_message": "请审核附件中的付款材料，逐项核对行项目、小计、税费或折扣以及最终金额，并生成一份中文审核报告。",
            "policy_version": "aurora_ap_lite_v1",
            "report_required": True,
            "auto_approve_tools": ["write_case_file", "render_pdf"],
            "attachments": [
                {
                    "id": "invoice",
                    "path": "attachments/invoice.pdf",
                    "sha256": digest,
                    "content_type": "application/pdf",
                    "source_url": source["canonical_url"],
                    "source_revision": source["repository_commit"],
                    "license": source["license"]["spdx"],
                }
            ],
        }
        oracle = _build_oracle(case_id, spec, _pdf_visible_text(source_pdf))
        if not spec.get("preserve_case_json"):
            (case_dir / "case.json").write_text(
                json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        (case_dir / "oracle.json").write_text(
            json.dumps(oracle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.append(case_dir)
    return written


if __name__ == "__main__":
    for path in build():
        print(path.relative_to(ROOT).as_posix())
