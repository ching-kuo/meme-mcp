from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def index() -> dict[str, str]:
    return {"service": "meme-mcp"}

