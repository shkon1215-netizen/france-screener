"""Pre-flight diagnostic: tests every live call the France screen makes.

    python check_setup.py

Run this before main_fr.py. Nearly every failure in this build is a SILENT
one - a renamed symbol reads as "too small", a throttled Yahoo as a market
where companies vanished, a changed factsheet as a market with no sectors,
and a currency convention Yahoo changes as a 12% error in a dollar
reporter's multiples - and a full run is slow enough that finding out
afterwards is expensive. Each check here names what it protects against.
"""
from __future__ import annotations

import logging
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  OK    {name:<44} {detail}  ({time.time() - t0:.1f}s)")
            except Exception as e:                                # noqa: BLE001
                RESULTS.append((name, False, str(e)))
                print(f"  FAIL  {name:<44} {e}")
        return run
    return deco


def _prov():
    from config_fr import ScreenConfig
    from providers_fr import EuronextProvider
    return EuronextProvider(ScreenConfig())


@check("Euronext download (roster)")
def roster():
    r = _prov().listing_roster()
    if len(r) < 500:
        raise RuntimeError(f"only {len(r)} lines - the download URL or format changed?")
    for sym in ("TTE", "MC", "AIR", "BNP", "STMPA"):
        if sym not in set(r["symbol"]):
            raise RuntimeError(f"{sym} missing from the roster")
    sec = set(r.loc[r["secondary"].eq("1"), "symbol"])
    if "MT" not in sec:
        raise RuntimeError("ArcelorMittal no longer reads as Amsterdam-home - the market "
                           "string changed, and the foreign-secondary rule reads it")
    boards = r["board"].value_counts().to_dict()
    return f"{len(r)} lines ({boards}), {len(sec)} with a home market elsewhere"


@check("Euronext factsheet (ICB + sub type)")
def factsheet():
    import requests
    from providers_fr import BLOCK_URL, HEADERS, parse_icb_block, parse_info_block
    icb = parse_icb_block(requests.get(BLOCK_URL.format(isin="FR0000120271", block="fs_icb_block"),
                                       headers=HEADERS, timeout=30).text)
    if icb.get("icb_subsector_code") != "60101000":
        raise RuntimeError(f"TotalEnergies ICB read as {icb} - the block's layout changed; "
                           "every company would screen without peers")
    sub = parse_info_block(requests.get(BLOCK_URL.format(isin="FR0000044323", block="fs_info_block"),
                                        headers=HEADERS, timeout=30).text)
    if "certificate" not in sub.get("subtype", "").lower():
        raise RuntimeError(f"a CRCAM CCI's sub type read as {sub} - the cooperative-certificate "
                           "exclusion reads it")
    return f"TotalEnergies -> {icb['icb_sector']}; CRCAM Alpes Provence -> {sub['subtype']}"


@check("Yahoo .info on a Paris line (rate limit)")
def info():
    import yfinance as yf
    i = yf.Ticker("BNP.PA").info
    if not i.get("marketCap") or not i.get("trailingPE"):
        raise RuntimeError("fields missing - Yahoo is throttling this IP; wait and retry")
    if i.get("currency") != "EUR":
        raise RuntimeError(f"currency {i.get('currency')!r}, expected EUR")
    return f"BNP Paribas mcap EUR {i['marketCap'] / 1e9:.0f}bn, P/E {i['trailingPE']:.1f}"


@check("currency convention on a dollar reporter")
def fxconv():
    """repair_foreign_reporters tests every field per row, so a convention
    change makes it refuse rather than mis-repair - but this says why before
    the run does. Vallourec is the reporter whose book value Yahoo leaves in
    dollars; TotalEnergies' it converts."""
    import yfinance as yf
    i = yf.Ticker("VK.PA").info
    if i.get("financialCurrency") != "USD" or i.get("currency") != "EUR":
        raise RuntimeError(f"Vallourec is {i.get('currency')}/{i.get('financialCurrency')}, "
                           "expected EUR quote / USD accounts - pick another reporter")
    r = _prov().eur_per(["USD"])["USD"]
    k_eps = i["trailingEps"] * i["sharesOutstanding"] / i["netIncomeToCommon"]
    ev_mc = i["enterpriseValue"] / i["marketCap"]
    if not 0.25 < ev_mc < 4:
        raise RuntimeError(f"EV / market cap {ev_mc:.2f} - EV is no longer in euros")
    return f"1 USD = {r:.3f} EUR; EPS x shares / NI {k_eps:.3f} (euro EPS), EV/mcap {ev_mc:.2f}"


@check("ISIN -> Yahoo symbol resolution")
def isin():
    import yfinance as yf
    q = yf.Search("FR0000120271", max_results=8, news_count=0).quotes or []
    syms = [x.get("symbol") for x in q]
    if "TTE.PA" not in syms:
        raise RuntimeError(f"TotalEnergies' ISIN resolved to {syms} - renamed lines will "
                           "drop out as unpriced")
    return f"TotalEnergies -> {syms[:4]}"


@check("batched price download (liquidity)")
def download():
    import yfinance as yf
    px = yf.download(["TTE.PA", "BNP.PA", "RBT.PA"], period="30d", interval="1d",
                     progress=False, auto_adjust=False, group_by="column")
    if px.empty or "Volume" not in px.columns.get_level_values(0):
        raise RuntimeError("no Close/Volume panel")
    n = int(px["Volume"].notna().sum().min())
    if n < 10:
        raise RuntimeError(f"only {n} sessions of volume")
    return f"{n} sessions for 3 tickers"


@check("filed and quarterly statements")
def statements():
    import yfinance as yf
    tk = yf.Ticker("MC.PA")
    inc, bs, q = tk.income_stmt, tk.balance_sheet, tk.quarterly_balance_sheet
    need_i = ("Total Revenue", "Operating Income", "Net Income Common Stockholders")
    need_b = ("Common Stock Equity", "Ordinary Shares Number", "Minority Interest")
    miss = [k for k in need_i if k not in inc.index] + [k for k in need_b if k not in bs.index]
    if miss:
        raise RuntimeError(f"rows missing {miss} - Yahoo renamed statement lines "
                           "(the pyramid test reads operating income and minorities)")
    if q is None or "Common Stock Equity" not in q.index:
        raise RuntimeError("no quarterly equity - the currency repair's book test needs it")
    return f"{inc.shape[1]} annual columns, {q.shape[1]} quarterly"


@check("FX EUR->USD")
def fx():
    r = _prov().eur_to_usd()
    if r == 1.17:
        raise RuntimeError("both live sources failed - hardcoded fallback in use")
    return f"1 EUR = {r:.4f} USD"


def main() -> int:
    print("France screener pre-flight\n")
    for fn in (roster, factsheet, info, fxconv, isin, download, statements, fx):
        fn()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + ("all checks passed - run python main_fr.py -v"
                  if not bad else f"{len(bad)} check(s) failed: {', '.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
