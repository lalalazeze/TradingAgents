# Design: A-Share Social Sentiment Data Integration

## Architecture

```
sentiment_analyst.py
        │
        ├── is_cn = is_cn_symbol(ticker)
        │
        ├── if is_cn:
        │       ├── news_block = fetch_cn_news(ticker)         ← stock_news_em
        │       ├── hot_keyword_block = fetch_cn_hot_keywords(ticker) ← stock_hot_keyword_em
        │       ├── comment_block = fetch_cn_comment(ticker)   ← stock_comment_em
        │       └── system_message = A-share 3-block prompt
        │
        └── else:
                ├── news_block = get_news(ticker, ...)          ← yfinance/alpha_vantage
                ├── stocktwits_block = fetch_stocktwits(ticker) ← stocktwits API
                ├── reddit_block = fetch_reddit(ticker)         ← Reddit RSS
                └── system_message = US/Global 3-block prompt
```

## New Module: `dataflows/cn_social.py`

Three functions, same pattern as `stocktwits.py` / `reddit.py`:

### `fetch_cn_news(symbol, limit=30, timeout=10.0) -> str`
- Wraps `ak.stock_news_em(symbol=symbol)`
- Returns: summary line + per-article `[时间·标题·来源·摘要]`
- Graceful degradation: returns `<unavailable: reason>` on failure

### `fetch_cn_hot_keywords(symbol, timeout=10.0) -> str`
- Wraps `ak.stock_hot_keyword_em(symbol=symbol)`
- Returns: ranked list of top-10 hot concepts with heat values
- Graceful degradation: returns placeholder on failure

### `fetch_cn_comment(symbol, timeout=10.0) -> str`
- Wraps `ak.stock_comment_em()`, filters by symbol
- Returns: comprehensive score, attention level, institutional participation
- Graceful degradation: returns placeholder on failure

All three use `_normalize_cn_symbol()` from `akshare_provider.py` and `_without_proxy()` for direct API access.

## Prompt Design (A-share path)

The `_build_system_message` gains an A-share branch that describes three data blocks:

1. **个股新闻** — event-driven, news framing (equivalent to US news_block)
2. **热门关键词** — what concepts/themes the market is discussing (equivalent to social signal)
3. **千股千评** — quantified sentiment metrics: composite score, attention, institutional participation

Analysis instructions adapted for A-share characteristics:
- News framing + hot concepts divergence detection
- 千股千评 score as quantitative anchor
- Confidence calibration: medium when all three blocks return data

## Dependency

`akshare` added to `pyproject.toml` main dependencies (not optional extra), since it's already used for OHLCV/indicators via `akshare_provider.py` — the import pattern with graceful degradation is already in place.

## Files Changed

| File | Change |
|------|--------|
| `tradingagents/dataflows/cn_social.py` | **NEW** — 3 fetch functions |
| `tradingagents/agents/analysts/sentiment_analyst.py` | Import cn_social, A-share path calls 3 fetchers, new prompt branch |
| `pyproject.toml` | Add `akshare` to dependencies |
