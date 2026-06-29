"""A-share social/sentiment data fetchers via AKShare.

Provides three data sources for Chinese A-share sentiment analysis:

  1. **个股新闻** (`fetch_cn_news`) — per-ticker news from 东方财富 via
     ``ak.stock_news_em``.  Analogous to the yfinance news block in the
     US/Global path.
  2. **热门关键词** (`fetch_cn_hot_keywords`) — top-10 hot concepts/keywords
     for a ticker from 东方财富 via ``ak.stock_hot_keyword_em``.  A lightweight
     social signal — what themes the market is discussing around this stock.
  3. **千股千评** (`fetch_cn_comment`) — composite score, attention level, and
     institutional participation from 东方财富 via ``ak.stock_comment_em``.
     Quantified sentiment metrics computed from guba browsing, watchlist adds,
     and capital flow data.

All functions follow the same pattern as ``stocktwits.py`` / ``reddit.py``:
they return a formatted plaintext string ready for prompt injection and
degrade gracefully to a placeholder on any failure — the calling agent
never has to handle exceptions or None.
"""

from __future__ import annotations

import logging

from .akshare_provider import _normalize_cn_symbol, _restore_proxy, _without_proxy

logger = logging.getLogger(__name__)

# Lazy akshare import — same pattern as akshare_provider.py.  The module is
# listed as a main dependency, but a graceful-degradation path is kept so that
# a broken install or version mismatch degrades to placeholders rather than
# crashing the sentiment analyst.
_AKSHARE_AVAILABLE = False
_ak = None

try:
    import akshare as ak

    _AKSHARE_AVAILABLE = True
    _ak = ak
except ImportError:
    logger.warning(
        "akshare not installed — A-share social/sentiment data unavailable. "
        "Install with: pip install akshare"
    )


# ---------------------------------------------------------------------------
# Block 1: 个股新闻 — per-ticker news from 东方财富
# ---------------------------------------------------------------------------


def fetch_cn_news(symbol: str, limit: int = 30, timeout: float = 15.0) -> str:
    """Fetch recent news for an A-share ticker via ``ak.stock_news_em``.

    Returns a formatted string with a summary line and per-article entries:
    ``[发布时间 · 文章来源] 标题\\n  摘要``.

    Returns a placeholder string on any failure (akshare unavailable,
    network error, empty result).
    """
    if not _AKSHARE_AVAILABLE:
        return "<cn_news unavailable: akshare not installed>"

    code = _normalize_cn_symbol(symbol)
    logger.info("CN social: fetching news for %s (normalized: %s)", symbol, code)

    saved_proxy = _without_proxy()
    try:
        df = _ak.stock_news_em(symbol=code)
    except Exception as exc:
        logger.warning("CN news fetch failed for %s: %s", code, exc)
        return f"<cn_news unavailable: {type(exc).__name__}: {exc}>"
    finally:
        _restore_proxy(saved_proxy)

    if df is None or df.empty:
        return f"<cn_news: no news found for {code}>"

    # Normalize column names — the actual column names returned by akshare
    # are Chinese: 发布时间, 新闻标题, 新闻内容, 文章来源, 新闻链接
    col_map = {
        "发布时间": "time",
        "新闻标题": "title",
        "新闻内容": "content",
        "文章来源": "source",
        "新闻链接": "url",
    }
    rename = {k: v for k, v in col_map.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Take up to `limit` rows
    df = df.head(limit)

    total = len(df)
    lines = [f"东方财富个股新闻 — {code}: {total} 条新闻"]

    for _, row in df.iterrows():
        time_str = str(row.get("time", "")).strip()
        title = str(row.get("title", "")).strip()
        source = str(row.get("source", "")).strip()
        content = str(row.get("content", "")).strip()

        # Truncate long content for prompt brevity
        if len(content) > 200:
            content = content[:200] + "…"

        meta_parts = []
        if time_str:
            meta_parts.append(time_str)
        if source:
            meta_parts.append(source)
        meta = " · ".join(meta_parts) if meta_parts else ""

        line = f"[{meta}] {title}" if meta else title
        if content:
            line += f"\n  {content}"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Block 2: 热门关键词 — hot concepts/themes for a ticker
# ---------------------------------------------------------------------------


def fetch_cn_hot_keywords(symbol: str, timeout: float = 10.0) -> str:
    """Fetch top-10 hot keywords/concepts for an A-share ticker.

    Uses ``ak.stock_hot_keyword_em`` which returns concept names, codes,
    and heat values — a lightweight social signal showing what themes
    the market is discussing around this stock.

    Returns a formatted ranked list, or a placeholder on failure.
    """
    if not _AKSHARE_AVAILABLE:
        return "<cn_hot_keywords unavailable: akshare not installed>"

    code = _normalize_cn_symbol(symbol)

    # stock_hot_keyword_em expects symbol with exchange prefix, e.g. "SZ000001"
    # Try to construct the prefixed form from the bare 6-digit code
    prefixed = _to_prefixed_symbol(code)
    logger.info("CN social: fetching hot keywords for %s (prefixed: %s)", symbol, prefixed)

    saved_proxy = _without_proxy()
    try:
        df = _ak.stock_hot_keyword_em(symbol=prefixed)
    except Exception as exc:
        logger.warning("CN hot keywords fetch failed for %s: %s", code, exc)
        return f"<cn_hot_keywords unavailable: {type(exc).__name__}: {exc}>"
    finally:
        _restore_proxy(saved_proxy)

    if df is None or df.empty:
        return f"<cn_hot_keywords: no hot keywords found for {code}>"

    # Column names from akshare: typically 时间, 股票代码, 概念名称, 概念代码, 热度
    # Take up to top 10
    df = df.head(10)

    lines = [f"热门关键词/概念 — {code}: Top {len(df)}"]

    for rank, (_, row) in enumerate(df.iterrows(), 1):
        # Try common column names
        concept = (
            row.get("概念名称")
            or row.get("concept_name")
            or row.get("名称")
            or ""
        )
        heat = row.get("热度") or row.get("heat") or ""
        concept_code = (
            row.get("概念代码")
            or row.get("concept_code")
            or ""
        )

        concept_str = str(concept).strip()
        heat_str = str(heat).strip()
        code_str = str(concept_code).strip()

        parts = [f"#{rank} {concept_str}"]
        if code_str:
            parts.append(f"({code_str})")
        if heat_str:
            parts.append(f"热度: {heat_str}")
        lines.append("  ".join(parts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Block 3: 千股千评 — composite score, attention, institutional participation
# ---------------------------------------------------------------------------


def fetch_cn_comment(symbol: str, timeout: float = 15.0) -> str:
    """Fetch 千股千评 (per-stock commentary) metrics for an A-share ticker.

    Uses ``ak.stock_comment_em`` which returns a full-market table; we filter
    to the target symbol.  Fields include:

      - 综合评分 (composite score, 0–100)
      - 关注度 (market attention level)
      - 机构参与度 (institutional participation ratio)

    Returns a formatted summary, or a placeholder on failure.
    """
    if not _AKSHARE_AVAILABLE:
        return "<cn_comment unavailable: akshare not installed>"

    code = _normalize_cn_symbol(symbol)
    logger.info("CN social: fetching comment metrics for %s (normalized: %s)", symbol, code)

    saved_proxy = _without_proxy()
    try:
        df = _ak.stock_comment_em()
    except Exception as exc:
        logger.warning("CN comment fetch failed for %s: %s", code, exc)
        return f"<cn_comment unavailable: {type(exc).__name__}: {exc}>"
    finally:
        _restore_proxy(saved_proxy)

    if df is None or df.empty:
        return f"<cn_comment: no data for {code}>"

    # The full-market table has a '代码' or '股票代码' column.
    # Filter to our target symbol.
    code_col = None
    for candidate in ("代码", "股票代码", "code", "symbol"):
        if candidate in df.columns:
            code_col = candidate
            break

    if code_col is None:
        return f"<cn_comment: could not identify code column in data for {code}>"

    # Match on the 6-digit code (the full-market table may use bare codes)
    mask = df[code_col].astype(str).str.contains(code, na=False)
    row_match = df[mask]

    if row_match.empty:
        return f"<cn_comment: {code} not found in 千股千评 data>"

    row = row_match.iloc[0]

    # Extract key metrics — column names are Chinese
    lines = [f"千股千评 — {code}"]

    metric_fields = [
        ("最新价", "最新价"),
        ("涨跌幅", "涨跌幅"),
        ("综合评分", "综合评分"),
        ("关注度", "关注度"),
        ("机构参与度", "机构参与度"),
        ("主力成本", "主力成本"),
        ("市场关注度", "市场关注度"),
    ]
    for cn_name, display_name in metric_fields:
        val = row.get(cn_name)
        if val is not None and str(val).strip():
            lines.append(f"  {display_name}: {val}")

    # Also include any other non-NaN columns we haven't explicitly listed
    for col in df.columns:
        if col in [cn for cn, _ in metric_fields] or col == code_col:
            continue
        val = row.get(col)
        if val is not None and str(val).strip() and str(val).strip() != "nan":
            lines.append(f"  {col}: {val}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_prefixed_symbol(code: str) -> str:
    """Convert a bare 6-digit A-share code to the prefixed form expected by
    some akshare functions (e.g. ``stock_hot_keyword_em``).

    Rules:
      - 6xx → SH (Shanghai)
      - 0xx, 3xx → SZ (Shenzhen)
      - 8xx, 4xx → BJ (Beijing)
    """
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return code

    if code.startswith("6"):
        return f"SH{code}"
    elif code.startswith("0") or code.startswith("3"):
        return f"SZ{code}"
    elif code.startswith("8") or code.startswith("4"):
        return f"BJ{code}"
    return code
