# Tasks: A-Share Social Sentiment Integration

## 1. Add akshare to dependencies
- [ ] Add `akshare` to `pyproject.toml` dependencies

## 2. Create cn_social.py module
- [ ] Create `tradingagents/dataflows/cn_social.py`
- [ ] Implement `fetch_cn_news(symbol, limit, timeout)` wrapping `ak.stock_news_em`
- [ ] Implement `fetch_cn_hot_keywords(symbol, timeout)` wrapping `ak.stock_hot_keyword_em`
- [ ] Implement `fetch_cn_comment(symbol, timeout)` wrapping `ak.stock_comment_em` with symbol filtering
- [ ] All functions: graceful degradation, formatted string output, proxy bypass, symbol normalization

## 3. Update sentiment_analyst.py
- [ ] Import `fetch_cn_news`, `fetch_cn_hot_keywords`, `fetch_cn_comment` from cn_social
- [ ] In A-share path (`is_cn=True`): call 3 fetch functions instead of returning unavailable placeholders
- [ ] Update `_build_system_message` A-share branch with 3-block prompt
- [ ] Adjust confidence guidance for A-share path

## 4. Test and verify
- [ ] Run `ruff check .` — no lint errors
- [ ] Run `pytest -m unit` — no regressions
- [ ] Verify akshare import doesn't break when akshare is not installed (graceful degradation)
