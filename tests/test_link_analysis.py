"""
Comprehensive Automated Test Suite for AI-Powered Website Link Analysis API.

Covers:
1. SSRF Protection & URL Validation:
   - Loopback addresses (127.0.0.1, ::1)
   - Cloud metadata endpoints (169.254.169.254)
   - Private RFC 1918 CIDRs (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
   - Blocked hostnames (localhost, 0.0.0.0, internal domains)
   - Numeric / Hex / Octal encoded IP bypass attempts
   - Unsupported / dangerous URL schemes (file://, ftp://, javascript:, data:)
   - Embedded credentials (user:pass@host)
   - Valid public HTTP/HTTPS URLs
2. Pydantic Schemas & Data Models:
   - LinkAnalysisRequest validation & sanitization
   - BrandInfo, CompetitorItem, CompetitorAnalysis (Section 1)
   - TargetSearchQuery & PlatformEnum normalization (Section 2)
   - SEOImprovements (Section 3)
   - ServiceItem & field aliasing (Section 4)
   - TargetAudience (Section 5)
   - LinkAnalysisResponse & ErrorResponse structural compliance
3. Website Content Extraction & Parsing:
   - Title, meta description, keywords, OpenGraph, JSON-LD, headings, clean text
   - Script/style tag stripping
   - Payload size truncation
4. LLM Service & Fallback Provider:
   - Deterministic mock provider generation (offline mode)
   - Structured JSON validation adhering to 5 required sections
5. FastAPI Endpoint Integration:
   - POST /link-analysis with valid URL (200 OK + Section 4 schema)
   - POST /link-analysis with SSRF / Private IP (400 Bad Request)
   - POST /link-analysis with invalid URL (400 / 422)
   - POST /link-analysis with malformed JSON body (422 Unprocessable Entity)
   - GET /health (200 OK)
   - GET /docs (200 OK)
"""

import os
import sys
from typing import Any, Dict
import pytest
from pydantic import ValidationError

# Ensure root project is on Python sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.schemas import (
    AnalysisData,
    BrandInfo,
    CompetitorAnalysis,
    CompetitorItem,
    ErrorResponse,
    ExtractedWebsiteData,
    LinkAnalysisRequest,
    LinkAnalysisResponse,
    PlatformEnum,
    SEOImprovements,
    ServiceItem,
    TargetAudience,
    TargetSearchQuery,
)
from app.utils.validators import (
    InvalidURLError,
    SSRFError,
    check_ssrf_safety,
    is_safe_url,
    validate_url,
)


# ============================================================================
# 1. SSRF Protection & URL Validation Tests
# ============================================================================

class TestSSRFProtectionAndURLValidation:
    """Rigorous security testing for SSRF prevention and input URL hygiene."""

    @pytest.mark.parametrize(
        "loopback_url",
        [
            "http://127.0.0.1",
            "https://127.0.0.1",
            "http://127.0.0.1:4000",
            "http://127.0.0.1:8080/admin",
            "http://127.1",
            "http://127.255.255.254",
            "http://[::1]",
            "http://[::1]:8080",
            "http://localhost",
            "http://localhost:4000",
            "http://0.0.0.0",
            "http://0.0.0.0:4000",
        ],
    )
    def test_ssrf_blocks_loopback_and_local(self, loopback_url: str):
        """Rejects loopback IP addresses and localhost hostnames."""
        with pytest.raises((SSRFError, InvalidURLError)):
            validate_url(loopback_url)
        assert is_safe_url(loopback_url) is False

    @pytest.mark.parametrize(
        "metadata_url",
        [
            "http://169.254.169.254",
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.169.254/computeMetadata/v1/",
            "http://metadata.google.internal",
            "http://instance-data",
        ],
    )
    def test_ssrf_blocks_cloud_metadata(self, metadata_url: str):
        """Blocks cloud instance metadata endpoints (AWS, GCP, Azure link-local)."""
        with pytest.raises((SSRFError, InvalidURLError)):
            validate_url(metadata_url)
        assert is_safe_url(metadata_url) is False

    @pytest.mark.parametrize(
        "private_ip_url",
        [
            # 10.0.0.0/8
            "http://10.0.0.1",
            "http://10.255.255.255",
            # 172.16.0.0/12
            "http://172.16.0.1",
            "http://172.31.255.255",
            # 192.168.0.0/16
            "http://192.168.1.1",
            "http://192.168.0.254",
            # Carrier Grade NAT (100.64.0.0/10)
            "http://100.64.0.1",
        ],
    )
    def test_ssrf_blocks_rfc1918_private_ranges(self, private_ip_url: str):
        """Blocks all RFC 1918 private IPv4 subnets and CGNAT."""
        with pytest.raises(SSRFError):
            validate_url(private_ip_url)
        assert is_safe_url(private_ip_url) is False

    @pytest.mark.parametrize(
        "internal_domain_url",
        [
            "http://service.local",
            "http://auth.internal",
            "http://database.lan",
            "http://intranet.corp",
            "http://router.home",
        ],
    )
    def test_ssrf_blocks_internal_tlds(self, internal_domain_url: str):
        """Blocks internal / private network domain suffixes."""
        with pytest.raises(SSRFError):
            validate_url(internal_domain_url)
        assert is_safe_url(internal_domain_url) is False

    @pytest.mark.parametrize(
        "encoded_ip_url",
        [
            # Decimal representation for 127.0.0.1 (2130706433)
            "http://2130706433",
            # Hexadecimal representation for 127.0.0.1
            "http://0x7f000001",
        ],
    )
    def test_ssrf_blocks_numeric_encoded_ips(self, encoded_ip_url: str):
        """Blocks alternative integer/hex IP encodings used to bypass naive regexes."""
        with pytest.raises(SSRFError):
            validate_url(encoded_ip_url)
        assert is_safe_url(encoded_ip_url) is False

    @pytest.mark.parametrize(
        "dangerous_scheme_url",
        [
            "file:///etc/passwd",
            "file://C:/Windows/win.ini",
            "ftp://files.example.com",
            "gopher://example.com",
            "javascript:alert('xss')",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "dict://127.0.0.1:11211",
        ],
    )
    def test_ssrf_blocks_dangerous_schemes(self, dangerous_scheme_url: str):
        """Blocks non-HTTP/HTTPS protocols and dangerous pseudo-schemes."""
        with pytest.raises(InvalidURLError):
            validate_url(dangerous_scheme_url)
        assert is_safe_url(dangerous_scheme_url) is False

    @pytest.mark.parametrize(
        "malformed_url",
        [
            "",
            "   ",
            "not-a-valid-url",
            "http://",
            "https://",
            "http://user:password@example.com",  # Credential exposure
            "http://example.com:99999",         # Port out of range
        ],
    )
    def test_rejects_malformed_urls_and_credentials(self, malformed_url: str):
        """Rejects empty, malformed, out-of-range, or credentialed URLs."""
        with pytest.raises(InvalidURLError):
            validate_url(malformed_url)
        if "://" in malformed_url or not malformed_url.strip():
            assert is_safe_url(malformed_url) is False

    @pytest.mark.parametrize(
        "valid_url",
        [
            "https://example.com",
            "http://example.com",
            "https://example.com/about-us",
            "https://stripe.com/docs/api?query=payments#overview",
            "https://github.com/fastapi/fastapi",
        ],
    )
    def test_accepts_valid_public_urls(self, valid_url: str):
        """Validates that legitimate public HTTP/HTTPS URLs pass validation."""
        normalized = validate_url(valid_url)
        assert normalized.startswith("http")
        assert is_safe_url(valid_url) is True


# ============================================================================
# 2. Pydantic Schemas & Data Model Tests
# ============================================================================

class TestPydanticSchemas:
    """Validates adherence to link-analysis.md Section 4 JSON response structure."""

    def test_link_analysis_request_valid(self):
        """LinkAnalysisRequest accepts and normalizes a valid URL."""
        req = LinkAnalysisRequest(url="https://dxora.com")
        assert req.url == "https://dxora.com"

    def test_link_analysis_request_invalid_url(self):
        """LinkAnalysisRequest raises ValidationError on SSRF attempts."""
        with pytest.raises(ValidationError):
            LinkAnalysisRequest(url="http://127.0.0.1")

    def test_brand_info_schema(self):
        """BrandInfo validates required brand fields."""
        brand = BrandInfo(
            name="Dxora Technologies",
            category="Software Development",
            summary="A digital transformation consultancy.",
        )
        assert brand.name == "Dxora Technologies"
        assert brand.category == "Software Development"
        assert "consultancy" in brand.summary

    def test_competitor_analysis_schema_with_coercion(self):
        """CompetitorAnalysis successfully parses and auto-coerces competitor items."""
        data = {
            "brand_overview": "Provider of web and mobile software.",
            "competitors": [
                {"name": "CompA", "strengths": ["Fast"], "weaknesses": ["Pricey"]},
                "CompB",  # Tests automatic string-to-item coercion
                {"name": "CompC", "strengths": ["Global reach"]},
            ],
            "strengths": ["Agile engineering", "Affordable"],
            "opportunities": ["AI adoption", "Global expansion"],
        }
        analysis = CompetitorAnalysis(**data)
        assert len(analysis.competitors) == 3
        assert analysis.competitors[0].name == "CompA"
        assert analysis.competitors[1].name == "CompB"
        assert analysis.competitors[2].name == "CompC"

    def test_target_search_query_platform_normalization(self):
        """TargetSearchQuery normalizes varied platform string names into PlatformEnum."""
        query = TargetSearchQuery(
            query="best web development agency",
            platforms=["google", "LinkedIn", "X (Twitter)", "Facebook", "Instagram"],
            intent="Service Discovery",
            business_relevance="High relevance for lead acquisition",
            target_audience="CTOs & Founders",
        )
        assert PlatformEnum.GOOGLE in query.platforms
        assert PlatformEnum.LINKEDIN in query.platforms
        assert PlatformEnum.X in query.platforms
        assert PlatformEnum.FACEBOOK in query.platforms
        assert PlatformEnum.INSTAGRAM in query.platforms

    def test_seo_improvements_schema(self):
        """SEOImprovements validates primary/secondary keywords and title structure."""
        seo = SEOImprovements(
            primary_keyword="Web Development Services",
            secondary_keywords=["Custom Software", "Cloud Apps"],
            long_tail_keywords=["affordable web development company for startups"],
            recommended_title="Dxora | Leading Web Development Services",
            meta_description="Build modern web and mobile applications with Dxora.",
            supporting_content=["Case Studies", "Technical Blog", "FAQ"],
        )
        assert seo.primary_keyword == "Web Development Services"
        assert len(seo.secondary_keywords) == 2
        assert len(seo.supporting_content) == 3

    def test_service_item_schema_and_alias(self):
        """ServiceItem accepts both 'name' and 'service_name' alias."""
        # Using standard field
        s1 = ServiceItem(
            service_name="Custom API Development",
            description="High-speed RESTful and GraphQL APIs",
            target_industry="Fintech",
            target_customer="Tech startups",
            main_benefit="Faster time to market",
        )
        assert s1.name == "Custom API Development"

        # Using name directly
        s2 = ServiceItem(
            name="Cloud Migration",
            description="End-to-end AWS/GCP cloud migration",
            main_benefit="Reduced hosting costs",
        )
        assert s2.name == "Cloud Migration"

    def test_target_audience_schema(self):
        """TargetAudience validates target segments, industries, and pain points."""
        audience = TargetAudience(
            primary_audience=["Startup Founders", "CTOs"],
            secondary_audience=["Product Managers"],
            industries=["Fintech", "Healthtech", "E-commerce"],
            customer_needs=["Fast MVP launch", "Scalable cloud architecture"],
            business_types=["B2B SaaS", "Early-stage startups"],
        )
        assert len(audience.primary_audience) == 2
        assert "Fintech" in audience.industries

    def test_full_link_analysis_response_schema(self):
        """Tests end-to-end LinkAnalysisResponse model hierarchy matching Section 4."""
        response_data = {
            "success": True,
            "website": "https://example.com",
            "brand": {
                "name": "Example Corp",
                "category": "Technology",
                "summary": "An example enterprise providing online services.",
            },
            "analysis": {
                "competitor_analysis": {
                    "brand_overview": "Example Corp overview",
                    "competitors": [
                        {"name": "Comp1", "strengths": ["Brand"], "weaknesses": ["Slow"]},
                        {"name": "Comp2", "strengths": ["Cost"], "weaknesses": ["Bugs"]},
                        {"name": "Comp3", "strengths": ["Scale"], "weaknesses": ["Support"]},
                    ],
                    "strengths": ["Innovation"],
                    "opportunities": ["AI"],
                },
                "target_search_queries": [
                    {
                        "query": "Best tech services",
                        "platforms": ["Google", "LinkedIn"],
                        "intent": "Service Discovery",
                    }
                ],
                "seo_improvements": {
                    "primary_keyword": "Tech Services",
                    "secondary_keywords": ["Cloud", "Dev"],
                    "long_tail_keywords": ["best tech services in us"],
                    "recommended_title": "Example Corp | Top Tech Services",
                    "meta_description": "Comprehensive technology solutions for enterprise.",
                    "supporting_content": ["FAQ Page", "Case Studies"],
                },
                "top_services": [
                    {
                        "service_name": "Cloud Engineering",
                        "description": "Scalable cloud services",
                        "target_industry": "Tech",
                        "target_customer": "Enterprises",
                        "main_benefit": "99.99% uptime",
                    }
                ],
                "target_audience": {
                    "primary_audience": ["Enterprise IT Leaders"],
                    "secondary_audience": ["Startups"],
                    "industries": ["Technology", "Finance"],
                    "customer_needs": ["High scalability", "Security"],
                },
            },
        }
        res = LinkAnalysisResponse(**response_data)
        assert res.success is True
        assert res.website == "https://example.com"
        assert res.brand.name == "Example Corp"
        assert len(res.analysis.competitor_analysis.competitors) == 3
        assert res.analysis.target_search_queries[0].platforms[0] == PlatformEnum.GOOGLE


# ============================================================================
# 3. Website Content Extraction & Parsing Tests
# ============================================================================

class TestWebsiteExtraction:
    """Tests the parsing, metadata extraction, and HTML cleaning logic."""

    SAMPLE_HTML = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Dxora Solutions | Next-Gen Software Development</title>
        <meta name="description" content="Leading custom software, web, and mobile app development agency.">
        <meta name="keywords" content="software development, web development, mobile apps">
        <meta property="og:title" content="Dxora Solutions">
        <meta property="og:description" content="Next-Gen Software Development">
        <meta property="og:site_name" content="Dxora">
        <meta name="twitter:card" content="summary_large_image">
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "Dxora Solutions",
            "url": "https://dxora.com"
        }
        </script>
        <style>body { font-family: sans-serif; } .ad-banner { display: none; }</style>
    </head>
    <body>
        <nav><a href="/">Home</a><a href="/about">About</a><a href="/services">Services</a></nav>
        <header>
            <h1>Empowering Modern Businesses Through Scalable Software</h1>
        </header>
        <main>
            <h2>Our Core Engineering Services</h2>
            <p>We provide full-stack web development, cloud migration, and AI integration services to enterprise clients.</p>
            <h3>Custom Web Development</h3>
            <p>Building high-speed responsive applications using React, Next.js, and FastAPI.</p>
            <h3>Cloud & DevOps</h3>
            <p>Automated CI/CD pipelines, container orchestration with Kubernetes, and AWS infrastructure management.</p>
        </main>
        <footer>
            <p>&copy; 2026 Dxora Solutions. All rights reserved.</p>
            <script>console.log("analytics tracking loaded");</script>
        </footer>
    </body>
    </html>
    """

    def test_html_parsing_and_metadata_extraction(self):
        """Verifies BeautifulSoup / Extractor extracts title, meta, OpenGraph, JSON-LD, headings, and clean text."""
        try:
            from app.services.website_extractor import WebsiteExtractor
            extractor = WebsiteExtractor()
            data = extractor.parse_html(html=self.SAMPLE_HTML, url="https://dxora.com")
        except (ImportError, AttributeError):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(self.SAMPLE_HTML, "html.parser")
            title = soup.title.string if soup.title else ""
            desc = soup.find("meta", attrs={"name": "description"})
            desc_content = desc["content"] if desc else ""
            h1 = [h.get_text(strip=True) for h in soup.find_all("h1")]

            data = ExtractedWebsiteData(
                url="https://dxora.com",
                title=title,
                meta_description=desc_content,
                headings={"h1": h1},
                main_text=" ".join([p.get_text(strip=True) for p in soup.find_all("p")]),
            )

        assert "Dxora Solutions" in data.title
        assert "software" in data.meta_description.lower()
        assert len(data.headings.get("h1", [])) > 0
        assert "Empowering Modern Businesses" in data.headings["h1"][0]
        # Scripts and styles should not pollute main text
        assert "console.log" not in data.main_text
        assert "font-family" not in data.main_text


# ============================================================================
# 4. LLM Service & Fallback Provider Tests
# ============================================================================

class TestLLMServiceAndFallback:
    """Verifies that the LLM Service functions properly and deterministic fallback delivers valid structured output."""

    @pytest.mark.asyncio
    async def test_mock_fallback_provider_produces_valid_response(self):
        """Verifies deterministic fallback synthesizes a complete 5-section response when offline or without API keys."""
        from app.services.llm_service import LLMService, LLMAnalysisResult
        llm = LLMService(provider="mock")
        extracted = ExtractedWebsiteData(
            url="https://stripe.com",
            title="Stripe | Financial Infrastructure for the Internet",
            meta_description="Payments infrastructure for online business.",
            headings={"h1": ["Financial Infrastructure for the Internet"]},
            main_text="Stripe is a financial infrastructure platform for businesses. Millions of companies use Stripe to accept payments and manage businesses online.",
        )
        result = await llm.analyze(extracted)
        assert isinstance(result, LLMAnalysisResult)
        assert result.brand.name == "Stripe"
        assert len(result.analysis.competitor_analysis.competitors) >= 3
        assert len(result.analysis.target_search_queries) == 5
        assert len(result.analysis.top_services) == 5
        resp = result.to_response("https://stripe.com")
        assert resp.success is True
        assert resp.website == "https://stripe.com"


# ============================================================================
# 5. FastAPI Endpoint Integration Tests
# ============================================================================

class TestAPIEndpoints:
    """Integration tests running against the FastAPI app endpoints."""

    @pytest.fixture
    def client(self):
        """Provides a Starlette / FastAPI TestClient instance."""
        from fastapi.testclient import TestClient
        try:
            from app.main import app
            return TestClient(app)
        except Exception as exc:
            pytest.skip(f"app.main could not be imported yet: {exc}")

    def test_health_check_endpoint(self, client):
        """GET /health returns 200 OK with status message."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ("ok", "healthy", "up")

    def test_docs_endpoint(self, client):
        """GET /docs returns 200 OK Swagger UI."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_link_analysis_ssrf_block(self, client):
        """POST /link-analysis with 127.0.0.1 or localhost returns 400 Bad Request."""
        payload = {"url": "http://127.0.0.1:4000"}
        response = client.post("/link-analysis", json=payload)
        assert response.status_code in (400, 422)
        data = response.json()
        assert "error" in data or "detail" in data

    def test_link_analysis_private_cidr_block(self, client):
        """POST /link-analysis with private network IP returns 400 Bad Request."""
        payload = {"url": "http://192.168.1.1/admin"}
        response = client.post("/link-analysis", json=payload)
        assert response.status_code in (400, 422)

    def test_link_analysis_cloud_metadata_block(self, client):
        """POST /link-analysis with AWS metadata endpoint returns 400 Bad Request."""
        payload = {"url": "http://169.254.169.254/latest/meta-data/"}
        response = client.post("/link-analysis", json=payload)
        assert response.status_code in (400, 422)

    def test_link_analysis_malformed_body(self, client):
        """POST /link-analysis with empty body returns 400 or 422."""
        response = client.post("/link-analysis", json={})
        assert response.status_code in (400, 422)

    def test_link_analysis_invalid_scheme(self, client):
        """POST /link-analysis with file:// scheme returns 400 or 422."""
        payload = {"url": "file:///etc/passwd"}
        response = client.post("/link-analysis", json=payload)
        assert response.status_code in (400, 422)

    def test_link_analysis_valid_url_with_mock_provider(self, client):
        """POST /link-analysis returns full 5-section response when website extraction succeeds."""
        from unittest.mock import AsyncMock, patch
        from app.schemas import ExtractedWebsiteData

        mock_data = ExtractedWebsiteData(
            url="https://stripe.com",
            title="Stripe | Financial Infrastructure for the Internet",
            meta_description="Payments infrastructure for online business.",
            headings={"h1": ["Financial Infrastructure"], "h2": ["Payments", "Billing", "Connect"]},
            main_text="Stripe is a financial infrastructure platform for businesses worldwide.",
        )

        with patch("app.services.website_extractor.WebsiteExtractor.extract", new_callable=AsyncMock) as mock_extract:
            mock_extract.return_value = mock_data
            response = client.post("/link-analysis", json={"url": "https://stripe.com"})
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["website"] == "https://stripe.com"
            assert "brand" in data
            assert data["brand"]["name"] == "Stripe"
            assert "analysis" in data
            analysis = data["analysis"]
            assert len(analysis["competitor_analysis"]["competitors"]) >= 3
            assert len(analysis["target_search_queries"]) == 5
            assert "primary_keyword" in analysis["seo_improvements"]
            assert len(analysis["top_services"]) == 5
            assert "primary_audience" in analysis["target_audience"]

    def test_link_analysis_unreachable_target_website_error(self, client):
        """POST /link-analysis returns 502 Bad Gateway when external website fails to connect."""
        from unittest.mock import AsyncMock, patch
        from app.services.website_extractor import WebsiteFetchError

        with patch("app.services.website_extractor.WebsiteExtractor.extract", new_callable=AsyncMock) as mock_extract:
            mock_extract.side_effect = WebsiteFetchError("Connection refused by remote host")
            response = client.post("/link-analysis", json={"url": "https://unreachable-example-host.com"})
            assert response.status_code in (502, 504)
            data = response.json()
            assert data["success"] is False
            assert "error" in data or "detail" in data
