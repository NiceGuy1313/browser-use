# Agent Observatory

Local web app that runs a Browser Use `Agent` and shows, side by side:

- **Left** – an Archify diagram of the agent loop, marking where the LLM call and tool-call boundaries are.
- **Right** – the run's call history, grouped per step: each LLM query and the actions it returned, then each tool execution with its params, duration and result/error.

## Run

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY in .env
uv sync
uv run uvicorn examples.ui.agent_dashboard.app:app --port 8000
```

Open http://127.0.0.1:8000. The key is read from the repository `.env` on the server and never sent to the browser.

## Where the history comes from

| Trace kind | Source | Location |
|---|---|---|
| `llm_query` / `llm_response` | `[LLM_QUERY]` / `[LLM_RESPONSE]` debug logs | `Agent.get_model_output` in `browser_use/agent/service.py` |
| `tool_start` / `tool_end` / `tool_error` | `[TOOL_EXECUTION_*]` debug logs | `Registry.execute_action` in `browser_use/tools/registry/service.py` |
| `step` | `Agent.run(on_step_end=...)` hook, read from `agent.history` | `_publish_step` in `app.py` |
| `run`, `log` | dashboard lifecycle and INFO+ `browser_use` logs | `app.py` |

`DashboardLogHandler` builds the structured fields from each log record's positional `args`, not from the formatted text. If you change the argument order of those log calls, update `_parse_boundary_record` as well.

Tool events don't carry a step number, so the hub assigns each one to the step of the most recent `llm_query`. Tool calls that run before the first query (`initial_actions`, including the auto `navigate` when the task contains a URL) are grouped under **Initial actions**.

The `step` event is the source of truth for whether an action succeeded. Most actions report failure by returning `ActionResult(error=...)` rather than raising, so the core log for those still says `tool_end`. The UI marks a tool row as failed using the step's results.

## Exporting traces

- **Export JSON** in the UI, or `GET /api/runs/current/trace.json`, returns the full `RunRecord` with every event.
- Each finished run is also written to `runs/<run_id>.json`. That folder is gitignored.
- If you open the page while a run is in progress, `GET /api/events` (SSE) replays the run's history first and then streams new events.

## Known gaps / next steps

- `[TOOL_EXECUTION_END]` doesn't include `ActionResult.error`. Until it does, failures show up only once the step finishes.
- If param validation fails in `Registry.execute_action`, nothing is logged, because the logging sits inside the inner `try`.
- When `Tools.act` hits its per-action timeout (`asyncio.wait_for`), the action is cancelled with `CancelledError`. That error bypasses `except Exception`, so the call gets `tool_start` but never `tool_end` or `tool_error`.
- Only the main-loop LLM call is traced. Judge, message compaction, `extract`'s `page_extraction_llm`, and rerun summaries aren't. Wrapping at the `BaseChatModel.ainvoke` level would cover all of them.
- The LLM duration is measured as wall time between the query and response logs, so it includes any retries the provider SDK does internally (`ChatAnthropic.max_retries`).
- `browser_use.beta.Agent` hands actions to the Rust session and bypasses the registry, so this dashboard won't see them.

## Regenerating the diagram

`static/agent-architecture.html` is a self-contained Archify export and needs no build step. Its source spec (`agent-architecture.json`) isn't in the repo yet. Once it's added, regenerate the HTML with:

```bash
node /path/to/archify/bin/archify.mjs deliver architecture \
  examples/ui/agent_dashboard/agent-architecture.json \
  examples/ui/agent_dashboard/static/agent-architecture.html \
  --quality showcase --json
```
