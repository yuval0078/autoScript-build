from fastapi import FastAPI

from .api.experiments import router as experiments_router
from .api.health import router as health_router
from .api.results import router as results_router
from .config import get_settings


def create_app():
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.5.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )
    application.include_router(health_router)
    application.include_router(experiments_router)
    application.include_router(results_router)
    return application


app = create_app()
