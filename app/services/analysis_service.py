"""
Analysis Orchestration Service.

Orchestrates:
1. Website content extraction and security validation (WebsiteExtractor)
2. AI-powered intelligence generation across 5 key dimensions (LLMService)
3. Competitor benchmarking and enrichment (CompetitorService)
4. Construction of fully validated LinkAnalysisResponse payloads
"""

import logging
from typing import Any, Dict, Optional, Union

from app.config import Settings, get_settings
from app.schemas import (
    ExtractedWebsiteData,
    LinkAnalysisRequest,
    LinkAnalysisResponse,
)
from app.services.competitor_service import CompetitorService
from app.services.llm_service import LLMService, get_llm_service
from app.services.website_extractor import (
    WebsiteBlockedError,
    WebsiteExtractionError,
    WebsiteExtractor,
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
    validate_url,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Analysis Orchestrator Exceptions
# ============================================================================

class AnalysisServiceError(Exception):
    """Base exception for analysis service errors."""
    pass


class WebsiteUnreachableError(AnalysisServiceError):
    """Raised when the target website cannot be reached or returned an error status."""
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class WebsiteTimeoutException(AnalysisServiceError):
    """Raised when the website request times out."""
    def __init__(self, message: str = "Target website connection timed out.", status_code: int = 504):
        super().__init__(message)
        self.status_code = status_code


# ============================================================================
# Analysis Service Class
# ============================================================================

class AnalysisService:
    """
    Coordinates the end-to-end link analysis workflow:
    extraction -> LLM analysis -> competitor enrichment -> LinkAnalysisResponse.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        extractor: Optional[WebsiteExtractor] = None,
        llm_service: Optional[LLMService] = None,
        competitor_service: Optional[CompetitorService] = None,
    ):
        self.settings = settings or get_settings()
        self.extractor = extractor or WebsiteExtractor(
            timeout=self.settings.TIMEOUT_SECONDS,
            max_content_length=self.settings.MAX_CONTENT_LENGTH,
            max_llm_input_chars=self.settings.MAX_LLM_INPUT_CHARS,
            user_agent=self.settings.USER_AGENT,
        )
        self.llm_service = llm_service or get_llm_service()
        self.competitor_service = competitor_service or CompetitorService()

    async def analyze_url(
        self,
        url: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LinkAnalysisResponse:
        """
        Executes complete website link analysis for a given URL string.

        Args:
            url: The HTTP or HTTPS website URL to analyze.
            provider: Optional LLM provider override ('gemini', 'openai', or 'mock').
            model: Optional model name override.

        Returns:
            LinkAnalysisResponse: Complete 5-section analysis payload.

        Raises:
            SSRFError / InvalidURLError: If the URL is invalid or targets private networks.
            WebsiteTimeoutException: If website fetch times out (504).
            WebsiteUnreachableError: If website cannot be fetched or returns HTTP errors (502).
        """
        # Step 1: Pre-validate URL and check SSRF
        clean_url = str(url).strip()
        validated_url = validate_url(clean_url)
        logger.info(f"Starting link analysis for validated URL: {validated_url}")

        # Step 2: Fetch and extract website content
        try:
            website_data: ExtractedWebsiteData = await self.extractor.extract(
                url=validated_url,
                raise_on_error=True,
            )
        except (WebsiteSSRFError, SSRFError) as exc:
            logger.warning(f"SSRF violation detected during extraction: {exc}")
            raise SSRFError(str(exc)) from exc
        except InvalidURLError as exc:
            logger.warning(f"Invalid URL during extraction: {exc}")
            raise InvalidURLError(str(exc)) from exc
        except WebsiteTimeoutError as exc:
            logger.error(f"Website extraction timed out for {validated_url}: {exc}")
            raise WebsiteTimeoutException(f"Target website request timed out: {exc}") from exc
        except (WebsiteBlockedError, WebsiteNotFoundError, WebsiteHTTPError, WebsiteFetchError) as exc:
            logger.error(f"Website unreachable or HTTP error for {validated_url}: {exc}")
            raise WebsiteUnreachableError(f"Target website is unreachable or returned an error: {exc}") from exc
        except WebsiteSizeExceededError as exc:
            logger.warning(f"Website size limit exceeded for {validated_url}: {exc}")
            raise InvalidURLError(f"Website content exceeds size limit: {exc}") from exc
        except WebsiteExtractionError as exc:
            logger.error(f"General website extraction failure for {validated_url}: {exc}")
            raise WebsiteUnreachableError(f"Failed to extract website content: {exc}") from exc

        # Step 3: Run LLM analysis across 5 sections
        logger.info(f"Running LLM analysis for {validated_url} using provider '{provider or self.settings.LLM_PROVIDER}'")
        llm_result = await self.llm_service.analyze(
            website_data=website_data,
            provider=provider,
            model=model,
        )

        brand = llm_result.brand
        analysis = llm_result.analysis
        metadata = llm_result.metadata or {}

        # Step 4: Enrich competitor intelligence to guarantee Section 1 compliance
        analysis.competitor_analysis = self.competitor_service.enrich_competitor_analysis(
            analysis=analysis.competitor_analysis,
            brand_name=brand.name,
            category=brand.category,
            brand_summary=brand.summary,
        )

        # Step 5: Construct standard LinkAnalysisResponse
        response = LinkAnalysisResponse(
            success=True,
            website=validated_url,
            brand=brand,
            analysis=analysis,
            metadata=metadata,
        )

        logger.info(f"Successfully generated link analysis for {validated_url}")
        return response

    async def analyze(
        self,
        request: LinkAnalysisRequest,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LinkAnalysisResponse:
        """
        Convenience wrapper accepting a LinkAnalysisRequest object.
        """
        return await self.analyze_url(
            url=request.url,
            provider=provider,
            model=model,
        )


# Singleton instance
_default_analysis_service: Optional[AnalysisService] = None


def get_analysis_service() -> AnalysisService:
    """Returns singleton AnalysisService instance."""
    global _default_analysis_service
    if _default_analysis_service is None:
        _default_analysis_service = AnalysisService()
    return _default_analysis_service
