from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from mcp import Client, StdioServerParameters


SERVER_PACKAGE = "@brave/brave-search-mcp-server@2.1.4"
DEFAULT_OUTPUT = Path("prospects.csv")
TARGET_PROSPECTS = 20

SEARCH_QUERIES = (
    "independent restaurants Montreal Quebec",
    "Montreal fine dining restaurant menu reservations",
    "Montreal bistro restaurant menu reservations",
    "restaurant gastronomique Montréal menu réservation",
    "restaurant Vieux-Montréal menu reservations",
    "restaurant Plateau Mont-Royal Montréal menu reservations",
    "restaurant Griffintown Montréal menu reservations",
    "restaurant Mile End Montréal menu reservations",
)

# These domains can discover restaurants, but the result URL is not the restaurant's
# own site and its title usually describes an article or directory entry.
NON_PROSPECT_DOMAINS = {
    "aircanada.com",
    "bonjourquebec.com",
    "canadas100best.com",
    "cultmtl.com",
    "eater.com",
    "experienceoldmontreal.com",
    "facebook.com",
    "foursquare.com",
    "fourseasons.com",
    "gillespudlowski.com",
    "go-montreal.com",
    "instagram.com",
    "ithq.qc.ca",
    "lapresse.ca",
    "lecuisinomane.com",
    "lefooding.com",
    "milesopedia.com",
    "mtl.org",
    "mtlblog.com",
    "mtlatable.com",
    "montrealsecret.co",
    "noovomoi.ca",
    "opentable.ca",
    "opentable.com",
    "pagesjaunes.ca",
    "restaurant-montreal.com",
    "reddit.com",
    "restomontreal.ca",
    "saveur.com",
    "tastet.ca",
    "theinfatuation.com",
    "timeout.com",
    "tripadvisor.ca",
    "tripadvisor.com",
    "ubereats.com",
    "wanderlog.com",
    "wikipedia.org",
    "yonder.fr",
    "yelp.ca",
    "yelp.com",
}

GENERIC_TITLE_RE = re.compile(
    r"\b(best|top|guide|list|directory|places? to eat|restaurants|meilleurs?|meilleures?|incontournables|under-the-radar|locals love)\b",
    re.IGNORECASE,
)
RESTAURANT_CUE_RE = re.compile(
    r"\b(restaurant|bistro|brasserie|fine dining|dining room|cuisine|menu|reservation)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\b(Montr[ée]al(?:\s*,?\s*(?:Qu[ée]bec|QC))?|Qu[ée]bec)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str


@dataclass
class Prospect:
    business_name: str
    website: str
    location: str
    description: str
    source_url: str
    search_query: str
    notes: str


@dataclass
class RunStats:
    queries_executed: int = 0
    raw_results: int = 0
    failures: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover plausible Montreal restaurant prospects via Brave Search MCP."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="CSV output path (default: prospects.csv)",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=TARGET_PROSPECTS,
        help="Maximum number of unique prospects to save (default: 20)",
    )
    return parser.parse_args()


def normalized_name(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", ascii_value.lower())


def registered_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host


def is_excluded_domain(domain: str) -> bool:
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in NON_PROSPECT_DOMAINS)


def clean_business_name(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    parts = re.split(r"\s+(?:\||–|—|-)\s+", title)
    useful = [part.strip(" .") for part in parts if part.strip(" .")]
    if not useful:
        return ""

    generic_segments = {
        "accueil",
        "home",
        "homepage",
        "welcome",
        "official site",
        "official website",
        "site officiel",
    }
    candidates = [part for part in useful if part.lower() not in generic_segments]
    if not candidates:
        return ""

    def segment_score(part: str) -> tuple[int, int]:
        score = 0
        if not RESTAURANT_CUE_RE.search(part):
            score += 4
        if not LOCATION_RE.search(part):
            score += 4
        if len(part.split()) <= 4:
            score += 2
        return score, -len(part)

    return max(candidates, key=segment_score)[:160]


def extract_location(title: str, description: str) -> str:
    matches: list[str] = []
    for match in LOCATION_RE.finditer(f"{title} {description}"):
        value = match.group(0)
        if value.casefold() not in {item.casefold() for item in matches}:
            matches.append(value)
    # Prefer the most specific explicit phrase (for example, "Montréal, Québec")
    # instead of repeating a shorter mention from the snippet.
    return max(matches, key=len) if matches else ""


def candidate_from_result(result: SearchResult, query: str) -> Prospect | None:
    domain = registered_domain(result.url)
    if not domain or is_excluded_domain(domain) or "hotel" in domain:
        return None

    combined = f"{result.title} {result.description}"
    if GENERIC_TITLE_RE.search(result.title) or not RESTAURANT_CUE_RE.search(combined):
        return None

    name = clean_business_name(result.title)
    if not name or len(normalized_name(name)) < 3:
        return None

    location = extract_location(result.title, result.description)
    uncertainty = [
        "Plausible prospect from a Brave result; independence, service style, and linen use are unverified."
    ]
    if not location:
        uncertainty.append("No explicit location was extracted from the title or snippet.")
    uncertainty.append("Website appears to be a direct business result but was not independently verified.")

    return Prospect(
        business_name=name,
        website=result.url,
        location=location,
        description=result.description,
        source_url=result.url,
        search_query=query,
        notes=" ".join(uncertainty),
    )


def add_unique(prospects: list[Prospect], candidate: Prospect) -> bool:
    candidate_name = normalized_name(candidate.business_name)
    candidate_domain = registered_domain(candidate.website)
    for existing in prospects:
        if normalized_name(existing.business_name) == candidate_name:
            return False
        if candidate_domain and registered_domain(existing.website) == candidate_domain:
            return False
    prospects.append(candidate)
    return True


def json_values_from_text(text: str) -> Iterable[Any]:
    text = text.strip()
    if not text:
        return
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass

    # Current Brave tools commonly return one JSON object per MCP text block,
    # but accepting JSONL makes the client tolerant of combined text blocks.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def result_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        url = value.get("url") or value.get("link")
        title = value.get("title") or value.get("name")
        if isinstance(url, str) and isinstance(title, str):
            yield value
        for key in ("web", "results", "items", "data"):
            if key in value:
                yield from result_dicts(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from result_dicts(item)


def parse_tool_result(call_result: Any) -> list[SearchResult]:
    payloads: list[Any] = []
    structured = getattr(call_result, "structured_content", None)
    if structured is not None:
        payloads.append(structured)

    for block in getattr(call_result, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            payloads.extend(json_values_from_text(text))

    parsed: list[SearchResult] = []
    seen: set[tuple[str, str]] = set()
    for payload in payloads:
        for item in result_dicts(payload):
            title = str(item.get("title") or item.get("name") or "").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
            description = str(
                item.get("description") or item.get("snippet") or item.get("text") or ""
            ).strip()
            key = (title, url)
            if title and url and key not in seen:
                seen.add(key)
                parsed.append(SearchResult(title=title, url=url, description=description))
    return parsed


def choose_web_search_tool(tools: list[Any]) -> Any:
    candidates: list[tuple[int, Any]] = []
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        description = str(getattr(tool, "description", ""))
        schema = getattr(tool, "input_schema", {}) or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "query" not in properties:
            continue

        normalized_tool_name = name.lower().replace("-", "_")
        searchable = f"{name} {description}".lower()
        score = 0
        if "search" in normalized_tool_name:
            score += 5
        if "web" in normalized_tool_name:
            score += 20
        if all(field in searchable for field in ("title", "description", "url")):
            score += 10

        # Penalize specialist tools by their identity/schema, not their prose.
        # The general Brave web-search description legitimately mentions news,
        # images, and videos, which caused the former heuristic to reject it.
        specialist_markers = ("news", "image", "video", "local", "place", "llm", "context")
        if any(marker in normalized_tool_name for marker in specialist_markers):
            score -= 30
        if "maximum_number_of_tokens" in properties:
            score -= 20
        if score > 0:
            candidates.append((score, tool))

    if not candidates:
        exposed = ", ".join(str(getattr(tool, "name", "<unnamed>")) for tool in tools)
        raise RuntimeError(f"No compatible web search tool found. Exposed tools: {exposed}")
    return max(candidates, key=lambda item: item[0])[1]


def search_arguments(tool: Any, query: str) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", {}) or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    arguments: dict[str, Any] = {"query": query}

    preferred = {
        "country": "CA",
        "search_lang": "en",
        "ui_lang": "en-CA",
        "count": 20,
        "safesearch": "moderate",
    }
    for name, value in preferred.items():
        if name not in properties:
            continue
        allowed = properties[name].get("enum") if isinstance(properties[name], dict) else None
        if allowed is None or value in allowed:
            arguments[name] = value
    return arguments


def error_text(call_result: Any) -> str:
    messages = []
    for block in getattr(call_result, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            messages.append(text)
    return " ".join(messages) or "Unknown MCP tool error"


async def call_with_retry(client: Client, tool: Any, query: str, attempts: int = 2) -> Any:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = await client.call_tool(tool.name, search_arguments(tool, query))
            if not getattr(result, "is_error", False):
                return result
            last_error = error_text(result)
        except Exception as exc:  # connection/protocol failures are reported in the summary
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            await asyncio.sleep(1)
    raise RuntimeError(last_error)


def write_csv(path: Path, prospects: list[Prospect]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(Prospect.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(prospect) for prospect in prospects)


def print_summary(stats: RunStats, prospects: list[Prospect], elapsed: float) -> None:
    print("\nRun summary")
    print(f"- Total search queries executed: {stats.queries_executed}")
    print(f"- Total raw search results: {stats.raw_results}")
    print(f"- Unique plausible prospects: {len(prospects)}")
    print(f"- With identifiable official-looking websites: {sum(bool(p.website) for p in prospects)}")
    print(f"- With location information: {sum(bool(p.location) for p in prospects)}")
    print(f"- Total execution time: {elapsed:.2f} seconds")
    if stats.failures:
        print("- MCP/API failures:")
        for failure in stats.failures:
            print(f"  - {failure}")
    else:
        print("- MCP/API failures: none")


async def discover(output: Path, target: int) -> int:
    started = time.monotonic()
    stats = RunStats()
    prospects: list[Prospect] = []

    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        print(
            "Error: BRAVE_API_KEY is not set. See README.md for setup instructions.",
            file=sys.stderr,
        )
        return 2
    if shutil.which("npx") is None:
        print("Error: npx was not found. Install Node.js 22 or newer.", file=sys.stderr)
        return 2

    server = StdioServerParameters(
        command="npx",
        args=["-y", SERVER_PACKAGE, "--transport", "stdio"],
        env={"BRAVE_API_KEY": api_key},
    )

    try:
        async with Client(server, mode="legacy") as client:
            listed = await client.list_tools()
            print("Discovered MCP tools: " + ", ".join(tool.name for tool in listed.tools))
            tool = choose_web_search_tool(listed.tools)
            print(f"Selected MCP search tool: {tool.name}")

            for query in SEARCH_QUERIES:
                if len(prospects) >= target:
                    break
                stats.queries_executed += 1
                try:
                    call_result = await call_with_retry(client, tool, query)
                    raw_results = parse_tool_result(call_result)
                    stats.raw_results += len(raw_results)
                    if not raw_results:
                        stats.failures.append(
                            f"{query!r}: {tool.name} returned no parseable web results"
                        )
                        continue
                    for raw_result in raw_results:
                        candidate = candidate_from_result(raw_result, query)
                        if candidate is not None:
                            add_unique(prospects, candidate)
                            if len(prospects) >= target:
                                break
                except Exception as exc:
                    stats.failures.append(f"{query!r}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        stats.failures.append(f"MCP connection: {type(exc).__name__}: {exc}")

    prospects = prospects[:target]
    elapsed = time.monotonic() - started
    if prospects:
        write_csv(output, prospects)
        print(f"Saved CSV: {output.resolve()}")
    else:
        print("No CSV written because no real prospects were returned.", file=sys.stderr)
    print_summary(stats, prospects, elapsed)
    return 0 if prospects and not stats.failures else 1


def main() -> int:
    args = parse_args()
    if args.target < 1:
        print("Error: --target must be at least 1.", file=sys.stderr)
        return 2
    return asyncio.run(discover(args.output, args.target))


if __name__ == "__main__":
    raise SystemExit(main())
