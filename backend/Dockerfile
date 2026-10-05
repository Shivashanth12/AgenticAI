FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY backend/app ./backend/app
COPY backend/app/orchestration/templates/acceptance.py.txt /opt/acceptance/test_acceptance.py
COPY backend/alembic ./backend/alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
