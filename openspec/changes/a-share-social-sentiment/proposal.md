# Proposal: A-Share Social Sentiment Data Integration

## Summary

Add Chinese A-share social/sentiment data sources to the Sentiment Analyst, replacing the current `<unavailable>` placeholders for A-share tickers with real data from three akshare-powered blocks: 个股新闻 (`stock_news_em`), 热门关键词 (`stock_hot_keyword_em`), and 千股千评 (`stock_comment_em`).

## Motivation

The sentiment analyst already detects A-share tickers via `is_cn_symbol()` but skips StockTwits and Reddit because those US-centric platforms don't cover A-share stocks. This leaves A-share sentiment reports with only a news block and low confidence. Adding domestic data sources fills this gap.

## Scope

- **New module**: `tradingagents/dataflows/cn_social.py` — three fetch functions following the stocktwits/reddit pattern (standalone, return formatted strings, graceful degradation)
- **Dependency**: Add `akshare` to main dependencies in `pyproject.toml`
- **Integration**: Update `sentiment_analyst.py` to call the new functions for A-share tickers
- **Prompt**: Update `_build_system_message` with A-share-specific analysis guidance for three data blocks
- **No schema change**: `SentimentReport` stays the same; the LLM organizes its narrative around the new blocks

## Out of Scope

- Routing through `interface.py` / `route_to_vendor` — social data is not a vendor-routing concern
- Xueqiu (雪球) integration — requires scraping beyond akshare's current wrappers
- CLS (财联社) telegraph — too unstable (multiple 2025 breakages)
- Direct guba post scraping — akshare has no `stock_guba_em` interface
