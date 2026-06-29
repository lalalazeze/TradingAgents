"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches three complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks.

**US/Global tickers** use:
  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing

**Chinese A-share tickers** use three domestic data sources (via akshare):
  1. 个股新闻 (fetch_cn_news) — per-ticker news from 东方财富
  2. 热门关键词 (fetch_cn_hot_keywords) — hot concepts/themes, a lightweight
     social signal showing what themes the market is discussing
  3. 千股千评 (fetch_cn_comment) — composite score, attention, institutional
     participation — quantified sentiment metrics

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.cn_social import (
    fetch_cn_comment,
    fetch_cn_hot_keywords,
    fetch_cn_news,
)
from tradingagents.dataflows.interface import is_cn_symbol
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch data sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        #
        # For Chinese A-share symbols, StockTwits and Reddit are skipped
        # because these US-centric social platforms do not cover A-share
        # tickers. Instead, three domestic data sources are used:
        #   1. 个股新闻 (fetch_cn_news) — per-ticker news from 东方财富
        #   2. 热门关键词 (fetch_cn_hot_keywords) — hot concepts/themes
        #   3. 千股千评 (fetch_cn_comment) — composite score + attention
        is_cn = is_cn_symbol(ticker)
        if is_cn:
            news_block = fetch_cn_news(ticker)
            hot_keyword_block = fetch_cn_hot_keywords(ticker)
            comment_block = fetch_cn_comment(ticker)
            stocktwits_block = None
            reddit_block = None
        else:
            news_block = get_news.func(ticker, start_date, end_date)
            stocktwits_block = fetch_stocktwits_messages(ticker, limit=30)
            reddit_block = fetch_reddit_posts(ticker)
            hot_keyword_block = None
            comment_block = None

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
            hot_keyword_block=hot_keyword_block,
            comment_block=comment_block,
            is_cn=is_cn,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}"
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str | None = None,
    reddit_block: str | None = None,
    hot_keyword_block: str | None = None,
    comment_block: str | None = None,
    is_cn: bool = False,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks.

    For Chinese A-share stocks (is_cn=True), the prompt describes three
    domestic data blocks: 个股新闻, 热门关键词, and 千股千评.
    For non-A-share stocks, the prompt describes the three US/Global blocks:
    news headlines, StockTwits messages, and Reddit posts.
    """
    # A-share: three domestic data sources
    if is_cn:
        return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} (Chinese A-share stock) covering the period from {start_date} to {end_date}, drawing on three domestic data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### 个股新闻 — 东方财富个股新闻快讯 (past 7 days)
Per-ticker news from 东方财富, analogous to institutional news framing. Fact-driven, event-oriented signal covering company announcements, analyst commentary, sector news, and market-moving events.

<start_of_news>
{news_block}
<end_of_news>

### 热门关键词/概念 — 市场热门主题
Top hot concepts and keywords the market is discussing around this ticker, from 东方财富. This is a lightweight social signal — what themes, sectors, or narratives are drawing attention. Higher heat values mean more market buzz.

<start_of_hot_keywords>
{hot_keyword_block}
<end_of_hot_keywords>

### 千股千评 — 综合评分与市场指标
Quantitative sentiment metrics from 东方财富's per-stock evaluation system, computed from guba (股吧) browsing activity, watchlist additions, capital flow, and institutional participation. Key fields:
- 综合评分: Composite score (higher = more positive sentiment)
- 关注度/市场关注度: Market attention level
- 机构参与度: Institutional participation ratio

<start_of_comment>
{comment_block}
<end_of_comment>

## How to analyze this data (best practices for A-share stocks)

1. **Cross-reference news events with hot concepts.** If news mentions a policy change and the hot keywords show a related concept trending, that's a reinforcing signal. If news is silent but a concept is hot, the market may be anticipating something.

2. **Use 千股千评 as a quantitative anchor.** The composite score and attention metrics provide a numerical baseline. Cross-check your qualitative reading of the news against these numbers — agreement increases confidence; divergence warrants investigation.

3. **Read hot keywords for narrative direction.** The top-ranked concepts reveal what themes the market is focused on for this stock. Are they bullish themes (new contracts, policy support) or bearish ones (regulatory risk, earnings miss)? The heat values indicate conviction.

4. **Distinguish opinion from event in news.** A headline about a policy change or earnings report is an event; editorial commentary is opinion. Weight events more heavily.

5. **Identify catalysts and risks** that emerge across the three sources — upcoming earnings, policy changes, competitive threats, institutional flow shifts, etc.

6. **Be honest about data limits.** If one or more sources returned a placeholder (e.g. "<cn_news unavailable>"), your analysis is less robust — flag this explicitly in the `confidence` field and the narrative.

7. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size. When all three sources return substantive data, confidence can be medium or high. If any source is a placeholder, cap at low or medium.
- **narrative**: Full source-by-source breakdown, cross-source divergences and alignments, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""

    # Non-A-share: full three-source analysis
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
