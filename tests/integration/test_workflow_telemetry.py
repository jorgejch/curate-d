"""DBOS's workflow and step spans on a launched worker, exported through Logfire's providers.

Tracing is configured over an in-memory exporter before DBOS is constructed, as a worker's
entry point orders it, so nothing is sent. The child workflow is enqueued from inside the
parent on a queue this process registers and listens to, and is dequeued by this process.
"""

import logging
from collections.abc import Iterator
from uuid import uuid4

import logfire
import pytest
from dbos import DBOS, PropagateOtelContext, SetWorkflowID, WorkflowHandleAsync
from logfire.testing import TestExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from curate_d.config import Settings
from curate_d.observability import configure_tracing
from curate_d.runtime import dbos_app
from curate_d.runtime.lease import ExecutorLease

pytestmark = pytest.mark.integration

_RUN = uuid4().hex[:8]
EXECUTOR = f"telemetry-{_RUN}"
QUEUE = f"telemetry-{_RUN}"

WORKFLOW_ID = "dbos.operation.workflow_id"
EXECUTOR_ID = "dbos.executor.id"


@DBOS.step()
async def _first_step() -> int:
    """Return a constant, as a step with an effect would return its result."""
    return 1


@DBOS.step()
async def _second_step(value: int) -> int:
    """Return `value` incremented."""
    return value + 1


@DBOS.workflow()
async def _telemetry_child(value: int) -> int:
    """Return `value` doubled."""
    return value * 2


@DBOS.workflow()
async def _telemetry_parent(child_id: str) -> str:
    """Run two steps, then enqueue the child under `child_id`, as the pipeline hands off."""
    value = await _second_step(await _first_step())
    # DBOS persists the enqueuer's trace on the child's row only inside this context;
    # a bare enqueue roots the dequeued child on a trace of its own.
    with PropagateOtelContext(), SetWorkflowID(child_id):
        handle = await DBOS.enqueue_workflow_async(QUEUE, _telemetry_child, value)
    return handle.get_workflow_id()


@pytest.fixture
def exporter(settings: Settings) -> Iterator[TestExporter]:
    """Configure tracing over an in-memory exporter, then launch DBOS as a leased worker.

    Synchronous, as `test_recovery_schedule.py`'s worker fixture is: DBOS is configured and
    launched outside the test's event loop.
    """
    captured = TestExporter()
    lease = ExecutorLease.for_settings(settings, EXECUTOR, version=dbos_app.APPLICATION_VERSION)
    lease.acquire(timeout=10)
    DBOS.destroy()
    # Before DBOS is constructed: it binds its log handler to the logger provider then.
    configure_tracing(
        service_name="test",
        service_version="0",
        local_processor=SimpleSpanProcessor(captured),
    )
    dbos_app.configure(settings, executor_id=EXECUTOR)
    DBOS.listen_queues([QUEUE])
    DBOS.launch()
    DBOS.register_queue(QUEUE)
    try:
        yield captured
    finally:
        rows = DBOS.list_workflows(queue_name=QUEUE) + DBOS.list_workflows(
            name=_telemetry_parent.__qualname__
        )
        if rows:
            DBOS.cancel_workflows([row.workflow_id for row in rows])
            DBOS.delete_workflows([row.workflow_id for row in rows])
        DBOS.destroy()
        # Logfire has no public reset; reconfiguring shuts down the provider `captured` hangs on.
        logfire.configure(send_to_logfire=False, console=False, metrics=False)
        lease.release()


def _ended_spans(exporter: TestExporter, workflow_id: str) -> list[ReadableSpan]:
    """Return the finished spans that carry `workflow_id`, leaving out Logfire's pending ones."""
    return [
        span
        for span in exporter.exported_spans
        if span.end_time is not None
        and span.attributes is not None
        and span.attributes.get("logfire.span_type") != "pending_span"
        and span.attributes.get(WORKFLOW_ID) == workflow_id
    ]


async def test_a_workflow_its_steps_and_its_queued_child_share_one_trace(
    exporter: TestExporter,
) -> None:
    """The parent's two step spans hang off its span, and the dequeued child joins its trace."""
    parent_id = f"telemetry-parent-{_RUN}"
    child_id = f"telemetry-child-{_RUN}"
    with SetWorkflowID(parent_id):
        handle = await DBOS.start_workflow_async(_telemetry_parent, child_id)
    assert await handle.get_result() == child_id
    child_handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(child_id)
    assert await child_handle.get_result() == 4

    parent_spans = _ended_spans(exporter, parent_id)
    names = sorted(span.name for span in parent_spans)
    [workflow] = [span for span in parent_spans if span.name == _telemetry_parent.__qualname__]
    steps = [span for span in parent_spans if span is not workflow]
    assert sorted(span.name for span in steps) == [
        _first_step.__qualname__,
        _second_step.__qualname__,
    ], names
    trace_id = workflow.context.trace_id
    for step in steps:
        assert step.context.trace_id == trace_id
        assert step.parent is not None
        assert step.parent.span_id == workflow.context.span_id
    assert workflow.attributes is not None
    assert workflow.attributes[EXECUTOR_ID] == EXECUTOR

    [child] = [
        span
        for span in _ended_spans(exporter, child_id)
        if span.name == _telemetry_child.__qualname__
    ]
    assert child.context.trace_id == trace_id, (
        f"child trace {child.context.trace_id:032x} != parent trace {trace_id:032x}"
    )


def test_dbos_attaches_its_log_handler_to_logfires_logger_provider(
    exporter: TestExporter,
) -> None:
    """DBOS constructed after Logfire attaches an OpenTelemetry handler beside its console one."""
    handlers = logging.getLogger("dbos").handlers
    assert any(isinstance(handler, LoggingHandler) for handler in handlers), handlers
