"""France (Euronext Paris + Euronext Growth Paris) relative-valuation screener.

  python main_fr.py -v                       # full run
  python main_fr.py --skip-liquidity         # size alone gates
  python main_fr.py --min-adv 1e6            # admit thinner Paris lines
  python main_fr.py --keep-pyramid-parents   # keep Christian Dior next to LVMH
  python main_fr.py --include-secondary      # keep ArcelorMittal, Solvay, Aperam
  python main_fr.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd

import fr_filters as FF
from config_fr import ScreenConfig
from providers_fr import EuronextProvider
from screener import run_screen

D = ScreenConfig()      # argparse defaults come from here, so the two cannot drift


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="fr_screen_results.csv")
    p.add_argument("--min-mcap", type=float, default=D.min_market_cap_usd, help="USD")
    p.add_argument("--min-adv", type=float, default=D.min_adv_usd,
                   help="USD median daily traded value on Euronext Paris")
    p.add_argument("--discount", type=float, default=D.discount_threshold)
    p.add_argument("--min-metrics", type=int, default=D.min_metrics_passing)
    p.add_argument("--min-peers", type=int, default=D.min_peers)
    p.add_argument("--peer-keys", default=",".join(D.peer_keys))
    p.add_argument("--fallback-peer-keys", default=",".join(D.fallback_peer_keys))
    p.add_argument("--fx", type=float, help="USD per EUR (default: live)")
    p.add_argument("--include-investment-cos", action="store_true",
                   help="keep venture-capital and closed-end vehicles")
    p.add_argument("--include-reits", action="store_true",
                   help="keep SIICs (Unibail, Klepierre, Gecina...); their P/E carries "
                        "IAS 40 revaluations")
    p.add_argument("--include-secondary", action="store_true",
                   help="keep lines whose home market Euronext names as Amsterdam or "
                        "Brussels (ArcelorMittal, Solvay, Aperam)")
    p.add_argument("--include-cooperative", action="store_true",
                   help="keep the Credit Agricole regional banks' cooperative "
                        "certificates (CCI), which trade at ~0.25x book for want of a vote")
    p.add_argument("--keep-pyramid-parents", action="store_true",
                   help="keep a parent that consolidates a listed subsidiary "
                        "(Christian Dior over LVMH)")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--exclude-controlled", action="store_true",
                   help="drop companies whose insiders hold a majority")
    p.add_argument("--adv-days", type=int, default=D.adv_lookback_days)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=D.min_roe_pct,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=D.abs_max_pbr)
    p.add_argument("--abs-ev", type=float, default=D.abs_max_ev_ebitda)
    p.add_argument("--abs-strict-financials", action="store_true")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true")
    p.add_argument("--coe", type=float, default=D.abs_cost_of_equity_pct)
    p.add_argument("--abs-min-div", type=float, default=D.abs_min_div_yield)
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-history screen")
    p.add_argument("--hist-discount", type=float, default=D.hist_min_discount)
    p.add_argument("--hist-min-metrics", type=int, default=D.hist_min_metrics)
    p.add_argument("--dashboard", default="fr_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just passes")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


# Yahoo's long names end in the legal form ("TotalEnergies SE", "Société
# anonyme"). It says nothing a reader needs.
LEGAL_FORM = re.compile(r"[\s,]+(soci[eé]t[eé] (anonyme|europ[eé]enne|en commandite par actions)"
                        r"( [aà] (directoire|conseil d'administration)[^,]*)?|soci[eé]t[eé] coop[eé]rative"
                        r"|s\.?\s?a\.?|se|s\.?c\.?a\.?|n\.?v\.?|plc|inc\.?|sca|s\.?e\.?)\s*$",
                        re.IGNORECASE)


def display_name(yahoo: str, euronext: str) -> str:
    """Yahoo's readable casing ('Société Générale') over Euronext's upper case
    ('SOCIETE GENERALE'), legal form removed."""
    n = str(yahoo or "").strip() or str(euronext or "").strip()
    for _ in range(2):
        n = LEGAL_FORM.sub("", n).strip()
    return n


def main() -> int:
    a = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        # French names on a Windows console whose code page cannot print them.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    for noisy in ("urllib3", "peewee", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("fr")

    keys = lambda s: tuple(k.strip() for k in s.split(",") if k.strip())  # noqa: E731
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=keys(a.peer_keys),
        fallback_peer_keys=keys(a.fallback_peer_keys),
        exclude_investment_companies=not a.include_investment_cos,
        exclude_reits=not a.include_reits,
        exclude_property=not a.include_reits,
        exclude_secondary=not a.include_secondary,
        exclude_cooperative_certs=not a.include_cooperative,
        exclude_pyramid_parents=not a.keep_pyramid_parents,
        exclude_holdcos=a.exclude_holdcos,
        exclude_controlled=a.exclude_controlled,
        adv_lookback_days=a.adv_days,
    )

    prov = EuronextProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    log.info("as of %s", asof)

    # 1. roster: Euronext's own download, one request
    log.info("building roster...")
    roster = prov.listing_roster()
    if roster.empty:
        log.error("empty roster - Euronext's download did not answer or did not parse")
        return 1
    log.info("roster: %d lines (%s)", len(roster),
             ", ".join(f"{b}={n}" for b, n in roster["board"].value_counts().items()))

    # 2. fundamentals: one yfinance .info per line (invariant 7 as the UK,
    #    Germany and Poland read it - .info returns market cap and every
    #    multiple together).
    log.info("fetching fundamentals for %d tickers...", len(roster))
    snap = prov.snapshot(roster["ticker"].tolist(), asof)

    # 2b. lines Yahoo does not know under Euronext's symbol, resolved by ISIN
    priced = set(snap.loc[snap["market_cap_local"].notna(), "ticker"])
    miss = roster[~roster["ticker"].isin(priced)]
    resolved = 0
    if not miss.empty:
        remap = prov.resolve_symbols(miss)
        for attempt in ("", "|alt"):
            m = {old: remap[old + attempt] for old in miss["ticker"]
                 if old + attempt in remap and old not in priced}
            if not m:
                continue
            s2 = prov.snapshot(list(m.values()), asof)
            ok = s2[s2["market_cap_local"].notna()]
            back = {v: k for k, v in m.items()}
            for new in ok["ticker"]:
                old = back[new]
                roster.loc[roster["ticker"] == old, "ticker"] = new
                priced.add(old)
                resolved += 1
            snap = pd.concat([snap[~snap["ticker"].isin(ok["ticker"])], ok],
                             ignore_index=True)

    df = roster.merge(snap, on="ticker", how="left")
    n_priced = int(df["market_cap_local"].notna().sum())
    log.info("  %d of %d lines priced (%d via ISIN resolution)", n_priced, len(df), resolved)
    # Refuse rather than screen half the market. Rights, warrants and
    # suspended lines are the healthy misses (~50 of ~620).
    if n_priced < 0.5 * len(df):
        log.error("only %d of %d lines priced. Yahoo is rate-limiting; the cache "
                  "has kept what arrived, so re-running in a few minutes will fill "
                  "the rest. Refusing to screen a partial universe.", n_priced, len(df))
        return 2
    ustats: dict = {"roster": len(roster), "priced": n_priced,
                    "priced_via_isin": resolved,
                    "mcap_via_fast_info": int((df["mcap_source"] == "fast_info").sum())}

    # 3. roster-level hygiene: foreign secondaries, fund quotes
    df, rstats = FF.apply_roster_filters(df, cfg)
    ustats.update(rstats)

    usd_per_eur = a.fx if a.fx else prov.eur_to_usd()
    log.info("FX: 1 EUR = %.4f USD", usd_per_eur)

    # 4. size gate
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * usd_per_eur
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    ustats["cleared_market_cap"] = len(pre)
    log.info("%d of %d lines cleared USD %.0fm", len(pre), len(df),
             cfg.min_market_cap_usd / 1e6)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 5. ICB and sub type from Euronext's factsheets, size survivors only -
    #    two requests per company, a week's cache.
    sheets = prov.factsheets(pre["isin"].tolist())
    pre = FF.attach_icb(pre, sheets)
    pre, ustats = FF.apply_icb_filters(pre, cfg, ustats)
    # Every hygiene drop, counted against the whole roster, so the funnel
    # reads as one narrowing line even though ICB is only read past the gate.
    drops = sum(v for k, v in ustats.items() if k.startswith("dropped_"))
    ustats["after_french_filters"] = len(roster) - drops

    # 6. liquidity: one batched price download, fetched even with
    #    --skip-liquidity so the measured value reaches the page.
    log.info("fetching %d days of prices for %d names...", cfg.adv_lookback_days, len(pre))
    panel = prov.price_panel(pre["ticker"].tolist(), cfg.adv_lookback_days)
    adv = prov.average_daily_value(panel, pre["ticker"].tolist(), cfg.adv_lookback_days)
    pre = pre.merge(adv, on="ticker", how="left")
    pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * usd_per_eur

    # 6b. one line per issuer (Robertet's certificates) - before the gate,
    #     so the traded line is kept whichever of them clears it.
    pre, lstats = FF.keep_one_line_per_issuer(pre)
    ustats.update(lstats)
    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    ustats["cleared_size_liquidity"] = len(pre)
    log.info("%d cleared size/liquidity", len(pre))
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 7. control flag (Yahoo's insider holding)
    pre, cstats = FF.apply_control(pre, cfg)
    ustats.update(cstats)

    # 8. filed statements, survivors only (invariant 7)
    if not (a.no_financials and a.no_history):
        from providers_fr import fetch_statements
        log.info("fetching filed statements for %d names...", len(pre))
        st = fetch_statements(pre, cfg, asof)
        if len(st.columns) > 1:
            pre = pre.merge(st, on="ticker", how="left")

    # 9. pyramids - a parent consolidating a listed subsidiary. Needs the
    #    filed revenue, so it runs after the statements and before the peer
    #    medians, which must not count one set of earnings twice.
    if "rev_y1" in pre.columns:
        pre, pstats = FF.flag_pyramids(pre, cfg)
        ustats.update(pstats)
    if a.no_financials:
        pre = pre.drop(columns=[c for c in pre.columns
                                if c.startswith(("rev_", "op_", "ebitda_", "np_",
                                                 "fin_years", "fin_n"))])

    # 10. .info onto one basis: splits first (its test reads filed equity in
    #     the filing's currency), then the currency repair.
    if "share_basis_g" in pre.columns:
        pre, sstats = FF.restate_info_for_splits(pre)
        ustats.update(sstats)
    foreign = pre[pre["fin_ccy"].notna() & pre["fin_ccy"].ne(pre["currency"])]
    qeq = prov.quarterly_equity(foreign["ticker"].tolist()) if not foreign.empty else {}
    pre, fxstats = FF.repair_foreign_reporters(pre, prov.eur_per(foreign["fin_ccy"].unique()), qeq)
    ustats.update(fxstats)

    # 11. screen
    res, stats = run_screen(pre, usd_per_eur, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = FF.add_quality_context(res)
    res = FF.add_valueup_flags(res)
    res = FF.flag_ttm_vs_filed(res)
    if cfg.min_roe_pct > 0:
        res, roestats = FF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = FF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = FF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    res["name_exchange"] = res["name"]
    yn = res.get("yf_name", pd.Series(index=res.index, dtype=object))
    res["name"] = [display_name(y if isinstance(y, str) else "", g)
                   for y, g in zip(yn, res["name_exchange"])]

    funnel = {**ustats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<28} {v}")

    out = res if a.all else res[res["passes_any"]]
    cols = [c for c in FF.fr_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res["passes_any"]]
    print(f"\n--- {len(hits)} stock(s) passing at least one screen ---")
    if not hits.empty:
        show = hits[["symbol", "name", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(40).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: f"{x:.1%}" if pd.notna(x) else "")
        show["name"] = show["name"].str.slice(0, 28)
        show["industry"] = show["industry"].str.slice(0, 24)
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "source": "euronext+yfinance", "board": "MAIN",
        "roster_asof": prov.roster_asof,
        "cmd": "python main_fr.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "usd_per_eur": round(usd_per_eur, 4),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard
            build_dashboard(a.out, meta_path, a.dashboard)
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:                            # noqa: BLE001
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
