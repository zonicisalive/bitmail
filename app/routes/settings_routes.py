"""Settings page API: read and change account-wide sending switches."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.app_settings import DEFAULTS, get_settings, update_settings

router = APIRouter(prefix="/api/settings", tags=["Settings"])


@router.get("")
async def read_settings() -> Dict[str, Any]:
    return {"settings": await get_settings(), "defaults": DEFAULTS}


@router.put("")
async def change_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"settings": await update_settings(changes)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
