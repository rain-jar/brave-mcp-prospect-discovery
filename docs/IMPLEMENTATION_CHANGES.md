# Implementation and Logical Change Record

This document explains what the prototype implements, how its parts work
together, and every material logical change made while building and debugging
it. It describes the final code in `main.py` and records why the implementation
changed during live validation.

## High-level implementation breakdown

The prototype has six main responsibilities:

1. **Runtime and credential validation** — validates CLI input, checks for
   `BRAVE_API_KEY`, verifies that `npx` is installed, and never prints the key.
2. **Real MCP integration** — starts Brave's maintained MCP server over stdio,
   initializes an official Python MCP client, and discovers tools with
   `tools/list`.
3. **Schema-aware search execution** — dynamically chooses the general web
   search tool, builds only arguments supported by its advertised JSON schema,
   and executes bounded-retry searches.
4. **Result parsing and prospect extraction** — accepts the MCP SDK's
   structured output and Brave's JSON text-block/JSONL output, then maps web
   results into typed records.
5. **Deterministic quality control** — rejects obvious directories, social
   networks, editorial lists, hotels, and non-restaurant results; normalizes
   names and domains; extracts only explicit location text; and deduplicates
   without an LLM.
6. **Honest output and reporting** — writes a CSV only when real candidates
   exist, adds uncertainty notes, and prints execution/completeness/failure
   totals.

The final live run executed eight queries, parsed 153 raw results, and produced
19 unique direct-domain prospects. All 19 have website values, 14 have explicit
location text, and the run reported no MCP/API failures.

## End-to-end data flow

1. `main()` parses `--output` and `--target` and rejects a target below one.
2. `discover()` checks local prerequisites and launches the pinned Brave server.
3. The MCP client performs the initialization handshake and calls `list_tools()`.
4. `choose_web_search_tool()` evaluates the discovered tool definitions and
   selects the general web-results tool.
5. Each query is converted into schema-compatible arguments and sent through
   MCP by `call_with_retry()`.
6. `parse_tool_result()` converts supported MCP response shapes into
   `SearchResult` objects.
7. `candidate_from_result()` filters and maps plausible restaurants into
   `Prospect` objects; `add_unique()` removes name/domain duplicates.
8. `write_csv()` saves real results and `print_summary()` reports the run.

No part of this flow calls Brave's REST API directly.

## Logical changes, in implementation order

### 1. Created the minimal project structure

Files added:

- `main.py` for the complete executable prototype.
- `requirements.txt` for the official Python MCP SDK.
- `.env.example` for the expected credential name.
- `.gitignore` for credentials, virtual environments, bytecode, and generated
  CSV output.
- `README.md` for setup, execution, pricing, interpretation, and limitations.

The architecture intentionally remains one Python module. No database,
frontend, scraping layer, LLM, or agent framework was introduced.

### 2. Pinned the external integration versions

- `SERVER_PACKAGE` pins Brave's MCP server to
  `@brave/brave-search-mcp-server@2.1.4`.
- `requirements.txt` pins the official Python SDK to `mcp==2.3.0`.

Pinning makes the verified transport, tool metadata, and SDK method behavior
reproducible instead of allowing an unreviewed dependency update to change the
prototype.

### 3. Added typed internal data models

Three dataclasses separate the stages of the pipeline:

- `SearchResult` stores parsed search-engine fields: title, URL, and snippet.
- `Prospect` exactly mirrors the required CSV columns.
- `RunStats` tracks executed queries, parsed raw results, and failures.

`SearchResult` is frozen because parsing should not mutate source data.
`Prospect` remains mutable because it is an output record. `RunStats` creates a
fresh failure list per run instead of sharing mutable state.

### 4. Added command-line configuration and validation

`parse_args()` added:

- `--output`, defaulting to `prospects.csv`.
- `--target`, defaulting to 20.

`main()` validates that the target is at least one and returns exit code `2` for
invalid local configuration. This keeps the normal command simple while making
the experiment reusable for smaller diagnostic runs.

### 5. Added secure runtime prerequisite checks

`discover()` checks that:

- `BRAVE_API_KEY` exists in the process environment.
- `npx` is available before trying to launch the server.

The key is passed only in `StdioServerParameters.env` to the Brave subprocess.
It is never logged, placed in command arguments, or written to the CSV.

During live execution, a populated `.env.example` was moved to the gitignored
`.env` to reduce accidental credential exposure, and `.env.example` was
restored with a placeholder. `main.py` still reads the process environment; a
shell must export the key or source `.env` before running it.

### 6. Implemented the real stdio MCP lifecycle

`discover()` constructs `StdioServerParameters` with `npx`, the pinned server
package, and `--transport stdio`. `Client(..., mode="legacy")` performs the MCP
initialization handshake compatible with the server's negotiated protocol.

The client then calls `list_tools()` and prints the names actually exposed by
the running server. This satisfies dynamic MCP discovery and avoids replacing
MCP with a direct HTTP integration.

The verified server exposed:

- `brave_web_search`
- `brave_local_search`
- `brave_video_search`
- `brave_image_search`
- `brave_news_search`
- `brave_summarizer`
- `brave_llm_context`
- `brave_place_search`

### 7. Implemented and then corrected dynamic tool selection

The first version of `choose_web_search_tool()` scored the combined tool name
and description. It rewarded the words `web` and `search`, but penalized
descriptions containing `news`, `image`, or `video`.

That logic selected `brave_llm_context` during the first credentialed run. The
general `brave_web_search` description legitimately mentions news, images, and
videos, while the LLM-context description repeats “web search.” The selected
LLM-context tool returned extracted page chunks rather than the expected web
result records, so parsing found zero prospects even though the API reported no
failure.

The corrected logic now:

- Requires a discovered `query` input property.
- Scores `search` and `web` primarily from the tool name.
- Rewards descriptions that advertise `title`, `description`, and `url` fields.
- Penalizes specialist markers (`news`, `image`, `video`, `local`, `place`,
  `llm`, and `context`) only when they identify the tool itself.
- Penalizes token-budget schemas characteristic of LLM-context tools.
- Raises an error listing exposed tools when no compatible general web tool is
  found.

The corrected selector was tested against both synthetic definitions and the
actual Brave metadata and selects `brave_web_search`.

### 8. Made search arguments schema-aware

`search_arguments()` always supplies the required query, then conditionally
adds preferred values only if the discovered input schema supports them:

- `country="CA"`
- `search_lang="en"`
- `ui_lang="en-CA"`
- `count=20`
- `safesearch="moderate"`

If a property declares an enum, the value is added only when permitted. This
keeps the client reusable across compatible server versions instead of assuming
that every optional parameter exists.

### 9. Added bounded MCP/API retries and error extraction

`call_with_retry()` attempts a tool call at most twice with a one-second delay.
It handles both:

- MCP tool results where `is_error` is true.
- Raised connection/protocol exceptions.

`error_text()` extracts readable text from MCP content blocks without assuming
all content blocks contain text. Exhausted attempts become a `RuntimeError`
that the query loop records in the terminal summary.

The retry is intentionally bounded to prevent an autonomous loop and avoid
unnecessary API usage.

### 10. Implemented tolerant MCP result parsing

Brave can return search data through different MCP result representations. The
parser was designed to accept all observed relevant shapes:

- SDK `structured_content`.
- One JSON object or array in an MCP text block.
- JSONL containing one object per line.
- Nested containers named `web`, `results`, `items`, or `data`.

`json_values_from_text()` decodes JSON and falls back to line-by-line JSONL.
`result_dicts()` recursively finds dictionaries with title/name and URL/link
fields. `parse_tool_result()` normalizes alternate field names and deduplicates
identical `(title, URL)` pairs.

Malformed non-JSON text is skipped rather than treated as prospect data. After
the tool-selection incident, the query loop was also changed to report “no
parseable web results” as a failure instead of silently returning zero.

### 11. Broadened the search strategy after live validation

The initial queries included quoted phrases such as `"official website"` and
`"site officiel"`. Three of five calls returned no web results, and the other
two produced only five raw results.

The quotes were removed and the strategy changed to eight progressively useful
queries:

- General independent restaurants.
- Fine dining.
- Bistros.
- French-language gastronomic dining.
- Vieux-Montréal.
- Plateau Mont-Royal.
- Griffintown.
- Mile End.

Menu/reservation terms retain full-service dining intent without demanding an
exact quoted phrase. The loop stops early when the target is reached, avoiding
unnecessary remaining requests.

### 12. Added deterministic source-domain filtering

`NON_PROSPECT_DOMAINS` rejects sources that may mention restaurants but are not
plausible restaurant-owned websites. The list grew during manual review to
cover:

- Review and booking platforms.
- Social networks and Reddit.
- Editorial, travel, tourism, and “best-of” publishers.
- Food directories and discovery sites.
- Hotel and institutional hospitality pages observed in the results.

`registered_domain()` normalizes hostnames by lowercasing and removing `www.`.
`is_excluded_domain()` rejects both exact blocked domains and their subdomains.
`candidate_from_result()` also rejects domains containing `hotel` as a simple
guard against hotel restaurant pages, which are outside the independent
restaurant ICP.

This is a transparent precision-oriented heuristic, not proof of restaurant
ownership or independence.

### 13. Added editorial-title and restaurant-cue filtering

`GENERIC_TITLE_RE` rejects titles containing list/directory language in English
or French, including `best`, `top`, `guide`, plural `restaurants`, `meilleurs`,
`incontournables`, and similar phrases observed during review.

`RESTAURANT_CUE_RE` requires at least one dining-related cue across the title
and snippet, such as restaurant, bistro, brasserie, dining, cuisine, menu, or
reservation.

Together these rules remove obvious articles while allowing a brand-only title
when its supporting snippet clearly describes a restaurant.

### 14. Improved business-name extraction

`clean_business_name()` originally selected the first segment before a title
separator. Live results showed generic segments such as `Official Website`,
`Accueil`, and descriptive taglines.

The revised logic:

- Normalizes whitespace.
- Splits common title separators (`|`, en dash, em dash, and hyphen).
- Removes generic home/official-site segments in English and French.
- Scores remaining segments in favor of short brand-like text without
  restaurant or location boilerplate.
- Caps the stored title at 160 characters.

The function never invents a name from an unrelated source. Some official
sites expose only taglines as search titles, so imperfect names remain a known
manual-review limitation.

### 15. Added explicit-only location extraction

`LOCATION_RE` recognizes Montréal/Montreal and Québec/Quebec/QC only when those
terms occur in the result title or snippet. `extract_location()` preserves the
matched spelling rather than inferring a standardized address.

The initial implementation could return both `Montréal, Québec` and a repeated
shorter `Montréal` mention. It was changed to retain the longest explicit
phrase, which is normally the most specific evidence available.

If no location is present, the CSV field remains empty and the prospect note
states that no explicit location was extracted.

### 16. Added deterministic deduplication

`normalized_name()` removes accents, punctuation, spacing, and case differences
for name comparison. `add_unique()` rejects a candidate when either:

- Its normalized business name already exists, or
- Its normalized website hostname already exists.

This catches variants such as accented/unaccented names and multiple result
pages from the same site without requiring fuzzy matching or an LLM.

### 17. Added uncertainty-preserving prospect mapping

`candidate_from_result()` copies only fields supported by the result:

- Business name from the selected title segment.
- Website/source URL from the search result.
- Description from the result snippet.
- Location only from explicit text.
- The exact query that discovered the candidate.

Every row states that independence, service style, linen use, and the apparent
official website are unverified. Missing fields are left empty rather than
filled with assumptions.

### 18. Added safe CSV generation

`write_csv()` uses the `Prospect` dataclass field order to produce exactly:

`business_name, website, location, description, source_url, search_query, notes`

It writes UTF-8 with `newline=""` for portable CSV behavior and creates only
the requested output parent directory. `discover()` writes the CSV only when at
least one real candidate exists. It does not create a fabricated or misleading
empty result file after a failed discovery run.

### 19. Added terminal metrics and meaningful exit codes

`print_summary()` reports:

- Queries executed.
- Parsed raw results.
- Unique prospects.
- Website and location completeness.
- Wall-clock execution time.
- Per-query or connection failures.

The process returns:

- `0` when prospects were produced and no failures were recorded.
- `1` when discovery was partial/unsuccessful or failures occurred.
- `2` for invalid input or missing local prerequisites.

### 20. Recorded live-run evidence and manual quality revisions

The first corrected web-search run produced 20 rows, but manual review found
editorial lists, tourism pages, a hotel result, Reddit, and an institutional
page. The domain/title filters were expanded and the query list gained
neighborhood searches.

The final run produced 19 unique domains from 153 raw results with no MCP/API
failures. Nineteen is intentionally retained rather than weakening filters or
inventing a twentieth record; the task asks for approximately 20 and prioritizes
real, manually reviewable evidence.

## Function-level reference

| Function | Responsibility |
|---|---|
| `parse_args` | Defines CLI options. |
| `normalized_name` | Produces deterministic comparison keys for names. |
| `registered_domain` | Extracts a normalized hostname from a result URL. |
| `is_excluded_domain` | Applies exact and subdomain source exclusions. |
| `clean_business_name` | Chooses a brand-like segment from a search title. |
| `extract_location` | Returns the most specific explicit Montréal/Québec phrase. |
| `candidate_from_result` | Filters and maps a raw result to a prospect. |
| `add_unique` | Deduplicates by normalized name or hostname. |
| `json_values_from_text` | Parses JSON or JSONL MCP text content. |
| `result_dicts` | Recursively locates web-result dictionaries. |
| `parse_tool_result` | Converts MCP output into unique `SearchResult` objects. |
| `choose_web_search_tool` | Selects the general web-results tool from discovered metadata. |
| `search_arguments` | Builds arguments supported by the selected tool schema. |
| `error_text` | Extracts readable MCP tool error content. |
| `call_with_retry` | Executes a tool with a bounded retry. |
| `write_csv` | Writes the required UTF-8 CSV schema. |
| `print_summary` | Prints execution and completeness metrics. |
| `discover` | Orchestrates the complete MCP discovery workflow. |
| `main` | Validates CLI input and starts the async workflow. |

## Verification performed

- Python compilation completed successfully.
- The installed dependency set passed `pip check`.
- Offline checks covered tool selection, JSON text-block parsing, candidate
  filtering, deduplication, location extraction, and CSV writing.
- A real stdio MCP handshake listed the Brave tools and negotiated protocol
  version `2025-11-25`.
- Tool selection was checked against actual Brave tool metadata.
- Multiple live Brave Search runs exercised error handling, query refinement,
  filtering, CSV generation, and terminal reporting.
- The final CSV was checked for the required columns and unique domains.

## Known limitations

- Search-result snippets and titles are not verified business facts.
- Some official websites publish taglines or neighborhood names as their page
  title, so `business_name` can require manual correction.
- The domain blocklist is scenario-specific and must evolve if the ICP changes.
- Deterministic filters can create false positives and false negatives.
- A direct-looking domain is not proof that the site is official.
- Independence, full-service dining, and linen usage are deliberately not
  inferred.
- `.env` is not loaded automatically; the key must be exported into the process
  environment before `python main.py` starts.
- The prototype does not scrape result pages, enrich records, qualify leads, or
  generate outreach.

