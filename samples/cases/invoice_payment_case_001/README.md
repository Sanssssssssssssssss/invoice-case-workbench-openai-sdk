# Sample Case: Invoice Payment Review 001

This sample gives the agent a complete but still reviewable invoice payment case.

Suggested flow:

1. Create the case.
2. Ask what materials are required.
3. Submit each evidence file under `evidence/`.
4. Ask for a manager report.

Run it with:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_sample_case.py
```

Run the complete five-material flow:

```powershell
.\.venv\Scripts\python.exe backend\scripts\run_sample_case.py --full
```

The generated local case workspace will be `workspace/cases/case_sample_001/`.
