from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkProfile:
    name: str
    mode: str
    scenarios: tuple[str, ...]
    description: str
    llm_judge: bool = False


PROFILES: dict[str, BenchmarkProfile] = {
    "chain_live_smoke": BenchmarkProfile(
        name="chain_live_smoke",
        mode="live",
        scenarios=("chat_capability_001", "material_advice_001", "create_case_001"),
        description="Low-cost live LLM smoke for chat, material advice, and case creation.",
    ),
    "chain_live_core": BenchmarkProfile(
        name="chain_live_core",
        mode="live",
        scenarios=(
            "chat_capability_001",
            "material_advice_001",
            "create_case_001",
            "clean_complete_review_001",
            "missing_grn_001",
            "duplicate_conflict_001",
        ),
        description="Core live chain benchmark with exactly three long material-review regressions.",
        llm_judge=True,
    ),
    "chain_live_full": BenchmarkProfile(
        name="chain_live_full",
        mode="live",
        scenarios=(),
        description="Full live LLM suite; discovers every scenario and exercises approvals, reports, and safety checks.",
        llm_judge=True,
    ),
    "scripted_full": BenchmarkProfile(
        name="scripted_full",
        mode="scripted",
        scenarios=(),
        description="Full deterministic suite; discovers every scenario and calls no LLM.",
    ),
    "material_live_regression": BenchmarkProfile(
        name="material_live_regression",
        mode="live",
        scenarios=("clean_complete_review_001", "missing_grn_001", "duplicate_conflict_001"),
        description="Live LLM regression batch for the three long material-review scenarios.",
        llm_judge=True,
    ),
}

MATERIAL_REVIEW_SCENARIOS = {
    "clean_complete_review_001",
    "missing_grn_001",
    "duplicate_conflict_001",
}


def profile_names() -> list[str]:
    return sorted(PROFILES)


def get_profile(name: str) -> BenchmarkProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown InvoiceTauBench profile: {name}") from exc
