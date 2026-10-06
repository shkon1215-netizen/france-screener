"""Offline validation of the France screener. No network required.

Run: python test_france.py

Every planted case is something a naive screener gets wrong, and most were
measured on the first live run (2026-10-06): the Credit Agricole regional
banks' cooperative certificates at a quarter of book, a holding company that
consolidates its listed subsidiary, ten unrelated pairs of companies with
the same revenue, and a dollar reporter whose book value Yahoo has ALREADY put
into euros next to one where it has not.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config_fr as K
import fr_filters as FF
from config_fr import ScreenConfig
from providers_fr import parse_icb_block, parse_info_block, parse_roster_csv, to_yahoo
from screener import run_screen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

rng = np.random.default_rng(7)

USD_PER_EUR = 1.13
MCAP = 2.0e9           # ~USD 2.3bn, clears the gate
ADV = 5.0e6            # ~USD 5.7m, clears the gate

FAILURES: list[str] = []

# ICB: (subsector code, sector name, supersector name)
ICB = {
    "ind": ("50204000", "Industrial Engineering", "Industrial Goods and Services"),
    "sw": ("10101015", "Software and Computer Services", "Technology"),
    "bank": ("30101010", "Banks", "Banks"),
    "am": ("30202010", "Investment Banking and Brokerage Services", "Financial Services"),
    "reit": ("35102030", "Real Estate Investment Trusts", "Real Estate"),
    "dev": ("35101010", "Real Estate Investment and Services", "Real Estate"),
    "lux": ("40204020", "Personal Goods", "Consumer Products and Services"),
    "build": ("50101010", "Construction and Materials", "Construction and Materials"),
    "elec": ("50202010", "Electronic and Electrical Equipment", "Industrial Goods and Services"),
}


ICB_INDUSTRY = {"10": "Technology", "30": "Financials", "35": "Real Estate",
                "40": "Consumer Discretionary", "50": "Industrials"}


def ok(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(label)


def base(icb="ind", **kw):
    code, sector6, super4 = ICB[icb]
    r = dict(name="", symbol="", isin="", board="MAIN", market="Euronext Paris",
             secondary="", country="FR", quote_type="EQUITY", subtype="Ordinary Shares",
             icb=code, industry=sector6, sector=super4, subsector="",
             icb_industry=ICB_INDUSTRY[code[:2]],
             market_cap_local=MCAP, adv_local=ADV, close_local=50.0,
             currency="EUR", fin_ccy="EUR", yf_name="",
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=4.0, book_value_ps=32.0, div_yield=1.5, yf_industry="")
    r.update(kw)
    r["ticker"] = to_yahoo(r["symbol"])
    if not r["isin"]:
        r["isin"] = "FR" + (r["symbol"] + "XXXXXXXX")[:8] + "01"
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    specs = [("ind", 15.0, 1.8, 9.0, 12), ("sw", 30.0, 4.5, 18.0, 10),
             ("bank", 9.0, 0.9, np.nan, 8)]
    n = 0
    for icb, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(icb, symbol=f"P{n:02d}", name=f"PAIR {n}",
                             trailing_pe=pe * rng.uniform(0.93, 1.09),
                             price_to_book=pb * rng.uniform(0.93, 1.09),
                             ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                             trailing_eps=pb / pe * 32.0 * rng.uniform(0.95, 1.05),
                             market_cap_local=MCAP * rng.uniform(0.9, 2.5),
                             adv_local=ADV * rng.uniform(0.9, 2.5)))

    # PASS: genuinely cheap vs its engineering peers, good ROE, a dividend.
    rows.append(base(symbol="GOOD", name="VRAIE VALEUR", trailing_pe=8.5, price_to_book=0.75,
                     ev_to_ebitda=5.0, trailing_eps=5.0, book_value_ps=40.0, div_yield=4.2))
    # FAIL: Credit Agricole regional bank's CCI - a quarter of book, for want
    # of a vote. Euronext calls it a certificate; the name says cooperative.
    rows.append(base("bank", symbol="CRAV", name="CRCAM ATL.VEND.CCI",
                     subtype="Depositary Receipt / Certificate",
                     yf_name="Caisse régionale de Crédit Agricole Mutuel Atlantique Vendée",
                     trailing_pe=8.5, price_to_book=0.20, trailing_eps=14.0, book_value_ps=600.0))
    # KEEP: SES's fiduciary certificates - also "Depositary Receipt /
    # Certificate", but the company's only line and not a cooperative.
    rows.append(base("elec", symbol="SESG", name="SES", isin="LU0088087324", country="LU",
                     subtype="Depositary Receipt / Certificate", yf_name="SES S.A.",
                     trailing_pe=14.0, price_to_book=1.0, ev_to_ebitda=6.0))
    # FAIL: venture-capital vehicle (Altamir), by Euronext's own sub type,
    # filed by ICB with the asset managers.
    rows.append(base("am", symbol="LTA", name="ALTAMIR", subtype="Venture Capital Fund Shares",
                     trailing_pe=4.0, price_to_book=0.6, trailing_eps=4.0, book_value_ps=40.0))
    # FAIL: SIIC. P/E of 3.5 from an IAS 40 revaluation.
    rows.append(base("reit", symbol="SIIC", name="FONCIERE DE TEST", trailing_pe=3.5,
                     price_to_book=0.6, ev_to_ebitda=18.0, trailing_eps=14.0,
                     book_value_ps=110.0))
    # KEEP: a developer - ICB 351010, an earnings multiple. Alone in its
    # sector and supersector, so unscored rather than benchmarked against banks.
    rows.append(base("dev", symbol="DEVL", name="PROMOTEUR", trailing_pe=8.9,
                     price_to_book=1.2, ev_to_ebitda=8.1, div_yield=6.3))
    # FAIL: home market elsewhere (the ArcelorMittal shape).
    rows.append(base("build", symbol="MT", isin="LU1598757687", name="ARCELORMITTAL SA",
                     country="LU", market="Euronext Amsterdam, Paris", secondary="1",
                     trailing_pe=7.0, price_to_book=0.5, ev_to_ebitda=4.0))
    # KEEP: foreign-domiciled, Paris home market (the Airbus shape).
    rows.append(base("sw", symbol="AIRX", isin="NL0000235190", name="AIRBUS", country="NL",
                     trailing_pe=29.0, price_to_book=4.6, ev_to_ebitda=17.0))
    # TAG, not drop: a named holding company (Wendel). Cheap, low ROE.
    rows.append(base("am", symbol="MF", isin="FR0000121204", name="WENDEL",
                     trailing_pe=9.0, price_to_book=0.55, trailing_eps=0.8,
                     book_value_ps=40.0, div_yield=6.5))
    # FAIL: loss-maker. Yahoo sometimes reports P/E 0; it must read as missing.
    rows.append(base(symbol="LOSS", name="PERTES", trailing_pe=0.0, price_to_book=0.6,
                     trailing_eps=-2.0, book_value_ps=30.0))
    # A bank: EV/EBITDA suppressed even though Yahoo gives one, and it clears
    # the absolute screen via the P/B + ROE carve-out.
    rows.append(base("bank", symbol="BANK", name="BANQUE BON MARCHE", trailing_pe=6.0,
                     price_to_book=0.8, ev_to_ebitda=4.0, trailing_eps=4.0,
                     book_value_ps=30.0, div_yield=7.0))
    # Alone in its ICB sector; Industrial Goods and Services is populated, so
    # it falls back to the supersector.
    rows.append(base("elec", symbol="ELEC", name="ELECTRIQUE", trailing_pe=16.0,
                     price_to_book=1.9, ev_to_ebitda=9.5))
    # Alone at both levels: unscored.
    rows.append(base("lux", symbol="LONE", name="LUXE SEUL", trailing_pe=16.0,
                     price_to_book=1.9, ev_to_ebitda=9.5))
    # FAIL (size): too small.
    rows.append(base(symbol="TINY", name="PETITE", market_cap_local=3e8, trailing_pe=6.0,
                     price_to_book=0.5, ev_to_ebitda=3.0))
    return pd.DataFrame(rows)


def check_parsers():
    print("=== Euronext parsers and helpers ===")
    csv_text = ("﻿Name;ISIN;Symbol;Market;Currency;\"Open Price\";\"High Price\";\"low Price\";"
                "\"last Price\";\"last Trade MIC Time\";\"Time Zone\";Volume;Turnover;"
                "\"Closing Price\";\"Closing Price DateTime\"\n"
                "\"European Equities\"\n\"06 Oct 2026\"\n"
                "\"All datapoints provided as of end of last active trading day.\"\n"
                "TOTALENERGIES;FR0000120271;TTE;\"Euronext Paris, Brussels\";EUR;74;75;74;74.8;"
                "\"05/10/2026 17:35\";CET;3431000;256689904.70;74.8;05/10/2026\n"
                "\"ARCELORMITTAL SA\";LU1598757687;MT;\"Euronext Amsterdam, Paris\";EUR;30;31;30;"
                "30.5;\"05/10/2026 17:35\";CET;2500000;77160364.77;30.5;05/10/2026\n"
                "\"AB SCIENCE BSA\";FR001400ZRT0;ABBS;\"Euronext Paris\";EUR;-;-;-;-;-;CET;-;-;-;-\n")
    df = parse_roster_csv(csv_text)
    ok(len(df) == 3 and df.attrs["asof"] == "06 Oct 2026", "roster CSV: banner rows skipped, date read")
    ok(K.board_of("Euronext Paris, Brussels") == "MAIN" and K.board_of("Euronext Growth Paris") == "GROWTH",
       "board read from the home venue")
    ok(K.is_secondary("Euronext Amsterdam, Paris") and not K.is_secondary("Euronext Paris, Amsterdam"),
       "home market elsewhere -> secondary; Paris first -> home")
    ok(to_yahoo("TTE") == "TTE.PA" and to_yahoo("2CRSI") == "2CRSI.PA", "Euronext symbol -> .PA")

    icb = parse_icb_block("""<div><strong>Industry</strong><span>60, Energy</span>
        <strong>SuperSector</strong><span>6010, Energy</span><strong>Sector</strong>
        <span>601010, Oil, Gas and Coal</span><strong>Subsector</strong>
        <span>60101000, Integrated Oil and Gas</span><button>Help</button></div>""")
    ok(icb.get("icb_sector") == "Oil, Gas and Coal" and icb.get("icb_subsector_code") == "60101000"
       and icb.get("icb_supersector") == "Energy",
       f"ICB block parsed, commas inside a name kept ({icb.get('icb_sector')!r})")
    ok(parse_info_block("<td>Sub type</td><td> Depositary Receipt / Certificate </td>"
                        "<td>Market</td>") == {"subtype": "Depositary Receipt / Certificate"},
       "sub type parsed")
    ok(K.issuer_key("ROBERTET CI") == K.issuer_key("ROBERTET") == K.issuer_key("ROBERTET CDV 87"),
       "Robertet's three lines are one issuer")
    ok(K.issuer_key("SOCIETE GENERALE") != K.issuer_key("SOCIETE BIC"),
       "two words of name keep unrelated issuers apart")
    ok(K.is_financial("Banks") and K.is_financial(icb="30202010") and not K.is_financial(icb="35101010"),
       "financials by ICB industry 30; real estate (35) is not one")
    ok(K.is_cooperative_cert("Depositary Receipt / Certificate", "CRCAM PARIS ET IDF")
       and not K.is_cooperative_cert("Depositary Receipt / Certificate", "SES", "SES S.A.")
       and not K.is_cooperative_cert("Ordinary Shares", "CRCAM X"),
       "cooperative certificate needs BOTH the certificate sub type and a cooperative name")


def check_pyramids():
    print("\n=== pyramids ===")
    cfg = ScreenConfig()

    def row(name, rev, op, eq, mi, ccy="EUR", fy="2023,2024,2025"):
        return dict(name=name, ticker=name[:4] + ".PA", rev_y3=rev, op_y3=op,
                    lf_equity=eq, lf_mi=mi, fin_ccy=ccy, fin_years=fy, is_holdco=False)
    df = pd.DataFrame([
        # Christian Dior / LVMH: identical revenue and operating profit; Dior's
        # equity is mostly LVMH's other shareholders.
        row("LVMH", 80807.0, 17668.0, 67.5e9, 1.5e9),
        row("CHRISTIAN DIOR", 80807.0, 17650.0, 25.0e9, 40.0e9),
        # Accor and Sopra Steria: revenue within 0.2%, nothing else in common.
        row("ACCOR", 5639.0, 900.0, 4.0e9, 0.3e9),
        row("SOPRA STERIA", 5648.0, 450.0, 2.5e9, 0.05e9),
        # Same revenue and margin by chance, neither carrying minorities.
        row("TWIN A", 3000.0, 300.0, 2.0e9, 0.0),
        row("TWIN B", 3010.0, 302.0, 2.2e9, 0.0),
    ])
    out, stats = FF.flag_pyramids(df, cfg)
    names = set(out["name"])
    ok(stats["pyramid_pairs"] == 1 and "CHRISTIAN DIOR" not in names and "LVMH" in names,
       f"Dior dropped as LVMH's consolidating parent, and only that pair ({stats})")
    ok(out.set_index("name").loc["LVMH", "pyramid_child_of"] == "CHRISTIAN DIOR",
       "LVMH tagged with its listed parent")
    ok({"ACCOR", "SOPRA STERIA", "TWIN A", "TWIN B"} <= names,
       "a revenue coincidence is not a pyramid (the first run's ten false pairs)")


def check_currency():
    print("\n=== currency repair (euro quote, dollar accounts) ===")
    R = 0.888                       # EUR per USD
    shares = 100e6
    common = dict(currency="EUR", fin_ccy="USD", close_local=50.0,
                  market_cap_local=50.0 * shares, yf_shares=shares, yf_roe=0.15,
                  lf_shares=shares, share_basis_g=1.0)
    rows = [
        # TotalEnergies shape: EPS in euros (k = R), book ALREADY in euros
        # against quarterly USD equity (k = R) -> P/B untouched; EV in euros
        # over USD EBITDA -> repaired.
        dict(symbol="TTEX", ticker="TTEX.PA", **common, trailing_eps=5.0 * R,
             trailing_pe=50.0 / (5.0 * R), yf_ni=500e6, book_value_ps=30.0 * R,
             price_to_book=50.0 / (30.0 * R), lf_equity=3.2e9,
             yf_ev=6e9, yf_ebitda=1e9, ev_to_ebitda=6.0),
        # Vallourec shape: book in DOLLARS (k = 1) -> P/B repaired.
        dict(symbol="VKX", ticker="VKX.PA", **common, trailing_eps=5.0 * R,
             trailing_pe=50.0 / (5.0 * R), yf_ni=500e6, book_value_ps=30.0,
             price_to_book=50.0 / 30.0, lf_equity=3.2e9,
             yf_ev=6e9, yf_ebitda=1e9, ev_to_ebitda=6.0),
        # Maurel & Prom shape: EPS ratio 0.828, 7% off the rate (four quarters
        # at four rates) - still read as euros, not refused.
        dict(symbol="MAUX", ticker="MAUX.PA", **common, trailing_eps=5.0 * 0.828,
             trailing_pe=50.0 / (5.0 * 0.828), yf_ni=500e6, book_value_ps=30.0 * R,
             price_to_book=50.0 / (30.0 * R), lf_equity=3.2e9,
             yf_ev=6e9, yf_ebitda=1e9, ev_to_ebitda=6.0),
        # STMicro shape: EPS rounded to 0.03 - ratio 0.06, evidence of nothing.
        dict(symbol="STMX", ticker="STMX.PA", **common, trailing_eps=0.03,
             trailing_pe=1725.0, yf_ni=466e6, book_value_ps=30.0 * R,
             price_to_book=50.0 / (30.0 * R), lf_equity=3.2e9,
             yf_ev=6e9, yf_ebitda=1e9, ev_to_ebitda=6.0),
        # A Swiss-franc reporter: 1 CHF ~ 1.07 EUR, too close to tell apart.
        dict(symbol="CHFX", ticker="CHFX.PA", **{**common, "fin_ccy": "CHF"},
             trailing_eps=5.0, trailing_pe=10.0, yf_ni=500e6, book_value_ps=30.0,
             price_to_book=1.67, lf_equity=3e9, yf_ev=6e9, yf_ebitda=1e9, ev_to_ebitda=6.0),
        # A euro reporter: untouched.
        dict(symbol="EURX", ticker="EURX.PA", currency="EUR", fin_ccy="EUR", close_local=50.0,
             market_cap_local=5e9, yf_shares=shares, trailing_eps=5.0, trailing_pe=10.0,
             yf_ni=500e6, yf_roe=0.15, book_value_ps=33.0, price_to_book=1.5, yf_ev=6e9,
             yf_ebitda=1e9, ev_to_ebitda=6.0, lf_equity=3.3e9, lf_shares=shares,
             share_basis_g=1.0),
    ]
    q_equity = {"TTEX.PA": 3.0e9, "VKX.PA": 3.0e9, "MAUX.PA": 3.0e9, "STMX.PA": 3.0e9}
    df, stats = FF.repair_foreign_reporters(pd.DataFrame(rows),
                                            {"EUR": 1.0, "USD": R, "CHF": 1.07}, q_equity)
    i = df.set_index("symbol")
    ok(abs(i.loc["TTEX", "price_to_book"] - 50.0 / (30.0 * R)) < 1e-9,
       "book already in euros: P/B left alone (assuming Poland's behaviour would break it)")
    ok(abs(i.loc["TTEX", "ev_to_ebitda"] - 6e9 / (1e9 * R)) < 1e-9,
       f"euro EV over dollar EBITDA repaired ({i.loc['TTEX', 'ev_to_ebitda']:.2f})")
    ok(abs(i.loc["VKX", "price_to_book"] - 50.0 / (30.0 * R)) < 1e-9,
       f"book in dollars: P/B repaired ({i.loc['VKX', 'price_to_book']:.3f})")
    ok(pd.notna(i.loc["MAUX", "trailing_pe"]),
       "an EPS ratio 7% off the rate is still read as euros, not refused")
    ok(np.isnan(i.loc["STMX", "trailing_pe"]) and pd.notna(i.loc["STMX", "price_to_book"]),
       "a rounded EPS refuses the P/E only")
    ok(i.loc["CHFX", ["trailing_pe", "price_to_book", "ev_to_ebitda"]].isna().all()
       and i.loc["CHFX", "fx_note"] == "fx basis untestable",
       "a currency near 1 EUR cannot be tested -> refused, not guessed")
    ok(i.loc["EURX", "price_to_book"] == 1.5 and i.loc["EURX", "fx_note"] == "",
       "euro reporter untouched")
    ok(stats["fx_pb_repaired"] == 1 and stats["fx_pe_refused"] == 1,
       f"funnel counts the repair ({stats})")


def check_statements():
    from providers_fr import build_statement_record
    print("\n=== filed statements ===")
    years = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31",
                            "2024-12-31", "2025-12-31"])

    def stmt(rows: dict) -> pd.DataFrame:
        df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=years).T
        return df[df.columns[::-1]]

    inc = stmt({"Total Revenue": (1000e6, 1100e6, 1200e6, 1300e6),
                "Operating Income": (150e6, 160e6, 170e6, 180e6),
                "EBITDA": (200e6, 210e6, 220e6, 230e6),
                "Net Income Common Stockholders": (100e6, 110e6, 120e6, 130e6)})
    bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": (1e8,) * 4,
               "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-02", freq="B"))
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [2.0] * 4, f"P/B history in euros ({rec['hist_pbr']})")
    # A DOLLAR reporter quoted in euros - TotalEnergies. EUR 2bn at 1.13
    # USD/EUR is USD 2.26bn over USD 1bn of equity: P/B 2.26.
    fx = pd.Series(1.13, index=px.index)
    rec = build_statement_record(inc, bs, px, fx, "EUR", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [2.26] * 4, f"USD reporter converted at each year-end ({rec['hist_pbr']})")


def main() -> int:
    check_parsers()
    check_pyramids()
    check_currency()
    cfg = ScreenConfig()
    df = make_universe()

    df, rstats = FF.apply_roster_filters(df, cfg)
    df["market_cap_usd"] = df["market_cap_local"] * USD_PER_EUR
    df = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    df, istats = FF.apply_icb_filters(df, cfg, rstats)
    res, stats = run_screen(df, USD_PER_EUR, cfg)
    res = FF.add_quality_context(res)
    res = FF.add_valueup_flags(res)
    res, _ = FF.apply_roe_gate(res, cfg)
    res, _ = FF.apply_absolute_screen(res, cfg)
    idx = res.set_index("symbol")

    print("\n=== universe hygiene ===")
    print(f"  funnel: {istats}")
    for sym, why in [("CRAV", "Credit Agricole CCI excluded"),
                     ("LTA", "venture-capital vehicle excluded by sub type"),
                     ("SIIC", "SIIC excluded (IAS 40 P/E)"),
                     ("MT", "home market Amsterdam -> secondary line excluded"),
                     ("TINY", "below the size floor")]:
        ok(sym not in idx.index, why)
    ok("SESG" in idx.index, "SES's certificates kept - not a cooperative")
    ok("DEVL" in idx.index, "developer kept (ICB 351010)")
    ok("AIRX" in idx.index, "foreign-domiciled, Paris-home company kept")
    ok(bool(idx.loc["MF", "is_holdco"]) and "Bureau Veritas" in idx.loc["MF", "holdco_note"],
       "named holding company tagged with what it holds, not dropped")

    print("\n=== screens ===")
    ok(bool(idx.loc["GOOD", "passes"]), "genuinely cheap name passes the peer screen")
    ok(not bool(idx.loc["MF", "passes_any"]), "cheap holdco with 2% ROE passes nothing")
    ok(pd.isna(idx.loc["LOSS", "trailing_pe"]), "P/E of 0 read as missing")
    ok(pd.isna(idx.loc["BANK", "ev_to_ebitda"]), "bank's EV/EBITDA suppressed (ICB)")
    ok(pd.notna(idx.loc["DEVL", "ev_to_ebitda"]), "developer keeps EV/EBITDA (ICB 35 is not financial)")
    ok(bool(idx.loc["BANK", "abs_passes"]) and bool(idx.loc["BANK", "abs_via_carveout"]),
       "bank clears the absolute screen via the P/B + ROE carve-out")
    ok(idx.loc["ELEC", "trailing_pe_peer_basis"] == "icb_industry",
       f"thin ICB sector falls back to the ICB industry "
       f"({idx.loc['ELEC', 'trailing_pe_peer_basis']!r})")
    ok(idx.loc["GOOD", "trailing_pe_peer_basis"] == "industry",
       "a populated ICB sector benchmarks within itself")
    ok(idx.loc["LONE", "trailing_pe_peer_n"] == 0, "a name with no cohort at either level is unscored")

    # One line per issuer: Robertet's ordinary shares trade, its CI barely.
    lines = pd.DataFrame([dict(ticker="RBT.PA", name="ROBERTET", adv_local=6e5,
                               is_class_line=False),
                          dict(ticker="CBE.PA", name="ROBERTET CI", adv_local=2e4,
                               is_class_line=True)])
    kept, lstats = FF.keep_one_line_per_issuer(lines)
    ok(kept["ticker"].tolist() == ["RBT.PA"] and lstats["dropped_second_line"] == 1,
       "one line per issuer: Robertet's ordinary line kept, its certificate dropped")

    check_statements()

    print("\n=== passing ===")
    hits = res[res["passes_any"]][["symbol", "name", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "screen"]]
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
