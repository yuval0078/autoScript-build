from fastapi import FastAPI

from .api.experiments import router as experiments_router
from .api.analysis import router as analysis_router
from .api.auth import router as auth_router
from .api.health import router as health_router
from .api.results import router as results_router
from .config import get_settings
from .observability import install_observability


def create_app():
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.7.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )
    install_observability(application)
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(experiments_router)
    application.include_router(results_router)
    application.include_router(analysis_router)
    return application


app = create_app()
