from __future__ import annotations

from fastapi import APIRouter

from application.registration_queries import registration_capabilities


router = APIRouter(prefix="/registration", tags=["registration"])


@router.get("/capabilities")
def get_registration_capabilities():
    return registration_capabilities.inspect(run_checks=False)


@router.post("/capabilities/test")
def test_registration_capabilities():
    """Run local dependency checks only; this never starts a registration task."""
    return registration_capabilities.inspect(run_checks=True)
