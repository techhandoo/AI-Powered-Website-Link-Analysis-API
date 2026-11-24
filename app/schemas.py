"""
Pydantic Schemas for AI-Powered Website Link Analysis API.

Strictly adhering to specifications in link-analysis.md.
Covers:
- LinkAnalysisRequest (with URL validation and SSRF defenses)
- BrandInfo
- CompetitorItem & CompetitorAnalysis (Section 1)
- TargetSearchQuery & PlatformEnum (Section 2)
- SEOImprovements (Section 3)
- ServiceItem (Section 4)
- TargetAudience (Section 5)
- AnalysisData
- LinkAnalysisResponse
- ErrorResponse
- ExtractedWebsiteData (Extraction payload helper)
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.validators import validate_url


class PlatformEnum(str, Enum):
    """Supported search and social discovery platforms."""
    GOOGLE = "Google"
    FACEBOOK = "Facebook"
    INSTAGRAM = "Instagram"
    LINKEDIN = "LinkedIn"
    X = "X"
    TWITTER = "Twitter"

    @classmethod
    def normalize(cls, value: str) -> "PlatformEnum":
        """Normalizes common variations into a recognized PlatformEnum member."""
        val = str(value).strip()
        lower = val.lower()
        if "google" in lower:
            return cls.GOOGLE
        if "facebook" in lower:
            return cls.FACEBOOK
        if "instagram" in lower:
            return cls.INSTAGRAM
        if "linkedin" in lower:
            return cls.LINKEDIN
        if lower in ("x", "twitter", "x (twitter)"):
            return cls.X
        # Default fallback
        for member in cls:
            if member.value.lower() == lower:
                return member
        return cls.GOOGLE


# ============================================================================
# Request Schema
# ============================================================================

class LinkAnalysisRequest(BaseModel):
    """Incoming request model containing the website URL to analyze."""
    url: str = Field(
        ...,
        description="The HTTP or HTTPS website URL to analyze",
        examples=["https://example.com"],
    )

    @field_validator("url")
    @classmethod
    def validate_url_field(cls, v: str) -> str:
        """Validates format, scheme, and SSRF security of the input URL."""
        return validate_url(v)


# ============================================================================
# Section 1: Brand & Competitor Schemas
# ============================================================================

class BrandInfo(BaseModel):
    """Identified brand summary and category."""
    name: str = Field(
        ...,
        description="Your brand or company name",
        examples=["ABC Technologies"],
    )
    category: str = Field(
        ...,
        description="Main business category or industry",
        examples=["Technology Services"],
    )
    summary: str = Field(
        ...,
        description="Concise business overview and value proposition",
        examples=["A technology company providing digital solutions."],
    )


class CompetitorItem(BaseModel):
    """Information regarding an identified competitor."""
    name: str = Field(
        ...,
        description="Competitor brand or company name",
        examples=["XYZ Technologies"],
    )
    strengths: List[str] = Field(
        default_factory=list,
        description="Key competitor strengths and market advantages",
    )
    weaknesses: List[str] = Field(
        default_factory=list,
        description="Competitor gaps, weaknesses, or market shortcomings",
    )
    market: Optional[str] = Field(
        default=None,
        description="Target market of competitor (e.g., Enterprise, SMB)",
    )
    main_service: Optional[str] = Field(
        default=None,
        description="Competitor's primary product or service offering",
    )

    @classmethod
    def from_raw(cls, item: Any) -> "CompetitorItem":
        """Coerces strings or dicts into CompetitorItem."""
        if isinstance(item, str):
            return cls(name=item)
        if isinstance(item, dict):
            return cls(**item)
        return item


class CompetitorAnalysis(BaseModel):
    """Section 1: Competitor vs Your Brand Details."""
    brand_overview: str = Field(
        ...,
        description="Overview of your brand and business positioning",
        examples=["Your brand overview"],
    )
    competitors: List[CompetitorItem] = Field(
        default_factory=list,
        description="Identified competitors (minimum 3 where available)",
    )
    strengths: List[str] = Field(
        default_factory=list,
        description="Your brand's key strengths and competitive advantages",
    )
    opportunities: List[str] = Field(
        default_factory=list,
        description="Opportunities to improve market positioning and visibility",
    )
    weaknesses: Optional[List[str]] = Field(
        default_factory=list,
        description="Identified internal gaps or areas for improvement",
    )
    competitive_advantages: Optional[List[str]] = Field(
        default_factory=list,
        description="Specific advantages your brand holds over competitors",
    )

    @field_validator("competitors", mode="before")
    @classmethod
    def coerce_competitors(cls, v: Any) -> Any:
        """Allows lists of strings or dicts to be coerced into CompetitorItem objects."""
        if isinstance(v, list):
            coerced = []
            for item in v:
                if isinstance(item, str):
                    coerced.append({"name": item})
                else:
                    coerced.append(item)
            return coerced
        return v


# ============================================================================
# Section 2: Target Search Queries
# ============================================================================

class TargetSearchQuery(BaseModel):
    """Section 2: Target Search Query across platforms."""
    query: str = Field(
        ...,
        description="Search query potential customers use",
        examples=["Best technology services company"],
    )
    platforms: List[PlatformEnum] = Field(
        default_factory=list,
        description="Platforms where this query is relevant (Google, Facebook, Instagram, LinkedIn, X)",
        examples=[[PlatformEnum.GOOGLE, PlatformEnum.LINKEDIN]],
    )
    intent: str = Field(
        ...,
        description="Search intent (e.g., Service Discovery, B2B Search, Inspiration, Commercial)",
        examples=["Service Discovery"],
    )
    business_relevance: Optional[str] = Field(
        default=None,
        description="Why this query is directly relevant to the business",
    )
    target_audience: Optional[str] = Field(
        default=None,
        description="Target audience segment searching for this query",
    )

    @field_validator("platforms", mode="before")
    @classmethod
    def normalize_platform_list(cls, v: Any) -> Any:
        """Normalizes list of platforms from string variations."""
        if isinstance(v, list):
            normalized = []
            for item in v:
                if isinstance(item, PlatformEnum):
                    normalized.append(item)
                elif isinstance(item, str):
                    normalized.append(PlatformEnum.normalize(item))
                else:
                    normalized.append(item)
            return normalized
        return v


# ============================================================================
# Section 3: SEO Improvements
# ============================================================================

class SEOImprovements(BaseModel):
    """Section 3: SEO Improvement Recommendations."""
    primary_keyword: str = Field(
        ...,
        description="Primary target keyword",
        examples=["Technology Services"],
    )
    secondary_keywords: List[str] = Field(
        default_factory=list,
        description="Secondary supporting keywords",
    )
    long_tail_keywords: List[str] = Field(
        default_factory=list,
        description="Target long-tail keyword variations",
    )
    recommended_title: str = Field(
        ...,
        description="Optimized SEO title structure",
        examples=["Recommended SEO Title"],
    )
    meta_description: str = Field(
        ...,
        description="Human-friendly, keyword-rich recommended meta description",
        examples=["Recommended SEO description"],
    )
    supporting_content: List[str] = Field(
        default_factory=list,
        description="Recommended additional website content (landing pages, FAQs, articles)",
    )


# ============================================================================
# Section 4: Top Services
# ============================================================================

class ServiceItem(BaseModel):
    """Section 4: Key Service Offering."""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        ...,
        alias="service_name",
        description="Name of the service",
        examples=["Web Development"],
    )
    description: str = Field(
        ...,
        description="Short service explanation",
    )
    target_industry: Optional[str] = Field(
        default=None,
        description="Relevant business industry",
    )
    target_customer: Optional[str] = Field(
        default=None,
        description="Potential customer type (e.g., Startups, Enterprise)",
    )
    main_benefit: Optional[str] = Field(
        default=None,
        description="Why customers need this service",
    )


# ============================================================================
# Section 5: Target Audience Identification
# ============================================================================

class TargetAudience(BaseModel):
    """Section 5: Target Audience Identification."""
    primary_audience: List[str] = Field(
        default_factory=list,
        description="Primary target audience segments",
    )
    secondary_audience: List[str] = Field(
        default_factory=list,
        description="Secondary target audience segments",
    )
    industries: List[str] = Field(
        default_factory=list,
        description="Target industry segments",
    )
    customer_needs: List[str] = Field(
        default_factory=list,
        description="Customer needs, requirements, and pain points",
    )
    business_types: Optional[List[str]] = Field(
        default_factory=list,
        description="Types of businesses served (e.g., SMBs, Startups, Enterprises)",
    )
    customer_profiles: Optional[List[str]] = Field(
        default_factory=list,
        description="Specific customer profiles or buyer personas",
    )
    challenges: Optional[List[str]] = Field(
        default_factory=list,
        description="Key customer challenges solved",
    )
    recommended_segments: Optional[List[str]] = Field(
        default_factory=list,
        description="Recommended audience segments for marketing campaigns",
    )


# ============================================================================
# Overall Analysis & Response Schemas
# ============================================================================

class AnalysisData(BaseModel):
    """Structured container holding all 5 required analysis sections."""
    competitor_analysis: CompetitorAnalysis
    target_search_queries: List[TargetSearchQuery]
    seo_improvements: SEOImprovements
    top_services: List[ServiceItem]
    target_audience: TargetAudience

    @field_validator("top_services", mode="before")
    @classmethod
    def coerce_services(cls, v: Any) -> Any:
        """Coerces strings or dicts into ServiceItem objects."""
        if isinstance(v, list):
            coerced = []
            for item in v:
                if isinstance(item, str):
                    coerced.append({"name": item, "description": item})
                else:
                    coerced.append(item)
            return coerced
        return v


class LinkAnalysisResponse(BaseModel):
    """Standard success response payload for POST /link-analysis."""
    success: bool = Field(default=True, description="Indicates successful analysis")
    website: str = Field(..., description="Target website URL that was analyzed")
    brand: BrandInfo = Field(..., description="Extracted brand details")
    analysis: AnalysisData = Field(..., description="5-section comprehensive website analysis")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional processing metadata (e.g. model, latency, timestamp)",
    )


class ErrorResponse(BaseModel):
    """Standard error response payload for failures."""
    success: bool = Field(default=False, description="Indicates failure")
    error: str = Field(..., description="Short error name or summary")
    detail: Optional[str] = Field(default=None, description="Detailed explanation of error")
    code: Optional[str] = Field(default=None, description="Application error code")


# ============================================================================
# Helper Schema for Website Extraction
# ============================================================================

class ExtractedWebsiteData(BaseModel):
    """Structured data extracted from raw HTML by WebsiteExtractor."""
    url: str
    final_url: Optional[str] = None
    status_code: int = 200
    title: str = ""
    meta_description: str = ""
    meta_keywords: List[str] = Field(default_factory=list)
    og_data: Dict[str, str] = Field(default_factory=dict)
    twitter_data: Dict[str, str] = Field(default_factory=dict)
    json_ld: List[Dict[str, Any]] = Field(default_factory=list)
    headings: Dict[str, List[str]] = Field(default_factory=dict)
    main_text: str = ""
    links: List[str] = Field(default_factory=list)
    content_length: int = 0
