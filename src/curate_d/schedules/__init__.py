"""Deployment-time reconciliation of the application's persisted DBOS schedules.

Schedule rows are shared by every worker and every version of the application, so no
worker writes them. One command, `python -m curate_d.schedules reconcile`, run by the
deploy job once a version's workers are up, is the only thing that changes them: it
applies the schedules this build declares (`workflows/registry.py`) and deletes this
application's stored schedules that it no longer declares. Restarting or scaling workers
leaves the rows alone. See `deployment.py` for the procedure and
`runtime/schedule_fence.py` for what keeps a stale command from writing.
"""
