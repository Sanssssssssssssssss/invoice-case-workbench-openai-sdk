# Runtime Debug Playbook

This playbook keeps runtime-debug pressure tests small and repeatable. The goal is not to add a second debug agent. The goal is to expose enough structured feedback for the planner and for humans reading traces.

## Invariants

- Terminal tool errors must become `runtime_feedback`.
- `runtime_feedback.retry_allowed=false` must point to one `recommended_action`.
- The planner context must include the latest `runtime_feedback`.
- The executor must block a repeated `runtime_feedback.blocked_action`.
- A blocked run should end with a user-visible next step, not a generic max-step failure.
- Trace order must remain causal: model call, planner action, tool or role call, observation, step feedback, checkpoint.

## Minimal Pressure Cases

1. Unsupported attachment format
   - Submit a `.pdf` attachment.
   - Expect one `read_attachment` call.
   - Expect `runtime_feedback.error_type=unsupported_file_type`.
   - Expect the next planner action to be `final_answer`.
   - The reply should ask the user to convert to `txt`, `md`, `json`, `csv`, `log`, `xml`, `yaml`, or `yml`.

2. Missing attachment path
   - Submit an attachment record pointing to a path that does not exist.
   - Expect one `read_attachment` call.
   - Expect `runtime_feedback.error_type=attachment_missing`.
   - Expect a clear re-upload request.

3. Stubborn repeated action
   - Force or fake a planner action that repeats the blocked tool.
   - Expect a `runtime/terminal_feedback_block` observation.
   - Expect no second tool call.

4. Recovery after conversion
   - After an unsupported PDF turn, submit a converted text or markdown file in the same case.
   - Expect normal evidence review and case patch flow.
   - The previous PDF filename or declaration must not become evidence.

5. Near step budget
   - Run with one step remaining.
   - Expect `runtime_feedback.error_type=step_budget_near_limit`.
   - Expect final answer based on existing observations.

## Trace Checks

For each live run, inspect:

- `workspace/cases/<case_id>/traces/<run_id>/events.jsonl`
- `workspace/cases/<case_id>/traces/<run_id>.json`
- `workspace/cases/<case_id>/traces/<run_id>/context_manifest_*_planner.json`

Check these fields:

- `event_id`, `parent_event_id`, `caused_by_event_id`
- `session_id`, `turn_id`, `case_id`, `run_id`
- `payload.runtime_feedback`
- planner `plan_progress` and `reason`
- tool call count for the blocked tool
- final reply wording

## Pass Criteria

A runtime-debug fix passes only when all are true:

- Unit tests cover the classification and executor block.
- Full backend tests pass.
- At least one live LLM run shows the planner seeing `runtime_feedback` and choosing `final_answer`.
- The live run does not reach `max_steps`.
- The trace can explain why the action stopped without reading hidden state or guessing.

## Things To Avoid

- Do not add a debug agent for runtime tool failures.
- Do not classify broad unknown errors as terminal.
- Do not retry the same terminal tool action.
- Do not let a system fallback hide the runtime reason from the planner.
- Do not treat attachment names, failed reads, or user claims as reviewed evidence.
