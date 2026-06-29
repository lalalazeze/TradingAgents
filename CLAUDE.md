# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable, with dev extras for test + lint)
pip install -e ".[dev]"

# Run the interactive CLI
tradingagents                    # installed console script
python -m cli.main               # equivalent, runs from source

# Run the test suite
pytest                           # full suite
pytest -m unit                   # fast isolated tests
pytest -m "not integration"      # skip tests that hit external services
pytest tests/test_symbol_utils.py -k some_name   # single test

# Lint (strict — full repo, excluding results/ and worklog/)
ruff check .

# Bedrock extra (optional, not in default install)
pip install ".[bedrock]"

# Docker
cp .env.example .env             # fill in keys
docker compose run --rm tradingagents
docker compose --profile ollama run --rm tradingagents-ollama
```

CI runs `pytest -q`, `ruff check .`, and a clean-install smoke (`pip install .` then `import tradingagents, cli.main`) across Python 3.10–3.13. The repo must stay clean under the strict `ruff` select (`E W F I B UP C4 SIM`); line-length enforcement is deferred (`E501` ignored, formatter not yet adopted repo-wide).

## Architecture

TradingAgents is a LangGraph workflow that mirrors a trading firm. A single run is a forward propagation through four tiers of agents, all sharing one `AgentState` (see `tradingagents/agents/utils/agent_states.py`):

**Tier 1 — Analysts** (`tradingagents/agents/analysts/`): Market, Sentiment, News, Fundamentals. Each analyst is a ReAct-style node with its own tool set (built in `tradingagents/agents/utils/{core_stock,fundamental_data,news_data,technical_indicators,macro_data,prediction_markets}_data_tools.py`). Analysts execute sequentially in the order selected; the plan is built by `build_analyst_execution_plan` in `tradingagents/graph/analyst_execution.py`. Each analyst has a `Msg Clear *` node that wipes its tool messages between analysts to keep context focused.

**Tier 2 — Researchers** (`tradingagents/agents/researchers/`): Bull and Bear researchers debate in `InvestDebateState`, routed by `should_continue_debate`. The Research Manager (structured output, `ResearchPlan` schema) synthesizes into an `investment_plan`.

**Tier 3 — Trader** (`tradingagents/agents/trader/`): Takes the research plan and produces a `trader_investment_plan` with a concrete `TraderAction` (Buy/Hold/Sell).

**Tier 4 — Risk + Portfolio** (`tradingagents/agents/risk_mgmt/`, `tradingagents/agents/managers/`): Aggressive/Neutral/Conservative debaters cycle through `RiskDebateState` per `max_risk_discuss_rounds`. The Portfolio Manager (structured output, `PortfolioRating`) renders the final `final_trade_decision`.

Orchestration lives in `tradingagents/graph/`:
- `trading_graph.py::TradingAgentsGraph` — top-level entry; builds LLMs, tool nodes, and the workflow. `propagate(ticker, date)` runs the graph; `reflect_and_remember` updates the memory log.
- `setup.py::GraphSetup` — wires the StateGraph edges.
- `conditional_logic.py` — the `should_continue_*` routing predicates.
- `propagation.py`, `reflection.py`, `signal_processing.py`, `checkpointer.py` — supporting passes.

### LLM provider layer (`tradingagents/llm_clients/`)

`factory.create_llm_client(provider, model, base_url, **kwargs)` is the single entry point. Anthropic/Google/Azure/Bedrock have dedicated clients; everything else (OpenAI, xAI, DeepSeek, Qwen, GLM, MiniMax, Groq, Mistral, NVIDIA, Kimi, OpenRouter, Ollama, any OpenAI-compatible server) routes through `openai_client.py`. **Per-model capability quirks live in `capabilities.py`'s declarative table**, not in `if` ladders in client code — adding a new model or provider means editing that table. The model catalog for the CLI dropdowns is `model_catalog.py`.

### Data vendor layer (`tradingagents/dataflows/`)

`interface.route_to_vendor(method, *args, **kwargs)` dispatches tool calls to the configured vendor. Vendors are set per **category** (`data_vendors`) or per **tool** (`tool_vendors`, wins) in `DEFAULT_CONFIG`. The configured chain is the *exact* resolution chain — there is no silent fallback to unselected vendors. A-share symbols (`000001`, `600036.SH`, `688987.BJ`) auto-route to Chinese vendors (`akshare` / `tushare` / `baostock`) per `china_data_vendor_default` before falling back to yfinance.

`market_data_validator.py` enforces the verified-data contract: symbol normalization, stale-OHLCV rejection, look-ahead-safe news windows. Errors use the typed `VendorError` taxonomy in `errors.py`.

### Configuration

`DEFAULT_CONFIG` in `tradingagents/default_config.py` is the single source of truth. Any key listed in `_ENV_OVERRIDES` can be set via a `TRADINGAGENTS_*` env var; coercion is type-driven from the existing default (so a misspelled bool fails loudly). The package `__init__.py` loads `.env` and `.env.enterprise` on import so the installed console script picks up the project's keys. `dataflows/config.py` holds a global `_config` that `set_config` merges one level deep — dict keys merge, scalars replace.

### Persistence

- **Decision log** (`~/.tradingagents/memory/trading_memory.md`, override via `TRADINGAGENTS_MEMORY_LOG_PATH`): append-only markdown. `TradingMemoryLog` in `agents/utils/memory.py` stores pending decisions on `propagate()`; `Reflector` resolves outcomes on `reflect_and_remember()` and injects same-ticker + cross-ticker context into the Portfolio Manager prompt.
- **Checkpoints** (`~/.tradingagents/cache/checkpoints/<TICKER>.db`): opt-in via `checkpoint_enabled` / `--checkpoint`. LangGraph state saved per node; cleared automatically on successful completion.

### Structured output

Three agents produce structured output via Pydantic schemas in `agents/schemas.py`: Research Manager (`ResearchPlan`), Trader (`TraderPlan`), Portfolio Manager (`PortfolioManagerDecision`). Each provider's native mode is used (json_schema for OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic) — dispatched through `capabilities.py`'s `preferred_structured_method`. Schemas render back to markdown so downstream consumers (memory log, saved reports) remain unchanged.

### Reports

`tradingagents/reporting.py::write_report_tree` is the shared writer used by both the CLI and `TradingAgentsGraph.save_reports()`. Output layout: `1_analysts/{market,sentiment,news,fundamentals}.md`, `2_research/{bull,bear,manager}.md`, `3_trading/trader.md`, `4_risk/{aggressive,conservative,neutral}.md`, `5_portfolio/portfolio_manager.md`, plus a consolidated `complete_report.md`.

## Testing notes

- `tests/conftest.py` auto-injects placeholder values for every `*_API_KEY` env var so tests don't skip or hit real services on collection. It also **replaces the global `dataflows._config` with a fresh `deepcopy(DEFAULT_CONFIG)` before and after every test** — partial `set_config` calls would otherwise leak between tests and make routing behavior order-dependent.
- Markers: `unit`, `integration`, `smoke`. Strict markers is on (`--strict-markers`), so new markers must be registered in `pyproject.toml`.
- When adding a data vendor or provider, add the corresponding env var to `_API_KEY_ENV_VARS` in `conftest.py` so CI doesn't accidentally call it.

## Tickers and markets

Any Yahoo Finance ticker works, with exchange suffix. Company identity and the alpha-vs benchmark resolve automatically from the suffix via `benchmark_map` in `DEFAULT_CONFIG` (`.HK` → `^HSI`, `.T` → `^N225`, `.SS`/`.SZ` → Shanghai/Shenzhen composites, no suffix → `SPY`). Override with `benchmark_ticker` for a fixed benchmark.
