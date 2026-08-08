from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.storage import get_object_storage


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready(
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
):
    components = {"database": "ready", "object_storage": "ready"}

    try:
        database.execute(text("SELECT 1"))
    except Exception:
        components["database"] = "unavailable"

    try:
        if not storage.bucket_exists():
            components["object_storage"] = "bucket_missing"
    except Exception:
        components["object_storage"] = "unavailable"

    is_ready = all(value == "ready" for value in components.values())
    payload = {
        "status": "ready" if is_ready else "not_ready",
        "components": components,
    }
    return JSONResponse(payload, status_code=200 if is_ready else 503)
