import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

from app.api.activity import router as activity_router
from app.api.assessment import router as assessment_router
from app.api.assessment import workflow_router as workflow_assessment_router
from app.api.auth import router as auth_router
from app.api.links import redirect_router
from app.api.links import router as links_router
from app.api.workflows import router as workflows_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import bind_request_context, configure_logging, logger
from app.db.base import SessionFactory, engine

settings = get_settings()
configure_logging(settings.log_level)
REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "route"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("application.started", environment=settings.environment)
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Governed agentic SDLC orchestration and URL-shortening APIs.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = bind_request_context(request.headers.get("x-correlation-id"))
    request.state.correlation_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            "http.unhandled",
            method=request.method,
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        response = JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": "https://agentic-url.local/problems/internal-error",
                "title": "INTERNAL ERROR",
                "status": 500,
                "detail": "An unexpected error occurred",
                "code": "INTERNAL_ERROR",
                "correlationId": request_id,
                "retryable": False,
            },
        )
    duration = time.perf_counter() - started
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    response.headers["x-correlation-id"] = request_id
    REQUESTS.labels(request.method, template, response.status_code).inc()
    LATENCY.labels(request.method, template).observe(duration)
    logger.info(
        "http.completed",
        method=request.method,
        path=template,
        status=response.status_code,
        duration_ms=round(duration * 1000, 2),
    )
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        media_type="application/problem+json",
        content={
            "type": f"https://agentic-url.local/problems/{exc.code.lower()}",
            "title": exc.code.replace("_", " "),
            "status": exc.status,
            "detail": exc.message,
            "code": exc.code,
            "correlationId": request.state.correlation_id,
            "retryable": exc.retryable,
            **({"errors": exc.details} if exc.details else {}),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "https://agentic-url.local/problems/request-validation",
            "title": "REQUEST VALIDATION",
            "status": 422,
            "detail": "Request validation failed",
            "code": "REQUEST_VALIDATION_ERROR",
            "correlationId": request.state.correlation_id,
            "retryable": False,
            "errors": [
                {"field": ".".join(str(value) for value in error["loc"]), "message": error["msg"]}
                for error in exc.errors()
            ],
        },
    )


@app.get("/health/live", tags=["health"])
async def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def ready() -> dict:
    async with SessionFactory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(links_router)
app.include_router(workflows_router)
app.include_router(activity_router)
app.include_router(assessment_router)
app.include_router(workflow_assessment_router)
app.include_router(auth_router)
app.include_router(redirect_router)
