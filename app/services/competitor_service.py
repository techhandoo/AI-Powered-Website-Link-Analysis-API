"""
Competitor Analysis and Domain Intelligence Service.

Provides:
- Domain and brand name extraction from URLs, metadata, and HTML.
- Competitor discovery, categorization, and enrichment.
- Fallback intelligence logic guaranteeing Section 1 requirements
  (minimum 3 competitors, strengths, weaknesses, competitive advantages, opportunities).
"""

import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

from app.schemas import CompetitorAnalysis, CompetitorItem


class CompetitorService:
    """
    Service responsible for domain extraction, brand identification,
    and competitor intelligence enrichment.
    """

    # Industry Category mapping heuristics
    CATEGORY_KEYWORDS: Dict[str, List[str]] = {
        "Technology & Software (SaaS)": [
            "software", "saas", "platform", "cloud", "api", "ai", "machine learning",
            "developer", "automation", "database", "analytics", "app", "tech"
        ],
        "Digital Marketing & SEO": [
            "seo", "marketing", "digital marketing", "advertising", "content marketing",
            "ppc", "social media", "branding", "lead generation", "inbound"
        ],
        "E-Commerce & Retail": [
            "shop", "store", "ecommerce", "e-commerce", "cart", "buy", "checkout",
            "products", "retail", "apparel", "goods"
        ],
        "Financial Services & FinTech": [
            "finance", "banking", "payment", "fintech", "investment", "wealth",
            "insurance", "credit", "loan", "crypto", "trading"
        ],
        "Healthcare & HealthTech": [
            "health", "medical", "clinic", "doctor", "telehealth", "patient",
            "wellness", "care", "hospital", "pharma"
        ],
        "Education & EdTech": [
            "education", "course", "learning", "school", "university", "academy",
            "training", "tutor", "students", "edtech"
        ],
        "Professional Services & Consulting": [
            "consulting", "advisory", "legal", "accounting", "agency", "management",
            "strategy", "recruiting", "human resources", "staffing"
        ],
        "Cybersecurity & IT Infrastructure": [
            "security", "cybersecurity", "firewall", "threat", "soc", "compliance",
            "identity", "network", "devops", "hosting"
        ],
    }

    # Reference competitors by category for enrichment & fallback
    CATEGORY_BENCHMARKS: Dict[str, List[Dict[str, any]]] = {
        "Technology & Software (SaaS)": [
            {
                "name": "Salesforce",
                "strengths": ["Enterprise market dominance", "Extensive ecosystem and app exchange", "Deep CRM capabilities"],
                "weaknesses": ["High implementation complexity", "Steep pricing for small businesses", "Legacy UI overhead"],
                "market": "Enterprise / Mid-Market",
                "main_service": "Enterprise Cloud CRM & Workflow Solutions"
            },
            {
                "name": "HubSpot",
                "strengths": ["Intuitive user interface", "Strong inbound marketing ecosystem", "Accessible free/starter tiers"],
                "weaknesses": ["Rapid cost escalation at scale", "Limited customization for complex enterprise workflows", "Rigid reporting"],
                "market": "SMB / Mid-Market",
                "main_service": "Inbound Marketing & CRM Platform"
            },
            {
                "name": "Atlassian",
                "strengths": ["Deep developer & engineering mindshare", "Comprehensive collaboration suite", "Widespread adoption"],
                "weaknesses": ["Performance lag on massive repositories", "Fragmented product configuration", "Complex permission management"],
                "market": "Enterprise / Developer Teams",
                "main_service": "Project Tracking & Agile Collaboration"
            },
            {
                "name": "Zoho One",
                "strengths": ["All-in-one suite cost efficiency", "Broad application breadth", "Rapid setup for small businesses"],
                "weaknesses": ["Inconsistent UI across tools", "Less specialized deep feature sets", "Slower customer support"],
                "market": "Small & Medium Business",
                "main_service": "Unified Business Operations Suite"
            }
        ],
        "Digital Marketing & SEO": [
            {
                "name": "Semrush",
                "strengths": ["Massive global keyword database", "Comprehensive SEO audit tools", "Strong competitive research"],
                "weaknesses": ["Steep pricing plans", "Overwhelming interface for beginners", "Single-user plan limits"],
                "market": "Marketing Agencies / Mid-Market",
                "main_service": "SEO & Competitive Intelligence Suite"
            },
            {
                "name": "Ahrefs",
                "strengths": ["Industry-leading backlink crawler", "High-accuracy organic search metrics", "Clear content explorer"],
                "weaknesses": ["Credit-based usage restrictions", "High enterprise tier pricing", "Limited social media metrics"],
                "market": "SEO Specialists / Agencies",
                "main_service": "Backlink & Search Traffic Analysis"
            },
            {
                "name": "Moz Pro",
                "strengths": ["Recognized Domain Authority (DA) metric", "Beginner-friendly educational resources", "Strong local SEO toolset"],
                "weaknesses": ["Smaller backlink index than competitors", "Slower feature development", "Basic technical audit depth"],
                "market": "SMB / In-house Marketers",
                "main_service": "SEO Auditing & Rank Tracking"
            }
        ],
        "E-Commerce & Retail": [
            {
                "name": "Shopify",
                "strengths": ["Turnkey storefront deployment", "Massive app marketplace", "Reliable hosted checkout infrastructure"],
                "weaknesses": ["Transaction fees unless using Shopify Payments", "Custom logic requires specialized Liquid theme development", "Recurring app subscription costs"],
                "market": "Retailers of all sizes",
                "main_service": "Hosted E-Commerce Platform"
            },
            {
                "name": "WooCommerce",
                "strengths": ["Open-source flexibility", "Zero core platform licensing cost", "Total hosting and data ownership"],
                "weaknesses": ["Self-managed security and maintenance", "Performance scaling challenges under high load", "Plugin conflicts"],
                "market": "SMB / WordPress Users",
                "main_service": "Open-Source E-Commerce Framework"
            },
            {
                "name": "BigCommerce",
                "strengths": ["Robust native B2B functionality", "Zero added transaction fees", "Multi-storefront capabilities"],
                "weaknesses": ["Revenue-based mandatory plan upgrades", "Smaller theme and app ecosystem than Shopify", "Complex product option rules"],
                "market": "Mid-Market / B2B E-Commerce",
                "main_service": "Enterprise E-Commerce Solutions"
            }
        ],
        "General / Professional Services": [
            {
                "name": "Accenture",
                "strengths": ["Global delivery capacity", "Deep multi-industry expertise", "End-to-end transformation capability"],
                "weaknesses": ["High billable rates", "Long ramp-up periods", "Less agile for fast-paced SMB needs"],
                "market": "Global Enterprise / Fortune 500",
                "main_service": "Digital Transformation & Management Consulting"
            },
            {
                "name": "Deloitte Digital",
                "strengths": ["Strong brand trust", "Integrated creative and technical offerings", "Regulatory and compliance mastery"],
                "weaknesses": ["Enterprise-focused cost structure", "Bureaucratic engagement cycles", "Slow bespoke delivery"],
                "market": "Enterprise / Public Sector",
                "main_service": "Enterprise Strategy & Digital Experience"
            },
            {
                "name": "Wipro Technologies",
                "strengths": ["Cost-effective engineering scale", "Global offshore support", "Extensive IT infrastructure capabilities"],
                "weaknesses": ["Perceived variable quality on innovation", "Communication overhead across tiers", "Slower strategic agility"],
                "market": "Large Enterprise / Global 2000",
                "main_service": "IT Consulting & Managed Engineering"
            }
        ]
    }

    @staticmethod
    def extract_domain(url: str) -> str:
        """
        Extracts clean registered domain or host from URL.
        e.g., 'https://www.dxoratech.com/about' -> 'dxoratech.com'
        """
        if not url:
            return ""
        if "://" not in url:
            url = f"https://{url}"
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.split(":")[0].lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return url.strip()

    @classmethod
    def extract_brand_name(
        cls,
        url: str,
        title: Optional[str] = None,
        og_site_name: Optional[str] = None,
    ) -> str:
        """
        Deduces the most accurate brand name using OG site name, title separators, or domain.
        """
        # 1. Use og:site_name if clean and concise
        if og_site_name and len(og_site_name.strip()) <= 40:
            return og_site_name.strip()

        # 2. Extract from page title using common brand separators
        if title:
            # Common patterns: "Page Title | Brand Name", "Brand Name - Tagline", "Brand Name :: Welcome"
            for sep in [" | ", " - ", " — ", " :: ", " • ", " » ", ": "]:
                if sep in title:
                    parts = title.split(sep)
                    # Often the brand is either the first or last token
                    first = parts[0].strip()
                    last = parts[-1].strip()
                    if 2 <= len(first) <= 30 and not any(w in first.lower() for w in ["home", "welcome", "about", "index"]):
                        return first
                    if 2 <= len(last) <= 30 and not any(w in last.lower() for w in ["home", "welcome", "about", "index"]):
                        return last

        # 3. Fallback to domain name root
        domain = cls.extract_domain(url)
        if domain:
            # e.g., 'dxoratech.com' -> 'dxoratech' -> 'Dxoratech'
            parts = domain.split(".")
            root = parts[0] if len(parts) > 1 else domain
            # Capitalize root nicely (handling hyphens)
            brand_words = [w.capitalize() for w in root.replace("-", " ").replace("_", " ").split()]
            return " ".join(brand_words) if brand_words else root.capitalize()

        return "Unknown Brand"

    @classmethod
    def detect_category(
        cls,
        title: str = "",
        meta_description: str = "",
        main_text: str = "",
        meta_keywords: Optional[List[str]] = None,
    ) -> str:
        """
        Infers the primary business category from extracted website content.
        """
        combined = f"{title} {meta_description} {' '.join(meta_keywords or [])} {main_text[:2000]}".lower()

        scores: Dict[str, int] = {}
        for category, keywords in cls.CATEGORY_KEYWORDS.items():
            score = 0
            for kw in keywords:
                # Count keyword occurrences
                matches = len(re.findall(r"\b" + re.escape(kw) + r"\b", combined))
                score += matches
            scores[category] = score

        best_category = max(scores.items(), key=lambda x: x[1])
        if best_category[1] > 0:
            return best_category[0]

        return "Technology & Software (SaaS)"

    @classmethod
    def get_benchmarks_for_category(cls, category: str) -> List[Dict[str, any]]:
        """
        Retrieves reference competitor items for the given category.
        """
        for cat_key, benchmarks in cls.CATEGORY_BENCHMARKS.items():
            if cat_key.lower() in category.lower() or category.lower() in cat_key.lower():
                return benchmarks
        return cls.CATEGORY_BENCHMARKS["General / Professional Services"]

    @classmethod
    def enrich_competitor_analysis(
        cls,
        analysis: CompetitorAnalysis,
        brand_name: str,
        category: str,
        brand_summary: Optional[str] = None,
    ) -> CompetitorAnalysis:
        """
        Guarantees that CompetitorAnalysis complies strictly with Section 1:
        - Minimum 3 competitors
        - Populated strengths, weaknesses, opportunities, competitive advantages
        """
        # Ensure brand overview is non-empty
        if not analysis.brand_overview or len(analysis.brand_overview.strip()) < 10:
            summary_text = brand_summary or f"{brand_name} is a leading provider in the {category} sector."
            analysis.brand_overview = f"{brand_name} delivers comprehensive offerings in {category}. {summary_text}"

        existing_competitors = list(analysis.competitors or [])
        existing_names = {c.name.lower() for c in existing_competitors}

        # Check if we have at least 3 competitors
        if len(existing_competitors) < 3:
            benchmarks = cls.get_benchmarks_for_category(category)
            for bm in benchmarks:
                if bm["name"].lower() not in existing_names and bm["name"].lower() != brand_name.lower():
                    existing_competitors.append(CompetitorItem(
                        name=bm["name"],
                        strengths=bm.get("strengths", ["Established brand presence"]),
                        weaknesses=bm.get("weaknesses", ["Higher pricing for smaller organizations"]),
                        market=bm.get("market", "Global"),
                        main_service=bm.get("main_service", category),
                    ))
                    existing_names.add(bm["name"].lower())
                if len(existing_competitors) >= 3:
                    break

        # Validate that each competitor has strengths and weaknesses
        for comp in existing_competitors:
            if not comp.strengths:
                comp.strengths = [f"Strong brand recognition in {category}", "Established customer base"]
            if not comp.weaknesses:
                comp.weaknesses = ["Higher overhead costs", "Less flexible bespoke service options"]

        analysis.competitors = existing_competitors

        # Ensure strengths are populated
        if not analysis.strengths:
            analysis.strengths = [
                f"Specialized domain focus in {category}",
                "Agile implementation and responsive customer service",
                "Cost-effective solution compared to legacy market alternatives"
            ]

        # Ensure opportunities are populated
        if not analysis.opportunities:
            analysis.opportunities = [
                "Expand targeted search visibility and digital content strategy",
                "Develop industry-specific vertical solutions to win niche market share",
                "Leverage modern tech agility to outpace slower legacy competitors"
            ]

        # Ensure competitive advantages are populated
        if not analysis.competitive_advantages:
            analysis.competitive_advantages = [
                "Tailored customer onboarding and personalized support",
                "Modern, streamlined architecture and transparent pricing",
                "Faster delivery and adaptation to evolving market needs"
            ]

        # Ensure weaknesses/gaps are populated
        if not analysis.weaknesses:
            analysis.weaknesses = [
                "Brand recognition still developing compared to legacy incumbents",
                "Need for expanded content library and case study documentation"
            ]

        return analysis

    @classmethod
    def build_fallback_competitor_analysis(
        cls,
        brand_name: str,
        category: str,
        summary: Optional[str] = None,
    ) -> CompetitorAnalysis:
        """
        Creates a complete, realistic Section 1 CompetitorAnalysis payload
        without requiring external LLM API calls.
        """
        benchmarks = cls.get_benchmarks_for_category(category)
        competitors = [
            CompetitorItem(
                name=bm["name"],
                strengths=bm.get("strengths", []),
                weaknesses=bm.get("weaknesses", []),
                market=bm.get("market"),
                main_service=bm.get("main_service")
            )
            for bm in benchmarks[:3]
        ]

        overview = summary or f"{brand_name} is a dedicated provider operating in the {category} industry."

        return CompetitorAnalysis(
            brand_overview=f"{overview} Competing within the {category} landscape, {brand_name} focuses on delivering high-impact value, efficiency, and customer satisfaction.",
            competitors=competitors,
            strengths=[
                f"Specialized expertise and agility in {category}",
                "Direct customer relationships and adaptive solution delivery",
                "High value-to-cost ratio compared to incumbent players"
            ],
            weaknesses=[
                "Developing brand footprint compared to multinational incumbents",
                "Organic search visibility currently scaling"
            ],
            competitive_advantages=[
                "Flexible client engagement models without cumbersome enterprise lock-in",
                "Rapid feature turnaround and personalized technical support",
                "Modern digital infrastructure optimized for current client requirements"
            ],
            opportunities=[
                "Capture market share in underserved SMB and mid-market tiers",
                "Strengthen search engine dominance across commercial intent queries",
                "Build thought leadership content to accelerate organic lead acquisition"
            ]
        )
