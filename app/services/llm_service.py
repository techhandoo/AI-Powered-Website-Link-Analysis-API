"""
LLM Integration Service for Website Link Analysis.

Supports:
- Multi-provider abstraction: Google Gemini, OpenAI, and Mock/Fallback.
- Structured JSON output enforcement (system prompt, JSON schema, Pydantic validation).
- Specific prompt engineering for the 5 required analysis sections:
  1. Competitor vs Your Brand Details
  2. Top 5 Target Search Queries (Google, Facebook, Instagram, LinkedIn, X - no invented search volumes)
  3. SEO Improvement (primary/secondary/long-tail keywords, title, meta description, supporting content)
  4. Top 5 Services (name, description, target industry, customer, main benefit)
  5. Target Audience Identification (primary, secondary, industries, business types, customer profiles, needs, challenges)
- Cost optimization: input text cleaning, prompt caching friendliness, token estimation helpers.
- Deterministic fallback provider parsing actual website content when offline or unconfigured.
"""

from abc import ABC, abstractmethod
import asyncio
import json
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from app.config import Settings, get_settings
from app.schemas import (
    AnalysisData,
    BrandInfo,
    CompetitorAnalysis,
    CompetitorItem,
    ExtractedWebsiteData,
    LinkAnalysisResponse,
    PlatformEnum,
    SEOImprovements,
    ServiceItem,
    TargetAudience,
    TargetSearchQuery,
)
from app.services.competitor_service import CompetitorService

logger = logging.getLogger(__name__)


# ============================================================================
# Pricing & Token Estimation Constants (Current Provider Rates)
# ============================================================================

# Pricing in USD per 1,000,000 tokens
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "mock": {"input": 0.0, "output": 0.0},
}


def estimate_tokens(text: str) -> int:
    """
    Estimates the number of tokens in a given text string.
    Rule of thumb: ~4 characters per token for English text.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimates the API cost in USD based on input and output token counts.
    """
    model_key = model.lower()
    pricing = MODEL_PRICING.get(model_key)
    if not pricing:
        # Fallback to gpt-4o-mini / gemini-2.0-flash ballpark
        for k, v in MODEL_PRICING.items():
            if k in model_key:
                pricing = v
                break
    if not pricing:
        pricing = {"input": 0.15, "output": 0.60}

    cost = (input_tokens / 1_000_000.0) * pricing["input"] + (
        output_tokens / 1_000_000.0
    ) * pricing["output"]
    return round(cost, 6)


# ============================================================================
# Prompt Engineering (Engineered for Link Analysis 5 Sections)
# ============================================================================

SYSTEM_INSTRUCTION = """You are an expert Chief Marketing Officer (CMO), Business Strategist, and Technical SEO Director.
Your task is to analyze the extracted website content and generate a comprehensive, highly accurate business, competitor, SEO, search query, and target audience intelligence report.

CRITICAL INSTRUCTIONS:
1. Base your analysis strictly on the actual content, offerings, and value proposition of the submitted website.
2. Return ONLY a single, valid JSON object strictly conforming to the requested schema. Do not enclose the output in markdown code fences (no ```json). Do not include any introductory or conversational text.
3. Competitor information must identify real-world, credible market competitors for the business category.
4. Top 5 Target Search Queries must cover the 5 designated discovery platforms: Google, Facebook, Instagram, LinkedIn, and X.
   IMPORTANT: DO NOT invent search volumes or ranking numbers. Focus on realistic search intent, business relevance, and target audience.
5. All 5 sections must be fully populated with realistic, rich insights."""

ANALYSIS_SCHEMA_PROMPT = """You must respond with a JSON object that adheres strictly to this structure:

{
  "brand": {
    "name": "Brand Name",
    "category": "Main business category or industry",
    "summary": "Concise business overview and value proposition."
  },
  "competitor_analysis": {
    "brand_overview": "Comprehensive overview of your brand and market positioning.",
    "competitors": [
      {
        "name": "Competitor 1 Name",
        "strengths": ["Competitor strength 1", "Competitor strength 2"],
        "weaknesses": ["Competitor gap 1", "Competitor gap 2"],
        "market": "Target market segment (e.g. Enterprise, SMB)",
        "main_service": "Competitor's primary product or service"
      },
      {
        "name": "Competitor 2 Name",
        "strengths": ["Competitor strength 1", "Competitor strength 2"],
        "weaknesses": ["Competitor gap 1", "Competitor gap 2"],
        "market": "Target market segment",
        "main_service": "Competitor's primary offering"
      },
      {
        "name": "Competitor 3 Name",
        "strengths": ["Competitor strength 1", "Competitor strength 2"],
        "weaknesses": ["Competitor gap 1", "Competitor gap 2"],
        "market": "Target market segment",
        "main_service": "Competitor's primary offering"
      }
    ],
    "strengths": ["Your brand's key strength 1", "Your brand's key strength 2", "Your brand's key strength 3"],
    "weaknesses": ["Your brand's internal gap or development area 1", "Development area 2"],
    "competitive_advantages": ["Advantage 1 over competitors", "Advantage 2 over competitors", "Advantage 3 over competitors"],
    "opportunities": ["Market opportunity 1", "Market opportunity 2", "Market opportunity 3"]
  },
  "target_search_queries": [
    {
      "query": "High-intent search query for Google",
      "platforms": ["Google"],
      "intent": "Service Discovery",
      "business_relevance": "Why this query directly drives leads for the brand",
      "target_audience": "Audience segment searching this"
    },
    {
      "query": "Professional B2B search query for LinkedIn",
      "platforms": ["LinkedIn"],
      "intent": "B2B Evaluation",
      "business_relevance": "Connects corporate decision makers to solutions",
      "target_audience": "C-level executives, IT Directors"
    },
    {
      "query": "Visual/case-study discovery query for Instagram",
      "platforms": ["Instagram"],
      "intent": "Inspiration",
      "business_relevance": "Builds visual proof and showcases portfolio",
      "target_audience": "Designers, Creative Leads, Small Business Owners"
    },
    {
      "query": "Community and brand query for Facebook",
      "platforms": ["Facebook"],
      "intent": "Business Discovery",
      "business_relevance": "Engages local and community client prospects",
      "target_audience": "Local business owners and consumers"
    },
    {
      "query": "Real-time industry and recommendation query for X",
      "platforms": ["X"],
      "intent": "Service Discovery",
      "business_relevance": "Captures peer recommendations and trend discussions",
      "target_audience": "Tech enthusiasts, Founders, Early Adopters"
    }
  ],
  "seo_improvements": {
    "primary_keyword": "Primary target keyword",
    "secondary_keywords": ["Secondary keyword 1", "Secondary keyword 2", "Secondary keyword 3"],
    "long_tail_keywords": ["Long tail keyword query 1", "Long tail keyword query 2", "Long tail keyword query 3"],
    "recommended_title": "Optimized SEO Title (under 60 characters with primary keyword)",
    "meta_description": "Engaging, human-friendly meta description (140-160 characters) summarizing business value.",
    "supporting_content": [
      "Content Page Idea 1 (e.g. Industry Landing Page)",
      "Content Page Idea 2 (e.g. Comprehensive Guide)",
      "Content Page Idea 3 (e.g. Frequently Asked Questions / FAQ)",
      "Content Page Idea 4 (e.g. Client Case Studies & Impact)",
      "Content Page Idea 5 (e.g. Comparison Article)"
    ]
  },
  "top_services": [
    {
      "name": "Service 1 Name",
      "description": "Clear service explanation derived from website offerings",
      "target_industry": "Primary industry served",
      "target_customer": "Target buyer type",
      "main_benefit": "Core value or pain point resolved"
    },
    {
      "name": "Service 2 Name",
      "description": "Clear service explanation",
      "target_industry": "Primary industry served",
      "target_customer": "Target buyer type",
      "main_benefit": "Core value or pain point resolved"
    },
    {
      "name": "Service 3 Name",
      "description": "Clear service explanation",
      "target_industry": "Primary industry served",
      "target_customer": "Target buyer type",
      "main_benefit": "Core value or pain point resolved"
    },
    {
      "name": "Service 4 Name",
      "description": "Clear service explanation",
      "target_industry": "Primary industry served",
      "target_customer": "Target buyer type",
      "main_benefit": "Core value or pain point resolved"
    },
    {
      "name": "Service 5 Name",
      "description": "Clear service explanation",
      "target_industry": "Primary industry served",
      "target_customer": "Target buyer type",
      "main_benefit": "Core value or pain point resolved"
    }
  ],
  "target_audience": {
    "primary_audience": ["Primary target audience segment 1", "Primary target audience segment 2"],
    "secondary_audience": ["Secondary target audience segment 1", "Secondary target audience segment 2"],
    "industries": ["Industry 1", "Industry 2", "Industry 3", "Industry 4"],
    "business_types": ["Enterprise Companies", "Mid-Market", "Startups & Scaleups", "Small Businesses"],
    "customer_profiles": ["Profile 1 (e.g., CTO)", "Profile 2 (e.g., Head of Operations)", "Profile 3 (e.g., Marketing Director)"],
    "customer_needs": ["Key need 1", "Key need 2", "Key need 3"],
    "challenges": ["Key challenge 1 solved", "Key challenge 2 solved"],
    "recommended_segments": ["Segment 1 for marketing", "Segment 2 for marketing"]
  }
}"""


def build_user_prompt(data: ExtractedWebsiteData, max_chars: int = 14000) -> str:
    """
    Constructs a clean, structured context payload for the LLM.
    Combines metadata, headings, key links, and prioritized body text.
    """
    parts: List[str] = []
    parts.append(f"WEBSITE URL: {data.url}")
    if data.final_url and data.final_url != data.url:
        parts.append(f"FINAL RESOLVED URL: {data.final_url}")
    if data.title:
        parts.append(f"PAGE TITLE: {data.title}")
    if data.meta_description:
        parts.append(f"META DESCRIPTION: {data.meta_description}")
    if data.meta_keywords:
        parts.append(f"META KEYWORDS: {', '.join(data.meta_keywords[:15])}")

    # OpenGraph tags
    if data.og_data:
        og_summary = [f"{k}: {v}" for k, v in data.og_data.items() if v]
        if og_summary:
            parts.append("OPEN GRAPH METADATA:\n" + "\n".join(f"  - {item}" for item in og_summary[:8]))

    # JSON-LD Schema
    if data.json_ld:
        schema_types = []
        for s in data.json_ld[:3]:
            stype = s.get("@type", "Schema")
            sname = s.get("name", "")
            sdesc = s.get("description", "")
            schema_types.append(f"{stype}: {sname} ({sdesc[:120]})" if sdesc else f"{stype}: {sname}")
        if schema_types:
            parts.append("STRUCTURED DATA (JSON-LD):\n" + "\n".join(f"  - {st}" for st in schema_types))

    # Headings hierarchy
    if data.headings:
        headings_list = []
        for level in ("h1", "h2", "h3"):
            h_items = data.headings.get(level, [])
            if h_items:
                headings_list.append(f"{level.upper()} HEADINGS: {' | '.join(h_items[:12])}")
        if headings_list:
            parts.append("\n".join(headings_list))

    # Links (service mentions)
    if data.links:
        parts.append("NAVIGATION / SERVICE LINKS:\n" + "\n".join(f"  * {link}" for link in data.links[:20]))

    # Main text content
    if data.main_text:
        # Clean repetitive whitespaces
        cleaned_text = re.sub(r"\n{3,}", "\n\n", data.main_text.strip())
        parts.append(f"EXTRACTED MAIN CONTENT:\n{cleaned_text}")

    full_content = "\n\n".join(parts)

    # Bound content length to respect max_chars budget
    if len(full_content) > max_chars:
        full_content = full_content[:max_chars] + "\n... [Remaining content truncated for LLM input optimization]"

    return (
        f"Analyze the following extracted website content and generate the full 5-section intelligence report.\n\n"
        f"--- EXTRACTED WEBSITE DATA ---\n"
        f"{full_content}\n"
        f"--- END EXTRACTED DATA ---\n\n"
        f"{ANALYSIS_SCHEMA_PROMPT}"
    )


def clean_and_parse_json(raw_text: str) -> Dict[str, Any]:
    """
    Sanitizes raw LLM output and parses it into a Python dictionary.
    Handles markdown code blocks, leading BOM, and malformed wrapper characters.
    """
    if not raw_text:
        raise ValueError("LLM returned empty response")

    text = raw_text.strip()

    # Remove markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Find outermost JSON object
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx : end_idx + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Attempt simple repairs (e.g. unescaped newlines or trailing commas)
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(fixed)
        except Exception:
            raise ValueError(f"Failed to parse LLM output as JSON: {exc}\nRaw text snippet: {raw_text[:200]}")


# ============================================================================
# LLM Result Container
# ============================================================================

class LLMAnalysisResult:
    """
    Rich result container returned by LLMService.

    Provides ergonomic access to brand, analysis, and metadata:
    - `res.brand`, `res.analysis`, `res.metadata`
    - Iterable unpacking: `brand, analysis = res`
    - Full 3-tuple unpacking: `brand, analysis, meta = res.as_tuple()`
    - Conversion helpers: `res.to_response(website)`, `res.to_dict()`
    """

    def __init__(self, brand: BrandInfo, analysis: AnalysisData, metadata: Dict[str, Any]):
        self.brand = brand
        self.analysis = analysis
        self.metadata = metadata

    def __iter__(self):
        """Enables standard 2-item unpacking: `brand, analysis = result`."""
        yield self.brand
        yield self.analysis

    def as_tuple(self) -> Tuple[BrandInfo, AnalysisData, Dict[str, Any]]:
        """Returns full (brand, analysis, metadata) 3-tuple."""
        return (self.brand, self.analysis, self.metadata)

    def __getitem__(self, index: int) -> Any:
        return [self.brand, self.analysis, self.metadata][index]

    def __len__(self) -> int:
        return 2

    def to_response(self, website: str) -> LinkAnalysisResponse:
        """Converts result into LinkAnalysisResponse Pydantic model."""
        return LinkAnalysisResponse(
            success=True,
            website=website,
            brand=self.brand,
            analysis=self.analysis,
            metadata=self.metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Returns standard dict representation."""
        return {
            "brand": self.brand.model_dump(),
            "analysis": self.analysis.model_dump(),
            "metadata": self.metadata,
        }


# ============================================================================
# Base LLM Provider Interface
# ============================================================================

class BaseLLMProvider(ABC):
    """Abstract Base Class for LLM providers."""

    @abstractmethod
    async def generate_analysis(
        self,
        extracted_data: ExtractedWebsiteData,
        system_prompt: str,
        user_prompt: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Executes the LLM request and returns:
        - raw parsed JSON dictionary matching analysis schema
        - execution metadata (model, tokens, cost, latency)
        """
        pass


# ============================================================================
# Google Gemini Provider
# ============================================================================

class GeminiProvider(BaseLLMProvider):
    """
    Google Gemini Provider.
    Supports models: gemini-2.0-flash, gemini-1.5-flash, gemini-1.5-pro.
    Calls Google Generative Language REST API directly via async HTTPX with JSON mode.
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 25,
    ):
        settings = get_settings()
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model = model or settings.GEMINI_MODEL or "gemini-2.0-flash"
        self.timeout = timeout

    async def generate_analysis(
        self,
        extracted_data: ExtractedWebsiteData,
        system_prompt: str,
        user_prompt: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not configured")

        url = f"{self.BASE_URL}/{self.model}:generateContent?key={self.api_key}"

        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 4096,
            },
        }

        start_time = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload)

        latency = round(time.perf_counter() - start_time, 3)

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API returned HTTP {response.status_code}: {response.text[:300]}"
            )

        resp_data = response.json()
        candidates = resp_data.get("candidates", [])
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {resp_data}")

        part = candidates[0].get("content", {}).get("parts", [{}])[0]
        raw_text = part.get("text", "")
        parsed_json = clean_and_parse_json(raw_text)

        # Token usage from response or estimation
        usage = resp_data.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount") or estimate_tokens(system_prompt + user_prompt)
        output_tokens = usage.get("candidatesTokenCount") or estimate_tokens(raw_text)
        cost_usd = estimate_cost(self.model, input_tokens, output_tokens)

        metadata = {
            "provider": "gemini",
            "model": self.model,
            "fallback_used": False,
            "latency_seconds": latency,
            "input_tokens_est": input_tokens,
            "output_tokens_est": output_tokens,
            "cost_usd_est": cost_usd,
        }

        return parsed_json, metadata


# ============================================================================
# OpenAI Provider
# ============================================================================

class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI Provider.
    Supports models: gpt-4o-mini, gpt-4o.
    Calls OpenAI Chat Completions REST API directly via async HTTPX with JSON mode.
    """

    BASE_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 30,
    ):
        settings = get_settings()
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_MODEL or "gpt-4o-mini"
        self.timeout = timeout

    async def generate_analysis(
        self,
        extracted_data: ExtractedWebsiteData,
        system_prompt: str,
        user_prompt: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not configured")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 4096,
        }

        start_time = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.BASE_URL, headers=headers, json=payload)

        latency = round(time.perf_counter() - start_time, 3)

        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI API returned HTTP {response.status_code}: {response.text[:300]}"
            )

        resp_data = response.json()
        choices = resp_data.get("choices", [])
        if not choices:
            raise ValueError(f"OpenAI returned no choices: {resp_data}")

        raw_text = choices[0].get("message", {}).get("content", "")
        parsed_json = clean_and_parse_json(raw_text)

        usage = resp_data.get("usage", {})
        input_tokens = usage.get("prompt_tokens") or estimate_tokens(system_prompt + user_prompt)
        output_tokens = usage.get("completion_tokens") or estimate_tokens(raw_text)
        cost_usd = estimate_cost(self.model, input_tokens, output_tokens)

        metadata = {
            "provider": "openai",
            "model": self.model,
            "fallback_used": False,
            "latency_seconds": latency,
            "input_tokens_est": input_tokens,
            "output_tokens_est": output_tokens,
            "cost_usd_est": cost_usd,
        }

        return parsed_json, metadata


# ============================================================================
# Deterministic Intelligent Mock / Fallback Provider
# ============================================================================

class MockFallbackProvider(BaseLLMProvider):
    """
    Intelligent Deterministic Mock / Fallback Provider.

    Parses actual website content, headings, and metadata to generate
    all 5 required sections deterministically without requiring external API keys.
    Guarantees:
    - Min 3 competitors with strengths, weaknesses, market, main service.
    - Top 5 search queries across Google, Facebook, Instagram, LinkedIn, X with intent and audience (NO invented search volumes).
    - SEO improvements: primary/secondary/long-tail keywords, title, meta description, supporting content pages.
    - Top 5 services derived from headings and content.
    - Target audience: primary, secondary, industries, business types, customer profiles, needs, challenges.
    """

    async def generate_analysis(
        self,
        extracted_data: ExtractedWebsiteData,
        system_prompt: str,
        user_prompt: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        start_time = time.perf_counter()

        # 1. Brand Identification
        og_name = extracted_data.og_data.get("og:site_name")
        brand_name = CompetitorService.extract_brand_name(
            url=extracted_data.url,
            title=extracted_data.title,
            og_site_name=og_name,
        )

        category = CompetitorService.detect_category(
            title=extracted_data.title,
            meta_description=extracted_data.meta_description,
            main_text=extracted_data.main_text,
            meta_keywords=extracted_data.meta_keywords,
        )

        # Brand summary
        summary = extracted_data.meta_description
        if not summary or len(summary.strip()) < 20:
            summary = extracted_data.og_data.get("og:description", "")
        if not summary or len(summary.strip()) < 20:
            if extracted_data.title:
                summary = f"{brand_name} delivers comprehensive offerings in the {category} sector, focused on {extracted_data.title}."
            else:
                summary = f"{brand_name} is a dedicated provider of {category} solutions designed to enhance business efficiency and digital reach."

        brand_dict = {
            "name": brand_name,
            "category": category,
            "summary": summary.strip(),
        }

        # 2. Section 1: Competitor Analysis
        clean_cat = category.split("(")[0].strip()
        comp_analysis = CompetitorService.build_fallback_competitor_analysis(
            brand_name=brand_name,
            category=category,
            summary=summary,
        )
        comp_analysis = CompetitorService.enrich_competitor_analysis(
            analysis=comp_analysis,
            brand_name=brand_name,
            category=category,
            brand_summary=summary,
        )

        competitor_analysis_dict = {
            "brand_overview": comp_analysis.brand_overview,
            "competitors": [c.model_dump() for c in comp_analysis.competitors],
            "strengths": comp_analysis.strengths,
            "weaknesses": comp_analysis.weaknesses or [
                f"Developing market share compared to legacy incumbents in {clean_cat}",
                "Expanding content library and brand search mindshare",
            ],
            "competitive_advantages": comp_analysis.competitive_advantages or [
                "Agile solution implementation with low onboarding overhead",
                "Personalized technical consultation and transparent pricing",
                "Modern architecture designed for current industry workflows",
            ],
            "opportunities": comp_analysis.opportunities,
        }

        # 3. Section 2: Top 5 Target Search Queries (Google, Facebook, Instagram, LinkedIn, X)
        # Strictly NO invented search volumes!
        target_search_queries = [
            {
                "query": f"Best {clean_cat.lower()} platform for business",
                "platforms": ["Google"],
                "intent": "Service Discovery",
                "business_relevance": f"Captures high-intent commercial buyers actively seeking solutions in {clean_cat}.",
                "target_audience": "Business Owners, Operations Directors & IT Leaders",
            },
            {
                "query": f"Enterprise {clean_cat.lower()} provider recommendations",
                "platforms": ["LinkedIn"],
                "intent": "B2B Evaluation",
                "business_relevance": f"Attracts corporate procurement and executive decision-makers evaluating partner vendors.",
                "target_audience": "CTOs, VP of Engineering, Procurement Managers",
            },
            {
                "query": f"{brand_name} modern {clean_cat.lower()} case studies",
                "platforms": ["Instagram"],
                "intent": "Inspiration",
                "business_relevance": f"Provides visual social proof and showcases client results on social discovery channels.",
                "target_audience": "Creative Professionals, Growth Marketers & Agency Leads",
            },
            {
                "query": f"Top rated {clean_cat.lower()} services",
                "platforms": ["Facebook"],
                "intent": "Business Discovery",
                "business_relevance": f"Engages local and community business networks looking for trusted service recommendations.",
                "target_audience": "Small Business Founders & Regional Managers",
            },
            {
                "query": f"Affordable {clean_cat.lower()} tools alternative",
                "platforms": ["X"],
                "intent": "Service Discovery",
                "business_relevance": f"Engages active tech community discussions seeking agile alternatives to legacy players.",
                "target_audience": "Founders, Early Adopters, Tech Enthusiasts",
            },
        ]

        # 4. Section 3: SEO Improvements
        primary_kw = clean_cat
        secondary_kws = [
            f"{clean_cat} solutions",
            f"professional {clean_cat.lower()} services",
            f"{brand_name.lower()} platform",
            f"enterprise {clean_cat.lower()}",
        ]
        long_tail_kws = [
            f"best {clean_cat.lower()} for small business",
            f"how to choose a {clean_cat.lower()} provider",
            f"custom {clean_cat.lower()} implementation services",
            f"affordable {clean_cat.lower()} consulting company",
        ]
        recommended_title = f"{brand_name} | Leading {clean_cat} Solutions & Services"
        if len(recommended_title) > 60:
            recommended_title = f"{brand_name} | {clean_cat} Solutions"

        meta_desc = (
            f"Discover how {brand_name} delivers industry-leading {clean_cat.lower()} solutions. "
            f"Streamline operations, enhance efficiency, and accelerate business growth today."
        )

        supporting_content = [
            f"Comprehensive Guide to Modern {clean_cat}",
            f"Client Impact Case Studies: Scaling Business Results with {brand_name}",
            f"Frequently Asked Questions (FAQ) About {clean_cat} Implementation",
            f"Industry Comparison: Traditional vs Agile {clean_cat} Approaches",
            f"Essential Checklist for Selecting a {clean_cat} Partner",
        ]

        seo_improvements_dict = {
            "primary_keyword": primary_kw,
            "secondary_keywords": secondary_kws,
            "long_tail_keywords": long_tail_kws,
            "recommended_title": recommended_title,
            "meta_description": meta_desc,
            "supporting_content": supporting_content,
        }

        # 5. Section 4: Top 5 Services (Extracted from headings / text or category)
        extracted_services: List[str] = []
        for level in ("h2", "h3"):
            for h in extracted_data.headings.get(level, []):
                h_clean = h.strip()
                if (
                    4 <= len(h_clean) <= 45
                    and not any(x in h_clean.lower() for x in ["cookie", "privacy", "about", "contact", "home", "menu", "sign", "log in"])
                    and h_clean not in extracted_services
                ):
                    extracted_services.append(h_clean)

        # Fallback service archetypes
        default_services = [
            {
                "name": f"Strategic {clean_cat} Consultation",
                "description": f"Tailored advisory and roadmap development aligned with organizational goals in {clean_cat}.",
                "target_industry": category,
                "target_customer": "Enterprise Executives & Strategy Directors",
                "main_benefit": "Eliminates planning risks and establishes clear execution benchmarks.",
            },
            {
                "name": f"Custom {clean_cat} Implementation",
                "description": f"End-to-end deployment and configuration tailored to existing enterprise tech stacks.",
                "target_industry": "Information Technology & Operations",
                "target_customer": "IT Managers & Department Leads",
                "main_benefit": "Ensures seamless integration without operational downtime.",
            },
            {
                "name": "Performance Optimization & Auditing",
                "description": "Comprehensive technical and architectural audit to uncover bottlenecks and maximize ROI.",
                "target_industry": "Digital Business & E-Commerce",
                "target_customer": "Product Owners & Engineering Leads",
                "main_benefit": "Increases throughput, system reliability, and resource efficiency.",
            },
            {
                "name": "Managed Operations & Ongoing Support",
                "description": "Proactive monitoring, maintenance, and expert support guaranteeing uninterrupted service.",
                "target_industry": "Corporate & Mid-Market",
                "target_customer": "Operations Managers & Small Business Owners",
                "main_benefit": "Reduces internal overhead and provides 24/7 technical peace of mind.",
            },
            {
                "name": "Digital Transformation & Workflow Automation",
                "description": "Modernizing legacy manual processes into automated, data-driven digital workflows.",
                "target_industry": "Cross-Industry",
                "target_customer": "Operations Directors & Scaling Startups",
                "main_benefit": "Accelerates time-to-market and cuts manual operating expenses.",
            },
        ]

        top_services = []
        for i in range(5):
            if i < len(extracted_services):
                svc_name = extracted_services[i]
                top_services.append({
                    "name": svc_name,
                    "description": f"Comprehensive {svc_name.lower()} offering delivering specialized value in {clean_cat}.",
                    "target_industry": category,
                    "target_customer": "Growing Businesses & Enterprise Teams",
                    "main_benefit": f"Drives high operational efficiency through proven {clean_cat.lower()} expertise.",
                })
            else:
                top_services.append(default_services[i])

        # 6. Section 5: Target Audience Identification
        target_audience_dict = {
            "primary_audience": [
                f"Business leaders and department heads seeking scalable {clean_cat.lower()} solutions",
                "Mid-market organizations looking to modernize operations and reduce complexity",
                "Corporate decision-makers requiring proven industry reliability and compliance",
            ],
            "secondary_audience": [
                "Fast-growing startups needing cost-efficient, rapid-turnaround solutions",
                "Independent technical consultants and system integrators",
                "Small business owners seeking accessible, high-ROI digital tools",
            ],
            "industries": [
                category,
                "Information Technology & SaaS",
                "Financial Services & Banking",
                "Healthcare & Life Sciences",
                "E-Commerce & Digital Retail",
                "Professional Services & Consulting",
            ],
            "business_types": [
                "Enterprise Organizations",
                "Mid-Market Businesses",
                "Venture-Backed Startups",
                "Regional SMBs",
            ],
            "customer_profiles": [
                "Chief Technology Officer (CTO) / VP of Engineering seeking robust architecture",
                "Chief Operating Officer (COO) / Head of Operations targeting workflow efficiency",
                "Marketing & Growth Director requiring high customer conversion and engagement",
                "Founder / Managing Director driving organizational digital transformation",
            ],
            "customer_needs": [
                "Seamless interoperability with existing enterprise workflows",
                "Transparent pricing without unexpected tier upgrades",
                "Responsive, dedicated onboarding and technical assistance",
                "Demonstrated compliance, data security, and high availability",
            ],
            "challenges": [
                "Overly complex and high-cost legacy market incumbents",
                "Siloed tools that hinder real-time operational collaboration",
                "Resource constraints when developing custom solutions internally",
            ],
            "recommended_segments": [
                f"Account-Based Marketing (ABM) campaigns targeting Mid-Market IT Directors in {clean_cat}",
                "Educational content series on workflow modernization for startup founders",
                "Targeted LinkedIn sponsorship focused on ROI and implementation speed",
            ],
        }

        latency = round(time.perf_counter() - start_time, 4)

        result_json = {
            "brand": brand_dict,
            "competitor_analysis": competitor_analysis_dict,
            "target_search_queries": target_search_queries,
            "seo_improvements": seo_improvements_dict,
            "top_services": top_services,
            "target_audience": target_audience_dict,
        }

        input_tokens = estimate_tokens(user_prompt or extracted_data.main_text)
        output_tokens = estimate_tokens(json.dumps(result_json))

        metadata = {
            "provider": "mock",
            "model": "deterministic-mock-v1",
            "fallback_used": True,
            "fallback_reason": "Offline deterministic generation / Mock provider selected",
            "latency_seconds": latency,
            "input_tokens_est": input_tokens,
            "output_tokens_est": output_tokens,
            "cost_usd_est": 0.0,
        }

        return result_json, metadata


# ============================================================================
# LLMService Main Facade
# ============================================================================

class LLMService:
    """
    Main LLM Integration Service.

    Coordinates provider selection (Gemini, OpenAI, Mock), prompt construction,
    JSON parsing, Pydantic validation, and automatic resilient fallback.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.settings = settings or get_settings()
        self.provider_name = (provider or self.settings.LLM_PROVIDER or "gemini").lower()
        self.model_name = model

        # Instantiate providers
        self.gemini_provider = GeminiProvider(
            api_key=self.settings.GEMINI_API_KEY,
            model=model if self.provider_name == "gemini" else self.settings.GEMINI_MODEL,
            timeout=self.settings.TIMEOUT_SECONDS,
        )
        self.openai_provider = OpenAIProvider(
            api_key=self.settings.OPENAI_API_KEY,
            model=model if self.provider_name == "openai" else self.settings.OPENAI_MODEL,
            timeout=self.settings.TIMEOUT_SECONDS,
        )
        self.mock_provider = MockFallbackProvider()

    def _get_provider(self, provider_name: str) -> BaseLLMProvider:
        """Returns the appropriate provider instance based on provider name."""
        p_clean = provider_name.lower().strip()
        if p_clean == "gemini":
            return self.gemini_provider
        elif p_clean == "openai":
            return self.openai_provider
        return self.mock_provider

    def _coerce_extracted_data(
        self, website_data: Union[ExtractedWebsiteData, Dict[str, Any], str]
    ) -> ExtractedWebsiteData:
        """Coerces input into a standardized ExtractedWebsiteData object."""
        if isinstance(website_data, ExtractedWebsiteData):
            return website_data
        if isinstance(website_data, dict):
            return ExtractedWebsiteData(**website_data)
        if isinstance(website_data, str):
            # Assume plain URL or text
            url = website_data if "://" in website_data else f"https://{website_data}"
            return ExtractedWebsiteData(
                url=url,
                final_url=url,
                title="",
                meta_description="",
                main_text=website_data,
            )
        raise TypeError(f"Unsupported website_data type: {type(website_data)}")

    def _validate_and_build_models(
        self,
        raw_dict: Dict[str, Any],
        extracted_data: ExtractedWebsiteData,
    ) -> Tuple[BrandInfo, AnalysisData]:
        """
        Validates the parsed JSON dictionary into Pydantic models (BrandInfo, AnalysisData).
        Applies enrichment heuristics and auto-repairs to guarantee schema compliance.
        """
        # 1. BrandInfo
        brand_data = raw_dict.get("brand") or {}
        if not brand_data.get("name"):
            brand_name = CompetitorService.extract_brand_name(
                url=extracted_data.url,
                title=extracted_data.title,
                og_site_name=extracted_data.og_data.get("og:site_name"),
            )
            brand_data["name"] = brand_name
        if not brand_data.get("category"):
            brand_data["category"] = CompetitorService.detect_category(
                title=extracted_data.title,
                meta_description=extracted_data.meta_description,
                main_text=extracted_data.main_text,
            )
        if not brand_data.get("summary"):
            brand_data["summary"] = extracted_data.meta_description or f"{brand_data['name']} operates in {brand_data['category']}."

        brand = BrandInfo(
            name=brand_data["name"],
            category=brand_data["category"],
            summary=brand_data["summary"],
        )

        # 2. Competitor Analysis
        comp_raw = raw_dict.get("competitor_analysis") or {}
        competitors_list = []
        for item in comp_raw.get("competitors", []):
            if isinstance(item, dict) and item.get("name"):
                competitors_list.append(CompetitorItem(
                    name=item["name"],
                    strengths=item.get("strengths", ["Established brand presence"]),
                    weaknesses=item.get("weaknesses", ["Higher operating costs"]),
                    market=item.get("market", "Enterprise / SMB"),
                    main_service=item.get("main_service", brand.category),
                ))
            elif isinstance(item, str) and item.strip():
                competitors_list.append(CompetitorItem(name=item.strip()))

        comp_analysis = CompetitorAnalysis(
            brand_overview=comp_raw.get("brand_overview") or f"{brand.name} delivers specialized solutions in {brand.category}.",
            competitors=competitors_list,
            strengths=comp_raw.get("strengths") or [
                f"Specialized domain focus in {brand.category}",
                "Responsive client support and rapid onboarding",
                "Cost-effective solution compared to legacy alternatives",
            ],
            opportunities=comp_raw.get("opportunities") or [
                "Expand targeted search visibility and content leadership",
                "Capture underserved mid-market segments",
            ],
            weaknesses=comp_raw.get("weaknesses") or [
                "Developing brand awareness compared to global incumbents",
            ],
            competitive_advantages=comp_raw.get("competitive_advantages") or [
                "Agile implementation without complex enterprise vendor lock-in",
                "Modern architecture and personalized customer care",
            ],
        )

        # Enrich competitor analysis to guarantee min 3 competitors and filled fields
        comp_analysis = CompetitorService.enrich_competitor_analysis(
            analysis=comp_analysis,
            brand_name=brand.name,
            category=brand.category,
            brand_summary=brand.summary,
        )

        # 3. Target Search Queries
        queries_raw = raw_dict.get("target_search_queries") or []
        queries: List[TargetSearchQuery] = []
        for q in queries_raw:
            if isinstance(q, dict) and q.get("query"):
                queries.append(TargetSearchQuery(
                    query=q["query"],
                    platforms=q.get("platforms", [PlatformEnum.GOOGLE]),
                    intent=q.get("intent", "Service Discovery"),
                    business_relevance=q.get("business_relevance"),
                    target_audience=q.get("target_audience"),
                ))

        # Guarantee at least 5 queries across designated platforms if fewer generated
        if len(queries) < 5:
            clean_cat = brand.category.split("(")[0].strip()
            fallback_queries = [
                TargetSearchQuery(
                    query=f"Best {clean_cat.lower()} provider for business",
                    platforms=[PlatformEnum.GOOGLE],
                    intent="Service Discovery",
                    business_relevance=f"Connects commercial buyers directly to {brand.name}.",
                    target_audience="Business Owners & IT Managers",
                ),
                TargetSearchQuery(
                    query=f"Enterprise {clean_cat.lower()} platform reviews",
                    platforms=[PlatformEnum.LINKEDIN],
                    intent="B2B Evaluation",
                    business_relevance="Attracts corporate procurement and executive decision-makers.",
                    target_audience="CTOs, Directors of Operations",
                ),
                TargetSearchQuery(
                    query=f"{brand.name} innovative {clean_cat.lower()} case studies",
                    platforms=[PlatformEnum.INSTAGRAM],
                    intent="Inspiration",
                    business_relevance="Builds visual social proof and showcases portfolio.",
                    target_audience="Creative Directors & Growth Marketers",
                ),
                TargetSearchQuery(
                    query=f"Top rated {clean_cat.lower()} services near me",
                    platforms=[PlatformEnum.FACEBOOK],
                    intent="Business Discovery",
                    business_relevance="Engages community and regional business networks.",
                    target_audience="SMB Founders & Operations Leads",
                ),
                TargetSearchQuery(
                    query=f"Affordable {clean_cat.lower()} solutions alternative",
                    platforms=[PlatformEnum.X],
                    intent="Service Discovery",
                    business_relevance="Captures tech community peer recommendations on X.",
                    target_audience="Early Adopters & Tech Enthusiasts",
                ),
            ]
            for fq in fallback_queries:
                if len(queries) >= 5:
                    break
                if not any(existing.query.lower() == fq.query.lower() for existing in queries):
                    queries.append(fq)

        # 4. SEO Improvements
        seo_raw = raw_dict.get("seo_improvements") or {}
        clean_cat = brand.category.split("(")[0].strip()
        seo_improvements = SEOImprovements(
            primary_keyword=seo_raw.get("primary_keyword") or clean_cat,
            secondary_keywords=seo_raw.get("secondary_keywords") or [
                f"{clean_cat} solutions",
                f"professional {clean_cat.lower()} services",
                f"enterprise {clean_cat.lower()}",
            ],
            long_tail_keywords=seo_raw.get("long_tail_keywords") or [
                f"best {clean_cat.lower()} for small business",
                f"how to choose a {clean_cat.lower()} provider",
                f"custom {clean_cat.lower()} implementation",
            ],
            recommended_title=seo_raw.get("recommended_title") or f"{brand.name} | Top {clean_cat} Solutions",
            meta_description=seo_raw.get("meta_description") or (
                f"Discover how {brand.name} provides leading {clean_cat.lower()} solutions to streamline operations and drive business growth."
            ),
            supporting_content=seo_raw.get("supporting_content") or [
                f"Comprehensive Guide to Modern {clean_cat}",
                f"Client Success Stories: Scaling with {brand.name}",
                f"Frequently Asked Questions (FAQ) on {clean_cat}",
                f"Traditional vs Agile {clean_cat} Comparison",
                f"Implementation Checklist for {clean_cat}",
            ],
        )

        # 5. Top Services
        services_raw = raw_dict.get("top_services") or []
        services: List[ServiceItem] = []
        for s in services_raw:
            if isinstance(s, dict) and (s.get("name") or s.get("service_name")):
                s_name = s.get("name") or s.get("service_name")
                services.append(ServiceItem(
                    name=s_name,
                    description=s.get("description") or f"Specialized {s_name.lower()} offering.",
                    target_industry=s.get("target_industry") or brand.category,
                    target_customer=s.get("target_customer") or "Business Clients",
                    main_benefit=s.get("main_benefit") or "Drives measurable operational efficiency.",
                ))

        if len(services) < 5:
            default_svc_names = [
                f"Strategic {clean_cat} Consulting",
                f"Custom {clean_cat} Implementation",
                "Technical Performance Auditing",
                "Managed Operations & Support",
                "Digital Transformation & Workflow Automation",
            ]
            for name in default_svc_names:
                if len(services) >= 5:
                    break
                if not any(existing.name.lower() == name.lower() for existing in services):
                    services.append(ServiceItem(
                        name=name,
                        description=f"Tailored {name.lower()} engineered to deliver high performance in {clean_cat}.",
                        target_industry=brand.category,
                        target_customer="Enterprise & SMB Leaders",
                        main_benefit="Streamlines workflow and eliminates operational friction.",
                    ))

        # 6. Target Audience
        aud_raw = raw_dict.get("target_audience") or {}
        target_audience = TargetAudience(
            primary_audience=aud_raw.get("primary_audience") or [
                f"Business executives seeking reliable {clean_cat.lower()} platforms",
                "Mid-market organizations modernizing core operational workflows",
            ],
            secondary_audience=aud_raw.get("secondary_audience") or [
                "Fast-growing startups requiring agile, cost-effective deployment",
                "Digital consultants and systems integrators",
            ],
            industries=aud_raw.get("industries") or [
                brand.category,
                "Technology & Software",
                "Financial Services",
                "E-Commerce & Retail",
                "Professional Services",
            ],
            business_types=aud_raw.get("business_types") or [
                "Enterprise Companies",
                "Mid-Market Businesses",
                "Startups & Scaleups",
                "Regional SMBs",
            ],
            customer_profiles=aud_raw.get("customer_profiles") or [
                "Chief Technology Officer (CTO) prioritizing architectural stability",
                "Chief Operating Officer (COO) driving operational efficiency",
                "Product & Marketing Director scaling customer engagement",
            ],
            customer_needs=aud_raw.get("customer_needs") or [
                "Seamless integration with existing enterprise stacks",
                "Transparent pricing without unexpected licensing hikes",
                "High reliability, SLA uptime, and responsive technical support",
            ],
            challenges=aud_raw.get("challenges") or [
                "Legacy systems with high total cost of ownership",
                "Manual and fragmented operational bottlenecks",
            ],
            recommended_segments=aud_raw.get("recommended_segments") or [
                f"Account-Based Marketing targeting IT Directors in {clean_cat}",
                "Founder-focused educational content on workflow automation",
            ],
        )

        analysis = AnalysisData(
            competitor_analysis=comp_analysis,
            target_search_queries=queries[:5],
            seo_improvements=seo_improvements,
            top_services=services[:5],
            target_audience=target_audience,
        )

        return brand, analysis

    # ------------------------------------------------------------------------
    # Core Analysis Methods
    # ------------------------------------------------------------------------

    async def analyze(
        self,
        website_data: Union[ExtractedWebsiteData, Dict[str, Any], str],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMAnalysisResult:
        """
        Executes end-to-end LLM website analysis with multi-provider failover.

        Args:
            website_data: ExtractedWebsiteData instance, raw dict, or URL string.
            provider: Target provider ('gemini', 'openai', or 'mock').
            model: Optional model override.

        Returns:
            LLMAnalysisResult containing brand, analysis, and execution metadata.
        """
        extracted = self._coerce_extracted_data(website_data)
        target_provider = (provider or self.provider_name).lower().strip()

        # Build clean prompts
        user_prompt = build_user_prompt(
            data=extracted,
            max_chars=self.settings.MAX_LLM_INPUT_CHARS,
        )

        provider_inst = self._get_provider(target_provider)

        parsed_json: Optional[Dict[str, Any]] = None
        metadata: Dict[str, Any] = {}

        # Attempt primary provider if not mock
        if target_provider in ("gemini", "openai"):
            try:
                logger.info(f"Invoking primary LLM provider '{target_provider}'...")
                parsed_json, metadata = await provider_inst.generate_analysis(
                    extracted_data=extracted,
                    system_prompt=SYSTEM_INSTRUCTION,
                    user_prompt=user_prompt,
                )
            except Exception as exc:
                logger.warning(
                    f"LLM provider '{target_provider}' failed with error: {exc}. "
                    "Engaging intelligent Mock/Fallback provider."
                )
                # Fallback to deterministic provider
                parsed_json, metadata = await self.mock_provider.generate_analysis(
                    extracted_data=extracted,
                    system_prompt=SYSTEM_INSTRUCTION,
                    user_prompt=user_prompt,
                )
                metadata["fallback_used"] = True
                metadata["fallback_reason"] = f"Primary provider '{target_provider}' failed: {str(exc)}"
                metadata["attempted_provider"] = target_provider
        else:
            # Deterministic Mock Provider
            parsed_json, metadata = await self.mock_provider.generate_analysis(
                extracted_data=extracted,
                system_prompt=SYSTEM_INSTRUCTION,
                user_prompt=user_prompt,
            )

        # Validate and build strongly typed Pydantic models
        brand, analysis = self._validate_and_build_models(parsed_json, extracted)

        return LLMAnalysisResult(
            brand=brand,
            analysis=analysis,
            metadata=metadata,
        )

    # Aliases for interface flexibility
    async def analyze_website(
        self,
        website_data: Union[ExtractedWebsiteData, Dict[str, Any], str],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMAnalysisResult:
        """Alias for analyze()."""
        return await self.analyze(website_data=website_data, provider=provider, model=model)

    async def generate_analysis(
        self,
        website_data: Union[ExtractedWebsiteData, Dict[str, Any], str],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LLMAnalysisResult:
        """Alias for analyze()."""
        return await self.analyze(website_data=website_data, provider=provider, model=model)

    async def analyze_to_response(
        self,
        website: str,
        website_data: Union[ExtractedWebsiteData, Dict[str, Any], str],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LinkAnalysisResponse:
        """Convenience method returning a completed LinkAnalysisResponse."""
        result = await self.analyze(website_data=website_data, provider=provider, model=model)
        return result.to_response(website=website)


# Module-level default singleton
_default_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Returns singleton LLMService instance."""
    global _default_llm_service
    if _default_llm_service is None:
        _default_llm_service = LLMService()
    return _default_llm_service
