"""
AKShare provider for A-share (China) stock data.

Provides get_stock_data and get_indicators functions that return the same
format as the yfinance/alpha_vantage counterparts so the routing layer
can transparently swap vendors for Chinese tickers.
"""
import logging
import os
import time
from datetime import datetime
from typing import Annotated

import pandas as pd

from .errors import NoMarketDataError, VendorRateLimitError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROXY_KEYS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
)


def _without_proxy():
    """Temporarily remove proxy env vars so Chinese domestic APIs (eastmoney
    etc.) are accessed directly, not through a VPN/proxy that may fail or
    rate-limit.  Returns a dict of the original values for restoration."""
    saved = {}
    for key in _PROXY_KEYS:
        val = os.environ.get(key)
        if val is not None:
            saved[key] = val
            del os.environ[key]
    return saved


def _restore_proxy(saved):
    """Restore proxy env vars previously removed by _without_proxy."""
    for key, val in saved.items():
        os.environ[key] = val
    for key in _PROXY_KEYS:
        if key not in saved and key in os.environ:
            del os.environ[key]

_AKSHARE_AVAILABLE = False
_ak = None

try:
    import akshare as ak
    _AKSHARE_AVAILABLE = True
    _ak = ak
except ImportError:
    logger.warning("akshare not installed — A-share data via AKShare unavailable. "
                   "Install with: pip install akshare")


def _normalize_cn_symbol(symbol: str) -> str:
    """Convert various Chinese stock code formats to the 6-digit form
    expected by AKShare (e.g. '000001', '600519').

    Supported inputs:
      - '000001.SZ', '600519.SH', '688987.BJ'  (tushare / Yahoo format)
      - 'SZ000001', 'SH600519'                  (prefix format)
      - '000001', '600519'                       (bare 6-digit)
    """
    s = str(symbol).upper().strip()
    # Strip suffix .SZ / .SH / .BJ
    for suffix in (".SZ", ".SH", ".BJ", ".SS"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    # Strip prefix SZ / SH / BJ
    for prefix in ("SZ", "SH", "BJ"):
        if s.startswith(prefix) and len(s) > 2:
            s = s[len(prefix):]
            break
    return s


def _is_cn_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a Chinese A-share code."""
    raw = str(symbol).upper().strip()
    if raw.endswith(".SZ") or raw.endswith(".SH") or raw.endswith(".BJ") or raw.endswith(".SS"):
        return True
    if raw.startswith("SZ") or raw.startswith("SH") or raw.startswith("BJ"):
        return True
    bare = raw.replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".SS", "")
    if len(bare) == 6 and bare.isdigit():
        if bare.startswith("6") or bare.startswith("0") or bare.startswith("3") or bare.startswith("8") or bare.startswith("4"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API — same signature as get_YFin_data_online
# ---------------------------------------------------------------------------

def get_stock_data_akshare(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch A-share daily OHLCV data via AKShare.

    Returns a CSV string with header, matching the format of
    get_YFin_data_online so the agent sees the same data layout
    regardless of which vendor served the request.
    """
    if not _AKSHARE_AVAILABLE:
        raise VendorNotConfiguredError("akshare not installed")

    code = _normalize_cn_symbol(symbol)
    logger.info("AKShare: fetching daily data for %s (normalized: %s)", symbol, code)

    # AKShare stock_zh_a_hist uses bare 6-digit code
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Retry logic — AKShare can be flaky
    max_retries = 3
    base_delay = 1.0
    saved_proxy = _without_proxy()
    try:
        for attempt in range(max_retries + 1):
            try:
                data = _ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="hfq",  # 后复权, consistent with yfinance adjusted close
                )
                break
            except Exception as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning("AKShare retry %d/%d for %s: %s (waiting %.1fs)",
                                   attempt + 1, max_retries, code, e, delay)
                    time.sleep(delay)
                else:
                    raise VendorRateLimitError(f"AKShare failed after {max_retries} retries: {e}")
    finally:
        _restore_proxy(saved_proxy)

    if data is None or data.empty:
        raise NoMarketDataError(symbol, code, f"no rows between {start_date} and {end_date}")

    # Normalize column names to match yfinance format
    col_map = {
        "日期": "Date",
        "开盘": "Open",
        "收盘": "Close",
        "最高": "High",
        "最低": "Low",
        "成交量": "Volume",
        "成交额": "Amount",
        "振幅": "Amplitude",
        "涨跌幅": "ChangePct",
        "涨跌额": "Change",
        "换手率": "TurnoverRate",
    }
    rename = {k: v for k, v in col_map.items() if k in data.columns}
    data = data.rename(columns=rename)

    # Ensure Date column is string yyyy-mm-dd
    if "Date" in data.columns:
        data["Date"] = pd.to_datetime(data["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # Round numeric columns
    numeric_cols = ["Open", "High", "Low", "Close"]
    for col in numeric_cols:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce").round(2)

    # Select core OHLCV columns
    output_cols = ["Date", "Open", "High", "Low", "Close", "Volume"]
    available_cols = [c for c in output_cols if c in data.columns]
    data = data[available_cols]

    csv_string = data.to_csv(index=False)

    label = code if code == symbol.upper() else f"{code} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Source: AKShare (A-share daily, 后复权)\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_indicators_akshare(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Fetch technical indicator values for A-share stocks via AKShare.

    Returns a date-value string matching the format of
    get_stock_stats_indicators_window.
    """
    if not _AKSHARE_AVAILABLE:
        raise VendorNotConfiguredError("akshare not installed")

    code = _normalize_cn_symbol(symbol)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before_dt = curr_dt - pd.Timedelta(days=look_back_days)

    # Fetch daily data first, then compute indicator
    saved_proxy = _without_proxy()
    try:
        data = _ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=before_dt.strftime("%Y%m%d"),
            end_date=curr_dt.strftime("%Y%m%d"),
            adjust="hfq",
        )
    except Exception as e:
        raise VendorRateLimitError(f"AKShare indicator fetch failed: {e}")
    finally:
        _restore_proxy(saved_proxy)

    if data is None or data.empty:
        raise NoMarketDataError(symbol, code, "no data for indicator calculation")

    # Normalize
    col_map = {"日期": "Date", "开盘": "Open", "收盘": "Close",
               "最高": "High", "最低": "Low", "成交量": "Volume"}
    rename = {k: v for k, v in col_map.items() if k in data.columns}
    data = data.rename(columns=rename)

    # Use stockstats to compute indicator
    try:
        from stockstats import StockDataFrame
        sdf = StockDataFrame.retype(data)
        indicator_values = sdf[indicator]
    except Exception as e:
        logger.warning("stockstats indicator %s failed for %s: %s", indicator, code, e)
        return f"Indicator {indicator} calculation failed for {code}: {e}"

    # Build result string
    date_col = data["Date"] if "Date" in data.columns else None
    result_lines = []
    for i, val in enumerate(indicator_values):
        date_str = str(date_col.iloc[i]) if date_col is not None else f"row_{i}"
        if pd.isna(val):
            result_lines.append(f"{date_str}: N/A")
        else:
            result_lines.append(f"{date_str}: {round(float(val), 4)}")

    indicator_desc = _INDICATOR_DESCRIPTIONS.get(indicator, "No description available.")

    result_str = (
        f"## {indicator} values for {code} from {before_dt.strftime('%Y-%m-%d')} "
        f"to {curr_date}:\n\n"
        + "\n".join(result_lines)
        + "\n\n"
        + indicator_desc
    )
    return result_str


# Indicator descriptions (subset relevant to A-share analysis)
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and dynamic support/resistance.",
    "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups.",
    "close_10_ema": "10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum.",
    "macd": "MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence.",
    "macds": "MACD Signal: An EMA smoothing of the MACD line.",
    "macdh": "MACD Histogram: Shows the gap between MACD line and its signal.",
    "rsi": "RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds.",
    "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle.",
    "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle.",
    "atr": "ATR: Averages true range to measure volatility.",
    "volume": "Volume: Raw trading volume data.",
}


# Export helpers for interface.py
is_cn_symbol = _is_cn_symbol
