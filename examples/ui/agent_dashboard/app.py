"""Local dashboard: run a Browser Use agent and stream its LLM/tool call history."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from uuid_extensions import uuid7str

if TYPE_CHECKING:
	from browser_use import Agent
	from browser_use.llm.base import BaseChatModel

APP_DIR = Path(__file__).resolve().parent
# Both agent dashboards serve the same paths on the same port; make browsers revalidate instead of reusing the other one's page.
NO_CACHE = {'Cache-Control': 'no-cache'}
RUNS_DIR = APP_DIR / 'runs'
load_dotenv(APP_DIR.parents[2] / '.env')

TraceKind = Literal['run', 'llm_query', 'llm_response', 'tool_start', 'tool_end', 'tool_error', 'step', 'log']
TAG_TO_KIND: dict[str, TraceKind] = {
	'[LLM_QUERY]': 'llm_query',
	'[LLM_RESPONSE]': 'llm_response',
	'[TOOL_EXECUTION_START]': 'tool_start',
	'[TOOL_EXECUTION_END]': 'tool_end',
	'[TOOL_EXECUTION_ERROR]': 'tool_error',
}
ANSI_PATTERN = re.compile(r'\x1b\[[0-9;]*m')


class RunRequest(BaseModel):
	"""Validated inputs for one dashboard agent run."""

	model_config = ConfigDict(extra='forbid')

	task: str = Field(min_length=3, max_length=2000)
	max_steps: int = Field(default=10, ge=1, le=50)
	model: str = Field(default='claude-sonnet-5', min_length=1, max_length=100)


class TraceEvent(BaseModel):
	"""One entry of the run history, streamed to clients and exported as JSON."""

	seq: int
	run_id: str
	timestamp: str
	kind: TraceKind
	level: str = 'INFO'
	step: int | None = None
	message: str
	data: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
	"""Full history of a single agent run."""

	id: str = Field(default_factory=uuid7str)
	task: str
	model: str
	max_steps: int
	status: Literal['running', 'finished', 'failed'] = 'running'
	started_at: str = Field(default_factory=lambda: _now())
	finished_at: str | None = None
	events: list[TraceEvent] = Field(default_factory=list)


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


class TraceHub:
	"""Owns the current run record and fans its events out to SSE subscribers."""

	def __init__(self) -> None:
		self._subscribers: set[asyncio.Queue[TraceEvent]] = set()
		self.run: RunRecord | None = None
		self.current_step: int | None = None
		self.published_steps = 0
		self._llm_started_at: float | None = None

	def subscribe(self) -> asyncio.Queue[TraceEvent]:
		queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=2000)
		self._subscribers.add(queue)
		return queue

	def unsubscribe(self, queue: asyncio.Queue[TraceEvent]) -> None:
		self._subscribers.discard(queue)

	def start(self, request: RunRequest) -> RunRecord:
		self.run = RunRecord(task=request.task, model=request.model, max_steps=request.max_steps)
		self.current_step = None
		self.published_steps = 0
		self._llm_started_at = None
		return self.run

	def publish(
		self,
		kind: TraceKind,
		message: str,
		*,
		level: str = 'INFO',
		step: int | None = None,
		data: dict[str, Any] | None = None,
	) -> None:
		run = self.run
		assert run is not None, 'publish() called without an active run'
		data = dict(data or {})
		if kind == 'llm_query':
			self.current_step = step
			self._llm_started_at = time.monotonic()
		elif kind == 'llm_response' and self._llm_started_at is not None:
			data['duration_seconds'] = round(time.monotonic() - self._llm_started_at, 3)
			self._llm_started_at = None
		if step is None and kind in ('tool_start', 'tool_end', 'tool_error'):
			step = self.current_step
		event = TraceEvent(
			seq=len(run.events),
			run_id=run.id,
			timestamp=_now(),
			kind=kind,
			level=level,
			step=step,
			message=message,
			data=data,
		)
		run.events.append(event)
		for queue in tuple(self._subscribers):
			if queue.full():
				queue.get_nowait()
			queue.put_nowait(event)

	def finish(self, status: Literal['finished', 'failed']) -> Path:
		run = self.run
		assert run is not None
		run.status = status
		run.finished_at = _now()
		RUNS_DIR.mkdir(exist_ok=True)
		path = RUNS_DIR / f'{run.id}.json'
		path.write_text(run.model_dump_json(indent=2), encoding='utf-8')
		return path


hub = TraceHub()
run_task: asyncio.Task[None] | None = None


def _parse_boundary_record(kind: TraceKind, args: tuple[Any, ...]) -> tuple[int | None, str, dict[str, Any]]:
	"""Turn the positional args of a boundary log call into structured trace data."""

	if kind == 'llm_query':
		step, model, n_messages, output_format = args
		return (
			int(step),
			f'{model} · {n_messages} messages',
			{'model': model, 'messages': n_messages, 'output_format': output_format},
		)
	if kind == 'llm_response':
		step, actions_json = args
		actions = json.loads(actions_json)
		names = ', '.join(next(iter(a)) for a in actions if a) or 'no action'
		return int(step), names, {'actions': actions}
	if kind == 'tool_start':
		action, params = args
		return None, action, {'action': action, 'params': params}
	action, type_name, duration = args
	key = 'result_type' if kind == 'tool_end' else 'error_type'
	return None, action, {'action': action, key: type_name, 'duration_seconds': round(float(duration), 3)}


class DashboardLogHandler(logging.Handler):
	"""Convert Browser Use log records into trace events on the event loop."""

	def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
		super().__init__(logging.DEBUG)
		self.loop = loop
		self.loop_thread_id = threading.get_ident()

	def emit(self, record: logging.LogRecord) -> None:
		try:
			boundary: TraceKind | None = next(
				(k for tag, k in TAG_TO_KIND.items() if isinstance(record.msg, str) and record.msg.startswith(tag)), None
			)
			kind: TraceKind
			step: int | None = None
			if boundary is not None:
				kind = boundary
				step, message, data = _parse_boundary_record(kind, record.args if isinstance(record.args, tuple) else ())
			elif record.levelno >= logging.INFO:
				kind = 'log'
				message, data = ANSI_PATTERN.sub('', record.getMessage()), {'logger': record.name}
			else:
				return
		except Exception:
			self.handleError(record)
			return

		def publish() -> None:
			hub.publish(kind, message, level=record.levelname, step=step, data=data)

		# Publish inline on the loop thread so ordering matches the on_step_end hook.
		if threading.get_ident() == self.loop_thread_id:
			publish()
		else:
			self.loop.call_soon_threadsafe(publish)


async def _publish_step(agent: Agent) -> None:
	"""on_step_end hook: emit the authoritative per-step result, including soft action errors."""

	for item in agent.history.history[hub.published_steps :]:
		output = item.model_output
		actions = [a.model_dump(exclude_unset=True, mode='json') for a in output.action] if output else []
		results = [
			{
				'action': next(iter(actions[i]), None) if i < len(actions) else None,
				'error': r.error,
				'is_done': r.is_done,
				'success': r.success,
				'extracted_content': (r.extracted_content or '')[:1000] or None,
			}
			for i, r in enumerate(item.result)
		]
		meta = item.metadata
		errors = sum(1 for r in results if r['error'])
		hub.publish(
			'step',
			f'{len(results)} action(s), {errors} error(s)',
			level='ERROR' if errors else 'INFO',
			step=meta.step_number if meta else None,
			data={
				'url': item.state.url,
				'title': item.state.title,
				'duration_seconds': round(meta.duration_seconds, 3) if meta else None,
				'evaluation_previous_goal': output.evaluation_previous_goal if output else None,
				'memory': output.memory if output else None,
				'next_goal': output.next_goal if output else None,
				'results': results,
			},
		)
	hub.published_steps = len(agent.history.history)


def build_llm(request: RunRequest) -> BaseChatModel:
	from browser_use import ChatAnthropic

	return ChatAnthropic(model=request.model)


async def execute_agent(request: RunRequest, llm: BaseChatModel) -> None:
	"""Run one Browser Use agent while its boundary logs flow into the trace hub."""

	from browser_use import Agent

	global run_task
	logger = logging.getLogger('browser_use')
	previous_level = logger.level
	handler = DashboardLogHandler(asyncio.get_running_loop())
	logger.addHandler(handler)
	logger.setLevel(logging.DEBUG)
	hub.publish('run', f'Run started: {request.task}', data=request.model_dump())
	status: Literal['finished', 'failed'] = 'failed'
	try:
		agent = Agent(task=request.task, llm=llm, use_judge=False, max_actions_per_step=5)
		history = await agent.run(max_steps=request.max_steps, on_step_end=_publish_step)
		status = 'finished'
		hub.publish(
			'run',
			f'Run finished: steps={history.number_of_steps()} success={history.is_successful()}',
			data={
				'steps': history.number_of_steps(),
				'success': history.is_successful(),
				'final_result': history.final_result(),
				'duration_seconds': round(history.total_duration_seconds(), 3),
			},
		)
	except Exception as exc:
		hub.publish('run', f'Run failed: {type(exc).__name__}: {exc}', level='ERROR')
	finally:
		logger.removeHandler(handler)
		logger.setLevel(previous_level)
		hub.finish(status)
		run_task = None


app = FastAPI(title='Browser Use Agent Observatory')


@app.get('/')
async def index() -> FileResponse:
	return FileResponse(APP_DIR / 'static' / 'index.html', headers=NO_CACHE)


@app.get('/architecture')
async def architecture() -> FileResponse:
	return FileResponse(APP_DIR / 'static' / 'agent-architecture.html', headers=NO_CACHE)


@app.get('/api/status')
async def status() -> dict[str, Any]:
	run = hub.run
	return {
		'has_api_key': bool(os.getenv('ANTHROPIC_API_KEY')),
		'running': bool(run_task and not run_task.done()),
		'run': run.model_dump(exclude={'events'}) if run else None,
	}


@app.post('/api/run', status_code=202)
async def start_run(request: RunRequest) -> dict[str, str]:
	global run_task
	if not os.getenv('ANTHROPIC_API_KEY'):
		raise HTTPException(status_code=400, detail='ANTHROPIC_API_KEY is missing from .env')
	if run_task and not run_task.done():
		raise HTTPException(status_code=409, detail='An agent is already running')
	run = hub.start(request)
	run_task = asyncio.create_task(execute_agent(request, build_llm(request)))
	return {'status': 'started', 'run_id': run.id}


@app.get('/api/runs/current/trace.json')
async def current_trace() -> JSONResponse:
	run = hub.run
	if run is None:
		raise HTTPException(status_code=404, detail='No run yet')
	return JSONResponse(
		run.model_dump(mode='json'),
		headers={'Content-Disposition': f'attachment; filename="browser-use-trace-{run.id}.json"'},
	)


@app.get('/api/events')
async def events() -> StreamingResponse:
	"""SSE: replay the current run's history, then stream new events live."""

	async def stream() -> AsyncIterator[str]:
		queue = hub.subscribe()
		try:
			run = hub.run
			snapshot = run.model_dump(mode='json', exclude={'events'}) if run else None
			yield f'event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n'
			for event in list(run.events) if run else []:
				yield f'data: {event.model_dump_json()}\n\n'
			while True:
				try:
					event = await asyncio.wait_for(queue.get(), timeout=15)
					yield f'data: {event.model_dump_json()}\n\n'
				except TimeoutError:
					yield ': keepalive\n\n'
		finally:
			hub.unsubscribe(queue)

	return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})
