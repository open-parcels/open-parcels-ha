"""Connection actions and their jobs: what the buttons, the job sensor and the events read.

The server declares each provider's actions in GET /carriers; a task action runs as a job,
reported by GET /jobs and the `job.*` live events (the server's
docs/connection-actions-and-jobs.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN, JOB_EVENTS
from .merge import OpenParcelsData, connection_of, merge_job

if TYPE_CHECKING:
    from .coordinator import OpenParcelsCoordinator

# The job states, as the server sends them; the job sensor's options.
JOB_STATES = ("queued", "running", "paused", "done", "partial", "failed", "cancelled")
# Live events fired on the Home Assistant bus, by job event.
_BUS_EVENTS = {
    "job.paused": f"{DOMAIN}_job_paused",
    "job.finished": f"{DOMAIN}_job_finished",
}


def actions_by_provider(
    carriers: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Each provider's declared actions, from GET /carriers."""
    return {
        str(carrier["id"]): [a for a in carrier.get("actions") or [] if a.get("id")]
        for carrier in carriers
        if carrier.get("id")
    }


def newest_jobs(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each connection's newest job (GET /jobs lists the newest first)."""
    newest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        connection_id = str(job.get("connection_id") or "")
        if connection_id and connection_id not in newest:
            newest[connection_id] = job
    return newest


def connection_actions(
    data: OpenParcelsData, connection_id: str
) -> list[dict[str, Any]]:
    """The actions of a connection's provider that apply to its kind."""
    connection = data.connections.get(connection_id) or {}
    kind = connection.get("kind")
    return [
        action
        for action in data.actions.get(str(connection.get("provider")), [])
        if kind in (action.get("connection_kinds") or [])
    ]


def button_actions(data: OpenParcelsData, connection_id: str) -> list[dict[str, Any]]:
    """Actions offered as buttons: tasks marked for HA that need no input or confirmation.

    A destructive action is never a button: HA cannot ask for its confirmation.
    """
    return [
        action
        for action in connection_actions(data, connection_id)
        if action.get("kind") == "task"
        and action.get("expose_to_ha") is True
        and action.get("destructive") is not True
        and action.get("input_required") is not True
    ]


def button_keys(data: OpenParcelsData | None) -> set[str]:
    """`{connection_id}:{action_id}` for every action button there should be."""
    if data is None:
        return set()
    return {
        f"{connection_id}:{action['id']}"
        for connection_id in data.connections
        for action in button_actions(data, connection_id)
    }


def handle_job_event(
    coordinator: OpenParcelsCoordinator, event: str, payload: dict[str, Any]
) -> None:
    """Apply a `job.*` live event; a paused or finished job also goes on the bus."""
    if event not in JOB_EVENTS:
        return
    job = merge_job(coordinator.data, payload)
    if job is None:
        return
    bus_event = _BUS_EVENTS.get(event)
    if bus_event is not None:
        connection_id = str(job["connection_id"])
        connection = connection_of(payload)
        coordinator.fire_once(
            (bus_event, str(job.get("id"))),
            [job.get("state"), job.get("reason"), job.get("updated_at")],
            {
                "job_id": job.get("id"),
                "connection_id": connection_id,
                "provider": job.get("provider"),
                "label": connection.get("label"),
                "action": job.get("action"),
                "state": job.get("state"),
                "reason": job.get("reason"),
                "summary": job.get("summary"),
                "resume_at": job.get("resume_at"),
                "device_id": coordinator.device_id_for_connection(connection_id),
                "hub_device_id": coordinator.device_id_for_connection(None),
            },
        )
    coordinator.async_update_listeners()
