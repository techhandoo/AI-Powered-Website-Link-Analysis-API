"""
AI-Powered Website Link Analysis API - Main FastAPI Application.

Provides:
- POST /link-analysis: Analyzes a target URL using website extraction, LLM intelligence,
  and competitor enrichment across 5 comprehensive sections.
- GET /health: Health check endpoint.
- GET /: Service info and navigation.
- Interactive Swagger docs at /docs, ReDoc at /redoc.
"""

from contextlib import asynccontextmanager
import logging
import sys
import time
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.schemas import (
    ErrorResponse,
    LinkAnalysisRequest,
    LinkAnalysisResponse,
)
from app.services.analysis_service import (
    AnalysisService,
    WebsiteTimeoutException,
    WebsiteUnreachableError,
    get_analysis_service,
)
from app.services.website_extractor import (
    WebsiteBlockedError,
    WebsiteExtractionError,
    WebsiteFetchError,
    WebsiteHTTPError,
    WebsiteNotFoundError,
    WebsiteSizeExceededError,
    WebsiteSSRFError,
    WebsiteTimeoutError,
)
from app.utils.validators import (
    InvalidURLError,
    SSRFError,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("link_analysis_api")

settings = get_settings()


# ============================================================================
# Application Lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifespan management."""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT} | LLM Provider: {settings.LLM_PROVIDER}")
    logger.info(f"Documentation available at http://{settings.HOST}:{settings.PORT}/docs")
    yield
    logger.info(f"Shutting down {settings.APP_NAME}")


# ============================================================================
# FastAPI Application Factory
# ============================================================================

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Production REST API that extracts and analyzes website content using advanced "
        "LLMs to generate competitor benchmarking, top target search queries, "
        "SEO improvements, core service offerings, and target audience identification."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Global Exception Handlers (Graceful Error Handling without Stack Leaks)
# ============================================================================

@app.exception_handler(SSRFError)
async def ssrf_error_handler(request: Request, exc: SSRFError):
    """Handles SSRF protection violations with HTTP 400."""
    logger.warning(f"SSRF violation blocked: {exc} | Path: {request.url.path}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="SSRF Attempt Blocked",
            detail=str(exc),
            code="SSRF_ATTEMPT_BLOCKED",
        ).model_dump(),
    )


@app.exception_handler(WebsiteSSRFError)
async def website_ssrf_error_handler(request: Request, exc: WebsiteSSRFError):
    """Handles SSRF violations encountered during redirection or extraction."""
    logger.warning(f"Website SSRF violation: {exc} | Path: {request.url.path}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="SSRF Attempt Blocked",
            detail=str(exc),
            code="SSRF_ATTEMPT_BLOCKED",
        ).model_dump(),
    )


@app.exception_handler(InvalidURLError)
async def invalid_url_error_handler(request: Request, exc: InvalidURLError):
    """Handles malformed URLs or invalid protocols with HTTP 400."""
    logger.warning(f"Invalid URL rejected: {exc} | Path: {request.url.path}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="Invalid URL",
            detail=str(exc),
            code="INVALID_URL",
        ).model_dump(),
    )


@app.exception_handler(WebsiteSizeExceededError)
async def website_size_exceeded_handler(request: Request, exc: WebsiteSizeExceededError):
    """Handles payload size limit violations."""
    logger.warning(f"Website size limit exceeded: {exc}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="Payload Too Large",
            detail=str(exc),
            code="PAYLOAD_TOO_LARGE",
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Handles incoming request validation errors."""
    errors = exc.errors()
    # Check if the validation error is from SSRF or URL validation
    for err in errors:
        msg = err.get("msg", "")
        ctx = err.get("ctx", {})
        inner_error = str(ctx.get("error", ""))
        combined_msg = f"{msg} {inner_error}".lower()

        if "ssrf" in combined_msg or "private" in combined_msg or "loopback" in combined_msg or "cloud metadata" in combined_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    success=False,
                    error="SSRF Attempt Blocked",
                    detail=msg or "Target URL violates SSRF safety constraints.",
                    code="SSRF_ATTEMPT_BLOCKED",
                ).model_dump(),
            )
        if "url" in combined_msg or "scheme" in combined_msg or "host" in combined_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    success=False,
                    error="Invalid URL",
                    detail=msg or "Target URL format is invalid.",
                    code="INVALID_URL",
                ).model_dump(),
            )

    first_err = errors[0] if errors else {}
    err_field = ".".join(str(loc) for loc in first_err.get("loc", []))
    err_msg = first_err.get("msg", "Invalid request body")

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="Validation Error",
            detail=f"{err_field}: {err_msg}" if err_field else err_msg,
            code="VALIDATION_ERROR",
        ).model_dump(),
    )


@app.exception_handler(WebsiteTimeoutException)
async def website_timeout_custom_handler(request: Request, exc: WebsiteTimeoutException):
    """Handles website request timeouts with HTTP 504 Gateway Timeout."""
    logger.error(f"Website connection timeout: {exc}")
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ErrorResponse(
            success=False,
            error="Website Connection Timed Out",
            detail=str(exc),
            code="GATEWAY_TIMEOUT",
        ).model_dump(),
    )


@app.exception_handler(WebsiteTimeoutError)
async def website_timeout_extractor_handler(request: Request, exc: WebsiteTimeoutError):
    """Handles website extractor timeouts with HTTP 504 Gateway Timeout."""
    logger.error(f"Website extractor timeout: {exc}")
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ErrorResponse(
            success=False,
            error="Website Connection Timed Out",
            detail=str(exc),
            code="GATEWAY_TIMEOUT",
        ).model_dump(),
    )


@app.exception_handler(WebsiteUnreachableError)
async def website_unreachable_handler(request: Request, exc: WebsiteUnreachableError):
    """Handles website unreachable errors with HTTP 502 Bad Gateway."""
    logger.error(f"Website unreachable: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website Unreachable",
            detail=str(exc),
            code="WEBSITE_UNREACHABLE",
        ).model_dump(),
    )


@app.exception_handler(WebsiteBlockedError)
async def website_blocked_handler(request: Request, exc: WebsiteBlockedError):
    """Handles HTTP 401/403 blocked websites with HTTP 502 Bad Gateway."""
    logger.error(f"Target website blocked access: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website Access Blocked",
            detail=f"Target website returned HTTP {exc.status_code} Forbidden/Unauthorized: {exc}",
            code="WEBSITE_BLOCKED",
        ).model_dump(),
    )


@app.exception_handler(WebsiteNotFoundError)
async def website_not_found_handler(request: Request, exc: WebsiteNotFoundError):
    """Handles HTTP 404 websites with HTTP 502 Bad Gateway."""
    logger.error(f"Target website not found: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website Not Found",
            detail=f"Target website returned HTTP 404 Not Found: {exc}",
            code="WEBSITE_NOT_FOUND",
        ).model_dump(),
    )


@app.exception_handler(WebsiteHTTPError)
async def website_http_error_handler(request: Request, exc: WebsiteHTTPError):
    """Handles general HTTP errors from target websites with HTTP 502 Bad Gateway."""
    logger.error(f"Target website returned HTTP error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website HTTP Error",
            detail=f"Target website returned HTTP {exc.status_code}: {exc}",
            code="WEBSITE_HTTP_ERROR",
        ).model_dump(),
    )


@app.exception_handler(WebsiteFetchError)
async def website_fetch_error_handler(request: Request, exc: WebsiteFetchError):
    """Handles connection or DNS failures when reaching target website with HTTP 502 Bad Gateway."""
    logger.error(f"Target website fetch failure: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website Unreachable",
            detail=str(exc),
            code="WEBSITE_UNREACHABLE",
        ).model_dump(),
    )


@app.exception_handler(WebsiteExtractionError)
async def website_extraction_error_handler(request: Request, exc: WebsiteExtractionError):
    """Handles general extraction errors with HTTP 502 Bad Gateway."""
    logger.error(f"Website extraction error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            success=False,
            error="Website Extraction Failed",
            detail=str(exc),
            code="EXTRACTION_FAILED",
        ).model_dump(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handles standard HTTPExceptions cleanly without leaking traces."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            success=False,
            error=detail,
            detail=detail,
            code=f"HTTP_{exc.status_code}",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all handler ensuring internal errors return clean JSON without leaking stack traces."""
    logger.exception(f"Unhandled server error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            success=False,
            error="Internal Server Error",
            detail="An unexpected error occurred during link analysis. Please try again later.",
            code="INTERNAL_SERVER_ERROR",
        ).model_dump(),
    )


# ============================================================================
# API Endpoints
# ============================================================================

@app.get(
    "/health",
    tags=["System"],
    summary="Service Health Check",
    response_model=Dict[str, Any],
)
async def health_check():
    """Returns the operational status and configuration of the API service."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "llm_provider": settings.LLM_PROVIDER,
        "timestamp": time.time(),
    }


@app.get(
    "/",
    tags=["System"],
    summary="Root Information",
    response_model=Dict[str, Any],
)
async def root():
    """Root info endpoint providing service metadata and documentation links."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "link_analysis": "POST /link-analysis",
        },
    }


@app.post(
    "/link-analysis",
    tags=["Analysis"],
    summary="Analyze Website Link",
    description=(
        "Accepts a website URL, securely fetches and extracts its content, "
        "processes it via an advanced LLM, and returns structured intelligence across 5 sections: "
        "1. Competitor vs Your Brand Details, 2. Top 5 Target Search Queries, "
        "3. SEO Improvements, 4. Top 5 Services, 5. Target Audience Identification."
    ),
    response_model=LinkAnalysisResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Comprehensive website analysis generated successfully.",
            "model": LinkAnalysisResponse,
        },
        400: {
            "description": "Invalid URL format or SSRF attempt detected and blocked.",
            "model": ErrorResponse,
        },
        502: {
            "description": "Target website is unreachable, not found (404), or access was blocked (403).",
            "model": ErrorResponse,
        },
        504: {
            "description": "Connection to target website timed out.",
            "model": ErrorResponse,
        },
        500: {
            "description": "Unhandled server failure during analysis.",
            "model": ErrorResponse,
        },
    },
)
async def analyze_link(
    payload: LinkAnalysisRequest,
    analysis_service: AnalysisService = Depends(get_analysis_service),
) -> LinkAnalysisResponse:
    """
    Main link analysis endpoint.

    Request:
    ```json
    {
      "url": "https://example.com"
    }
    ```
    """
    logger.info(f"Received link analysis request for URL: '{payload.url}'")
    response = await analysis_service.analyze(request=payload)
    return response


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
