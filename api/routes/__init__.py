"""Aggregate API route modules."""
from fastapi import APIRouter

from . import compute, config_routes, databases, elements, health, phases

router = APIRouter()
router.include_router(health.router)
router.include_router(config_routes.router)
router.include_router(databases.router)
router.include_router(elements.router)
router.include_router(phases.router)
router.include_router(compute.router)
