"""
Website Content Extraction Service.

Features:
- Asynchronous website fetching via HTTPX with custom User-Agent, timeout, and size limits
- Strict SSRF validation on initial URL and on every redirect destination
- Comprehensive HTML parsing via BeautifulSoup (with html.parser or lxml)
- Rich metadata extraction: OpenGraph, Twitter Cards, meta description/keywords, and JSON-LD
- Clean text extraction: strips scripts, styles, navigation, footers, cookie banners, and ads
- Structured semantic heading extraction (H1, H2, H3) and relevant service links
- Token and size optimization: intelligent content truncation preserving high-priority sections
  for LLM context windows
- Robust error handling: specific exception hierarchy for timeouts, blocked access, not found,
  and connection failures
"""

import json
import logging
import re
import ssl
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import bs4
from bs4 import BeautifulSoup
import httpx

from app.config import get_settings
from app.schemas import ExtractedWebsiteData
from app.utils.validators import (
    InvalidURLError,
    SSRFError,
    check_ssrf_safety,
    validate_url,
)

logger = logging.getLogger(__name__)

# Choose the fastest available parser
try:
    import lxml  # noqa: F401
    DEFAULT_PARSER = "lxml"
except ImportError:
    DEFAULT_PARSER = "html.parser"


# ============================================================================
# Custom Exceptions
# ============================================================================

class WebsiteExtractionError(Exception):
    """Base exception for all website extraction and fetching failures."""
    pass


class WebsiteSSRFError(WebsiteExtractionError):
    """Raised when a URL or redirect location violates SSRF safety constraints."""
    pass


class WebsiteTimeoutError(WebsiteExtractionError):
    """Raised when an HTTP connection or read times out."""
    pass


class WebsiteFetchError(WebsiteExtractionError):
    """Raised for network connection errors, SSL handshake failures, or invalid protocols."""
    pass


class WebsiteHTTPError(WebsiteExtractionError):
    """Raised when a website returns an HTTP error status (4xx or 5xx)."""
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class WebsiteBlockedError(WebsiteHTTPError):
    """Raised when a website responds with 401 Unauthorized or 403 Forbidden."""
    pass


class WebsiteNotFoundError(WebsiteHTTPError):
    """Raised when a website responds with 404 Not Found."""
    pass


class WebsiteSizeExceededError(WebsiteExtractionError):
    """Raised when response payload exceeds the maximum permitted content length."""
    pass


# ============================================================================
# Extraction Helpers & Keywords
# ============================================================================

# Tags to completely remove from DOM before text extraction
DISCARD_TAGS: Set[str] = {
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "video",
    "audio",
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    "button",
    "select",
    "dialog",
    "template",
}

# Substrings in class or id attributes that indicate ads, popups, or cookie banners
DISCARD_ATTR_PATTERNS = re.compile(
    r"(cookie|gdpr|consent|privacy-notice|advertisement|banner-ad|ad-container|"
    r"popup|modal-dialog|newsletter-signup|widget-footer|social-share)",
    re.IGNORECASE,
)

# Common non-informative link texts to exclude
IGNORE_LINK_TEXTS: Set[str] = {
    "home", "about", "about us", "contact", "contact us", "privacy policy",
    "privacy", "terms", "terms of service", "terms & conditions", "terms and conditions",
    "cookie policy", "login", "log in", "signin", "sign in", "signup", "sign up",
    "register", "cart", "checkout", "search", "menu", "close", "back", "next",
    "read more", "learn more", "click here", "view more", "subscribe", "share",
    "facebook", "twitter", "linkedin", "instagram", "youtube", "github",
}

# Business-critical keywords used to prioritize sentences and paragraphs for LLM
BUSINESS_KEYWORDS: Set[str] = {
    "service", "services", "solution", "solutions", "product", "products",
    "offer", "offering", "offerings", "platform", "technology", "technologies",
    "about us", "we are", "we provide", "we help", "we build", "specializ",
    "industry", "industries", "client", "clients", "customer", "customers",
    "enterprise", "startup", "startups", "business", "consulting", "development",
    "pricing", "features", "mission", "vision", "benefit", "benefits",
}


# ============================================================================
# WebsiteExtractor Service
# ============================================================================

class WebsiteExtractor:
    """
    Asynchronous website content extraction service.

    Key capabilities:
    - Safe async HTTP fetching via HTTPX with custom headers and timeouts
    - SSRF protection enforced on original URL and re-verified on every redirect
    - Payload size cap enforcement (streaming check up to MAX_CONTENT_LENGTH)
    - Structured extraction of title, meta description, keywords, OpenGraph, and Twitter tags
    - Full JSON-LD schema parsing (Organization, WebSite, LocalBusiness, Product, Service)
    - Semantic heading hierarchy extraction (H1, H2, H3)
    - Main body text sanitization with boilerplate and script stripping
    - Intelligent prompt truncation preserving high-value context within token budget
    """

    def __init__(
        self,
        timeout: Optional[int] = None,
        max_content_length: Optional[int] = None,
        max_llm_input_chars: Optional[int] = None,
        user_agent: Optional[str] = None,
        allow_local: bool = False,
        max_redirects: int = 5,
    ):
        settings = get_settings()
        self.timeout = timeout or settings.TIMEOUT_SECONDS
        self.max_content_length = max_content_length or settings.MAX_CONTENT_LENGTH
        self.max_llm_input_chars = max_llm_input_chars or settings.MAX_LLM_INPUT_CHARS
        self.user_agent = user_agent or settings.USER_AGENT
        self.allow_local = allow_local
        self.max_redirects = max_redirects

    # ------------------------------------------------------------------------
    # Network Fetching with SSRF and Redirect Protection
    # ------------------------------------------------------------------------

    async def fetch_html(self, url: str) -> Tuple[str, str, int]:
        """
        Asynchronously fetches the HTML content of the target URL.

        Enforces:
        - Initial SSRF verification
        - Redirect tracking with SSRF re-validation on every hop
        - Timeout handling
        - Maximum content length limits
        - Specific HTTP status code exception handling

        Returns:
            Tuple of (html_content, final_url, status_code).

        Raises:
            WebsiteSSRFError: If any target or redirect address is internal/private.
            WebsiteTimeoutError: On request timeout.
            WebsiteBlockedError: On 401 or 403 HTTP status.
            WebsiteNotFoundError: On 404 HTTP status.
            WebsiteHTTPError: On any other 4xx/5xx HTTP status.
            WebsiteSizeExceededError: If content length exceeds max_content_length.
            WebsiteFetchError: On connection/network/SSL errors.
        """
        # 1. Validate initial URL and SSRF safety
        try:
            current_url = validate_url(url, allow_local=self.allow_local)
        except (SSRFError, InvalidURLError) as exc:
            raise WebsiteSSRFError(f"URL failed SSRF/format validation: {exc}") from exc

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        # Use an async client without automatic redirect following
        timeout_config = httpx.Timeout(self.timeout, connect=self.timeout)

        async with httpx.AsyncClient(timeout=timeout_config, verify=True) as client:
            redirect_count = 0

            while redirect_count <= self.max_redirects:
                # Re-verify SSRF on the current destination host before making request
                try:
                    check_ssrf_safety(current_url, allow_local=self.allow_local)
                except (SSRFError, InvalidURLError) as exc:
                    raise WebsiteSSRFError(
                        f"SSRF violation detected for destination '{current_url}': {exc}"
                    ) from exc

                try:
                    # Stream request to enforce payload limit without loading unbounded memory
                    async with client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    ) as response:
                        # Handle HTTP Redirects (301, 302, 303, 307, 308)
                        if response.is_redirect:
                            redirect_count += 1
                            if redirect_count > self.max_redirects:
                                raise WebsiteFetchError(
                                    f"Exceeded maximum redirect limit of {self.max_redirects}."
                                )

                            location = response.headers.get("Location")
                            if not location:
                                raise WebsiteFetchError(
                                    f"Redirect response {response.status_code} missing Location header."
                                )

                            # Resolve relative redirect URLs against current URL
                            next_url = urllib.parse.urljoin(current_url, location)

                            # Validate new redirect target against SSRF
                            try:
                                next_url = validate_url(next_url, allow_local=self.allow_local)
                            except (SSRFError, InvalidURLError) as exc:
                                raise WebsiteSSRFError(
                                    f"Redirect to '{next_url}' blocked by SSRF protection: {exc}"
                                ) from exc

                            logger.debug(
                                f"Following redirect ({redirect_count}/{self.max_redirects}): "
                                f"{current_url} -> {next_url}"
                            )
                            current_url = next_url
                            continue

                        # Handle HTTP error responses
                        status = response.status_code
                        if status == 401 or status == 403:
                            raise WebsiteBlockedError(
                                f"Access to website was denied (HTTP {status}): {current_url}",
                                status_code=status,
                            )
                        elif status == 404:
                            raise WebsiteNotFoundError(
                                f"Website resource not found (HTTP 404): {current_url}",
                                status_code=status,
                            )
                        elif status >= 400:
                            raise WebsiteHTTPError(
                                f"Website returned HTTP {status} error: {current_url}",
                                status_code=status,
                            )

                        # Check declared Content-Length header
                        content_len_hdr = response.headers.get("Content-Length")
                        if content_len_hdr and content_len_hdr.isdigit():
                            if int(content_len_hdr) > self.max_content_length:
                                raise WebsiteSizeExceededError(
                                    f"Content-Length ({content_len_hdr} bytes) exceeds maximum "
                                    f"allowed size ({self.max_content_length} bytes)."
                                )

                        # Stream chunks and enforce size limit
                        body_chunks = []
                        total_bytes = 0
                        async for chunk in response.aiter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > self.max_content_length:
                                raise WebsiteSizeExceededError(
                                    f"Downloaded content exceeded limit of {self.max_content_length} bytes."
                                )
                            body_chunks.append(chunk)

                        raw_bytes = b"".join(body_chunks)

                        # Determine encoding from Content-Type or fall back to utf-8
                        encoding = response.encoding or "utf-8"
                        try:
                            html_text = raw_bytes.decode(encoding)
                        except (UnicodeDecodeError, LookupError):
                            # Resilient fallback with replacement
                            html_text = raw_bytes.decode("utf-8", errors="replace")

                        return html_text, str(response.url), status

                except httpx.TimeoutException as exc:
                    raise WebsiteTimeoutError(
                        f"Request timed out after {self.timeout}s while fetching '{current_url}'."
                    ) from exc
                except (ssl.SSLError, httpx.ConnectError) as exc:
                    raise WebsiteFetchError(
                        f"Failed to establish connection to '{current_url}': {exc}"
                    ) from exc
                except httpx.NetworkError as exc:
                    raise WebsiteFetchError(
                        f"Network error encountered while fetching '{current_url}': {exc}"
                    ) from exc
                except httpx.HTTPError as exc:
                    raise WebsiteFetchError(
                        f"HTTP protocol error for '{current_url}': {exc}"
                    ) from exc

            raise WebsiteFetchError(f"Too many redirects for URL: '{url}'.")

    # ------------------------------------------------------------------------
    # HTML Parsing and Information Extraction
    # ------------------------------------------------------------------------

    def parse_html(
        self,
        html: str,
        url: str,
        final_url: Optional[str] = None,
        status_code: int = 200,
    ) -> ExtractedWebsiteData:
        """
        Parses raw HTML and extracts rich, structured metadata and clean body content.

        Args:
            html: Raw HTML content string.
            url: The initial requested URL.
            final_url: The final URL after redirect resolution.
            status_code: HTTP status code of the response.

        Returns:
            ExtractedWebsiteData containing title, meta, og, twitter, json-ld,
            headings, clean main text, and links.
        """
        if not html:
            return ExtractedWebsiteData(
                url=url,
                final_url=final_url or url,
                status_code=status_code,
                content_length=0,
            )

        soup = BeautifulSoup(html, DEFAULT_PARSER)

        # 1. Extract Page Title
        title = self._extract_title(soup)

        # 2. Extract Meta Description & Keywords
        meta_description, meta_keywords = self._extract_meta(soup)

        # 3. Extract OpenGraph and Twitter Metadata
        og_data = self._extract_opengraph(soup)
        twitter_data = self._extract_twitter(soup)

        # Fallback for title/description if missing from HTML head
        if not title and og_data.get("og:title"):
            title = og_data["og:title"]
        elif not title and twitter_data.get("twitter:title"):
            title = twitter_data["twitter:title"]

        if not meta_description and og_data.get("og:description"):
            meta_description = og_data["og:description"]
        elif not meta_description and twitter_data.get("twitter:description"):
            meta_description = twitter_data["twitter:description"]

        # 4. Extract JSON-LD Schema Metadata
        json_ld = self._extract_json_ld(soup)

        # 5. Extract Semantic Headings (H1, H2, H3)
        headings = self._extract_headings(soup)

        # 6. Extract Informative Service / Product Links
        links = self._extract_links(soup, final_url or url)

        # 7. Extract Clean Main Content Text
        main_text = self._extract_clean_text(soup)

        return ExtractedWebsiteData(
            url=url,
            final_url=final_url or url,
            status_code=status_code,
            title=title.strip(),
            meta_description=meta_description.strip(),
            meta_keywords=meta_keywords,
            og_data=og_data,
            twitter_data=twitter_data,
            json_ld=json_ld,
            headings=headings,
            main_text=main_text,
            links=links,
            content_length=len(html),
        )

    # ------------------------------------------------------------------------
    # Internal Extraction Utilities
    # ------------------------------------------------------------------------

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extracts the page title from <title> tag."""
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return self._clean_whitespace(title_tag.string)
        return ""

    def _extract_meta(self, soup: BeautifulSoup) -> Tuple[str, List[str]]:
        """Extracts meta description and keywords list."""
        description = ""
        keywords: List[str] = []

        # Description
        desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if not desc_tag:
            desc_tag = soup.find("meta", attrs={"property": re.compile(r"^description$", re.I)})
        if desc_tag and desc_tag.get("content"):
            description = self._clean_whitespace(desc_tag.get("content", ""))

        # Keywords
        kw_tag = soup.find("meta", attrs={"name": re.compile(r"^keywords$", re.I)})
        if kw_tag and kw_tag.get("content"):
            raw_kws = kw_tag.get("content", "").split(",")
            keywords = [self._clean_whitespace(k) for k in raw_kws if self._clean_whitespace(k)]

        return description, keywords

    def _extract_opengraph(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extracts OpenGraph protocol meta properties."""
        og_dict: Dict[str, str] = {}
        for tag in soup.find_all("meta", attrs={"property": re.compile(r"^og:", re.I)}):
            prop = tag.get("property", "").strip().lower()
            content = tag.get("content", "").strip()
            if prop and content:
                og_dict[prop] = content

        # Also support meta name="og:..."
        for tag in soup.find_all("meta", attrs={"name": re.compile(r"^og:", re.I)}):
            name = tag.get("name", "").strip().lower()
            content = tag.get("content", "").strip()
            if name and content and name not in og_dict:
                og_dict[name] = content

        return og_dict

    def _extract_twitter(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extracts Twitter card meta tags."""
        tw_dict: Dict[str, str] = {}
        for tag in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:", re.I)}):
            name = tag.get("name", "").strip().lower()
            content = tag.get("content", "").strip()
            if name and content:
                tw_dict[name] = content

        for tag in soup.find_all("meta", attrs={"property": re.compile(r"^twitter:", re.I)}):
            prop = tag.get("property", "").strip().lower()
            content = tag.get("content", "").strip()
            if prop and content and prop not in tw_dict:
                tw_dict[prop] = content

        return tw_dict

    def _extract_json_ld(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extracts and parses JSON-LD schema blocks."""
        extracted: List[Dict[str, Any]] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw_text = script.get_text()
            if not raw_text or not raw_text.strip():
                continue
            try:
                data = json.loads(raw_text.strip())
                if isinstance(data, dict):
                    # Handle @graph container
                    if "@graph" in data and isinstance(data["@graph"], list):
                        for item in data["@graph"]:
                            if isinstance(item, dict):
                                extracted.append(self._sanitize_json_ld_item(item))
                    else:
                        extracted.append(self._sanitize_json_ld_item(data))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            extracted.append(self._sanitize_json_ld_item(item))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return extracted

    def _sanitize_json_ld_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Filters relevant properties from JSON-LD schema objects."""
        allowed_keys = {
            "@context", "@type", "name", "legalName", "alternateName",
            "description", "headline", "url", "serviceType", "brand",
            "category", "offers", "hasOfferCatalog", "address", "areaServed",
            "knowsAbout", "slogan", "telephone", "email",
        }
        sanitized: Dict[str, Any] = {}
        for k, v in item.items():
            if k in allowed_keys or k.startswith("@"):
                if isinstance(v, (str, int, float, bool)):
                    sanitized[k] = v
                elif isinstance(v, list) and all(isinstance(x, (str, dict)) for x in v):
                    sanitized[k] = v[:10]  # Limit list size
                elif isinstance(v, dict):
                    # Capture nested names/types
                    nested = {
                        nk: nv for nk, nv in v.items()
                        if nk in allowed_keys and isinstance(nv, (str, int, float, bool))
                    }
                    if nested:
                        sanitized[k] = nested
        return sanitized

    def _extract_headings(self, soup: BeautifulSoup) -> Dict[str, List[str]]:
        """Extracts text of H1, H2, and H3 tags in order without duplicates."""
        headings: Dict[str, List[str]] = {"h1": [], "h2": [], "h3": []}
        seen = set()

        for level in ("h1", "h2", "h3"):
            for tag in soup.find_all(level):
                text = self._clean_whitespace(tag.get_text())
                if text and len(text) > 1 and text not in seen:
                    seen.add(text)
                    headings[level].append(text)

        return headings

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extracts informative link anchor texts and service paths."""
        results: List[str] = []
        seen = set()

        for a in soup.find_all("a", href=True):
            text = self._clean_whitespace(a.get_text())
            href = a.get("href", "").strip()

            if not text or len(text) < 3 or len(text) > 60:
                continue

            lower_text = text.lower()
            if lower_text in IGNORE_LINK_TEXTS:
                continue

            # Prioritize service, solution, product, feature, or pricing links
            is_relevant = any(kw in lower_text or kw in href.lower() for kw in BUSINESS_KEYWORDS)

            if is_relevant or len(results) < 15:
                entry = f"{text} ({href})" if (href.startswith("http") or href.startswith("/")) else text
                if entry not in seen:
                    seen.add(entry)
                    results.append(entry)

            if len(results) >= 30:
                break

        return results

    def _extract_clean_text(self, soup: BeautifulSoup) -> str:
        """
        Cleans the HTML tree and extracts readable body content text.
        Removes scripts, styles, navigation, footer, and boilerplate ads.
        """
        # Create a fresh copy of the body (or whole soup) to strip elements
        body = soup.find("body") or soup
        # Clone tree to avoid mutating caller's structure
        import copy
        body_copy = copy.copy(body)

        # Decompose unwanted elements
        for tag_name in DISCARD_TAGS:
            for el in body_copy.find_all(tag_name):
                el.decompose()

        # Decompose elements with ad, popup, cookie banner classes/ids
        for el in body_copy.find_all(True, attrs={"class": DISCARD_ATTR_PATTERNS}):
            el.decompose()
        for el in body_copy.find_all(True, attrs={"id": DISCARD_ATTR_PATTERNS}):
            el.decompose()

        # Identify main content container if available
        main_container = (
            body_copy.find("main")
            or body_copy.find("article")
            or body_copy.find(id=re.compile(r"(content|main|primary)", re.I))
            or body_copy.find(class_=re.compile(r"(content|main|article|entry)", re.I))
            or body_copy
        )

        # Extract text elements (p, li, h1-h6, blockquote, div text)
        paragraphs: List[str] = []
        seen_texts = set()

        for el in main_container.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote"]):
            txt = self._clean_whitespace(el.get_text())
            if txt and len(txt) > 20:
                # Deduplicate exact repeating sentences or menus
                if txt not in seen_texts:
                    seen_texts.add(txt)
                    paragraphs.append(txt)

        if not paragraphs:
            # Fallback to full body get_text with separator
            raw_text = main_container.get_text(separator="\n")
            lines = [self._clean_whitespace(line) for line in raw_text.splitlines()]
            paragraphs = [line for line in lines if len(line) > 20]

        return "\n\n".join(paragraphs)

    @staticmethod
    def _clean_whitespace(text: str) -> str:
        """Collapses consecutive spaces, tabs, and newlines."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------------
    # Token / Size Optimization for LLM Context Window
    # ------------------------------------------------------------------------

    def build_llm_context(
        self,
        data: ExtractedWebsiteData,
        max_chars: Optional[int] = None,
    ) -> str:
        """
        Builds an optimized, highly informative plain-text context string
        tailored for LLM prompt ingestion.

        Strategy:
        1. Always preserve critical top-level metadata:
           - Page Title, Meta Description, Meta Keywords
           - OpenGraph (site_name, title, description)
           - JSON-LD Schema (Organizations, Products, Services)
           - Complete Heading Hierarchy (H1, H2, H3)
           - Top Service and Product Mentions
        2. Filter and rank main body paragraphs:
           - Prioritize paragraphs with business-critical keywords
             (offerings, solutions, target audience, features, about)
           - Append remaining paragraphs until max_chars budget is reached
        3. Guarantee deterministic bounds preventing context overflow.

        Args:
            data: ExtractedWebsiteData instance.
            max_chars: Maximum character budget (defaults to self.max_llm_input_chars).

        Returns:
            Formatted prompt context string ready for LLM consumption.
        """
        budget = max_chars or self.max_llm_input_chars

        sections: List[str] = []

        # 1. Header Information
        sections.append("=== WEBSITE METADATA ===")
        sections.append(f"Target URL: {data.url}")
        if data.final_url and data.final_url != data.url:
            sections.append(f"Final URL: {data.final_url}")
        if data.title:
            sections.append(f"Page Title: {data.title}")
        if data.meta_description:
            sections.append(f"Meta Description: {data.meta_description}")
        if data.meta_keywords:
            sections.append(f"Meta Keywords: {', '.join(data.meta_keywords[:15])}")

        # OpenGraph
        if data.og_data:
            og_items = []
            for k in ("og:site_name", "og:title", "og:description", "og:type"):
                if k in data.og_data:
                    og_items.append(f"{k}: {data.og_data[k]}")
            if og_items:
                sections.append("OpenGraph Details:\n" + "\n".join(f"  - {item}" for item in og_items))

        # JSON-LD Schemas
        if data.json_ld:
            sections.append("Structured Schema (JSON-LD):")
            for item in data.json_ld[:5]:
                try:
                    summary = json.dumps(item, indent=2)
                    sections.append(f"```json\n{summary}\n```")
                except Exception:
                    pass

        # 2. Semantic Headings
        headings_lines = []
        if data.headings.get("h1"):
            headings_lines.append("H1 Headings: " + " | ".join(data.headings["h1"][:5]))
        if data.headings.get("h2"):
            headings_lines.append("H2 Headings: " + " | ".join(data.headings["h2"][:15]))
        if data.headings.get("h3"):
            headings_lines.append("H3 Headings: " + " | ".join(data.headings["h3"][:20]))

        if headings_lines:
            sections.append("\n=== CONTENT HEADINGS & OFFERINGS ===")
            sections.extend(headings_lines)

        # 3. Key Navigation & Services Links
        if data.links:
            sections.append("\n=== IDENTIFIED SERVICE & SECTION LINKS ===")
            sections.append("\n".join(f"- {link}" for link in data.links[:25]))

        # Calculate budget used so far
        header_text = "\n".join(sections)
        remaining_budget = max(0, budget - len(header_text) - 200)

        # 4. Main Body Content (Prioritized Paragraphs)
        if data.main_text and remaining_budget > 200:
            sections.append("\n=== MAIN PAGE CONTENT ===")
            paragraphs = data.main_text.split("\n\n")

            high_priority: List[str] = []
            medium_priority: List[str] = []

            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue
                p_lower = p_clean.lower()
                # Check for business keywords
                if any(kw in p_lower for kw in BUSINESS_KEYWORDS):
                    high_priority.append(p_clean)
                else:
                    medium_priority.append(p_clean)

            selected_body: List[str] = []
            current_len = 0

            # First add high priority paragraphs
            for p in high_priority:
                if current_len + len(p) + 4 > remaining_budget:
                    # Truncate if partially fits
                    cutoff = remaining_budget - current_len - 20
                    if cutoff > 100:
                        selected_body.append(p[:cutoff] + "... [truncated]")
                    break
                selected_body.append(p)
                current_len += len(p) + 4

            # If room remains, add medium priority paragraphs
            if current_len < remaining_budget:
                for p in medium_priority:
                    if current_len + len(p) + 4 > remaining_budget:
                        cutoff = remaining_budget - current_len - 20
                        if cutoff > 100:
                            selected_body.append(p[:cutoff] + "... [truncated]")
                        break
                    selected_body.append(p)
                    current_len += len(p) + 4

            sections.append("\n\n".join(selected_body))

        final_context = "\n".join(sections)

        # Hard safety ceiling
        if len(final_context) > budget:
            final_context = final_context[:budget] + "\n... [Context truncated for LLM input limit]"

        return final_context

    # ------------------------------------------------------------------------
    # High-Level End-to-End Method
    # ------------------------------------------------------------------------

    async def extract(
        self,
        url: str,
        raise_on_error: bool = True,
    ) -> ExtractedWebsiteData:
        """
        Executes complete website extraction pipeline for a given URL:
        validates URL, fetches HTML asynchronously, and parses structured data.

        Args:
            url: The HTTP/HTTPS website URL to analyze.
            raise_on_error: If True, raises specific WebsiteExtractionError subclasses.
                            If False, returns partial ExtractedWebsiteData on error.

        Returns:
            ExtractedWebsiteData populated with metadata and clean text.
        """
        try:
            html, final_url, status = await self.fetch_html(url)
            return self.parse_html(
                html=html,
                url=url,
                final_url=final_url,
                status_code=status,
            )
        except Exception as exc:
            if raise_on_error:
                raise
            logger.warning(f"Website extraction encountered error for '{url}': {exc}")
            return ExtractedWebsiteData(
                url=url,
                final_url=url,
                status_code=getattr(exc, "status_code", 500),
                title="",
                meta_description="",
                main_text=f"Extraction failed: {str(exc)}",
            )


# ============================================================================
# Module-level Convenience Helper
# ============================================================================

async def extract_website(
    url: str,
    timeout: Optional[int] = None,
    allow_local: bool = False,
    raise_on_error: bool = True,
) -> ExtractedWebsiteData:
    """Convenience function to extract website data using default extractor."""
    extractor = WebsiteExtractor(timeout=timeout, allow_local=allow_local)
    return await extractor.extract(url, raise_on_error=raise_on_error)
