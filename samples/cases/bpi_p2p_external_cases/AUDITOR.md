# Strict BPI External Case Auditor

This suite should be used as a fail-fast system auditor, not as a bulk scoring
benchmark.

## Principle

Run one case or a very small slice. Stop at the first visible issue. The goal is
to find the first broken link in the chain:

1. Planner route
2. Evidence review
3. Case memory
4. Risk explanation
5. Report quality

Do not average away failures. If Planner skipped RAG, there is no value in
judging the final memo yet. If evidence IDs were lost, fix that before checking
report polish. If Clear Invoice is overclaimed, stop immediately because that is
a boundary failure.

## Commands

Contract-only audit, no agent run required:

```powershell
.\.venv\Scripts\python.exe backend\tests\infra\bpi_external_case_auditor.py contract
```

Audit one case definition:

```powershell
.\.venv\Scripts\python.exe backend\tests\infra\bpi_external_case_auditor.py contract --case-id INV-BPI-001
```

Run-level audit from a workspace case folder after an agent run:

```powershell
.\.venv\Scripts\python.exe backend\tests\infra\bpi_external_case_auditor.py run `
  --workspace-root workspace\cases `
  --case-id INV-BPI-001
```

Run-level audit from an exported observed JSON:

```powershell
.\.venv\Scripts\python.exe backend\tests\infra\bpi_external_case_auditor.py run `
  --observed artifacts\audit\observed_bpi_case.json `
  --case-id INV-BPI-001 `
  --json
```

## Expected Observed JSON Shape

The auditor accepts either a single case object, a list of case objects, or:

```json
{
  "cases": [
    {
      "case_id": "INV-BPI-001",
      "turns": [
        {
          "reply": "...",
          "trace": {
            "planner_actions": []
          },
          "case_state": {}
        }
      ],
      "case_state": {},
      "report_markdown": "# Manager memo..."
    }
  ]
}
```

## Failure Semantics

- `critical`: the chain is broken or a boundary was violated. Stop immediately.
- `major`: the chain continued, but an expected control fact, risk, evidence ID,
  credibility, or missing-material state was lost. Stop and fix before running
  more cases.
- `minor`: the system is functionally on track but report shape or polish is
  below the handoff bar.

Every finding includes:

- `layer`
- `turn_index` when applicable
- expected vs observed
- `problem_chain`
- next concrete action

## Boundary

BPI 2019 evidence is read-only supporting P2P process evidence. It is not an ERP
integration, not approval workflow evidence, not production benchmark accuracy,
and not proof that payment was approved or executed.
