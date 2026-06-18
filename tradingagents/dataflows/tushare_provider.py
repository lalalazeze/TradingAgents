"""
Tushare provider for A-share (China) stock data.

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

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tushare SDK bootstrap
# ---------------------------------------------------------------------------

_TUSHARE_AVAILABLE = False
_ts = None
_pro = None

try:
    import tushare as ts
    _TUSHARE_AVAILABLE = True
    _ts = ts
except ImportError:
    logger.warning("tushare not installed — A-share data via Tushare unavailable. "
                   "Install with: pip install tushare")


def _init_pro_api():
    """Initialise the Tushare Pro API (lazy, on first call)."""
    global _pro
    if _pro is not None:
        return _pro

    if not _TUSHARE_AVAILABLE:
        raise VendorNotConfiguredError("tushare SDK not installed")

    # Token priority: environment variable > .env file (explicit load)
    token = os.environ.get("TUSHARE_TOKEN", "") or os.environ.get("TUSHARE_PRO_TOKEN", "")
    if not token:
        # Explicitly load .env from the TradingAgents project root
        # (find_dotenv(usecwd=True) may miss it when run from a different project)
        try:
            from dotenv import find_dotenv, load_dotenv
            env_path = find_dotenv(usecwd=True)
            if env_path:
                load_dotenv(env_path, override=False)
            # Also try the package's own .env as a fallback.
            # Use override=True so that a real token from the package's .env
            # is not blocked by an empty value loaded from the CWD's .env
            # by the earlier tradingagents.__init__ load_dotenv call.
            from pathlib import Path
            pkg_env = Path(__file__).resolve().parent.parent.parent / ".env"
            if pkg_env.exists() and str(pkg_env) != env_path:
                load_dotenv(str(pkg_env), override=True)
            token = os.environ.get("TUSHARE_TOKEN", "") or os.environ.get("TUSHARE_PRO_TOKEN", "")
        except ImportError:
            pass

    if not token:
        raise VendorNotConfiguredError(
            "TUSHARE_TOKEN not set — add it to .env or export it as an environment variable"
        )

    ts.set_token(token)
    _pro = ts.pro_api()
    logger.info("Tushare Pro API initialised (token length: %d)", len(token))
    return _pro


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_cn_symbol(symbol: str) -> str:
    """Convert to tushare ts_code format (e.g. '000001.SZ', '600519.SH').

    Supported inputs:
      - '000001.SZ', '600519.SH', '688987.BJ'  (already correct)
      - 'SZ000001', 'SH600519'                  (prefix format)
      - '000001', '600519'                       (bare 6-digit — auto-detect market)
    """
    s = str(symbol).upper().strip()

    # Already in tushare format (6-digit + .SZ/.SH/.BJ)
    if len(s) == 9 and s[6] == "." and s[7:9] in ("SZ", "SH", "BJ"):
        return s

    # Strip known suffixes
    for suffix in (".SZ", ".SH", ".BJ", ".SS"):
        if s.endswith(suffix):
            bare = s[:-len(suffix)]
            market = suffix[1:]  # SZ, SH, BJ
            if suffix == ".SS":
                market = "SH"
            if len(bare) == 6 and bare.isdigit():
                return f"{bare}.{market}"

    # Strip prefix SZ/SH/BJ
    for prefix, market in (("SZ", "SZ"), ("SH", "SH"), ("BJ", "BJ")):
        if s.startswith(prefix) and len(s) > 2:
            bare = s[len(prefix):]
            if len(bare) == 6 and bare.isdigit():
                return f"{bare}.{market}"

    # Bare 6-digit — infer market from first digit
    if len(s) == 6 and s.isdigit():
        if s.startswith("6"):
            return f"{s}.SH"   # Shanghai main board
        elif s.startswith("0") or s.startswith("3"):
            return f"{s}.SZ"   # Shenzhen main board / ChiNext
        elif s.startswith("8") or s.startswith("4"):
            return f"{s}.BJ"   # Beijing STAR Market / BSE
        else:
            return f"{s}.SZ"   # Default to Shenzhen

    return s


def _is_cn_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a Chinese A-share code."""
    raw = str(symbol).upper().strip()
    if raw.endswith(".SZ") or raw.endswith(".SH") or raw.endswith(".BJ") or raw.endswith(".SS"):
        return True
    if raw.startswith("SZ") or raw.startswith("SH") or raw.startswith("BJ"):
        return True
    bare = raw.replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".SS", "")
    for prefix in ("SZ", "SH", "BJ"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    if len(bare) == 6 and bare.isdigit():
        if bare.startswith("6") or bare.startswith("0") or bare.startswith("3") or bare.startswith("8") or bare.startswith("4"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API — same signature as get_YFin_data_online
# ---------------------------------------------------------------------------

def get_stock_data_tushare(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch A-share daily OHLCV data via Tushare Pro API.

    Returns a CSV string with header, matching the format of
    get_YFin_data_online so the agent sees the same data layout.
    """
    pro = _init_pro_api()

    ts_code = _normalize_cn_symbol(symbol)
    logger.info("Tushare: fetching daily data for %s (normalized: %s)", symbol, ts_code)

    start_fmt = start_date.replace("-", "")
    end_fmt = end_date.replace("-", "")

    # Retry logic — Tushare rate-limits at ~200 req/min per interface
    max_retries = 3
    base_delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            data = pro.daily(
                ts_code=ts_code,
                start_date=start_fmt,
                end_date=end_fmt,
            )
            break
        except Exception as e:
            err_text = str(e).lower()
            if "每分钟最多访问" in err_text or "limit" in err_text:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + 1
                    logger.warning("Tushare rate-limited, retry %d/%d (waiting %.1fs)",
                                   attempt + 1, max_retries, delay)
                    time.sleep(delay)
                else:
                    raise VendorRateLimitError(f"Tushare rate limit after {max_retries} retries: {e}")
            else:
                raise

    if data is None or data.empty:
        raise NoMarketDataError(symbol, ts_code, f"no rows between {start_date} and {end_date}")

    # Normalize column names to match yfinance format
    col_map = {
        "trade_date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "vol": "Volume",
        "amount": "Amount",
        "pre_close": "PreClose",
        "change": "Change",
        "pct_chg": "ChangePct",
    }
    rename = {k: v for k, v in col_map.items() if k in data.columns}
    data = data.rename(columns=rename)

    # Format Date column to yyyy-mm-dd
    if "Date" in data.columns:
        data["Date"] = data["Date"].astype(str)
        # Tushare returns YYYYMMDD, convert to YYYY-MM-DD
        data["Date"] = data["Date"].apply(
            lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(str(d)) == 8 else str(d)
        )

    # Sort by date ascending
    if "Date" in data.columns:
        data = data.sort_values("Date").reset_index(drop=True)

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

    label = ts_code if ts_code == symbol.upper() else f"{ts_code} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Source: Tushare Pro (A-share daily, 未复权)\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_indicators_tushare(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Fetch technical indicator values for A-share stocks via Tushare + stockstats.

    Returns a date-value string matching the format of
    get_stock_stats_indicators_window.
    """
    pro = _init_pro_api()

    ts_code = _normalize_cn_symbol(symbol)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before_dt = curr_dt - pd.Timedelta(days=look_back_days)

    # Fetch daily data first, then compute indicator
    try:
        data = pro.daily(
            ts_code=ts_code,
            start_date=before_dt.strftime("%Y%m%d"),
            end_date=curr_dt.strftime("%Y%m%d"),
        )
    except Exception as e:
        raise VendorRateLimitError(f"Tushare indicator fetch failed: {e}")

    if data is None or data.empty:
        raise NoMarketDataError(symbol, ts_code, "no data for indicator calculation")

    # Normalize columns
    col_map = {
        "trade_date": "Date", "open": "Open", "close": "Close",
        "high": "High", "low": "Low", "vol": "Volume",
    }
    rename = {k: v for k, v in col_map.items() if k in data.columns}
    data = data.rename(columns=rename)

    if "Date" in data.columns:
        data["Date"] = data["Date"].astype(str).apply(
            lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(str(d)) == 8 else str(d)
        )
    data = data.sort_values("Date").reset_index(drop=True)

    # Compute indicator via stockstats
    try:
        from stockstats import StockDataFrame
        sdf = StockDataFrame.retype(data)
        indicator_values = sdf[indicator]
    except Exception as e:
        logger.warning("stockstats indicator %s failed for %s: %s", indicator, ts_code, e)
        return f"Indicator {indicator} calculation failed for {ts_code}: {e}"

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
        f"## {indicator} values for {ts_code} from {before_dt.strftime('%Y-%m-%d')} "
        f"to {curr_date}:\n\n"
        + "\n".join(result_lines)
        + "\n\n"
        + indicator_desc
    )
    return result_str


_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and dynamic support/resistance.",
    "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend.",
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
