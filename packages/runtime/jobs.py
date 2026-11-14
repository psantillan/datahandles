"""Asynchronous jobs (ARCHITECTURE.md section 6.2). A job is a resource, and a caller holds a handle to it.

The job table in the registry is the durable state. This adapter gives the job handle its three
capabilities. The runtime owns the thread that runs the jobs.
"""
from ..core import HandleError, capability
from .adapter import Known, ResourceAdapter, ResourceRef, Value

# queued -> running -> completed | failed | cancelled. A queued job can also go to cancelled.
TERMINAL = ("completed", "failed", "cancelled")


class JobAdapter(ResourceAdapter):
    name = "job"

    def __init__(self, registry, now):
        self._registry, self._now = registry, now

    def _job(self, ref: ResourceRef) -> dict:
        return self._registry.job(ref.locator["job"])

    def open(self, source: dict) -> ResourceRef:
        raise HandleError("INVALID_INPUT", "a job handle comes from an asynchronous operation, not from handle.create")

    def describe(self, ref: ResourceRef) -> dict:
        job = self._job(ref)
        return {"description": f"The asynchronous run of {job['capability']['name']} on {job['operation']['target']}."}

    def discover(self, ref: ResourceRef) -> list[dict]:
        # job.result gives what the operation gives, so it has the `returns` and the size limit of that capability.
        of = self._job(ref)["capability"]
        return [
            capability("job.status", "Return the state of the job: queued, running, completed, failed, or cancelled."),
            capability("job.cancel", "Stop the job. A queued job ends now. The output of a running job is discarded.", write=True),
            capability("job.result", "Return what the operation gives. Before the job ends the answer is the error JOB_NOT_FINISHED, and a later call can succeed.",
                       returns=of["returns"], max_output_bytes=of.get("limits", {}).get("max_output_bytes")),
        ]

    def observe(self, ref: ResourceRef) -> dict:
        job = self._job(ref)
        return {"version": f"{job['state']}:{job['attempts']}", "state": job["state"]}

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool) -> Value | Known:
        if capability == "job.cancel":
            self._registry.cancel_job(ref.locator["job"], self._now())
        job = self._job(ref)
        if capability == "job.result":
            return self._result(job)
        status = {k: job[k] for k in ("state", "attempts", "submitted_at", "started_at", "finished_at") if job[k] is not None}
        status.update(operation_id=job["id"], target=job["operation"]["target"], capability=job["capability"]["name"])
        if job["cancel"] and job["state"] == "running":
            status["cancel_requested"] = True
        if job["state"] == "failed":
            status["error"] = job["result"]["error"]
        return Value(status, f"{job['state']}:{job['attempts']}")

    def _result(self, job: dict) -> Value | Known:
        state, result = job["state"], job["result"]
        if state not in TERMINAL:
            raise HandleError("JOB_NOT_FINISHED", f"the job is {state}", retryable=True, details={"state": state})
        if state == "cancelled":
            raise HandleError("JOB_CANCELLED", "the job was cancelled and has no result")
        if state == "failed":
            e = result["error"]
            raise HandleError(e["code"], e["message"], retryable=e["retryable"], details=e.get("details"))
        if "handles" in result:
            return Known(result["handles"][0]["id"])
        return Value(result["output"], result["metadata"].get("source_version", ""))
