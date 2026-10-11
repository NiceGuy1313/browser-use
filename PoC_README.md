# PoC: Browser Use Agent 호출 이력 대시보드

browser-use Agent가 **LLM에 무엇을 물었고, 어떤 tool을 어떤 파라미터로 실행했는지** step 단위로 뽑아서 웹에서 보여주는 PoC입니다.

- **왼쪽:** Archify로 그린 Agent loop 구조도. LLM 호출 경계와 tool 실행 경계가 어디인지 표시돼 있습니다.
- **오른쪽:** 실행 이력. step마다 LLM 호출 → LLM이 고른 액션 → tool 실행(파라미터, 걸린 시간, 결과/에러) 순서로 쌓입니다.

## 1. 실행 방법

준비물: Python 3.11+, [uv](https://docs.astral.sh/uv/), Chrome, Anthropic API 키

```bash
git clone https://github.com/NiceGuy1313/browser-use.git
cd browser-use

# API 키 설정 (처음 한 번)
cp .env.example .env
# .env를 열어서 아래 줄의 주석(#)을 지우고 실제 키를 넣기
# ANTHROPIC_API_KEY=sk-ant-...

uv sync
uv run uvicorn examples.ui.agent_dashboard.app:app --port 8000
```

브라우저에서 http://127.0.0.1:8000 을 엽니다.

## 2. 사용 방법

1. 오른쪽 위 입력칸에 **task**, **최대 step 수**, **모델**(기본 `claude-sonnet-5`)을 넣고 **Run**을 누릅니다.
2. 에이전트가 조작하는 Chrome 창이 따로 뜨고, 오른쪽 **Call history**에 step별 이력이 실시간으로 쌓입니다.
   - `LLM` 행: LLM 호출 1회와 그 응답으로 나온 액션 목록
   - `TOOL` 행: 실제 실행된 tool, 파라미터, 걸린 시간, 결과. 실패하면 빨간색으로 표시됩니다.
   - 상단 카운터: steps / LLM 호출 수 / tool 실행 수 / 에러 수
3. **Raw log** 탭에서는 가공하지 않은 이벤트 전체를 시간순으로 볼 수 있습니다.
4. **Export JSON**으로 이번 실행의 전체 이력을 받습니다. 실행이 끝날 때마다 `examples/ui/agent_dashboard/runs/<run_id>.json`에도 자동 저장됩니다(git에는 안 올라감).

실행 도중이나 끝난 뒤에 페이지를 새로 열어도 이력이 처음부터 다시 표시됩니다. 한 번에 하나의 실행만 할 수 있습니다.

## 3. 이력 JSON 형식

```jsonc
{
  "id": "…", "task": "…", "model": "claude-sonnet-5", "status": "finished",
  "events": [
    { "seq": 6,  "kind": "llm_query",    "step": 1, "data": { "model": "…", "messages": 2 } },
    { "seq": 7,  "kind": "llm_response", "step": 1, "data": { "actions": [{ "navigate": { "url": "…" } }], "duration_seconds": 1.8 } },
    { "seq": 12, "kind": "tool_start",   "step": 1, "data": { "action": "navigate", "params": "{'url': '…'}" } },
    { "seq": 14, "kind": "tool_end",     "step": 1, "data": { "action": "navigate", "duration_seconds": 0.28 } },
    { "seq": 15, "kind": "step",         "step": 1, "data": { "url": "…", "next_goal": "…", "results": [{ "action": "navigate", "error": null }] } }
  ]
}
```

`kind`는 `run`, `llm_query`, `llm_response`, `tool_start`, `tool_end`, `tool_error`, `step`, `log` 중 하나입니다. **액션의 성공/실패는 `step` 이벤트의 `results[].error`로 판단하세요.** browser-use는 대부분의 실패를 예외 대신 결과 값으로 돌려주기 때문에, 실패한 액션도 `tool_end`로 찍힙니다.

## 4. 코드 위치

| 무엇 | 어디 |
|---|---|
| LLM 호출 로그 (`[LLM_QUERY]`, `[LLM_RESPONSE]`) | `browser_use/agent/service.py` → `Agent.get_model_output` |
| tool 실행 로그 (`[TOOL_EXECUTION_START/END/ERROR]`) | `browser_use/tools/registry/service.py` → `Registry.execute_action` |
| 대시보드 서버 (로그 → 구조화 이력, SSE, export) | `examples/ui/agent_dashboard/app.py` |
| 화면 | `examples/ui/agent_dashboard/static/index.html` |
| Archify 구조도 | `examples/ui/agent_dashboard/static/agent-architecture.html` |

API 키나 `sensitive_data` 값은 로그에 남기 전에 `<secret>키이름</secret>`으로 치환됩니다. API 키는 서버에서만 읽고 브라우저로 보내지 않습니다.

## 5. 아직 안 되는 것 / 다음 작업

- **메인 루프의 LLM 호출만 잡힙니다.** judge, 메시지 compaction, `extract` 액션 안의 LLM 호출, rerun 요약은 이력에 안 나옵니다. `BaseChatModel.ainvoke` 레벨에서 감싸면 한 번에 커버됩니다.
- **tool이 타임아웃(기본 180초)으로 취소되면** `tool_start`만 남고 `tool_end`/`tool_error`가 없습니다.
- **LLM이 잘못된 파라미터를 보내 검증에 실패하면** tool 로그가 아예 안 남습니다.
- **LLM 소요 시간에는 SDK 내부 재시도 시간이 포함됩니다.** `[LLM_QUERY]` 1건이 실제 HTTP 요청 여러 번일 수 있습니다.
- `browser_use.beta.Agent`는 다른 실행 경로를 써서 이 대시보드에 안 잡힙니다.
- Archify 구조도의 원본 spec(`agent-architecture.json`)은 아직 저장소에 없습니다. 구조도를 수정하려면 원본이 필요합니다.

더 자세한 내부 동작은 `examples/ui/agent_dashboard/README.md`를 보세요.
