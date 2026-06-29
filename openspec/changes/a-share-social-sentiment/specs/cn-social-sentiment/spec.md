# Spec: CN Social Sentiment Module

## cn_social.py

### fetch_cn_news
- Input: `symbol: str, limit: int = 30, timeout: float = 10.0`
- Calls `ak.stock_news_em(symbol=normalized_symbol)`
- Returns formatted string: summary line + per-article `[发布时间·新闻标题·文章来源] 新闻内容摘要`
- On failure (ImportError, network, empty): returns `<cn_news unavailable: reason>`
- Uses `_normalize_cn_symbol` and `_without_proxy` from akshare_provider

### fetch_cn_hot_keywords
- Input: `symbol: str, timeout: float = 10.0`
- Calls `ak.stock_hot_keyword_em(symbol=normalized_symbol)`
- Returns formatted string: ranked list of hot concepts with heat values
- On failure: returns `<cn_hot_keywords unavailable: reason>`

### fetch_cn_comment
- Input: `symbol: str, timeout: float = 10.0`
- Calls `ak.stock_comment_em()`, filters result by symbol
- Returns formatted string: comprehensive score, attention, institutional participation metrics
- On failure: returns `<cn_comment unavailable: reason>`

## sentiment_analyst.py changes

### A-share data path
When `is_cn=True`:
1. Call `fetch_cn_news(ticker)` → `news_block`
2. Call `fetch_cn_hot_keywords(ticker)` → `hot_keyword_block`
3. Call `fetch_cn_comment(ticker)` → `comment_block`

### A-share prompt
`_build_system_message` gains an `is_cn=True` branch that:
- Describes three data blocks (新闻, 热门关键词, 千股千评)
- Guides LLM to analyze news framing, concept热度, and quantified scores
- Maps concepts to US equivalents for consistent output quality
- Adjusts confidence guidance (medium when all three return data)

## pyproject.toml changes

Add `akshare` to the main `dependencies` list.
