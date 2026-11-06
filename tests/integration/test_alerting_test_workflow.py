"""The deliberate worker failure, run by a launched worker, emits what the worker alert matches.

The alert watches the workers' records for an ERROR level or an exception. A dequeued
workflow failing produces two such records: its spans end with an error status, and DBOS
logs the exception at ERROR through the handler it attaches to Logfire's logger provider.
Tracing is configured over an in-memory exporter before DBOS is constructed, as a worker's
entry point orders it, so nothing is sent. The workflow is enqueued, by its registered
name, on a queue only this process listens to, so no stack container can take it.
"""

import logging
from collections.abc import Iterator
from uuid import uuid4

import logfire
import pytest
from dbos import DBOS, EnqueueOptions, WorkflowHandleAsync, WorkflowStatusString
from logfire.testing import TestExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode

from curate_d.config import Settings
from curate_d.observability import configure_tracing
from curate_d.runtime import dbos_app
from curate_d.runtime.lease import ExecutorLease
from curate_d.workflows import alerting_test
from curate_d.workflows.alerting_test import ALERTING_TEST_WORKFLOW, WorkerAlertingTestError

pytestmark = pytest.mark.integration

_RUN = uuid4().hex[:8]
EXECUTOR = f"alerting-test-{_RUN}"
QUEUE = f"alerting-test-{_RUN}"
WORKFLOW_ID = "dbos.operation.workflow_id"
# The alert's `level >= 17`: Logfire's span processor sets this from an error status.
LEVEL_NUM = "logfire.level_num"
ERROR_LEVEL = 17


class _Records(logging.Handler):
    """Keeps every record the `dbos` logger emits."""

    def __init__(self) -> None:
        """Keep nothing yet."""
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record."""
        self.records.append(record)


@pytest.fixture
def worker(settings: Settings) -> Iterator[tuple[TestExporter, _Records]]:
    """Configure tracing over an in-memory exporter, then launch DBOS as a leased worker."""
    captured = TestExporter()
    records = _Records()
    lease = ExecutorLease.for_settings(settings, EXECUTOR, version=dbos_app.APPLICATION_VERSION)
    lease.acquire(timeout=10)
    DBOS.destroy()
    configure_tracing(
        service_name="test",
        service_version="0",
        local_processor=SimpleSpanProcessor(captured),
    )
    dbos_app.configure(settings, executor_id=EXECUTOR)
    DBOS.listen_queues([QUEUE])
    DBOS.launch()
    DBOS.register_queue(QUEUE)
    logging.getLogger("dbos").addHandler(records)
    try:
        yield captured, records
    finally:
        logging.getLogger("dbos").removeHandler(records)
        rows = DBOS.list_workflows(queue_name=QUEUE)
        if rows:
            DBOS.delete_workflows([row.workflow_id for row in rows])
        DBOS.destroy()
        # Logfire has no public reset; reconfiguring shuts down the provider `captured` hangs on.
        logfire.configure(send_to_logfire=False, console=False, metrics=False)
        lease.release()


def _ended(exporter: TestExporter, workflow_id: str) -> list[ReadableSpan]:
    """Return the finished spans that carry `workflow_id`, leaving out Logfire's pending ones."""
    return [
        span
        for span in exporter.exported_spans
        if span.end_time is not None
        and span.attributes is not None
        and span.attributes.get("logfire.span_type") != "pending_span"
        and span.attributes.get(WORKFLOW_ID) == workflow_id
    ]


async def test_the_dequeued_failure_ends_in_error_spans_and_an_error_log(
    settings: Settings, worker: tuple[TestExporter, _Records]
) -> None:
    """ERROR row, error-level workflow and step spans, and an ERROR log of the test's error."""
    exporter, records = worker
    workflow_id = f"alerting-test:worker-io:{_RUN}"
    client = dbos_app.client(settings)
    try:
        options: EnqueueOptions = {
            "workflow_name": ALERTING_TEST_WORKFLOW,
            "queue_name": QUEUE,
            "workflow_id": workflow_id,
            "app_version": dbos_app.APPLICATION_VERSION,
        }
        handle: WorkflowHandleAsync[None] = await client.enqueue_async(options, "io")
        with pytest.raises(WorkerAlertingTestError, match="deliberate worker error"):
            await handle.get_result()
    finally:
        client.destroy()

    status = await DBOS.get_workflow_status_async(workflow_id)
    assert status is not None
    assert status.status == WorkflowStatusString.ERROR.value

    spans = {span.name: span for span in _ended(exporter, workflow_id)}
    workflow = spans[alerting_test.fail_for_alerting_test.__qualname__]
    step = spans[alerting_test._fail_step.__qualname__]
    for span in (workflow, step):
        assert span.status.status_code is StatusCode.ERROR, span.name
        assert "deliberate worker error" in (span.status.description or "")
        assert span.attributes is not None
        level = span.attributes.get(LEVEL_NUM)
        assert isinstance(level, int) and level >= ERROR_LEVEL, (span.name, level)
    assert step.parent is not None and step.parent.span_id == workflow.context.span_id

    errors = [record for record in records.records if record.levelno >= logging.ERROR]
    assert any(
        record.exc_info is not None and isinstance(record.exc_info[1], WorkerAlertingTestError)
        for record in errors
    ), [record.getMessage() for record in records.records]
