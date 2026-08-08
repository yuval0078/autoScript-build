FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 autoscript

COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=autoscript:autoscript backend/alembic.ini ./alembic.ini
COPY --chown=autoscript:autoscript backend/migrations ./migrations
COPY --chown=autoscript:autoscript backend/app ./app
COPY --chown=autoscript:autoscript schemas/data-contracts ./schemas/data-contracts

USER autoscript

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && python -m app.bootstrap && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
