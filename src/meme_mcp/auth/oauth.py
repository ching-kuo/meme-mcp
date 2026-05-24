from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/auth")


@router.get("/login")
async def login() -> dict[str, str]:
    return {"status": "oauth_not_configured_in_test_slice"}

