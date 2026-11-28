"""Pluggable LLM and article extraction helpers for automatic reading ingestion.

Supports Google Gemini (`GEMINI_API_KEY`) and Anthropic (`ANTHROPIC_API_KEY`) over `httpx`
with zero extra third-party SDK dependencies, plus stdlib `html.parser` URL extraction.
"""

from __future__ import annotations

import base64
import json
import re
from html.parser import HTMLParser
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError

from tempus.cards.functions import CATALOG, signature
from tempus.cards.panels import PANELS, accepts_text
from tempus.config import Settings

USER_AGENT = "tempus-research-reader/1.0 (+https://github.com/GTLangseth/tempus)"
MAX_HTML_CHARS = 120_000
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """Raised when LLM credentials are missing, unreachable, or return invalid output."""


class EnrichmentQuery(BaseModel):
    topic: str = Field(min_length=1)
    claim_quote: str = Field(min_length=1)
    search_query: str = Field(min_length=1)
    anchor_heading: str | None = None


class ExtractedArticle(BaseModel):
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=lambda: ["Unknown Author"])
    source: str | None = None
    published: str = Field(min_length=4)
    summary: str = Field(min_length=1)
    concepts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    body_markdown: str = Field(min_length=1)
    footnotes: list[dict[str, str]] = Field(default_factory=list)
    queries: list[EnrichmentQuery] = Field(default_factory=list)


class ProposedClaim(BaseModel):
    quote: str = Field(min_length=1)
    status: Literal["confirmed", "extended", "nuanced", "unverified"] = "confirmed"
    verdict: str = Field(min_length=1)
    series: list[str] = Field(default_factory=list)


class ProposedCard(BaseModel):
    id: str = Field(min_length=1)
    anchor_heading: str | None = None
    caption: str | None = None
    spec: dict
    note_title: str | None = None
    note_body: str | None = None
    claim: ProposedClaim | None = None


class ComposedEnrichment(BaseModel):
    cards: list[ProposedCard] = Field(default_factory=list)
    extra_footnotes: list[dict[str, str]] = Field(default_factory=list)


class IngestLLM(Protocol):
    provider_name: str

    def extract_article(
        self,
        *,
        pdf_bytes: bytes | None = None,
        text: str | None = None,
        source_url: str | None = None,
        title_hint: str | None = None,
        known_concepts: list[str] | None = None,
    ) -> ExtractedArticle: ...

    def compose_enrichment(
        self,
        *,
        article: ExtractedArticle,
        series_context: list[dict],
        known_concepts: list[str],
    ) -> ComposedEnrichment: ...


class _ArticleHTMLParser(HTMLParser):
    """Extract headings, paragraphs, blockquotes, list items, and basic page metadata."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "aside", "form"})
    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
    _BLOCK_TAGS = frozenset({"p", "li", "blockquote", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._capture_tag: str | None = None
        self._buf: list[str] = []
        self.page_title: str = ""
        self.meta: dict[str, str] = {}
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag == "meta":
            key = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "")
            if key and content and key not in self.meta:
                self.meta[key] = content
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in self._HEADING_TAGS or tag in self._BLOCK_TAGS:
            self._flush()
            self._capture_tag = tag
        elif tag == "br" and self._capture_tag is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == self._capture_tag:
            self._flush()
            self._capture_tag = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.page_title += data
            return
        if self._capture_tag is not None:
            self._buf.append(data)

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf.clear()
        if not text:
            return
        tag = self._capture_tag
        if tag in ("h1", "h2"):
            self.blocks.append(f"## {text}")
        elif tag in ("h3", "h4"):
            self.blocks.append(f"### {text}")
        elif tag == "blockquote":
            self.blocks.append(f"> {text}")
        elif tag == "li":
            self.blocks.append(f"- {text}")
        else:
            self.blocks.append(text)


def fetch_url_article(url: str, *, client: httpx.Client | None = None) -> dict:
    """Fetch an HTML/text article URL and extract clean readable text and metadata."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMError(f"Unsupported article URL: {url!r} (expected http:// or https://)")

    owns_client = client is None
    http = client or httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        resp = http.get(url.strip())
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not fetch URL {url}: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    ctype = resp.headers.get("content-type", "").lower()
    raw = resp.text[:MAX_HTML_CHARS]
    if "html" not in ctype and "<html" not in raw[:500].lower():
        clean = raw.strip()
        if len(clean) < 40:
            raise LLMError(f"URL returned too little text ({len(clean)} chars)")
        return {
            "url": str(resp.url),
            "title": parsed.path.rsplit("/", 1)[-1] or parsed.netloc,
            "author": None,
            "source": parsed.netloc.removeprefix("www."),
            "published": None,
            "text": clean,
        }

    parser = _ArticleHTMLParser()
    parser.feed(raw)
    parser.close()
    text = "\n\n".join(parser.blocks).strip()
    if len(text) < 80:
        raise LLMError(
            f"Could not extract enough article prose from {url} — try Paste Text or Upload PDF."
        )
    meta = parser.meta
    title = (meta.get("og:title") or parser.page_title or parsed.netloc).strip()
    source = (meta.get("og:site_name") or parsed.netloc.removeprefix("www.")).strip()
    author = meta.get("author") or meta.get("article:author")
    published = meta.get("article:published_time") or meta.get("pubdate") or meta.get("date")
    if published:
        published = published[:10]
    return {
        "url": str(resp.url),
        "title": title,
        "author": author,
        "source": source,
        "published": published,
        "text": text,
    }


def _dsl_reference() -> str:
    funcs = "\n".join(f"- {signature(fn)}: {fn.doc}" for fn in CATALOG.values())
    panels = "\n".join(
        f"- {p.name} ({p.field}: {accepts_text(p)}): {p.doc}" for p in PANELS.values()
    )
    return (
        "Tempus Card Spec Rules:\n"
        "- Required top-level keys in `spec`: version (must be integer 1), title, question, "
        "concepts (list of known concept ids), tags (list of short lowercase strings), "
        "inputs (dict mapping variable name -> 'fred:SERIES_ID' from the provided series list), "
        "formulas (optional dict mapping derived var -> expression), panels (non-empty list).\n"
        "- Valid formula functions:\n"
        f"{funcs}\n"
        "- Valid panel types:\n"
        f"{panels}\n"
        "- Do NOT invent series IDs outside SERIES_CONTEXT. "
        "Do NOT invent unknown function names or panel types."
    )


def _extract_prompt(
    *,
    source_url: str | None,
    title_hint: str | None,
    known_concepts: list[str],
    article_text: str | None,
) -> str:
    concepts_str = ", ".join(known_concepts[:80]) if known_concepts else "(none)"
    hints = []
    if title_hint:
        hints.append(f"Title hint: {title_hint}")
    if source_url:
        hints.append(f"Source URL: {source_url}")
    hint_block = "\n".join(hints)
    schema_hint = """{
  "slug": "kebab-case-short-id",
  "title": "Article Title",
  "authors": ["Author Name"],
  "source": "Publication or Institution",
  "published": "YYYY-MM or YYYY",
  "summary": "2-3 sentence synthesis of the essay and what empirical data illuminates.",
  "concepts": ["macro.inflation"],
  "tags": ["macro", "rates"],
  "body_markdown": "Clean essay text with ## headings and lists; no code fences or page noise.",
  "footnotes": [{"id": "1", "text": "Source citation or clarification."}],
  "queries": [
    {
      "topic": "Short economic topic",
      "claim_quote": "Direct or close empirical statement from the essay.",
      "search_query": "2-6 keyword FRED macro series search phrase",
      "anchor_heading": "Exact ## heading text from body_markdown where a chart fits best"
    }
  ]
}"""
    parts = [
        "You are digesting a macro/economic research article for Tempus "
        "(an empirical Neon Notebook reader).",
        "Task: Preserve complete readable essay prose cleanly as Markdown WITHOUT OCR or "
        "PDF page-break artifacts, extract metadata, and identify 3-5 empirical US or "
        "macroeconomic claims suitable for live FRED time-series charts.",
        f"Known ontology concepts (use ONLY ids from this list): {concepts_str}",
    ]
    if hint_block:
        parts.append(hint_block)
    parts.append(f"Return ONLY valid JSON matching this schema:\n{schema_hint}")
    if article_text is not None:
        parts.append(f"ARTICLE CONTENT:\n{article_text[:MAX_HTML_CHARS]}")
    return "\n\n".join(parts)


def _compose_prompt(
    *,
    article: ExtractedArticle,
    series_context: list[dict],
    known_concepts: list[str],
) -> str:
    headings = [
        line[3:].strip() for line in article.body_markdown.splitlines() if line.startswith("## ")
    ]
    schema_hint = """{
  "cards": [
    {
      "id": "kebab-case-card-id",
      "anchor_heading": "Matching ## heading from ARTICLE HEADINGS",
      "caption": "1-2 sentence caption grounding the reader.",
      "note_title": "Optional Tempus Data Context heading",
      "note_body": "Brief note comparing recent stored series values with the essay.",
      "claim": {
        "quote": "Empirical claim from the essay.",
        "status": "confirmed | extended | nuanced | unverified",
        "verdict": "Concrete verdict using ONLY observed numbers/dates from SERIES_CONTEXT.",
        "series": ["fred:SERIES_ID"]
      },
      "spec": {
        "version": 1,
        "title": "Chart card title",
        "question": "Empirical question tested",
        "concepts": ["macro.rates"],
        "tags": ["rates"],
        "inputs": {"x": "fred:SERIES_ID"},
        "formulas": {"x_yoy": "yoy(x)"},
        "panels": [{"type": "timeseries", "title": "...", "y": ["x_yoy"]}]
      }
    }
  ],
  "extra_footnotes": [{"id": "n1", "text": "Brief methodological or series definition note."}]
}"""
    payload = {
        "article_title": article.title,
        "article_summary": article.summary,
        "headings": headings,
        "queries": [q.model_dump() for q in article.queries],
        "known_concepts": known_concepts[:80],
        "series_context": series_context,
    }
    return "\n\n".join(
        [
            "Compose 2-4 verified Tempus chart cards, companion data notes, "
            "and empirical claim checks for this reading.",
            _dsl_reference(),
            "Every numeric claim in `verdict` and `note_body` MUST be grounded in "
            "`series_context` (never fabricate numbers).",
            f"Return ONLY valid JSON matching this schema:\n{schema_hint}",
            f"INPUT CONTEXT:\n{json.dumps(payload, default=str, indent=2)}",
        ]
    )


def _parse_json_response[T: BaseModel](raw_text: str, model_cls: type[T]) -> T:
    cleaned = raw_text.strip()
    if match := _JSON_FENCE.search(cleaned):
        cleaned = match.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        return model_cls.model_validate_json(cleaned)
    except (ValidationError, ValueError) as exc:
        raise LLMError(f"LLM returned invalid {model_cls.__name__} JSON: {exc}") from exc


class GeminiIngestLLM:
    """Ingest LLM backed by the Gemini `generateContent` REST API via `httpx`."""

    provider_name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self._client = client

    def _call(self, parts: list[dict]) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        )
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        owns = self._client is None
        http = self._client or httpx.Client(timeout=120.0)
        try:
            resp = http.post(
                url,
                headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"Gemini API request failed: {exc}") from exc
        finally:
            if owns:
                http.close()

        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini API returned no candidates")
        text_parts = [
            p["text"]
            for p in candidates[0].get("content", {}).get("parts", [])
            if isinstance(p, dict) and p.get("text")
        ]
        if not text_parts:
            raise LLMError("Gemini API returned empty text response")
        return "\n".join(text_parts)

    def extract_article(
        self,
        *,
        pdf_bytes: bytes | None = None,
        text: str | None = None,
        source_url: str | None = None,
        title_hint: str | None = None,
        known_concepts: list[str] | None = None,
    ) -> ExtractedArticle:
        prompt = _extract_prompt(
            source_url=source_url,
            title_hint=title_hint,
            known_concepts=known_concepts or [],
            article_text=None if pdf_bytes is not None else text,
        )
        parts: list[dict] = [{"text": prompt}]
        if pdf_bytes is not None:
            parts.insert(
                0,
                {
                    "inline_data": {
                        "mime_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                },
            )
        return _parse_json_response(self._call(parts), ExtractedArticle)

    def compose_enrichment(
        self,
        *,
        article: ExtractedArticle,
        series_context: list[dict],
        known_concepts: list[str],
    ) -> ComposedEnrichment:
        prompt = _compose_prompt(
            article=article,
            series_context=series_context,
            known_concepts=known_concepts,
        )
        return _parse_json_response(self._call([{"text": prompt}]), ComposedEnrichment)


class AnthropicIngestLLM:
    """Ingest LLM backed by the Anthropic Messages REST API via `httpx`."""

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-5-20250929",
        *,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self._client = client

    def _call(self, content: list[dict]) -> str:
        owns = self._client is None
        http = self._client or httpx.Client(timeout=120.0)
        try:
            resp = http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 8192,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": content}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"Anthropic API request failed: {exc}") from exc
        finally:
            if owns:
                http.close()

        texts = [
            b["text"]
            for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if not texts:
            raise LLMError("Anthropic API returned empty response content")
        return "\n".join(texts)

    def extract_article(
        self,
        *,
        pdf_bytes: bytes | None = None,
        text: str | None = None,
        source_url: str | None = None,
        title_hint: str | None = None,
        known_concepts: list[str] | None = None,
    ) -> ExtractedArticle:
        prompt = _extract_prompt(
            source_url=source_url,
            title_hint=title_hint,
            known_concepts=known_concepts or [],
            article_text=None if pdf_bytes is not None else text,
        )
        blocks: list[dict] = []
        if pdf_bytes is not None:
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    },
                }
            )
        blocks.append({"type": "text", "text": prompt})
        return _parse_json_response(self._call(blocks), ExtractedArticle)

    def compose_enrichment(
        self,
        *,
        article: ExtractedArticle,
        series_context: list[dict],
        known_concepts: list[str],
    ) -> ComposedEnrichment:
        prompt = _compose_prompt(
            article=article,
            series_context=series_context,
            known_concepts=known_concepts,
        )
        raw = self._call([{"type": "text", "text": prompt}])
        return _parse_json_response(raw, ComposedEnrichment)


def configured_provider(settings: Settings) -> str | None:
    """Return which LLM provider has credentials configured, respecting TEMPUS_INGEST_PROVIDER."""
    if settings.ingest_provider == "gemini":
        return "gemini" if settings.gemini_api_key else None
    if settings.ingest_provider == "anthropic":
        return "anthropic" if settings.anthropic_api_key else None
    if settings.gemini_api_key:
        return "gemini"
    if settings.anthropic_api_key:
        return "anthropic"
    return None


def select_ingest_llm(settings: Settings) -> IngestLLM:
    """Instantiate the configured LLM backend or raise a clear configuration error."""
    if settings.ingest_provider == "gemini":
        if not settings.gemini_api_key:
            raise LLMError("Set GEMINI_API_KEY in .env (TEMPUS_INGEST_PROVIDER=gemini).")
        return GeminiIngestLLM(settings.gemini_api_key, settings.gemini_model)
    if settings.ingest_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMError("Set ANTHROPIC_API_KEY in .env (TEMPUS_INGEST_PROVIDER=anthropic).")
        return AnthropicIngestLLM(settings.anthropic_api_key, settings.anthropic_model)
    if settings.gemini_api_key:
        return GeminiIngestLLM(settings.gemini_api_key, settings.gemini_model)
    if settings.anthropic_api_key:
        return AnthropicIngestLLM(settings.anthropic_api_key, settings.anthropic_model)
    raise LLMError(
        "Set GEMINI_API_KEY or ANTHROPIC_API_KEY in .env to enable automatic PDF/URL digestion."
    )
