# Montréal Restaurant Prospect Discovery

A minimal feasibility prototype that uses the official Python MCP SDK and
Brave's maintained Search MCP server to discover plausible B2B restaurant
prospects. It does not qualify prospects, infer linen usage, or verify that a
restaurant wants a new supplier.

For the architecture, function-by-function behavior, debugging history, and a
record of every material logical change, see
[Implementation and Logical Change Record](docs/IMPLEMENTATION_CHANGES.md).

## Integration verified

- **Server:** [`@brave/brave-search-mcp-server`](https://github.com/brave/brave-search-mcp-server), pinned to `2.1.4`
- **Transport:** stdio (the server's default); the Python process launches the server with `npx`
- **Authentication:** `BRAVE_API_KEY`, passed only to the MCP server subprocess
- **Client:** official [`mcp`](https://github.com/modelcontextprotocol/python-sdk) Python package
- **Tools actually exposed in the local protocol smoke test:** `brave_web_search`, `brave_local_search`, `brave_video_search`, `brave_image_search`, `brave_news_search`, `brave_summarizer`, `brave_llm_context`, and `brave_place_search`. The negotiated MCP protocol version was `2025-11-25`. The script calls `tools/list` and selects a compatible general web-search tool from its name, description, and input schema; it does not assume a fixed tool name.

The implementation uses the MCP protocol end to end. It does not call the
Brave REST API directly.

## Prerequisites

- Python 3.12 or newer
- Node.js 22 or newer, including `npx`
- Internet access (for the MCP server package and Brave Search API)
- A Brave Search API account, activated Search plan, and API key

Brave currently prices Search at **US$5 per 1,000 requests**, with **US$5 in
monthly free credits** (equivalent to 1,000 Search requests). It does not offer
a standalone free plan. Account activation requires a payment card even when
the prepaid amount is set to $0. Check the live
[pricing page](https://brave.com/search/api/) and
[API help](https://api-dashboard.search.brave.com/documentation/resources/help-feedback)
before relying on these limits; they can change.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export BRAVE_API_KEY="your-key-here"
```

Do not commit a real API key. `.env.example` is a reference only; the script
reads the key from the process environment and never prints it.

## Run

```bash
python main.py
```

Optional arguments:

```bash
python main.py --target 20 --output prospects.csv
```

The program:

1. launches Brave Search MCP over stdio;
2. initializes an MCP session;
3. discovers the server's tools with `tools/list`;
4. selects and calls the general web-search tool;
5. applies deterministic candidate filtering and deduplication by normalized
   business name and website hostname;
6. writes `prospects.csv` only when Brave returned real candidates; and
7. prints query, raw-result, completeness, timing, and failure totals.

## Output and interpretation

The CSV contains:

`business_name, website, location, description, source_url, search_query, notes`

Names, descriptions, and URLs come from search results. A location is included
only when Montréal or Québec appears explicitly in the result title/snippet.
The direct result URL is retained as an official-looking website but is marked
as unverified in `notes`. Search snippets are discovery evidence, not verified
facts. Independence, full-service status, linen usage, and qualification are
never inferred.

The parser accepts both structured MCP output and the JSON-per-text-block shape
used by the current Brave server. Results from directories, review platforms,
social networks, and editorial list pages are excluded by deterministic rules.
That keeps the prototype readable but can create false positives and false
negatives. Manual review remains required.

## Current execution status

Live discovery was completed on October 29, 2026. The client negotiated the
stdio MCP connection, dynamically selected `brave_web_search`, and executed
eight queries without MCP/API failures. From 153 raw results, deterministic
filtering and deduplication produced 19 plausible direct-domain prospects; all
19 have website values and 14 have explicit location text. The generated
`prospects.csv` still requires manual review because search-result titles can
be taglines rather than canonical business names.
