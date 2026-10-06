"""Data providers for the France (Euronext Paris) screener.

  roster       Euronext's own equities download (live.euronext.com), one CSV
               request for Euronext Paris and Euronext Growth Paris: name,
               ISIN, symbol, and the market string that names each line's
               home venue first ("Euronext Amsterdam, Paris" is ArcelorMittal,
               whose home is Amsterdam). The counterpart of Deutsche Börse's
               workbook and GPW's company search.

  factsheet    Euronext's per-company blocks, for the size survivors only:
               ICB (four levels) and the line's sub type (ordinary shares,
               certificate, venture-capital fund). Two requests per company,
               cached for a week - classification changes on a corporate
               action, not daily.

  fundamentals yfinance .info on the Paris line (SYMBOL.PA), one call per
               ticker, fast_info as the market-cap fallback - Poland's
               snapshot(), plus heldPercentInsiders for the control flag.

  liquidity    one batched yf.download for the size survivors: median daily
               traded value over the lookback, Euronext Paris only (MTF
               volume is not in Yahoo's figure), so a lower bound.

  statements   Yahoo annual income statement + balance sheet + seven years of
               daily closes. build_statement_record is the UK build's,
               unchanged since Poland.

THE CURRENCY TRAP, SMALL: thirteen Paris lines report in dollars while quoting
in euros (2026-10-06), TotalEnergies, Vallourec and Maurel & Prom among the
large ones. Yahoo's EPS, market cap and EV on those lines are in euros; its
book value, EBITDA and net income are in dollars - Poland's split exactly
(measured: EPS x shares / net income = 0.83-0.90 against EUR per USD 0.855).
So TotalEnergies' P/B and EV/EBITDA are ~15% off. Poland's test could not
see it: its band was set for a 4.3x rate. fr_filters.repair_foreign_reporters
tightens it, and quarterly_equity() below gives the book-value test an exact
figure to read against.

Euronext's index-composition feed is encrypted; it is not read. Nothing here
depends on index membership.
"""
from __future__ import annotations

import concurrent.futures as cf
import csv
import io
import json
import logging
import os
import re
import time

import numpy as np
import pandas as pd
import requests

import config_fr as K
from config_fr import ScreenConfig

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en"}

EURONEXT = "https://live.euronext.com"
ROSTER_URL = (EURONEXT + "/en/pd_es/data/stocks/download?mics={mics}&initialLetter="
              "&fe_type=csv&fe_decimal_separator=.&fe_date_format=d%2Fm%2FY")
BLOCK_URL = EURONEXT + "/en/ajax/getFactsheetInfoBlock/STOCK/{isin}-XPAR/{block}"
YAHOO_SUFFIX = ".PA"

# Every field snapshot() promises to return. Declared rather than inferred so a
# fully throttled fetch still produces a correctly-shaped frame.
SNAPSHOT_FIELDS = (
    "yf_name", "currency", "market_cap_local", "mcap_source", "close_local",
    "trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps",
    "book_value_ps", "div_yield", "yf_sector", "yf_industry", "yf_country",
    "quote_type", "yf_shares",
    # The currency the ACCOUNTS are in - TotalEnergies quotes in euros and
    # reports in dollars.
    "fin_ccy",
    # The currency repair's evidence - see fr_filters.repair_foreign_reporters.
    "yf_ev", "yf_ebitda", "yf_ni", "yf_roe",
    # Share of the company held by insiders (family, parent holding) - the
    # control flag, config_fr.CONTROL_MIN_INSIDERS.
    "insiders_pct",
)


def out_schema(df: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame({"ticker": tickers})
    df = df.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def to_yahoo(symbol: str) -> str:
    """Euronext symbol -> Yahoo symbol. TTE -> TTE.PA, STMPA -> STMPA.PA."""
    t = re.sub(r"[^A-Z0-9]", "", str(symbol).strip().upper())
    return f"{t}{YAHOO_SUFFIX}" if t else ""


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def parse_roster_csv(text: str) -> pd.DataFrame:
    """Euronext's download: a header row, three banner rows ("European
    Equities", the date, a disclaimer), then one row per line, ';'-separated."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    if not rows:
        return pd.DataFrame()
    head = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if len(r) == len(head) and re.match(r"^[A-Z]{2}[A-Z0-9]{10}$", r[1] or "")]
    df = pd.DataFrame(body, columns=head)
    asof = next((r[0] for r in rows[1:5] if re.match(r"^\d{1,2} \w{3} \d{4}$", r[0] or "")), "")
    df.attrs["asof"] = asof
    return df


def parse_icb_block(html: str) -> dict:
    """{'icb': '60101000', 'icb_industry': 'Energy', 'icb_supersector': ...}
    from the factsheet's ICB block, which reads "Industry 60, Energy
    SuperSector 6010, Energy Sector 601010, Oil, Gas and Coal Subsector
    60101000, Integrated Oil and Gas"."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    out = {}
    pat = r"{lab}\s+(\d{{{n}}}),\s*(.+?)\s*(?=Industry\s+\d|SuperSector\s+\d|Sector\s+\d|Subsector\s+\d|Help|$)"
    for key, lab, n in (("industry", "Industry", 2), ("supersector", "SuperSector", 4),
                        ("sector", "Sector", 6), ("subsector", "Subsector", 8)):
        m = re.search(pat.format(lab=r"(?<!Super)(?<!Sub)" + lab, n=n), text)
        if m:
            out[f"icb_{key}_code"] = m.group(1)
            out[f"icb_{key}"] = m.group(2).strip()
    return out


def parse_info_block(html: str) -> dict:
    """{'subtype': 'Ordinary Shares'} from the general-information block."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    m = re.search(r"Sub type\s+(.+?)\s+(?:Market|ISIN Code)\b", text)
    return {"subtype": m.group(1).strip()} if m else {}


class EuronextProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.roster_asof = ""

    # -- http --------------------------------------------------------------
    def _req(self, method: str, url: str, tries: int = 5, **kw) -> requests.Response:
        last = None
        for i in range(tries):
            try:
                r = self.s.request(method, url, timeout=60, **kw)
                r.raise_for_status()
                return r
            except Exception as e:                        # noqa: BLE001
                last = e
                time.sleep(1.5 + 2 * i)
        raise RuntimeError(f"{method} {url} failed after {tries} tries: {last}")

    # -- calendar ----------------------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """Real Euronext sessions, read off a liquid line - Euronext closes on
        Good Friday, Easter Monday, 1 May and 26 December, which no French
        public-holiday calendar gets right."""
        import yfinance as yf
        try:
            h = yf.Ticker("TTE.PA").history(period=f"{max(n * 2, 120)}d")
            if not h.empty:
                return [d.strftime("%Y-%m-%d") for d in h.index[-n:]]
        except Exception as e:
            log.warning("calendar via yfinance failed: %s", e)
        days = pd.bdate_range(end=pd.Timestamp.today(), periods=n)
        return [d.strftime("%Y-%m-%d") for d in days]

    # -- roster ------------------------------------------------------------
    def listing_roster(self) -> pd.DataFrame:
        """Every line on Euronext Paris and Euronext Growth Paris.

        Rights and warrants ("AB SCIENCE BSA") arrive in the same list; they
        have no Yahoo market cap and fall out at the size gate. Cached per day.
        """
        cols = ["ticker", "symbol", "isin", "name", "market", "board", "country",
                "secondary", "eur_turnover"]
        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        path = os.path.join(self.cfg.cache_dir, f"roster_{today}.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, dtype=str, keep_default_na=False)
                self.roster_asof = df.attrs.get("asof", today)
                meta = path + ".asof"
                if os.path.exists(meta):
                    self.roster_asof = open(meta, encoding="utf-8").read().strip() or today
                log.info("  roster: %d lines from today's cache", len(df))
                return df[cols]
            except Exception as e:                        # noqa: BLE001
                log.warning("  roster cache unreadable (%s), refetching", e)
        try:
            text = self._req("GET", ROSTER_URL.format(mics="%2C".join(K.MICS))).content \
                       .decode("utf-8-sig", errors="replace")
        except Exception as e:                            # noqa: BLE001
            log.error("Euronext download failed: %s", e)
            return pd.DataFrame(columns=cols)
        raw = parse_roster_csv(text)
        if raw.empty or "ISIN" not in raw.columns:
            log.error("Euronext download did not parse (%d bytes)", len(text))
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame({
            "symbol": raw["Symbol"].str.strip(),
            "isin": raw["ISIN"].str.strip(),
            "name": raw["Name"].str.strip(),
            "market": raw["Market"].str.strip(),
            "eur_turnover": raw.get("Turnover", pd.Series("", index=raw.index)).str.strip(),
        })
        # Euronext Access lines can share the download when a MIC is added
        # later; only the two boards in config_fr.BOARDS are read.
        home = df["market"].str.split(",").str[0].str.strip()
        df = df[home.isin(K.BOARDS) | df["market"].str.contains("Paris")]
        df = df[~df["market"].str.startswith("Euronext Access")]
        df["board"] = df["market"].map(K.board_of)
        df["secondary"] = df["market"].map(K.is_secondary).map({True: "1", False: ""})
        df["country"] = df["isin"].str[:2]
        df["ticker"] = df["symbol"].map(to_yahoo)
        df = df[df["ticker"].ne("")].drop_duplicates(subset=["isin"], keep="first")
        self.roster_asof = raw.attrs.get("asof", "") or today
        df = df[cols].reset_index(drop=True)
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            df.to_csv(path, index=False, encoding="utf-8")
            with open(path + ".asof", "w", encoding="utf-8") as fh:
                fh.write(self.roster_asof)
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write roster cache: %s", e)
        return df

    # -- company factsheets ------------------------------------------------
    FACTSHEET_CACHE_VERSION = 1

    def factsheets(self, isins: list[str], max_age_days: float = 7.0) -> dict[str, dict]:
        """ICB and sub type per ISIN, cached for a week.

        Two requests per company, so only ever asked for the size survivors.
        A failure leaves the company without ICB - unscored on the peer
        screen, counted in the funnel - rather than guessed.
        """
        path = os.path.join(self.cfg.cache_dir, "euronext_factsheets.json")
        cache: dict = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = json.load(fh)
                if cache.get("_v") != self.FACTSHEET_CACHE_VERSION:
                    cache = {}
            except Exception:                             # noqa: BLE001
                cache = {}
        now = time.time()
        out: dict[str, dict] = {}
        fetched = failed = 0
        for isin in dict.fromkeys(str(i) for i in isins if i):
            rec = cache.get(isin) or {}
            if rec and (now - rec.get("_t", 0)) < max_age_days * 86400:
                out[isin] = rec
                continue
            try:
                rec = {"_t": now}
                rec.update(parse_icb_block(self._req(
                    "GET", BLOCK_URL.format(isin=isin, block="fs_icb_block"), tries=3).text))
                rec.update(parse_info_block(self._req(
                    "GET", BLOCK_URL.format(isin=isin, block="fs_info_block"), tries=3).text))
                time.sleep(0.15)
            except Exception as e:                        # noqa: BLE001
                log.warning("  Euronext factsheet %s: %s", isin, e)
                failed += 1
                continue
            fetched += 1
            cache[isin] = rec
            out[isin] = rec
        if fetched:
            try:
                os.makedirs(self.cfg.cache_dir, exist_ok=True)
                cache["_v"] = self.FACTSHEET_CACHE_VERSION
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, ensure_ascii=False)
            except Exception as e:                        # noqa: BLE001
                log.warning("  could not write factsheet cache: %s", e)
        log.info("  Euronext factsheets: %d requested, %d fetched, %d failed",
                 len(set(isins)), fetched, failed)
        return out

    # -- symbol resolution -------------------------------------------------
    def resolve_symbols(self, rows: pd.DataFrame) -> dict[str, str]:
        """Yahoo symbols for lines whose Euronext symbol Yahoo does not know,
        by ISIN - Germany's Schaeffler case. Paris quotes only; a hit only on
        another exchange is left unresolved. Cached for a week."""
        import yfinance as yf
        path = os.path.join(self.cfg.cache_dir, "isin_symbols.json")
        cache: dict = {}
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 7 * 86400:
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except Exception:                             # noqa: BLE001
                cache = {}

        out: dict[str, str] = {}
        for _, r in rows.iterrows():
            isin, old = str(r["isin"]), str(r["ticker"])
            if isin not in cache:
                try:
                    quotes = yf.Search(isin, max_results=8, news_count=0).quotes or []
                    syms = [str(q.get("symbol") or "") for q in quotes]
                except Exception as e:                    # noqa: BLE001
                    log.debug("ISIN search %s: %s", isin, e)
                    continue
                cache[isin] = [x for x in syms if x.endswith(YAHOO_SUFFIX)]
                time.sleep(self.cfg.request_delay)
            cand = cache.get(isin) or []
            if cand and cand[0] != old:
                out[old] = cand[0]
                if len(cand) > 1:
                    out[old + "|alt"] = cand[1]
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write ISIN cache: %s", e)
        n = len([k for k in out if not k.endswith("|alt")])
        log.info("  resolved %d of %d unpriced lines by ISIN", n, len(rows))
        return out

    # -- fundamentals ------------------------------------------------------
    def _cache_path(self, asof: str) -> str:
        return os.path.join(self.cfg.cache_dir, f"snapshot_{asof or 'latest'}.csv")

    def snapshot(self, tickers: list[str], asof: str = "") -> pd.DataFrame:
        """One yfinance .info per ticker, fast_info when .info has no market cap.

        Cached per session date for the reason the UK build found: Yahoo
        throttles by returning dicts with fields MISSING, not by raising, so
        successive runs must fill gaps rather than re-ask for everything.
        """
        import yfinance as yf

        cached = pd.DataFrame()
        path = self._cache_path(asof)
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h <= self.cfg.cache_ttl_hours:
                try:
                    cached = pd.read_csv(path)
                    log.info("  cache: %d rows from %s (%.1fh old)",
                             len(cached), os.path.basename(path), age_h)
                except Exception as e:                    # noqa: BLE001
                    log.warning("  cache unreadable (%s), refetching", e)
                stale = [f for f in SNAPSHOT_FIELDS if f not in cached.columns]
                if stale and not cached.empty:
                    log.info("  cache predates field(s) %s - refetching",
                             ", ".join(stale))
                    cached = pd.DataFrame()

        have = set()
        if not cached.empty and "market_cap_local" in cached.columns:
            have = set(cached.loc[cached["market_cap_local"].notna(), "ticker"])
        todo = [t for t in tickers if t not in have]
        if not todo:
            log.info("  all %d tickers served from cache", len(tickers))
            return cached[cached["ticker"].isin(tickers)].reset_index(drop=True)

        try:
            yf.Ticker(todo[0]).info
        except Exception as e:                            # noqa: BLE001
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                log.error("Yahoo is rate-limiting this IP (%s). Nothing was "
                          "fetched; wait a few minutes and re-run - the cache "
                          "keeps whatever has already arrived.", type(e).__name__)
                return out_schema(cached, tickers)
            log.debug("probe ticker %s failed non-fatally: %s", todo[0], e)

        def one(t: str) -> dict:
            rec = {"ticker": t}
            i = {}
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            for attempt in (0, 1):
                try:
                    i = yf.Ticker(t).info or {}
                    if i.get("marketCap") is not None or i.get("regularMarketPrice"):
                        break
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s attempt %d: %s", t, attempt, e)
                    if "RateLimit" in type(e).__name__:
                        return rec
                if attempt == 0:
                    time.sleep(0.5 + self.cfg.request_delay)
            if not i:
                return rec
            price = i.get("regularMarketPrice") or i.get("currentPrice")
            mcap, src = i.get("marketCap"), "info"
            if mcap is None and price:
                # Germany's Allianz case, and Christian Dior's here: every
                # multiple, no marketCap. 31 Paris lines on 2026-10-06.
                try:
                    mcap = yf.Ticker(t).fast_info.get("marketCap")
                    src = "fast_info" if mcap else ""
                except Exception as e:                    # noqa: BLE001
                    log.debug("%s fast_info: %s", t, e)
                    src = ""
            rec.update({
                "yf_name": i.get("longName") or i.get("shortName"),
                "currency": i.get("currency"),
                "market_cap_local": mcap,
                "mcap_source": src if mcap else "",
                "close_local": price,
                "trailing_pe": i.get("trailingPE"),
                "price_to_book": i.get("priceToBook"),
                "ev_to_ebitda": i.get("enterpriseToEbitda"),
                "trailing_eps": i.get("trailingEps"),
                "book_value_ps": i.get("bookValue"),
                "div_yield": i.get("dividendYield"),
                "yf_sector": i.get("sector"),
                "yf_industry": i.get("industry"),
                "yf_country": i.get("country"),
                "quote_type": i.get("quoteType"),
                "yf_shares": i.get("impliedSharesOutstanding") or i.get("sharesOutstanding"),
                "fin_ccy": i.get("financialCurrency"),
                "yf_ev": i.get("enterpriseValue"),
                "yf_ebitda": i.get("ebitda"),
                "yf_ni": i.get("netIncomeToCommon"),
                "yf_roe": i.get("returnOnEquity"),
                "insiders_pct": i.get("heldPercentInsiders"),
            })
            return rec

        log.info("  fetching %d tickers (%d already cached)", len(todo), len(have))
        rows: list[dict] = []
        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for n, rec in enumerate(ex.map(one, todo), 1):
                rows.append(rec)
                if n % 50 == 0:
                    log.info("  fetched %d/%d", n, len(todo))

        fresh = pd.DataFrame(rows)
        parts = [d for d in (cached, fresh) if not d.empty]
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        out = out.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
        out = out.sort_values("market_cap_local", na_position="last")
        out = out.drop_duplicates(subset=["ticker"], keep="first")

        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            out.to_csv(self._cache_path(asof), index=False, encoding="utf-8")
        except Exception as e:                            # noqa: BLE001
            log.warning("  could not write cache: %s", e)

        out = out[out["ticker"].isin(tickers)].reset_index(drop=True)
        got = int(out["market_cap_local"].notna().sum())
        fb = int((out["mcap_source"] == "fast_info").sum())
        log.info("  %d/%d tickers have a market cap (%d via fast_info)",
                 got, len(tickers), fb)
        return out

    def quarterly_equity(self, tickers: list[str]) -> dict[str, float]:
        """Latest quarterly common equity, in the reporting currency.

        Only for the foreign reporters, a handful of names. Yahoo's bookValue
        is the latest quarter's equity over today's shares; the annual figure
        can be 5-10% away by the time it is read, which is half the distance
        between euros and dollars. The repair's book-value test needs the
        number Yahoo actually divided.
        """
        import yfinance as yf
        out: dict[str, float] = {}
        for t in tickers:
            try:
                q = yf.Ticker(t).quarterly_balance_sheet
                for row in ("Common Stock Equity", "Stockholders Equity"):
                    if q is not None and row in q.index:
                        s = pd.to_numeric(q.loc[row], errors="coerce").dropna()
                        if not s.empty:
                            s.index = pd.to_datetime(s.index)
                            out[t] = float(s.sort_index().iloc[-1])
                            break
            except Exception as e:                        # noqa: BLE001
                log.debug("%s quarterly balance sheet: %s", t, e)
            time.sleep(self.cfg.request_delay)
        log.info("  quarterly equity for %d of %d foreign reporters", len(out), len(tickers))
        return out

    # -- liquidity ---------------------------------------------------------
    def price_panel(self, tickers: list[str], days: int, chunk: int = 80) -> pd.DataFrame:
        """Daily close and volume for every ticker at once (batched endpoint,
        not .info's throttle)."""
        import yfinance as yf
        period = f"{max(int(days * 1.6), 30)}d"
        frames = []
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            try:
                px = yf.download(part, period=period, interval="1d", progress=False,
                                 auto_adjust=False, actions=False, threads=True,
                                 group_by="column")
            except Exception as e:                                # noqa: BLE001
                log.warning("price chunk %d failed: %s", i // chunk, e)
                continue
            if px is not None and not px.empty:
                frames.append(px)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    @staticmethod
    def average_daily_value(panel: pd.DataFrame, tickers: list[str],
                            days: int) -> pd.DataFrame:
        """Median daily traded value in EUR over the last `days` sessions.
        Median, not mean: one index-rebalance day can be twenty times normal."""
        if panel.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        try:
            close, vol = panel["Close"], panel["Volume"]
        except KeyError:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        if isinstance(close, pd.Series):                      # single ticker
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        rows = []
        for t in tickers:
            if t not in close.columns or t not in vol.columns:
                continue
            c = close[t]
            v = vol[t]
            if isinstance(c, pd.DataFrame):                   # duplicate column
                c, v = c.iloc[:, 0], v.iloc[:, 0]
            tv = (pd.to_numeric(c, errors="coerce")
                  * pd.to_numeric(v, errors="coerce")).dropna().tail(days)
            if tv.empty:
                continue
            rows.append({"ticker": t, "adv_local": float(tv.median()),
                         "adv_sessions": int(len(tv))})
        return pd.DataFrame(rows)

    # -- FX ----------------------------------------------------------------
    def _rate(self, base: str, quote: str, lo: float, hi: float) -> float:
        """Units of `quote` per one `base`: Yahoo, then the ECB via Frankfurter."""
        import yfinance as yf
        try:
            h = yf.Ticker(f"{base}{quote}=X").history(period="5d")
            if not h.empty:
                rate = float(h["Close"].iloc[-1])
                if lo < rate < hi:
                    return rate
        except Exception as e:                            # noqa: BLE001
            log.info("FX %s/%s via Yahoo failed (%s)", base, quote, e)
        try:
            r = self.s.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}",
                           timeout=20)
            rate = float(r.json()["rates"][quote])
            if lo < rate < hi:
                log.info("FX %s/%s from Frankfurter (ECB reference)", base, quote)
                return rate
        except Exception as e:                            # noqa: BLE001
            log.warning("FX %s/%s via Frankfurter failed: %s", base, quote, e)
        return float("nan")

    def eur_to_usd(self) -> float:
        """USD per EUR - multiplied, Germany's direction."""
        rate = self._rate("EUR", "USD", 0.8, 1.6)
        if np.isfinite(rate):
            return rate
        log.warning("FX: both live sources failed, using the hardcoded 1.17. "
                    "The USD size gate is only as good as this number.")
        return 1.17

    def eur_per(self, currencies) -> dict[str, float]:
        """EUR per one unit of each reporting currency, for the currency
        repair. A rate that cannot be found is NaN, and the repair then
        refuses the affected multiples rather than guessing."""
        out = {"EUR": 1.0}
        for c in sorted({str(c) for c in currencies if isinstance(c, str) and c}):
            if c not in out:
                out[c] = self._rate(c, "EUR", 1e-5, 1e3)
                log.info("  FX: 1 %s = %.4f EUR", c, out[c])
        return out


# ---------------------------------------------------------------------------
# Filed statements: three-year history and the own-history benchmark
# ---------------------------------------------------------------------------
# Ported from the UK build unchanged in logic, via Germany. The worked examples
# in the comments below (NatWest, Reckitt, Shell, Craneware) were measured in
# London; the rules they justify are Yahoo's, so they hold for .PA lines too.
# The French cases that exercise them are in test_france.py.
# Row names Yahoo uses in its annual statements, first match wins. Net income
# is taken to COMMON shareholders where Yahoo separates it: NatWest's differs
# by ~6% because of AT1 coupons, and a P/E is a price for the common equity.
IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "np": ("Net Income Common Stockholders", "Net Income"),
    # Yahoo's "normalized" lines strip exactly its Total Unusual Items row. The
    # 3-year growth history stays on the REPORTED rows above, as Korea's does
    # - that is what happened. The own-history VALUATION uses these instead,
    # because a one-off is not a change in what the business is worth: Reckitt
    # sold Essential Home in 2025, reported EBITDA jumped 4,760 vs 3,966
    # normalized and net income 3,182 vs 2,535, and on reported figures it
    # passed the history screen on P/E and EV/EBITDA while its P/B - which a
    # disposal gain does not move - sat only 15% below its history. For an
    # ordinary year the two lines agree to within a few percent.
    "np_norm": ("Normalized Income",),
    "ebitda_norm": ("Normalized EBITDA",),
}
BS_ROWS = {
    "equity": ("Common Stock Equity", "Stockholders Equity"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "debt": ("Total Debt",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "mi": ("Minority Interest",),
}

# Quote currencies Yahoo reports in minor units, and the major unit each is.
MINOR_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
             "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Consecutive filed share counts outside this band are treated as a corporate
# action (consolidation, split, rights issue) rather than buybacks. Shell's
# heavy buybacks move ~7% a year, so the band is wide enough to leave them
# alone. It cannot catch small consolidations; it catches the ones that would
# otherwise manufacture a 30%+ "discount" out of a unit change.
SHARE_BREAK_BAND = (0.67, 1.5)
# Today's price x latest filed shares must land near today's market cap,
# once any split or consolidation since the filing is allowed for. If it does
# not, the price series and the share count are on different bases and every
# historical market value built from them is wrong by the same factor.
#
# Measured 2026-10-06 on names with no split since their filing: UK 1st-99th
# percentile 0.75-1.07 (max 1.12), Germany 0.91-1.10. Below 1 is shares issued
# since the filing (IQE 0.73, Rockhopper 0.79), above it buybacks. The old
# 0.6-1.6 admitted an unrestated 3:2 split (0.67) or 4:3 consolidation (1.33)
# - a fake 25-33% move in every past valuation. 0.7-1.25 refuses both and
# keeps every healthy name measured.
NOW_BASIS_BAND = (0.7, 1.25)


def _splits_after(splits, when) -> list:
    """Split / consolidation ratios Yahoo recorded after `when`, oldest first.

    Yahoo reports a consolidation as a ratio below one (Johnson Matthey's
    4-for-3 in Aug 2026 is 0.75) and a demerger price adjustment the same way,
    so the caller decides what a ratio means by whether it explains the share
    count, never by its size alone.
    """
    if splits is None or len(splits) == 0:
        return []
    s = pd.to_numeric(splits, errors="coerce")
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize() \
        if getattr(s.index, "tz", None) is not None else pd.to_datetime(s.index).normalize()
    s = s[(s.index > when) & (s > 0) & (s != 1)].sort_index()
    return [float(v) for v in s.tolist()]
# A latest filing older than this is not the latest filing - Yahoo has missed
# one. "Today" is then today's price over earnings from two years ago, while
# yfinance's own twelve-month figure knows better: Craneware read 52.6x on
# the same basis against 28.6x trailing, and passed the history screen on that
# gap. 13 of 251 survivors (Sept 2026) had a newest filed year of 2024. Eighteen
# months leaves room for a late filer without admitting a skipped year.
MAX_FILING_AGE_DAYS = 548

# The statements cache stores DERIVED records, not raw frames (seven years of
# daily prices per name would be ~40x larger). So a change to
# build_statement_record does not reach names already cached - bump this and
# the next run refetches instead of serving numbers the new code would not
# produce.
STATEMENTS_CACHE_VERSION = 4


def cagr(values: list) -> float:
    """Compound annual growth across the span the values actually cover.

    Identical to the Korea build: undefined when the base is zero or negative.
    A company that lost money three years ago has no meaningful growth RATE,
    and inventing one is worse than reporting nothing. The yearly figures
    always ship alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def major_ccy(ccy) -> tuple[str, float]:
    """(major currency, divisor) for a Yahoo quote currency."""
    c = str(ccy or "")
    return MINOR_CCY.get(c, (c, 1.0))


def _row(df: pd.DataFrame, names) -> pd.Series:
    """First matching statement row, indexed by fiscal year-end, oldest first."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index()
    return pd.Series(dtype=float)


def _at(series: pd.Series, when: pd.Timestamp, tolerance_days: int = 10) -> float:
    """Last value on or before `when`, if it is within `tolerance_days`. A
    fiscal year-end that falls in a data gap is missing, not the nearest price
    from months earlier."""
    if series is None or series.empty:
        return np.nan
    s = series.loc[:when].dropna()
    if s.empty or (when - s.index[-1]).days > tolerance_days:
        return np.nan
    return float(s.iloc[-1])


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_statement_record(inc: pd.DataFrame, bs: pd.DataFrame,
                           px: pd.Series, fx: pd.Series | None,
                           quote_ccy: str, fin_ccy: str,
                           close_now: float, mcap_now: float,
                           fin_years: int = 3, max_hist: int = 5,
                           asof: str | pd.Timestamp | None = None,
                           splits: pd.Series | None = None) -> dict:
    """Everything the two statement-based features need, from raw frames.

    Pure: no network, so test_france.py can exercise every trap offline. `px` is
    the daily close in QUOTE units (euros for every .PA line); `fx` converts the
    quote's MAJOR currency into the reporting currency (None when they are the
    same); `close_now` and `mcap_now` are in the quote's major currency.

    Every multiple is a market value over a reported total, never a price over
    a per-share figure. Yahoo's per-share rows are in reporting-currency units
    against a price in the quote currency (TotalEnergies: dollars against euros), and
    its EPS row is frequently rounded to zero; totals over totals sidestep both.

    Three-year history: the last `fin_years` filed years, oldest first, in
    millions of the REPORTING currency. Deliberately not converted - a growth
    rate should describe the business, not the dollar against the euro.

    Own history: for each filed year, that year-end market value over that
    year's filed figures, converted into the reporting currency at that
    year-end rate. Plus the latest filing's components, so the screen can put
    TODAY's market value over them on exactly the same definitions - see
    fr_filters.apply_history_screen for why that matters.
    """
    rec: dict = {"fin_ccy": fin_ccy or ""}

    rows = {k: _row(inc, v) for k, v in IS_ROWS.items()}
    rows.update({k: _row(bs, v) for k, v in BS_ROWS.items()})

    # A fiscal year counts as filed when it has revenue or net income. Yahoo
    # pads the frame with an extra, entirely empty oldest column (2021 in
    # every UK name checked, and in German, Polish and French ones too), and that must not be read
    # as a zero year.
    dates = sorted(set(rows["rev"].dropna().index) | set(rows["np"].dropna().index))
    if not dates:
        rec["hist_note"] = "no filed years"
        return rec

    # ---- three-year history ---------------------------------------------
    fy = dates[-fin_years:]
    rec["fin_years"] = ",".join(d.strftime("%Y") for d in fy)
    rec["fin_n"] = len(fy)
    for key in ("rev", "op", "ebitda", "np"):
        series = [_num(rows[key].get(d)) / 1e6 for d in fy]
        for i, v in enumerate(series, 1):
            rec[f"{key}_y{i}"] = round(v, 1) if np.isfinite(v) else np.nan
        rec[f"{key}_cagr"] = cagr(series)

    # ---- own history ------------------------------------------------------
    hy = dates[-max_hist:]
    rec["hist_years"] = ",".join(d.strftime("%Y") for d in hy)
    q_major, q_div = major_ccy(quote_ccy)
    same_ccy = bool(fin_ccy) and fin_ccy == q_major
    # No reporting currency is not the same as euros. Assuming it would
    # treat a dollar reporter's accounts as pounds and scale every historical
    # multiple by the exchange rate - a fake discount or premium of 20-35%.
    # Missing means no benchmark (invariant 2), never a guess.
    if not fin_ccy:
        rec["hist_note"] = "no reporting currency"

    def fx_at(when):
        if same_ccy:
            return 1.0
        return _at(fx, when) if fx is not None else np.nan

    shares = [_num(rows["shares"].get(d)) for d in hy]
    lf = hy[-1]

    # Earnings basis for the VALUATION: normalized where Yahoo has it for
    # every year in the window, reported otherwise - never a mix within one
    # company's series, because a history that switches definition part-way
    # compares a year with itself on two different measures (see IS_ROWS).
    def pick(norm_key, rep_key):
        n = rows[norm_key]
        if all(np.isfinite(_num(n.get(d))) for d in hy):
            return n, "normalized"
        return rows[rep_key], "reported"
    ni_row, ni_basis = pick("np_norm", "np")
    eb_row, _ = pick("ebitda_norm", "ebitda")
    rec["hist_earnings"] = ni_basis

    # Guard 0: a stale latest filing. Checked first, because every other
    # number in the record would be struck against it.
    if asof is not None and "hist_note" not in rec:
        age = (pd.Timestamp(asof).normalize() - lf).days
        if age > MAX_FILING_AGE_DAYS:
            rec["hist_note"] = "stale filings"

    # Guard 1: a share count that jumps between filings is a corporate action.
    # The price series is split-adjusted while filed counts are not
    # necessarily, so a market value built across the break is wrong by the
    # split ratio - which the screen would read as a huge discount.
    valid = [s for s in shares if np.isfinite(s) and s > 0]
    for a, b in zip(valid, valid[1:]):
        if not (SHARE_BREAK_BAND[0] <= b / a <= SHARE_BREAK_BAND[1]):
            rec["hist_note"] = "share-count break"
            break

    # Guard 2: today's price x latest filed shares against today's market cap,
    # allowing for a split or consolidation Yahoo has not restated yet.
    #
    # Yahoo's price series is adjusted for every split it records; its filed
    # share counts catch up later. Johnson Matthey consolidated 4-for-3 in Aug
    # 2026: the prices were rescaled at once, the FY2026 filing still carried
    # the old 167.9m shares against 125.9m today. The ratio here read 1.33,
    # inside the old 0.6-1.6 band, so every past market value was a third too
    # high and the history screen showed a ~20% discount to its own history
    # that did not exist. A 3:2 split would do the same in the other
    # direction. So, as the Japan build does: among "no change" and each
    # suffix of the ratios recorded since the filing, take the one that
    # explains today's count, restate every filed year by it, and refuse a gap
    # nothing explains.
    s_lf = _num(rows["shares"].get(lf))
    g = 1.0
    if "hist_note" not in rec and np.isfinite(s_lf) and s_lf > 0:
        mc = _num(mcap_now)
        ratio = (_num(close_now) * s_lf / mc) if mc else np.nan
        rec["basis_ratio"] = ratio
        if np.isfinite(ratio) and ratio > 0:
            fs = _splits_after(splits, lf)
            cands = [1.0] + [float(np.prod(fs[k:])) for k in range(len(fs))]
            g = min(cands, key=lambda c: abs(np.log(ratio * c)))
        if not (np.isfinite(ratio) and NOW_BASIS_BAND[0] <= ratio * g <= NOW_BASIS_BAND[1]):
            rec["hist_note"] = "price/share basis mismatch"
            g = 1.0
    # The factor the latest filing's per-share basis is off by: also what the
    # screen uses to check Yahoo's .info per-share fields (see the filters).
    rec["share_basis_g"] = g
    rec["lf_shares"] = s_lf
    shares = [s * g for s in shares]

    per, pbr, evx = [], [], []
    for d, sh in zip(hy, shares):
        p = _at(px, d) / q_div if px is not None else np.nan
        mv = p * sh * fx_at(d)        # year-end market value, reporting ccy
        ni, eq = _num(ni_row.get(d)), _num(rows["equity"].get(d))
        eb = _num(eb_row.get(d))
        debt, cash = _num(rows["debt"].get(d)), _num(rows["cash"].get(d))
        mi = _num(rows["mi"].get(d))
        mi = 0.0 if not np.isfinite(mi) else mi
        # Non-positive denominators become NaN here; bounds are applied by the
        # screen, which is also where a loss year drops out of the benchmark.
        per.append(mv / ni if np.isfinite(mv) and ni > 0 else np.nan)
        pbr.append(mv / eq if np.isfinite(mv) and eq > 0 else np.nan)
        ev = mv + debt - cash + mi
        evx.append(ev / eb if np.isfinite(ev) and eb > 0 else np.nan)

    if "hist_note" in rec:
        per = pbr = evx = []
    rec["hist_per"] = [round(v, 2) if np.isfinite(v) else None for v in per]
    rec["hist_pbr"] = [round(v, 3) if np.isfinite(v) else None for v in pbr]
    rec["hist_evx"] = [round(v, 2) if np.isfinite(v) else None for v in evx]

    # Latest filing's components, for today's multiples on the same basis.
    fx_last = np.nan
    if same_ccy:
        fx_last = 1.0
    elif fx is not None and not fx.dropna().empty:
        fx_last = float(fx.dropna().iloc[-1])
    rec.update({
        # On the same earnings basis as the history, or today's value would be
        # compared across two definitions - the thing this whole design avoids.
        "lf_ni": _num(ni_row.get(lf)), "lf_equity": _num(rows["equity"].get(lf)),
        "lf_ebitda": _num(eb_row.get(lf)), "lf_debt": _num(rows["debt"].get(lf)),
        "lf_cash": _num(rows["cash"].get(lf)), "lf_mi": _num(rows["mi"].get(lf)),
        # Today's quote-major -> reporting-currency rate, carried so the screen
        # does not have to look it up again.
        "fx_now": fx_last,
    })
    rec.setdefault("hist_note", "")
    return rec


def _json_safe(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (float, np.floating)):
            out[k] = float(v) if np.isfinite(float(v)) else None
        elif isinstance(v, np.integer):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def fetch_statements(snap: pd.DataFrame, cfg: ScreenConfig,
                     asof: str = "") -> pd.DataFrame:
    """Filed statements for the size-gated survivors. Invariant 7: this is the
    slowest work in the run, so it only ever sees names that already cleared
    every cheap filter.

    Three Yahoo calls per ticker (income statement, balance sheet, seven years
    of daily closes) plus one FX history per foreign reporting currency, paced
    like snapshot() and cached per session date for the same reason: a
    throttled statement call returns an empty frame, not an error, and an
    empty frame reads as "no history" - which quietly removes a name from the
    third screen instead of failing loudly.
    """
    import yfinance as yf

    tickers = snap["ticker"].tolist()
    path = os.path.join(cfg.cache_dir,
                        f"statements_v{STATEMENTS_CACHE_VERSION}_{asof or 'latest'}.json")
    cached: dict = {}
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= cfg.cache_ttl_hours:
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception as e:
                log.warning("  statements cache unreadable (%s), refetching", e)

    # FX: one history per reporting currency that differs from the quote's.
    pairs = set()
    for _, r in snap.iterrows():
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy")
        if isinstance(f, str) and f and f != q_major:
            pairs.add((q_major, f))
    fxs: dict = {}
    for q, f in sorted(pairs):
        try:
            h = yf.Ticker(f"{q}{f}=X").history(period="7y", interval="1d")
            s = h["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            fxs[(q, f)] = s
            log.info("  FX history %s->%s: %d days", q, f, len(s))
        except Exception as e:
            log.warning("  FX history %s->%s failed: %s - those reporters get "
                        "no own-history benchmark", q, f, e)

    todo = [t for t in tickers if t not in cached]
    rows = {t: r for t, r in snap.set_index("ticker").iterrows()}

    def one(t: str):
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
        r = rows[t]
        try:
            tk = yf.Ticker(t)
            inc, bs = tk.income_stmt, tk.balance_sheet
            h = tk.history(period="7y", interval="1d", auto_adjust=False)
        except Exception as e:
            log.debug("%s statements: %s", t, e)
            return t, None
        if inc is None or inc.empty:
            return t, None
        px = pd.Series(dtype=float)
        if h is not None and not h.empty:
            px = h["Close"].copy()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy") if isinstance(r.get("fin_ccy"), str) else ""
        rec = build_statement_record(
            inc, bs, px, fxs.get((q_major, f)), r.get("currency"), f,
            _num(r.get("close_local")), _num(r.get("market_cap_local")),
            asof=asof or None,
            splits=(h["Stock Splits"] if h is not None and "Stock Splits" in h else None))
        return t, _json_safe(rec)

    if todo:
        log.info("  statements: fetching %d tickers (%d cached)", len(todo),
                 len(tickers) - len(todo))
        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            for n, (t, rec) in enumerate(ex.map(one, todo), 1):
                if rec is not None:
                    cached[t] = rec
                if n % 50 == 0:
                    log.info("  statements %d/%d", n, len(todo))
        try:
            os.makedirs(cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cached, fh)
        except Exception as e:
            log.warning("  could not write statements cache: %s", e)

    got = [t for t in tickers if t in cached]
    log.info("  statements for %d/%d survivors", len(got), len(tickers))
    if len(got) < 0.8 * len(tickers):
        log.warning("  only %.0f%% have statements - Yahoo is probably "
                    "throttling. The two statement features will be thin; "
                    "re-run to fill the gaps from cache.",
                    100.0 * len(got) / max(len(tickers), 1))
    if not got:
        return pd.DataFrame(columns=["ticker"])
    out = pd.DataFrame([{"ticker": t, **cached[t]} for t in got])
    for k in ("hist_per", "hist_pbr", "hist_evx"):
        if k in out.columns:
            out[k] = out[k].map(lambda v: v if isinstance(v, list) else [])
    # fin_ccy also comes from the snapshot, and the snapshot's is the one kept.
    return out.drop(columns=["fin_ccy"], errors="ignore")
