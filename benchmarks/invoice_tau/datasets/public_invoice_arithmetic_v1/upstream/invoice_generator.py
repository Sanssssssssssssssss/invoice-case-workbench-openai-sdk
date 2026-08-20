#!/usr/bin/env python3
"""
Invoice Generator for LLM Benchmarking Experiments
===================================================

Generates a reproducible corpus of ~200 synthetic invoices, each with a
corresponding canonical ground-truth JSON, varying along five controlled
dimensions:

  1. VAT phrasing        (explicit_included, explicit_excluded,
                          implicit_rate_stated, implicit_no_rate)
  2. Discount phrasing   (none, explicit_percentage, explicit_amount,
                          trade_terms, obfuscated)
  3. Number formatting   (english, german, swiss)
  4. Layout              (table, paragraph, mixed)
  5. Consistency         (correct, subtotal_error, total_error)

Numerical convention
--------------------
The canonical ground truth is computed in NET terms:

    subtotal        = sum(quantity_i * unit_price_i)          # net
    vat_amount      = subtotal * vat_rate                     # on net subtotal
    discount_amount = subtotal * discount_pct  (percentage)   # OR
                      fixed_value              (explicit amount)
    total           = subtotal + vat_amount - discount_amount

In other words: VAT is calculated on the full net subtotal and the discount
is subtracted from the grand total afterwards.  This matches the worked
example in the spec (7,542.50 + 1,508.50 - 377.13 = 8,673.87) and is a
common real-world pattern for early-payment / rebate discounts that sit
outside the taxable base.

All arithmetic uses Decimal with ROUND_HALF_UP to 2 decimal places.  No
floating point is used anywhere in the money pipeline.

For "VAT included" rendering, net unit prices are scaled by (1 + vat_rate)
so the rendered line items and gross subtotal remain internally consistent
with the canonical total.  Unit prices are generated in 10-cent multiples
so the scaled gross prices never drift by a rounding cent.

Usage
-----
    python invoice_generator.py --output ./output --count 200 --seed 42
    python invoice_generator.py --output ./output --verify
    python invoice_generator.py --output ./output --stats

Runs with --verify and --stats may be combined with generation.  When run
without --count, the script expects an existing output directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any, Optional

getcontext().prec = 28
TWO_PLACES = Decimal("0.01")


# =============================================================================
# Decimal helpers
# =============================================================================

def round2(x: Decimal) -> Decimal:
    """Round a Decimal to two decimal places using ROUND_HALF_UP."""
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def D(x: Any) -> Decimal:
    """Build a Decimal safely from int/str/Decimal (avoids binary-float loss)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


# =============================================================================
# Data pools
# =============================================================================

# Each vendor is (display_name, ISO country code).  Country drives address,
# bank, IBAN format, and whether the invoice renders in English or German.
VENDORS: list[tuple[str, str]] = [
    ("Pierce & Pierce Holdings Ltd", "GB"),
    ("Vandelay Industries GmbH", "AT"),
    ("Praxis Consulting Group AG", "AT"),
    ("ACME Laboratories International S.A.", "FR"),
    ("Prestige Worldwide Ltd", "GB"),
    ("Sterling Cooper Draper Pryce", "GB"),
    ("Bluth Development Company LLC", "GB"),
    ("Hooli Enterprise Solutions GmbH", "DE"),
    ("Rekall Incorporated", "GB"),
    ("Cyberdyne Systems Austria GmbH", "AT"),
    ("Wonka Industries AG", "CH"),
    ("Tyrell Corporation Europe S.à r.l.", "FR"),
    ("Initech Solutions GmbH", "DE"),
    ("Stark Industries Europe Ltd", "GB"),
    ("Oscorp Technologies AG", "DE"),
    ("Nakatomi Trading Corporation", "GB"),
    ("Soylent GmbH & Co. KG", "DE"),
    ("Weyland-Yutani Europe S.A.", "FR"),
    ("Dunder Mifflin Europe GmbH", "DE"),
    ("Gekko & Associates Capital Advisory", "GB"),
    ("Stratton Oakmont Partners AG", "CH"),
    ("Sabre International GmbH", "AT"),
    ("Massive Dynamic Europe Ltd", "GB"),
    ("InGen BioSciences S.A.", "FR"),
    ("Umbrella Corporation Europe AG", "CH"),
    ("Goliath National Bank", "GB"),
    ("Wayne Enterprises", "GB"),
    ("Planet Express", "GB"),
    ("The Daily Planet", "GB"),
    ("Duff Beverages Ltd.", "GB"),
    ("Los Pollos Hermanos Europe LLC", "GB"),
    ("Pearson Hardman", "GB"),
]

RECIPIENTS: list[tuple[str, str]] = [
    ("Mittelstand Digital Solutions GmbH", "DE"),
    ("Europäische Verwaltungskontor AG", "AT"),
    ("Nordic Lighthouse Infrastructure AS", "NO"),
    ("Groupe Industriel Dufresne S.A.", "FR"),
    ("Bergkristall Maschinenbau GmbH", "DE"),
    ("Thames Gate Capital Partners LLP", "GB"),
    ("Instituto Meridiano Tecnológico S.L.", "ES"),
    ("Donaustadt Digitalisierung GmbH", "AT"),
    ("Rheintal Infrastruktur Holding AG", "AT"),
    ("Benelux Windmill Automation B.V.", "NL"),
    ("Caledonian Moor Pension Management Ltd", "GB"),
    ("Alpenvista Kredit AG", "CH"),
    ("Société Continentale de Logistique S.A.", "FR"),
    ("Helvetica Technische Beratung AG", "CH"),
    ("Baltic Amber Services OÜ", "EE"),
]

# Fully fictitious addresses per country (street, postal code, city).
# Real cities and realistic postal-code formats; the STREET NAMES ARE
# INVENTED — they should not match any known real street.  Country-
# specific patterns (`-gasse`, `-straße`, `gate`, `rue de…`, etc.) are
# preserved so the addresses still look plausible at a glance.
ADDRESSES: dict[str, list[tuple[str, str, str]]] = {
    "AT": [
        ("Stummfuchsgasse 47", "1100", "Wien"),
        ("Nebelkaiserweg 128", "1070", "Wien"),
        ("Silberknopfstraße 14", "8010", "Graz"),
        ("Feldspatplatz 9", "5020", "Salzburg"),
        ("Holunderried 4", "1020", "Wien"),
        ("Zinnpfeifferallee 33", "1080", "Wien"),
    ],
    "DE": [
        ("Eichkätzchenweg 12", "40990", "Düsseldorf"),
        ("Beispielgasse 34", "80331", "München"),
        ("Haselgrundstraße 78", "10115", "Berlin"),
        ("Roggenfeldallee 42", "60311", "Frankfurt am Main"),
        ("Kupferschmiedsweg 17", "20354", "Hamburg"),
        ("Nachtvioleplatz 203", "10405", "Berlin"),
    ],
    "GB": [
        ("7 Merrowdale Lane", "EC2V 5AQ", "London"),
        ("14 Pembroke-Brook Passage", "EC1R 0AA", "London"),
        ("42 Fenfell Rise", "EC1M 3JB", "London"),
        ("19 Whitcomb-Stead Mews", "E1 6BG", "London"),
        ("91 Gussetry Chambers", "WC2B 6AA", "London"),
        ("25 Greywell Close", "EC2R 6AA", "London"),
    ],
    "FR": [
        ("47 rue des Lilas Perdus", "75008", "Paris"),
        ("12 avenue Verrebrille", "75008", "Paris"),
        ("8 place Thomery-Dupré", "69001", "Lyon"),
        ("23 rue de la Croix-Brisée", "75001", "Paris"),
        ("5 cours Magnolia-Sèche", "13100", "Aix-en-Provence"),
    ],
    "CH": [
        ("Rebenhangweg 91", "8001", "Zürich"),
        ("Blumhofstrasse 5a", "8001", "Zürich"),
        ("Kirschfeldvorstadt 17", "4052", "Basel"),
        ("rue du Lac-Caché 42", "1204", "Genève"),
    ],
    "NL": [
        ("Walvisgracht 421", "1017 BP", "Amsterdam"),
        ("Pelikaanhofkade 88", "1015 DV", "Amsterdam"),
    ],
    "NO": [
        ("Fjordlundveien 12", "0154", "Oslo"),
        ("Tyttebærgaten 45", "0182", "Oslo"),
    ],
    "ES": [
        ("Calle de las Alamedas Viejas 28", "28013", "Madrid"),
        ("Passeig del Carmel Amagat 91", "08008", "Barcelona"),
    ],
    "EE": [
        ("Pajumarja tänav 23", "10141", "Tallinn"),
    ],
}

# Bank (name, BIC) pairs per country.  BICs are realistic but FICTITIOUS;
# IBAN digits are generated randomly.
BANKS: dict[str, list[tuple[str, str]]] = {
    "AT": [("Erste Bank", "GIBAATWWXXX"), ("Raiffeisenbank Austria", "RZBAATWWXXX"), ("Bank Austria", "BKAUATWWXXX")],
    "DE": [("Deutsche Bank", "DEUTDEFFXXX"), ("Commerzbank", "COBADEFFXXX"), ("Sparkasse", "SPKADEFFXXX")],
    "GB": [("Barclays", "BARCGB22XXX"), ("HSBC UK", "HBUKGB4BXXX"), ("Lloyds Bank", "LOYDGB21XXX")],
    "FR": [("BNP Paribas", "BNPAFRPPXXX"), ("Société Générale", "SOGEFRPPXXX"), ("Crédit Agricole", "AGRIFRPPXXX")],
    "CH": [("UBS", "UBSWCHZH80A"), ("Zürcher Kantonalbank", "ZKBKCHZZ80A"), ("Raiffeisen Schweiz", "RAIFCH22XXX")],
    "NL": [("ABN AMRO", "ABNANL2AXXX"), ("ING Bank", "INGBNL2AXXX")],
    "NO": [("DNB Bank", "DNBANOKKXXX")],
    "ES": [("Banco Santander", "BSCHESMMXXX"), ("BBVA", "BBVAESMMXXX")],
    "EE": [("SEB Pank", "EEUHEE2XXXX"), ("Swedbank", "HABAEE2XXXX")],
}

# IBAN total length (including the country prefix and 2 check digits).
IBAN_LENGTHS: dict[str, int] = {
    "AT": 20, "DE": 22, "GB": 22, "FR": 27, "CH": 21,
    "NL": 18, "NO": 15, "ES": 24, "EE": 20,
}

COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria", "DE": "Germany", "GB": "United Kingdom", "FR": "France",
    "CH": "Switzerland", "NL": "Netherlands", "NO": "Norway", "ES": "Spain", "EE": "Estonia",
}

# The line-item pools are deliberately a mix of straight-laced descriptions
# and mildly literary ones.  Drawn from the spec's guidance.
LINE_ITEMS: list[str] = [
    "Strategic advisory — Q1 2026 (PowerPoints not included)",
    "Annual subscription — enterprise tier (unused features included)",
    "Emergency weekend support call (45 minutes, felt longer)",
    "Cloud infrastructure migration — Phase 2 of 7 (estimated)",
    "Compliance audit preparation (regulations may have changed since)",
    "Executive workshop: 'AI Strategy'",
    "Software licence renewal — 50 seats",
    "Server maintenance — March 2026",
    "Travel expenses (London–Vienna)",
    "Travel expenses (Frankfurt–Zürich)",
    "Executive workshop (2 days)",
    "Penetration testing — scope per SOW 2026-03",
    "Consulting hours — Senior Partner",
    "Consulting hours — Associate",
    "Database optimisation retainer — March",
    "Incident response (post-mortem deliverable pending)",
    "Architecture review (whiteboarding session)",
    "Documentation cleanup (Sisyphean endeavour)",
    "Due-diligence memorandum — Project Thornbury",
    "Retainer fee — ongoing counsel",
    "Marketing collateral refresh",
    "SEO audit (Q1 2026)",
    "Board meeting facilitation",
    "Training materials — onboarding programme",
    "Legal opinion — cross-border financing",
    "Translation services (EN ↔ DE, 12k words)",
    "Data room hosting — February 2026",
    "Quarterly newsletter design",
    "Server colocation fees — rack 14-B",
    "Photocopier toner (see framework agreement)",
    "External reviewer honorarium",
    "Hardware procurement — 4 workstations",
    "Backup verification (annual)",
    "Regulatory filing — Companies House",
    "Professional indemnity insurance — pro rata",
]
PAYMENT_TERMS: list[str] = [
    "Net 30 days", "Due upon receipt", "Net 60 days",
    "2/10 net 30", "Payable within 14 days",
]

# Number-of-days-to-due lookup keyed by payment-terms text.
DUE_DAYS: dict[str, int] = {
    "Net 30 days": 30, "Due upon receipt": 0, "Net 60 days": 60,
    "2/10 net 30": 30, "Payable within 14 days": 14,
}

MONTHS: list[str] = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Variant vocabularies -- kept as module constants so --stats can refer to them.
VAT_VARIANTS = ["explicit_included", "explicit_excluded", "implicit_rate_stated", "implicit_no_rate"]
DISCOUNT_VARIANTS = ["none", "explicit_percentage", "explicit_amount", "trade_terms", "obfuscated"]
NUMBER_FORMATS = ["english", "german", "swiss"]
LAYOUTS = ["table", "paragraph", "mixed"]
CONSISTENCY = ["correct", "subtotal_error", "total_error"]
EDGE_CASES = ["none", "reverse_charge", "mixed_vat", "credit_note", "single_item"]


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal        # net
    vat_rate: Decimal          # 0, 0.10, 0.20 etc.  Per-line so mixed-VAT works.


@dataclass
class Discount:
    type: str                  # "percentage" | "amount" | "trade_terms"
    value: Decimal             # if percentage: fraction (0.05); if amount: EUR
    applied_to: str            # "subtotal"
    description: str
    conditional: bool = False  # True for trade_terms ("2/10 net 30")


@dataclass
class Invoice:
    invoice_id: str
    vendor_name: str
    vendor_country: str
    vendor_address: tuple[str, str, str]   # street, postal, city
    recipient_name: str
    recipient_country: str
    recipient_address: tuple[str, str, str]
    date: date
    due_date: date
    line_items: list[LineItem]
    vat_rate: Optional[Decimal]            # None when mixed
    subtotal: Decimal                      # canonical net
    vat_amount: Decimal                    # canonical
    discount: Optional[Discount]
    discount_amount: Decimal               # canonical (0 if no discount or conditional)
    total: Decimal                         # canonical
    currency: str
    payment_terms: str
    bank_name: str
    bic: str
    iban: str
    # Variant tags
    vat_variant: str
    discount_variant: str
    number_format: str
    layout: str
    consistency: str
    edge_case: str
    # Error injection: what the rendered invoice actually says (may differ
    # from canonical when consistency != "correct").
    rendered_subtotal: Decimal = Decimal("0.00")
    rendered_total: Decimal = Decimal("0.00")
    error_note: Optional[str] = None


# =============================================================================
# Number formatting
# =============================================================================

def format_number(x: Decimal, number_format: str) -> str:
    """Format a Decimal with the requested thousands/decimal separators.

    Always emits exactly two decimal places.  A negative value gets a
    leading minus sign; callers responsible for subtracting-sign display
    (e.g. "- 377.13") should pass a positive value and add the sign.
    """
    neg = x < 0
    s = f"{round2(abs(x)):.2f}"
    int_part, _, dec_part = s.partition(".")

    if number_format == "english":
        thou, dec = ",", "."
    elif number_format == "german":
        thou, dec = ".", ","
    elif number_format == "swiss":
        thou, dec = "'", "."
    else:
        raise ValueError(f"Unknown number_format: {number_format!r}")

    # Insert thousands separators right-to-left in groups of three.
    rev = int_part[::-1]
    chunks = [rev[i:i + 3] for i in range(0, len(rev), 3)]
    int_fmt = thou.join(chunks)[::-1]

    return ("-" if neg else "") + f"{int_fmt}{dec}{dec_part}"


def format_money(x: Decimal, number_format: str, currency: str = "EUR",
                 trailing: bool = True) -> str:
    """Format a money amount with currency code.

    By convention for this generator: currency follows the number in
    German/Swiss style (`1.234,56 EUR`), precedes it in English style
    (`EUR 1,234.56`).  `trailing=False` reverses that.
    """
    body = format_number(x, number_format)
    if trailing and number_format != "english":
        return f"{body} {currency}"
    if trailing and number_format == "english":
        return f"{currency} {body}"
    return f"{body} {currency}" if not trailing else f"{currency} {body}"


# =============================================================================
# IBAN (fake but plausible)
# =============================================================================

def fake_iban(country: str, rng: random.Random) -> str:
    """Build a plausibly-formatted IBAN for a given country.

    Does NOT generate valid check digits — these are synthetic invoices
    for an experiment, not a banking app.  The format (prefix, total
    length, four-character grouping) is correct; the digits are random.
    """
    length = IBAN_LENGTHS[country]
    digits_needed = length - 2  # minus the country prefix
    # Start with 2 random check digits then fill BBAN with random digits.
    digits = "".join(rng.choice("0123456789") for _ in range(digits_needed))
    full = country + digits
    # Pretty-print in 4-character groups, as banks always do on statements.
    return " ".join(full[i:i + 4] for i in range(0, len(full), 4))


# =============================================================================
# Date formatting
# =============================================================================

def format_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# =============================================================================
# Sampling
# =============================================================================

def sample_configs(count: int, seed: int) -> list[dict]:
    """Stratified sampling of variant combinations.

    Each dimension is assigned from a pool sized to give roughly-even
    coverage; the pools are shuffled and zipped.  Some combinations
    (explicit_included + reverse_charge, explicit_included + mixed_vat)
    are incoherent and get locally re-mapped — this slightly skews the
    VAT distribution but keeps the rendered invoices internally sane.
    """
    rng = random.Random(seed)

    def stratified_pool(variants: list[str], n: int) -> list[str]:
        per = n // len(variants)
        extra = n % len(variants)
        pool: list[str] = []
        for i, v in enumerate(variants):
            pool.extend([v] * (per + (1 if i < extra else 0)))
        rng.shuffle(pool)
        return pool

    # Consistency: 60% correct, 20% subtotal_error, 20% total_error.
    correct_n = round(count * 0.60)
    sub_err_n = round(count * 0.20)
    tot_err_n = count - correct_n - sub_err_n
    consistency_pool = (
        ["correct"] * correct_n
        + ["subtotal_error"] * sub_err_n
        + ["total_error"] * tot_err_n
    )
    rng.shuffle(consistency_pool)

    # Discount: ~30% none, remaining 70% split evenly across 4 variants.
    none_n = round(count * 0.30)
    with_n = count - none_n
    per_disc = with_n // 4
    rem_disc = with_n % 4
    discount_pool = ["none"] * none_n
    for i, v in enumerate(["explicit_percentage", "explicit_amount",
                           "trade_terms", "obfuscated"]):
        discount_pool.extend([v] * (per_disc + (1 if i < rem_disc else 0)))
    rng.shuffle(discount_pool)

    # Edge cases: ~10% of invoices get one.
    edge_n = round(count * 0.10)
    base_n = count - edge_n
    specials = ["reverse_charge", "mixed_vat", "credit_note", "single_item"]
    per_sp = edge_n // len(specials)
    rem_sp = edge_n % len(specials)
    edge_pool = ["none"] * base_n
    for i, v in enumerate(specials):
        edge_pool.extend([v] * (per_sp + (1 if i < rem_sp else 0)))
    rng.shuffle(edge_pool)

    vat_pool = stratified_pool(VAT_VARIANTS, count)
    number_pool = stratified_pool(NUMBER_FORMATS, count)
    layout_pool = stratified_pool(LAYOUTS, count)

    configs = []
    for i in range(count):
        cfg = {
            "vat_variant": vat_pool[i],
            "discount_variant": discount_pool[i],
            "number_format": number_pool[i],
            "layout": layout_pool[i],
            "consistency": consistency_pool[i],
            "edge_case": edge_pool[i],
        }
        # Incoherent combos: reverse-charge invoices have 0% VAT, so
        # "prices include 0% VAT" is nonsensical.  Swap to excluded.
        if cfg["edge_case"] == "reverse_charge" and cfg["vat_variant"] == "explicit_included":
            cfg["vat_variant"] = "explicit_excluded"
        # Mixed-VAT invoices can't claim a single "all prices include X%".
        if cfg["edge_case"] == "mixed_vat" and cfg["vat_variant"] == "explicit_included":
            cfg["vat_variant"] = "explicit_excluded"
        # Credit notes with a discount are unusual in practice; drop the
        # discount to keep ground-truth semantics clean.
        if cfg["edge_case"] == "credit_note":
            cfg["discount_variant"] = "none"
        configs.append(cfg)
    return configs


# =============================================================================
# Invoice generation
# =============================================================================

def random_price(rng: random.Random, min_eur: int = 50, max_eur: int = 50_000) -> Decimal:
    """Generate a net unit price in 10-cent multiples.

    The 10-cent grid is deliberate: multiplying by (1 + 0.20) or
    (1 + 0.10) from this grid always lands on a whole cent, so the
    VAT-included rendering never drifts by a rounding cent.
    """
    # Choose a cent value that's a multiple of 10 in [min_eur*100, max_eur*100].
    lo_decicents = min_eur * 10
    hi_decicents = max_eur * 10
    decicents = rng.randrange(lo_decicents, hi_decicents + 1)
    cents = decicents * 10
    return round2(D(cents) / D(100))


def generate_invoice(invoice_id: str, cfg: dict, rng: random.Random) -> Invoice:
    """Build a canonical Invoice from a variant config."""
    # ---- Parties --------------------------------------------------------
    vendor_name, vendor_country = rng.choice(VENDORS)
    recipient_name, recipient_country = rng.choice(RECIPIENTS)
    # Avoid vendor and recipient being identical countries only for cosmetics;
    # not enforced.
    vendor_address = rng.choice(ADDRESSES[vendor_country])
    recipient_address = rng.choice(ADDRESSES[recipient_country])
    bank_name, bic = rng.choice(BANKS[vendor_country])
    iban = fake_iban(vendor_country, rng)

    # ---- Dates ----------------------------------------------------------
    # Invoice dates span Q1 2026 plus early Q2.
    start = date(2026, 1, 1)
    end = date(2026, 4, 15)
    offset_days = rng.randint(0, (end - start).days)
    invoice_date = start + timedelta(days=offset_days)

    # ---- Payment terms + due date --------------------------------------
    # If the discount variant is trade_terms, force the matching
    # payment-terms string so the two agree.
    if cfg["discount_variant"] == "trade_terms":
        payment_terms = "2/10 net 30"
    else:
        # Draw from pool but avoid the 2/10 net 30 phrasing when no trade
        # terms discount is intended (to keep the ground truth unambiguous).
        candidates = [t for t in PAYMENT_TERMS if t != "2/10 net 30"]
        payment_terms = rng.choice(candidates)
    due_date = invoice_date + timedelta(days=DUE_DAYS[payment_terms])

    # ---- Line items -----------------------------------------------------
    # Number of line items: 1..8, with single_item edge case forcing 1.
    if cfg["edge_case"] == "single_item":
        n_items = 1
    else:
        n_items = rng.randint(1, 8)

    # VAT rate decision.
    if cfg["edge_case"] == "reverse_charge":
        default_rate = D("0.00")
        invoice_vat_rate: Optional[Decimal] = D("0.00")
        rates_for_lines = [default_rate] * n_items
    elif cfg["edge_case"] == "mixed_vat":
        # Two rates: 20% and 10%.  Ensure both appear.
        invoice_vat_rate = None
        rates_for_lines = []
        # Assign at least one of each, then random for the rest.
        if n_items < 2:
            # Degenerate: single-item mixed VAT doesn't make sense; fall back.
            invoice_vat_rate = D("0.20")
            rates_for_lines = [D("0.20")]
        else:
            rates_for_lines = [D("0.20"), D("0.10")] + [
                rng.choice([D("0.20"), D("0.10")]) for _ in range(n_items - 2)
            ]
            rng.shuffle(rates_for_lines)
    else:
        default_rate = D("0.20")
        invoice_vat_rate = default_rate
        rates_for_lines = [default_rate] * n_items

    # Descriptions + quantities + prices.
    chosen_descriptions = rng.sample(LINE_ITEMS, n_items)
    line_items: list[LineItem] = []
    for i in range(n_items):
        qty = rng.choices(
            population=[1, 2, 3, 4, 5, 10, 20, 50, 100],
            weights=[40, 15, 10, 8, 6, 8, 5, 5, 3],
            k=1,
        )[0]
        # Narrow the price range when quantity is large, to keep totals plausible.
        if qty >= 20:
            unit_price = random_price(rng, 50, 500)
        elif qty >= 5:
            unit_price = random_price(rng, 50, 2000)
        else:
            unit_price = random_price(rng, 50, 50_000)
        # Credit notes flip the sign on unit prices.
        if cfg["edge_case"] == "credit_note":
            unit_price = -unit_price
        line_items.append(LineItem(
            description=chosen_descriptions[i],
            quantity=qty,
            unit_price=unit_price,
            vat_rate=rates_for_lines[i],
        ))

    # ---- Subtotal / VAT -------------------------------------------------
    subtotal = round2(sum(
        (round2(li.unit_price * D(li.quantity)) for li in line_items),
        Decimal("0.00"),
    ))
    vat_amount = round2(sum(
        (round2(li.unit_price * D(li.quantity) * li.vat_rate) for li in line_items),
        Decimal("0.00"),
    ))

    # ---- Discount -------------------------------------------------------
    discount: Optional[Discount] = None
    discount_amount = Decimal("0.00")
    dv = cfg["discount_variant"]
    if dv == "explicit_percentage":
        pct = rng.choice([D("0.02"), D("0.03"), D("0.05"), D("0.10")])
        discount_amount = round2(subtotal * pct)
        discount = Discount(
            type="percentage", value=pct, applied_to="subtotal",
            description="early payment discount",
        )
    elif dv == "explicit_amount":
        # A plausible fixed-value rebate, pegged to subtotal size.
        fixed = round2(subtotal * rng.choice([D("0.03"), D("0.05"), D("0.07")]))
        discount_amount = fixed
        discount = Discount(
            type="amount", value=fixed, applied_to="subtotal",
            description="rebate per agreement",
        )
    elif dv == "trade_terms":
        # 2/10 net 30 — the 2% is CONDITIONAL.  The displayed total does
        # NOT include the discount; it's only taken if the payer pays
        # within 10 days.  Ground truth reflects this: discount_amount = 0
        # but the Discount object carries the conditional rate.
        discount = Discount(
            type="trade_terms", value=D("0.02"), applied_to="subtotal",
            description="2/10 net 30 — conditional early-payment discount",
            conditional=True,
        )
        discount_amount = Decimal("0.00")
    elif dv == "obfuscated":
        pct = rng.choice([D("0.025"), D("0.05"), D("0.075")])
        discount_amount = round2(subtotal * pct)
        ref = f"FA-{rng.randint(2020, 2025)}-{rng.randint(10, 999):03d}"
        discount = Discount(
            type="percentage", value=pct, applied_to="subtotal",
            description=f"adjustment per framework agreement ref. {ref}",
        )

    # ---- Total ----------------------------------------------------------
    total = round2(subtotal + vat_amount - discount_amount)

    # ---- Error injection (rendered vs. canonical) ----------------------
    rendered_subtotal = subtotal
    rendered_total = total
    error_note: Optional[str] = None
    if cfg["consistency"] == "subtotal_error":
        # Shift the subtotal by a small but noticeable amount.  Magnitude is
        # scaled to the subtotal so it's visible but not absurd.
        delta = round2(D(rng.choice([-50, -25, -10, -5, 5, 10, 25, 50])))
        # Skip exactly 0 (shouldn't happen given the choices above).
        rendered_subtotal = subtotal + delta
        error_note = (
            f"Displayed subtotal {rendered_subtotal} differs from true "
            f"subtotal {subtotal} by {delta:+}."
        )
    elif cfg["consistency"] == "total_error":
        # Off by 1–3% of the total, rounded to two decimals.
        pct = D(rng.choice([-3, -2, -1, 1, 2, 3])) / D(100)
        delta = round2(total * pct)
        rendered_total = round2(total + delta)
        error_note = (
            f"Displayed total {rendered_total} differs from true total "
            f"{total} by {delta:+} ({pct*100:+}%)."
        )

    return Invoice(
        invoice_id=invoice_id,
        vendor_name=vendor_name,
        vendor_country=vendor_country,
        vendor_address=vendor_address,
        recipient_name=recipient_name,
        recipient_country=recipient_country,
        recipient_address=recipient_address,
        date=invoice_date,
        due_date=due_date,
        line_items=line_items,
        vat_rate=invoice_vat_rate,
        subtotal=subtotal,
        vat_amount=vat_amount,
        discount=discount,
        discount_amount=discount_amount,
        total=total,
        currency="EUR",
        payment_terms=payment_terms,
        bank_name=bank_name,
        bic=bic,
        iban=iban,
        vat_variant=cfg["vat_variant"],
        discount_variant=cfg["discount_variant"],
        number_format=cfg["number_format"],
        layout=cfg["layout"],
        consistency=cfg["consistency"],
        edge_case=cfg["edge_case"],
        rendered_subtotal=rendered_subtotal,
        rendered_total=rendered_total,
        error_note=error_note,
    )


# =============================================================================
# Rendering
# =============================================================================

COPY: dict[str, str] = {
    "invoice_title": "INVOICE",
    "credit_note_title": "CREDIT NOTE",
    "invoice_no": "Invoice No",
    "credit_no": "Credit Note No",
    "date": "Date",
    "due": "Due",
    "bill_to": "Bill to:",
    "desc": "Description",
    "qty": "Qty",
    "unit_price": "Unit Price",
    "amount": "Amount",
    "subtotal": "Subtotal",
    "subtotal_incl_vat": "Subtotal (incl. VAT)",
    "vat": "VAT",
    "vat_statutory": "Statutory VAT",
    "discount": "Discount",
    "total": "TOTAL",
    "payment_terms": "Payment terms",
    "bank": "Bank",
    "thanks": "Thank you for your business.",
    "all_incl": "All prices include {rate}% VAT.",
    "plus_vat": "Plus {rate}% VAT on the subtotal.",
    "statutory_applies": "Statutory VAT ({rate}%) applies.",
    "net_implicit": "Net prices. Statutory VAT applies.",
    "reverse_charge": (
        "VAT reverse charge mechanism applies "
        "per Article 196 Council Directive 2006/112/EC."
    ),
    "credit_heading": "This document is issued as a credit note.",
    "para_opening": "For the services rendered, we invoice as follows:",
    "para_item": "{desc}: {qty} × {unit} = {amt}.",
    "para_single_item": "For {desc}, we invoice {amt}.",
}


def _rate_text(rate: Decimal) -> str:
    """Render a VAT rate like `0.20` as '20' (or '7.7' for fractional rates)."""
    as_pct = rate * 100
    if as_pct == as_pct.to_integral_value():
        return str(int(as_pct))
    return f"{as_pct.normalize()}"


def _display_unit_price(inv: Invoice, li: LineItem) -> Decimal:
    """Unit price to DISPLAY on the line (gross when 'explicit_included')."""
    if inv.vat_variant == "explicit_included":
        # Use the line's own rate (handles mixed-VAT in the unlikely event
        # that explicit_included sneaks through, though sample_configs
        # prevents that combo).
        return round2(li.unit_price * (Decimal("1") + li.vat_rate))
    return li.unit_price


def _display_line_amount(inv: Invoice, li: LineItem) -> Decimal:
    return round2(_display_unit_price(inv, li) * D(li.quantity))


def _address_block(name: str, address: tuple[str, str, str],
                   country: str) -> list[str]:
    street, postal, city = address
    country_name = COUNTRY_NAMES[country]
    return [name, street, f"{postal} {city}, {country_name}"]


def _render_header(inv: Invoice) -> list[str]:
    """Top-of-invoice block: title, vendor identity, numbering, dates."""
    c = COPY
    is_cn = inv.edge_case == "credit_note"
    title = c["credit_note_title"] if is_cn else c["invoice_title"]
    no_key = "credit_no" if is_cn else "invoice_no"

    # Two-column-ish: left = vendor address, right = metadata.
    street, postal, city = inv.vendor_address
    country_name = COUNTRY_NAMES[inv.vendor_country]
    left = [inv.vendor_name, street, f"{postal} {city}", country_name]
    right = [
        f"{c[no_key]}: {inv.invoice_id}",
        f"{c['date']}: {format_date(inv.date)}",
        f"{c['due']}: {format_date(inv.due_date)}",
    ]

    lines = [title, ""]
    width = 44
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        lines.append(f"{l:<{width}}{r}")
    lines.append("")

    # Recipient block.
    lines.append(c["bill_to"])
    lines.extend(_address_block(
        inv.recipient_name, inv.recipient_address, inv.recipient_country,
    ))
    lines.append("")
    if is_cn:
        lines.append(c["credit_heading"])
        lines.append("")
    return lines


def _render_table(inv: Invoice) -> list[str]:
    """Markdown-style pipe table of line items."""
    c = COPY
    rows: list[list[str]] = [[c["desc"], c["qty"], c["unit_price"], c["amount"]]]
    for li in inv.line_items:
        unit = format_number(_display_unit_price(inv, li), inv.number_format)
        amt = format_number(_display_line_amount(inv, li), inv.number_format)
        rows.append([li.description, str(li.quantity), unit, amt])

    # Column widths (measure content; pad to at least header width).
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    # Header row, separator, then data rows.
    out = []
    sep = (
        "| "
        + " | ".join(["-" * widths[0],
                      "-" * (widths[1] - 1) + ":",
                      "-" * (widths[2] - 1) + ":",
                      "-" * (widths[3] - 1) + ":"])
        + " |"
    )

    def fmt_row(r: list[str], aligns=("<", ">", ">", ">")) -> str:
        cells = []
        for i, (cell, w, a) in enumerate(zip(r, widths, aligns)):
            cells.append(f"{cell:{a}{w}}")
        return "| " + " | ".join(cells) + " |"

    out.append(fmt_row(rows[0], aligns=("<", ">", ">", ">")))
    out.append(sep)
    for r in rows[1:]:
        out.append(fmt_row(r))
    return out


def _render_paragraph_lines(inv: Invoice) -> list[str]:
    """Natural-language prose version of the line items."""
    c = COPY
    items = []
    for li in inv.line_items:
        unit_disp = format_money(
            _display_unit_price(inv, li), inv.number_format, inv.currency,
        )
        amt_disp = format_money(
            _display_line_amount(inv, li), inv.number_format, inv.currency,
        )
        if li.quantity == 1:
            items.append(c["para_single_item"].format(
                desc=li.description, amt=amt_disp,
            ))
        else:
            items.append(c["para_item"].format(
                desc=li.description, qty=li.quantity,
                unit=unit_disp, amt=amt_disp,
            ))
    out = [c["para_opening"], ""]
    out.extend(items)
    return out


def _render_summary(inv: Invoice) -> list[str]:
    """Subtotal / VAT / discount / total block.

    The displayed *subtotal* and *total* respect error injection: when
    consistency == 'subtotal_error' the subtotal line shows the wrong
    number; when consistency == 'total_error' the TOTAL line shows the
    wrong number.  All other figures (VAT, discount) always remain
    canonical — the LLM's task is to spot the inconsistency.
    """
    c = COPY
    fmt = inv.number_format

    # Which subtotal number to display?
    if inv.vat_variant == "explicit_included":
        # Display the gross subtotal = canonical net + canonical VAT.
        # Error injection on subtotal still applies (delta applied to gross).
        gross_subtotal = inv.subtotal + inv.vat_amount
        if inv.consistency == "subtotal_error":
            displayed_subtotal = gross_subtotal + (inv.rendered_subtotal - inv.subtotal)
        else:
            displayed_subtotal = gross_subtotal
        subtotal_label = c["subtotal_incl_vat"]
    else:
        displayed_subtotal = inv.rendered_subtotal
        subtotal_label = c["subtotal"]

    lines: list[str] = []

    # Optional top banner lines (VAT phrasing, reverse charge, etc.).
    banner: list[str] = []
    if inv.edge_case == "reverse_charge":
        banner.append(c["reverse_charge"])
    else:
        if inv.vat_variant == "explicit_included" and inv.vat_rate is not None:
            banner.append(c["all_incl"].format(rate=_rate_text(inv.vat_rate)))
        elif inv.vat_variant == "explicit_excluded" and inv.vat_rate is not None:
            banner.append(c["plus_vat"].format(rate=_rate_text(inv.vat_rate)))
        elif inv.vat_variant == "implicit_rate_stated" and inv.vat_rate is not None:
            banner.append(c["statutory_applies"].format(rate=_rate_text(inv.vat_rate)))
        elif inv.vat_variant == "implicit_no_rate":
            banner.append(c["net_implicit"])
        elif inv.edge_case == "mixed_vat":
            # Mixed-VAT invoices always say "Plus VAT" with no single rate.
            banner.append("Plus statutory VAT as detailed below.")

    if banner:
        lines.extend(banner)
        lines.append("")

    # Subtotal line.
    lines.append(f"{subtotal_label}: {format_money(displayed_subtotal, fmt, inv.currency)}")

    # VAT line(s).
    if inv.edge_case == "reverse_charge":
        pass  # No VAT line — reverse charge.
    elif inv.vat_variant == "explicit_included":
        pass  # Prices were gross already; no separate VAT line.
    elif inv.edge_case == "mixed_vat":
        # Break out per-rate VAT totals.
        by_rate: dict[Decimal, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for li in inv.line_items:
            by_rate[li.vat_rate] += round2(li.unit_price * D(li.quantity) * li.vat_rate)
        for rate in sorted(by_rate.keys(), reverse=True):
            amount = round2(by_rate[rate])
            rate_txt = _rate_text(rate)
            lines.append(
                f"{c['vat']} ({rate_txt}%): {format_money(amount, fmt, inv.currency)}"
            )
    elif inv.vat_variant == "explicit_excluded" and inv.vat_rate is not None:
        rate_txt = _rate_text(inv.vat_rate)
        lines.append(f"{c['vat']} ({rate_txt}%): {format_money(inv.vat_amount, fmt, inv.currency)}")
    elif inv.vat_variant == "implicit_rate_stated" and inv.vat_rate is not None:
        rate_txt = _rate_text(inv.vat_rate)
        lines.append(
            f"{c['vat_statutory']} ({rate_txt}%): "
            f"{format_money(inv.vat_amount, fmt, inv.currency)}"
        )
    elif inv.vat_variant == "implicit_no_rate":
        # Show the VAT amount but NOT the rate.  Model has to infer it.
        lines.append(f"{c['vat']}: {format_money(inv.vat_amount, fmt, inv.currency)}")

    # Discount line — rendering varies by variant.
    if inv.discount is not None and inv.discount_variant != "trade_terms":
        disc_label = _discount_label(inv)
        # Always render as a minus of a positive number.
        lines.append(
            f"{disc_label}: -{format_money(inv.discount_amount, fmt, inv.currency)}"
        )

    # Separator.
    lines.append("-" * 44)

    # Total.
    lines.append(f"{c['total']}: {format_money(inv.rendered_total, fmt, inv.currency)}")
    return lines


def _discount_label(inv: Invoice) -> str:
    """Variant-specific discount label text."""
    c = COPY
    d = inv.discount
    assert d is not None  # caller checked
    if inv.discount_variant == "explicit_percentage":
        pct_txt = _rate_text(d.value)
        return f"{c['discount']} ({pct_txt}% early payment discount)"
    if inv.discount_variant == "explicit_amount":
        return "Less: rebate per agreement"
    if inv.discount_variant == "obfuscated":
        # Bury the percentage in a reference string; keep the sign visible.
        desc = d.description  # already includes the reference text
        pct_txt = _rate_text(d.value)
        return f"{desc} (-{pct_txt}%)"
    return c["discount"]


def _render_footer(inv: Invoice) -> list[str]:
    c = COPY
    out: list[str] = [""]
    out.append(f"{c['payment_terms']}: {inv.payment_terms}")
    out.append(f"{c['bank']}: {inv.bank_name} | IBAN: {inv.iban} | BIC: {inv.bic}")
    out.append("")
    out.append(c["thanks"])
    return out


def render_invoice(inv: Invoice) -> str:
    """Return the full rendered invoice text."""
    lines = _render_header(inv)

    if inv.layout == "table":
        lines.extend(_render_table(inv))
        lines.append("")
        lines.extend(_render_summary(inv))
    elif inv.layout == "paragraph":
        lines.extend(_render_paragraph_lines(inv))
        lines.append("")
        lines.extend(_render_summary(inv))
    elif inv.layout == "mixed":
        lines.extend(_render_table(inv))
        lines.append("")
        # Summary as prose.
        lines.extend(_render_summary_prose(inv))
    else:
        raise ValueError(f"Unknown layout: {inv.layout!r}")

    lines.extend(_render_footer(inv))
    return "\n".join(lines) + "\n"


def _render_summary_prose(inv: Invoice) -> list[str]:
    """Narrative-style version of the summary block (used by 'mixed' layout)."""
    c = COPY
    fmt = inv.number_format

    pieces: list[str] = []

    if inv.edge_case == "reverse_charge":
        pieces.append(c["reverse_charge"])

    # Subtotal sentence.
    if inv.vat_variant == "explicit_included":
        gross_subtotal = inv.subtotal + inv.vat_amount
        if inv.consistency == "subtotal_error":
            displayed_subtotal = gross_subtotal + (inv.rendered_subtotal - inv.subtotal)
        else:
            displayed_subtotal = gross_subtotal
        pieces.append(
            f"The subtotal including VAT is "
            f"{format_money(displayed_subtotal, fmt, inv.currency)}."
        )
    else:
        pieces.append(
            f"The subtotal is {format_money(inv.rendered_subtotal, fmt, inv.currency)}."
        )

    # VAT sentence (skip if reverse charge or VAT-included).
    if inv.edge_case != "reverse_charge" and inv.vat_variant != "explicit_included":
        if inv.edge_case == "mixed_vat":
            pieces.append(
                "Statutory VAT at mixed rates is applied, totalling "
                f"{format_money(inv.vat_amount, fmt, inv.currency)}."
            )
        elif inv.vat_variant == "implicit_no_rate":
            pieces.append(
                f"Plus statutory VAT of {format_money(inv.vat_amount, fmt, inv.currency)}."
            )
        else:
            rate_txt = _rate_text(inv.vat_rate or D("0.20"))
            if inv.vat_variant == "implicit_rate_stated":
                pieces.append(
                    f"Statutory VAT ({rate_txt}%) applies: "
                    f"{format_money(inv.vat_amount, fmt, inv.currency)}."
                )
            else:  # explicit_excluded
                pieces.append(
                    f"Plus {rate_txt}% VAT on the subtotal, amounting to "
                    f"{format_money(inv.vat_amount, fmt, inv.currency)}."
                )

    # Discount sentence.
    if inv.discount is not None and inv.discount_variant != "trade_terms":
        amt = format_money(inv.discount_amount, fmt, inv.currency)
        if inv.discount_variant == "explicit_percentage":
            pct = _rate_text(inv.discount.value)
            pieces.append(f"Less {pct}% early payment discount ({amt}).")
        elif inv.discount_variant == "explicit_amount":
            pieces.append(f"Less an agreed rebate of {amt}.")
        elif inv.discount_variant == "obfuscated":
            pct = _rate_text(inv.discount.value)
            pieces.append(f"{inv.discount.description}: -{pct}% ({amt}).")

    # Total sentence.
    pieces.append(
        f"The total amount due is "
        f"{format_money(inv.rendered_total, fmt, inv.currency)}."
    )

    return pieces


# =============================================================================
# Serialization
# =============================================================================

def _dec(x: Decimal) -> str:
    """JSON-safe Decimal serialisation: always 2-dp string."""
    return f"{round2(x):.2f}"


def _dec_rate(x: Decimal) -> str:
    """Like _dec, but preserves precision for rate/percentage values (up to 4dp).

    Rounding discount percentages like 0.025 to 2dp (-> '0.03') would break
    the verification check `subtotal * value == discount_amount`.
    """
    # Keep 4 decimal places, stripping trailing zeroes for cleanliness.
    q = x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    s = f"{q:.4f}"
    # Trim trailing zeros but always keep at least 2dp.
    if "." in s:
        s = s.rstrip("0")
        head, _, tail = s.partition(".")
        if len(tail) < 2:
            tail = (tail + "00")[:2]
        s = f"{head}.{tail}"
    return s


def invoice_to_dict(inv: Invoice) -> dict:
    """Canonical ground-truth JSON for one invoice.

    Note: all monetary values are emitted as two-decimal strings to avoid
    any chance of a downstream float-parser losing precision.  Tools that
    care can `Decimal(value_str)` them back.
    """
    d: dict[str, Any] = {
        "invoice_id": inv.invoice_id,
        "vendor": inv.vendor_name,
        "vendor_country": inv.vendor_country,
        "vendor_address": {
            "street": inv.vendor_address[0],
            "postal_code": inv.vendor_address[1],
            "city": inv.vendor_address[2],
        },
        "recipient": inv.recipient_name,
        "recipient_country": inv.recipient_country,
        "recipient_address": {
            "street": inv.recipient_address[0],
            "postal_code": inv.recipient_address[1],
            "city": inv.recipient_address[2],
        },
        "date": inv.date.isoformat(),
        "due_date": inv.due_date.isoformat(),
        "line_items": [
            {
                "description": li.description,
                "quantity": li.quantity,
                "unit_price": _dec(li.unit_price),
                "vat_rate": _dec_rate(li.vat_rate),
                "amount": _dec(round2(li.unit_price * D(li.quantity))),
            }
            for li in inv.line_items
        ],
        "subtotal": _dec(inv.subtotal),
        "vat_rate": _dec_rate(inv.vat_rate) if inv.vat_rate is not None else None,
        "vat_amount": _dec(inv.vat_amount),
        "discount": None if inv.discount is None else {
            "type": inv.discount.type,
            "value": _dec_rate(inv.discount.value),
            "applied_to": inv.discount.applied_to,
            "description": inv.discount.description,
            "conditional": inv.discount.conditional,
        },
        "discount_amount": _dec(inv.discount_amount),
        "total": _dec(inv.total),
        "currency": inv.currency,
        "payment_terms": inv.payment_terms,
        "bank_details": f"{inv.bank_name} | IBAN: {inv.iban} | BIC: {inv.bic}",
        "variants": {
            "vat_variant": inv.vat_variant,
            "discount_variant": inv.discount_variant,
            "number_format": inv.number_format,
            "layout": inv.layout,
            "consistency": inv.consistency,
            "edge_case": inv.edge_case,
        },
        "rendered_subtotal": _dec(inv.rendered_subtotal),
        "rendered_total": _dec(inv.rendered_total),
        "error_note": inv.error_note,
        "is_credit_note": inv.edge_case == "credit_note",
    }
    return d


# =============================================================================
# Verification
# =============================================================================

def verify_json(data: dict) -> list[str]:
    """Return a list of errors (empty = OK) after re-running all math on the JSON."""
    errors: list[str] = []

    try:
        subtotal = D(data["subtotal"])
        vat_amount = D(data["vat_amount"])
        discount_amount = D(data["discount_amount"])
        total = D(data["total"])
    except Exception as e:  # pragma: no cover — caught only on malformed files
        return [f"malformed JSON: {e!r}"]

    # Line items sum to subtotal.
    line_sum = sum(
        (round2(D(li["unit_price"]) * D(li["quantity"])) for li in data["line_items"]),
        Decimal("0.00"),
    )
    line_sum = round2(line_sum)
    if line_sum != subtotal:
        errors.append(f"subtotal {subtotal} != sum(line_items) {line_sum}")

    # VAT: sum of per-line VAT (works for single-rate and mixed-rate).
    expected_vat = round2(sum(
        (round2(D(li["unit_price"]) * D(li["quantity"]) * D(li["vat_rate"]))
         for li in data["line_items"]),
        Decimal("0.00"),
    ))
    if expected_vat != vat_amount:
        errors.append(f"vat_amount {vat_amount} != sum(line VAT) {expected_vat}")

    # If the top-level vat_rate is set (non-mixed), also cross-check.
    if data.get("vat_rate") is not None:
        top_rate = D(data["vat_rate"])
        cross = round2(subtotal * top_rate)
        if cross != vat_amount:
            errors.append(
                f"vat_amount {vat_amount} != subtotal*rate {cross} (top-level vat_rate)"
            )

    # Discount amount.
    disc = data.get("discount")
    if disc is None:
        expected_disc = Decimal("0.00")
    elif disc.get("conditional"):
        # Conditional discounts (trade_terms) don't hit the total.
        expected_disc = Decimal("0.00")
    elif disc["type"] == "percentage":
        expected_disc = round2(subtotal * D(disc["value"]))
    elif disc["type"] == "amount":
        expected_disc = round2(D(disc["value"]))
    elif disc["type"] == "trade_terms":
        expected_disc = Decimal("0.00")
    else:
        expected_disc = discount_amount  # unknown type — skip
        errors.append(f"unknown discount type: {disc['type']!r}")

    if expected_disc != discount_amount:
        errors.append(
            f"discount_amount {discount_amount} != expected {expected_disc}"
        )

    # Total.
    expected_total = round2(subtotal + vat_amount - discount_amount)
    if expected_total != total:
        errors.append(f"total {total} != subtotal+VAT-discount {expected_total}")

    # Consistency flag sanity: rendered_* fields should agree with canonical
    # when consistency=='correct'.
    consistency = data["variants"]["consistency"]
    rendered_subtotal = D(data["rendered_subtotal"])
    rendered_total = D(data["rendered_total"])
    if consistency == "correct":
        if rendered_subtotal != subtotal:
            errors.append(f"consistency=correct but rendered_subtotal != subtotal")
        if rendered_total != total:
            errors.append(f"consistency=correct but rendered_total != total")
    elif consistency == "subtotal_error":
        if rendered_subtotal == subtotal:
            errors.append("consistency=subtotal_error but rendered_subtotal == subtotal")
        if rendered_total != total:
            errors.append("consistency=subtotal_error but rendered_total != total")
    elif consistency == "total_error":
        if rendered_subtotal != subtotal:
            errors.append("consistency=total_error but rendered_subtotal != subtotal")
        if rendered_total == total:
            errors.append("consistency=total_error but rendered_total == total")

    return errors


def verify_dataset(output_dir: Path) -> int:
    gt_dir = output_dir / "ground_truth"
    if not gt_dir.is_dir():
        print(f"error: {gt_dir} not found", file=sys.stderr)
        return 2

    files = sorted(gt_dir.glob("INV-*.json"))
    if not files:
        print(f"error: no ground_truth files found in {gt_dir}", file=sys.stderr)
        return 2

    bad = 0
    for fp in files:
        with fp.open() as f:
            data = json.load(f)
        errs = verify_json(data)
        if errs:
            bad += 1
            print(f"✗ {fp.name}")
            for e in errs:
                print(f"    {e}")

    total = len(files)
    if bad == 0:
        print(f"✓ verified {total} invoices — all consistent.")
        return 0
    print(f"✗ {bad}/{total} invoices had inconsistencies.")
    return 1


# =============================================================================
# Stats
# =============================================================================

def print_stats(output_dir: Path) -> int:
    gt_dir = output_dir / "ground_truth"
    files = sorted(gt_dir.glob("INV-*.json"))
    if not files:
        print(f"error: no ground_truth files found in {gt_dir}", file=sys.stderr)
        return 2

    counters: dict[str, Counter] = {
        "vat_variant": Counter(),
        "discount_variant": Counter(),
        "number_format": Counter(),
        "layout": Counter(),
        "consistency": Counter(),
        "edge_case": Counter(),
        "vendor_country": Counter(),
    }
    line_counts: Counter = Counter()
    totals: list[Decimal] = []

    for fp in files:
        with fp.open() as f:
            data = json.load(f)
        for k in ("vat_variant", "discount_variant", "number_format",
                  "layout", "consistency", "edge_case"):
            counters[k][data["variants"][k]] += 1
        counters["vendor_country"][data["vendor_country"]] += 1
        line_counts[len(data["line_items"])] += 1
        totals.append(D(data["total"]))

    n = len(files)
    print(f"Dataset: {n} invoices in {output_dir}\n")
    for dim in ("vat_variant", "discount_variant", "number_format",
                "layout", "consistency", "edge_case",
                "vendor_country"):
        print(f"{dim}:")
        for k, v in counters[dim].most_common():
            pct = v / n * 100
            print(f"  {k:28s}  {v:4d}  ({pct:5.1f}%)")
        print()

    print("line items per invoice:")
    for k in sorted(line_counts):
        print(f"  {k:2d}  {line_counts[k]:4d}")
    print()

    # Total amount summary.
    tmin = min(totals)
    tmax = max(totals)
    tsum = sum(totals, Decimal("0.00"))
    tmean = round2(tsum / D(len(totals)))
    print(f"total amount:  min={tmin}  max={tmax}  mean={tmean}")
    return 0


# =============================================================================
# Dataset generation
# =============================================================================

def generate_dataset(output_dir: Path, count: int, seed: int) -> dict:
    invoice_dir = output_dir / "invoices"
    gt_dir = output_dir / "ground_truth"
    invoice_dir.mkdir(parents=True, exist_ok=True)
    gt_dir.mkdir(parents=True, exist_ok=True)

    # Use a seeded RNG for the config sample AND a separately-seeded one
    # for invoice construction, so the mapping from id → config is stable.
    configs = sample_configs(count, seed)
    rng = random.Random(seed + 1)

    manifest_rows: list[list[str]] = [
        ["id", "vat_variant", "discount_variant", "number_format",
         "layout", "consistency", "edge_case", "correct_total",
         "rendered_total", "currency", "has_discount", "is_credit_note"]
    ]

    for i, cfg in enumerate(configs):
        inv_id = f"INV-2026-{i + 1:04d}"
        inv = generate_invoice(inv_id, cfg, rng)
        text = render_invoice(inv)
        gt = invoice_to_dict(inv)

        (invoice_dir / f"{inv_id}.md").write_text(text, encoding="utf-8")
        with (gt_dir / f"{inv_id}.json").open("w", encoding="utf-8") as f:
            json.dump(gt, f, ensure_ascii=False, indent=2)

        manifest_rows.append([
            inv_id,
            inv.vat_variant, inv.discount_variant, inv.number_format,
            inv.layout, inv.consistency, inv.edge_case,
            _dec(inv.total), _dec(inv.rendered_total),
            inv.currency,
            "no" if inv.discount is None else "yes",
            "yes" if inv.edge_case == "credit_note" else "no",
        ])

    # Manifest CSV.
    with (output_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(manifest_rows)

    # Summary JSON (distribution checks).
    def count_dim(key: str) -> dict[str, int]:
        c: Counter = Counter()
        for row in manifest_rows[1:]:
            header_idx = manifest_rows[0].index(key)
            c[row[header_idx]] += 1
        return dict(c)

    summary = {
        "count": count,
        "seed": seed,
        "distribution": {
            "vat_variant": count_dim("vat_variant"),
            "discount_variant": count_dim("discount_variant"),
            "number_format": count_dim("number_format"),
            "layout": count_dim("layout"),
            "consistency": count_dim("consistency"),
            "edge_case": count_dim("edge_case"),
        },
        "coverage_check": _coverage_report(configs),
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return summary


def _coverage_report(configs: list[dict]) -> dict:
    """For each dimension, count how many times each variant appears.

    The spec requires every variant to appear at least 10 times.  This
    is where that is surfaced for downstream inspection.
    """
    report = {}
    for dim in ("vat_variant", "discount_variant", "number_format",
                "layout", "consistency", "edge_case"):
        c = Counter(c[dim] for c in configs)
        min_count = min(c.values())
        report[dim] = {
            "counts": dict(c),
            "min": min_count,
            "meets_min_10": min_count >= 10,
        }
    return report


# =============================================================================
# CLI
# =============================================================================

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--output", "-o", type=Path, required=True,
                   help="Output directory (will be created if missing).")
    p.add_argument("--count", "-n", type=int, default=None,
                   help="Number of invoices to generate (omit to skip generation).")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for reproducibility (default 42).")
    p.add_argument("--verify", action="store_true",
                   help="Verify ground-truth JSONs are internally consistent.")
    p.add_argument("--stats", action="store_true",
                   help="Print the dataset variant distribution.")
    args = p.parse_args(argv)

    rc = 0

    if args.count is not None:
        summary = generate_dataset(args.output, args.count, args.seed)
        print(f"generated {args.count} invoices under {args.output}")
        cov = summary["coverage_check"]
        under = [k for k, v in cov.items() if not v["meets_min_10"]]
        if under:
            print(f"warning: dimensions below 10-min coverage: {under}")

    if args.verify:
        rc = max(rc, verify_dataset(args.output))

    if args.stats:
        rc = max(rc, print_stats(args.output))

    if args.count is None and not args.verify and not args.stats:
        p.error("provide --count to generate, or --verify / --stats to inspect.")

    return rc


if __name__ == "__main__":
    sys.exit(main())
