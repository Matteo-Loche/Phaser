"""Compute job endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ... import config
from ...services.compute import (
    delete_job,
    get_job,
    get_job_result,
    queue_snapshot,
    try_admit_compute_job,
)
from ..models import ComputeRequest

router = APIRouter(tags=["compute"])


@router.get("/api/queue")
def get_queue():
    return queue_snapshot()


@router.post("/api/compute")
def start_compute(body: ComputeRequest):
    if not body.totals:
        raise HTTPException(400, "At least one total concentration is required.")
    n_pts = body.ph_levels * body.pe_levels
    if n_pts > config.MAX_GRID_POINTS:
        raise HTTPException(400, f"Grid too large ({n_pts} > {config.MAX_GRID_POINTS}).")

    job_id = try_admit_compute_job(body)
    if job_id is None:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Server compute queue is full.",
                "error_code": "queue_full",
            },
        )

    job = get_job(job_id) or {}
    return {
        "job_id": job_id,
        "status": job.get("status", "queued"),
        "queue_position": job.get("queue_position"),
        "queue_size": job.get("queue_size"),
    }


@router.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {k: v for k, v in job.items() if k != "result"}


@router.get("/api/job/{job_id}/result")
def job_result(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done":
        raise HTTPException(409, f"Job status: {job.get('status')}")
    result = get_job_result(job_id)
    if result is None:
        raise HTTPException(409, f"Job status: {job.get('status')}")
    return result


@router.delete("/api/job/{job_id}")
def remove_job(job_id: str):
    if not delete_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"deleted": True}
