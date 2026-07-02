# Spec: CN Social Sentiment Module

## Purpose

Provides A-share (Chinese market) social sentiment data fetching capabilities and integrates them into the sentiment analyst workflow. This module enables the system to analyze Chinese stock sentiment using news, hot keywords, and comment data from akshare.

## Requirements

### Requirement: fetch_cn_news
The system SHALL fetch Chinese stock news via akshare.

#### Scenario: Successful news fetch
- **WHEN** `fetch_cn_news(symbol, limit=30, timeout=10.0)` is called with a valid symbol
- **THEN** the system calls `ak.stock_news_em(symbol=normalized_symbol)` and returns a formatted string with summary line + per-article `[发布时间·新闻标题·文章来源] 新闻内容摘要`

#### Scenario: News fetch failure
- **WHEN** `fetch_cn_news` encounters ImportError, network error, or empty result
- **THEN** the system returns `<cn_news unavailable: reason>`

#### Scenario: Symbol normalization and proxy handling
- **WHEN** fetching news
- **THEN** the system uses `_normalize_cn_symbol` for symbol normalization and `_without_proxy` from akshare_provider

### Requirement: fetch_cn_hot_keywords
The system SHALL fetch Chinese stock hot keywords via akshare.

#### Scenario: Successful hot keywords fetch
- **WHEN** `fetch_cn_hot_keywords(symbol, timeout=10.0)` is called with a valid symbol
- **THEN** the system calls `ak.stock_hot_keyword_em(symbol=normalized_symbol)` and returns a formatted string with ranked list of hot concepts with heat values

#### Scenario: Hot keywords fetch failure
- **WHEN** `fetch_cn_hot_keywords` encounters an error
- **THEN** the system returns `<cn_hot_keywords unavailable: reason>`

### Requirement: fetch_cn_comment
The system SHALL fetch Chinese stock comment metrics via akshare.

#### Scenario: Successful comment fetch
- **WHEN** `fetch_cn_comment(symbol, timeout=10.0)` is called with a valid symbol
- **THEN** the system calls `ak.stock_comment_em()`, filters result by symbol, and returns a formatted string with comprehensive score, attention, institutional participation metrics

#### Scenario: Comment fetch failure
- **WHEN** `fetch_cn_comment` encounters an error
- **THEN** the system returns `<cn_comment unavailable: reason>`

### Requirement: A-share data path in sentiment analyst
The system SHALL use Chinese data sources when `is_cn=True` in the sentiment analyst.

#### Scenario: A-share data collection
- **WHEN** sentiment analysis runs with `is_cn=True`
- **THEN** the system calls `fetch_cn_news(ticker)` → `news_block`, `fetch_cn_hot_keywords(ticker)` → `hot_keyword_block`, and `fetch_cn_comment(ticker)` → `comment_block`

### Requirement: A-share prompt in sentiment analyst
The system SHALL use a specialized prompt for A-share sentiment analysis when `is_cn=True`.

#### Scenario: A-share system message
- **WHEN** `_build_system_message` is called with `is_cn=True`
- **THEN** the system describes three data blocks (新闻, 热门关键词, 千股千评), guides LLM to analyze news framing, concept热度, and quantified scores, maps concepts to US equivalents for consistent output quality, and adjusts confidence guidance (medium when all three return data)

### Requirement: akshare dependency
The system SHALL include akshare in the main dependencies.

#### Scenario: pyproject.toml configuration
- **WHEN** the package is installed
- **THEN** `akshare` is listed in the main `dependencies` list in `pyproject.toml`
