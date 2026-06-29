"""
BaoStock provider for A-share (China) stock data.

Provides get_stock_data and get_indicators functions that return the same
format as the yfinance/alpha_vantage counterparts so the routing layer
can transparently swap vendors for Chinese tickers.

BaoStock is a free, open-source A-share data library — no API key required.
It connects to the BaoStock data server directly via TCP.
"""
import logging
import os
from datetime import datetime
from io import StringIO
from typing import Annotated

import pandas as pd

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BaoStock SDK bootstrap
# ---------------------------------------------------------------------------

_BAOSTOCK_AVAILABLE = False
_bs = None

try:
    import baostock as bs
    _BAOSTOCK_AVAILABLE = True
    _bs = bs
except ImportError:
    logger.warning("baostock not installed — A-share data via BaoStock unavailable. "
                   "Install with: pip install baostock")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_cn_symbol(symbol: str) -> str:
    """Convert to BaoStock code format (e.g. 'sh.600000', 'sz.000001').

    Supported inputs:
      - '000001.SZ', '600519.SH', '688987.BJ'  (tushare / Yahoo format)
      - 'SZ000001', 'SH600519'                  (prefix format)
      - '000001', '600519'                       (bare 6-digit — auto-detect market)
    """
    s = str(symbol).upper().strip()

    # Already in baostock format (sh.600000 / sz.000001)
    if s.startswith("SH.") or s.startswith("SZ."):
        return s.lower()

    # tushare format: 6-digit + .SZ/.SH/.BJ
    if len(s) == 9 and s[6] == "." and s[7:9] in ("SZ", "SH", "BJ"):
        bare = s[:6]
        market = s[7:9]
        if market == "BJ":
            # BaoStock doesn't support BJ market well, fallback to SZ
            market = "SZ"
        return f"{market.lower()}.{bare}"

    # Strip known suffixes
    for suffix in (".SZ", ".SH", ".BJ", ".SS"):
        if s.endswith(suffix):
            bare = s[:-len(suffix)]
            # Infer correct market from code prefix, not just suffix.
            # .SS (yfinance Shanghai) with a 0/3-prefix code → actually SZ.
            if len(bare) == 6 and bare.isdigit():
                if bare.startswith("6"):
                    market = "sh"
                elif bare.startswith("0") or bare.startswith("3"):
                    market = "sz"
                elif bare.startswith("8") or bare.startswith("4"):
                    market = "sz"
                else:
                    # Fallback: use suffix mapping
                    market = suffix[1:].lower()
                    if suffix == ".SS":
                        market = "sh"
                    elif suffix == ".BJ":
                        market = "sz"
                return f"{market}.{bare}"

    # Strip prefix SZ/SH/BJ
    for prefix, market in (("SZ", "sz"), ("SH", "sh"), ("BJ", "sz")):
        if s.startswith(prefix) and len(s) > 2:
            bare = s[len(prefix):]
            if len(bare) == 6 and bare.isdigit():
                return f"{market}.{bare}"

    # Bare 6-digit — infer market from first digit
    if len(s) == 6 and s.isdigit():
        if s.startswith("6"):
            return f"sh.{s}"    # Shanghai main board
        elif s.startswith("0") or s.startswith("3"):
            return f"sz.{s}"    # Shenzhen main board / ChiNext
        elif s.startswith("8") or s.startswith("4"):
            return f"sz.{s}"    # BSE — baostock doesn't have BJ prefix
        else:
            return f"sz.{s}"    # Default to Shenzhen

    return s.lower()


def _is_cn_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a Chinese A-share code."""
    raw = str(symbol).upper().strip()
    if raw.endswith(".SZ") or raw.endswith(".SH") or raw.endswith(".BJ") or raw.endswith(".SS"):
        return True
    if raw.startswith("SH.") or raw.startswith("SZ.") or raw.startswith("BJ."):
        return True
    if (raw.startswith("SZ") or raw.startswith("SH") or raw.startswith("BJ")) and len(raw) > 6:
        return True
    bare = raw.replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".SS", "")
    for prefix in ("SZ", "SH", "BJ"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
    if len(bare) == 6 and bare.isdigit():
        if bare.startswith("6") or bare.startswith("0") or bare.startswith("3") or bare.startswith("8") or bare.startswith("4"):
            return True
    return False


def _safe_float(value):
    """Convert a string to float, returning None on failure."""
    if value is None or str(value).strip() == "" or str(value).strip() == "-":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _baostock_login():
    """Login to BaoStock server with retry."""
    if not _BAOSTOCK_AVAILABLE:
        raise VendorNotConfiguredError("baostock SDK not installed")
    lg = _bs.login()
    if lg.error_code != '0':
        raise VendorRateLimitError(
            f"BaoStock login failed: {lg.error_msg} (code: {lg.error_code})"
        )


def _baostock_logout():
    """Logout from BaoStock server (safe, ignores errors)."""
    try:
        _bs.logout()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API — same signature as get_YFin_data_online
# ---------------------------------------------------------------------------

def get_stock_data_baostock(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch A-share daily OHLCV data via BaoStock (free, no API key needed).

    Returns a CSV string with header, matching the format of
    get_YFin_data_online so the agent sees the same data layout.
    """
    if not _BAOSTOCK_AVAILABLE:
        raise VendorNotConfiguredError("baostock not installed")

    bs_code = _normalize_cn_symbol(symbol)
    logger.info("BaoStock: fetching daily data for %s (normalized: %s)", symbol, bs_code)

    # Retry logic — BaoStock may have intermittent connection issues
    max_retries = 3
    base_delay = 1.0
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            _baostock_login()

            rs = _bs.query_history_k_data_plus(
                code=bs_code,
                fields="date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",  # forward-adjust (hfq equivalent)
            )

            if rs.error_code != '0':
                error_msg = f"BaoStock query error: {rs.error_msg} (code: {rs.error_code})"
                logger.warning(error_msg)
                raise VendorRateLimitError(error_msg)

            data_list = []
            while (rs.error_code == '0') and rs.next():
                data_list.append(rs.get_row_data())

            _baostock_logout()

            if not data_list:
                raise NoMarketDataError(
                    symbol, bs_code,
                    f"BaoStock returned no rows for {bs_code} "
                    f"from {start_date} to {end_date}"
                )

            # Build DataFrame
            df = pd.DataFrame(data_list, columns=rs.fields)

            # Convert numeric columns
            for col in ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]:
                if col in df.columns:
                    df[col] = df[col].apply(_safe_float)

            # Rename columns to match yfinance format (capitalized)
            rename_map = {
                "date": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "amount": "Amount",
                "pctChg": "ChangePercent",
                "preclose": "PrevClose",
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

            # Format Date column
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

            # Drop rows with missing Close
            df = df.dropna(subset=["Close"])

            # Sort by Date ascending
            df = df.sort_values("Date", ascending=True).reset_index(drop=True)

            # Build CSV output matching yfinance format
            output_columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
            available_columns = [c for c in output_columns if c in df.columns]
            csv_buffer = StringIO()

            header_comment = (
                f"# Stock data for {bs_code} (from {symbol}) "
                f"from {start_date} to {end_date}\n"
                f"# Source: BaoStock (A-share daily, 前复权)\n"
                f"# Total records: {len(df)}\n"
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )

            csv_buffer.write(header_comment)
            df[available_columns].to_csv(csv_buffer, index=False, encoding="utf-8")
            return csv_buffer.getvalue()

        except VendorRateLimitError as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "BaoStock retry %d/%d for %s: %s (waiting %.1fs)",
                    attempt + 1, max_retries, symbol, str(e)[:80], delay,
                )
                import time
                time.sleep(delay)
                continue
            raise

        except NoMarketDataError:
            raise

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "BaoStock retry %d/%d for %s: %s (waiting %.1fs)",
                    attempt + 1, max_retries, symbol, str(e)[:80], delay,
                )
                import time
                time.sleep(delay)
                continue
            raise VendorRateLimitError(
                f"BaoStock failed after {max_retries} retries: {str(e)[:200]}"
            )

        finally:
            _baostock_logout()

    # Should not reach here, but just in case
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"BaoStock: unexpected state for {symbol}")


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

_INDICATOR_DESCRIPTIONS = {
    "close_10_ema":  "10-day Exponential Moving Average of Close",
    "close_50_sma":  "50-day Simple Moving Average of Close",
    "close_200_sma": "200-day Simple Moving Average of Close",
    "rsi":           "Relative Strength Index (14-day)",
    "boll":          "Bollinger Bands middle line (20-day SMA)",
    "boll_ub":       "Bollinger Bands upper band",
    "boll_lb":       "Bollinger Bands lower band",
    "macd":          "MACD line (12/26 EMA difference)",
    "macds":         "MACD signal line (9-day EMA of MACD)",
    "macdh":         "MACD histogram (MACD - signal)",
    "atr":           "Average True Range (14-day)",
    "vwma":          "Volume-weighted Moving Average",
    "tr":            "True Range",
}


def get_indicators_baostock(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Fetch A-share technical indicators via BaoStock + stockstats.

    Follows the same signature and output format as
    get_stock_stats_indicators_window.
    """
    if not _BAOSTOCK_AVAILABLE:
        raise VendorNotConfiguredError("baostock not installed")

    bs_code = _normalize_cn_symbol(symbol)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    # Fetch enough data: look_back_days + indicator window
    lookback_days = max(look_back_days, 200)
    start_dt = curr_dt - pd.Timedelta(days=lookback_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = curr_date

    try:
        _baostock_login()

        rs = _bs.query_history_k_data_plus(
            code=bs_code,
            fields="date,code,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",  # forward-adjust
        )

        if rs.error_code != '0':
            raise VendorRateLimitError(
                f"BaoStock query error: {rs.error_msg} (code: {rs.error_code})"
            )

        data_list = []
        while (rs.error_code == '0') and rs.next():
            data_list.append(rs.get_row_data())

        _baostock_logout()

    except VendorRateLimitError:
        raise

    except Exception as e:
        _baostock_logout()
        raise VendorRateLimitError(f"BaoStock indicator fetch failed: {str(e)[:200]}")

    if not data_list:
        raise NoMarketDataError(
            symbol, bs_code,
            f"BaoStock returned no data for indicators of {bs_code}"
        )

    # Build DataFrame
    df = pd.DataFrame(data_list, columns=rs.fields)
    rename_map = {
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = df[col].apply(_safe_float)

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Close"])
    df = df.sort_values("Date", ascending=True).reset_index(drop=True)

    # Use stockstats to compute indicator
    try:
        from stockstats import StockDataFrame
        sdf = StockDataFrame.retype(df)
        indicator_values = sdf[indicator]
    except Exception as e:
        raise ValueError(
            f"BaoStock: could not compute indicator '{indicator}' "
            f"for {symbol}: {e}"
        )

    desc = _INDICATOR_DESCRIPTIONS.get(indicator, f"Technical indicator: {indicator}")

    # Format output — same style as get_stock_stats_indicators_window
    result_str = (
        f"[{indicator}] for {symbol} on {curr_date} "
        f"(lookback: {look_back_days} days, source: BaoStock)\n"
        f"Description: {desc}\n"
        f"Most recent value on {curr_date}: {indicator_values.iloc[-1] if len(indicator_values) > 0 else 'N/A'}\n"
    )

    # Add recent values
    recent = indicator_values.tail(min(10, len(indicator_values)))
    if len(recent) > 0:
        result_str += "\nRecent values:\n"
        for date_val, ind_val in zip(
            df["Date"].tail(min(10, len(df))),
            recent
        ):
            result_str += f"  {date_val.strftime('%Y-%m-%d')}: {ind_val:.4f}\n"

    return result_str


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

is_cn_symbol = _is_cn_symbol
