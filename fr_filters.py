"""France-specific filters layered on top of the generic screener.

Everything from add_valueup_flags down is Poland's, which is Germany's, which
is the UK's, which is Korea's: the quality floor, the absolute screen and the
own-history screen are not country-specific. What is local, and which earlier
market each piece comes from:

  * roster hygiene - foreign secondaries by Euronext's home-venue field
    (Germany's rule, with the exchange's own answer instead of index
    membership), fund quotes
  * ICB hygiene - investment vehicles (Poland's exact lookup, via Euronext's
    sub type), SIICs (every market's REIT rule)
  * one line per issuer - Germany's, for Robertet's certificates
  * holding companies - the UK's investment-trust lesson, with a named list
    because no register exists, plus a data test for pyramids that closes the
    listed-subsidiary gap Japan and Germany both carried
  * control - Poland's flag, from Yahoo's insider holding
  * the currency repair - Poland's, with a test band sized to the rate
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_fr as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, insurers and financial services, by ICB industry 30.

    Exact, as in Japan, Germany and Poland - and ICB puts real estate in its
    own industry, so no carve-out is needed."""
    icb = df.get("icb", pd.Series("", index=df.index)).fillna("").astype(str)
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("").astype(str)
    return pd.Series([K.is_financial(s, "", c) for s, c in zip(sec, icb)], index=df.index)


# ---------------------------------------------------------------------------
# Universe hygiene
# ---------------------------------------------------------------------------
def apply_roster_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """What the roster alone can decide, before the size gate.

    Foreign secondaries: a line whose home market Euronext names as
    Amsterdam or Brussels - ArcelorMittal, Solvay, Aperam. Its price is set
    there and Paris follows. Germany used index membership for this; Euronext
    states the home venue outright, so the rule is the exchange's answer
    rather than a proxy for it (its index feed is encrypted besides).
    Foreign-domiciled companies whose home IS Paris - Airbus, STMicro,
    Stellantis, Technip Energies - stay.
    """
    df = df.copy()
    stats = {"listings": len(df)}
    qtype = df.get("quote_type", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_foreign_secondary"] = df.get("secondary", pd.Series("", index=df.index)) \
        .fillna("").astype(str).eq("1")
    df["is_fund_quote"] = qtype.str.upper().isin(["ETF", "MUTUALFUND", "FUND"])
    for flag, key, on in (("is_foreign_secondary", "dropped_foreign_secondary", cfg.exclude_secondary),
                          ("is_fund_quote", "dropped_fund_quote", True)):
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]
    return df.copy(), stats


def attach_icb(df: pd.DataFrame, sheets: dict) -> pd.DataFrame:
    """ICB and sub type from Euronext's factsheets onto each line.

    Column names follow the other markets so screener.py and the dashboard
    read them unchanged: `industry` is the ICB sector (the peer group),
    `sector` the ICB supersector (its fallback), `subsector` the ICB
    subsector, `icb` the eight-digit code.
    """
    df = df.copy()

    def g(i, k):
        return (sheets.get(str(i)) or {}).get(k, "") or ""
    isin = df["isin"].astype(str)
    df["icb"] = [g(i, "icb_subsector_code") for i in isin]
    df["industry"] = [g(i, "icb_sector") for i in isin]
    df["sector"] = [g(i, "icb_supersector") for i in isin]
    df["icb_industry"] = [g(i, "icb_industry") for i in isin]
    df["subsector"] = [g(i, "icb_subsector") for i in isin]
    df["subtype"] = [g(i, "subtype") for i in isin]
    return df


def apply_icb_filters(df: pd.DataFrame, cfg: K.ScreenConfig,
                      stats: dict) -> tuple[pd.DataFrame, dict]:
    """What needs Euronext's factsheet, on the size survivors.

    Cooperative investment certificates (config_fr.COOPERATIVE_NAME_TOKENS),
    investment vehicles by sub type or ICB (Altamir is "Venture Capital Fund
    Shares"), SIICs by ICB's REIT sector. Holding companies are tagged, not
    dropped - see config_fr.KNOWN_HOLDCOS for why a list, and flag_pyramids
    for the ones the data finds.
    """
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    icb = df["icb"].fillna("").astype(str)
    sub = df["subtype"].fillna("").astype(str)
    yind = df.get("yf_industry", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_investment_co"] = [K.is_investment_company(s, c) for s, c in zip(sub, icb)]
    df["is_reit"] = (pd.Series([K.is_reit(n, c) for n, c in zip(name, icb)], index=df.index)
                     | yind.str.contains("REIT", case=False, na=False))
    df["is_class_line"] = sub.map(K.is_class_line)
    yname = df.get("yf_name", pd.Series("", index=df.index)).fillna("").astype(str)
    df["is_coop_cert"] = [K.is_cooperative_cert(s, n, y) for s, n, y in zip(sub, name, yname)]
    df["is_holdco"] = [K.is_holdco(n, i) for n, i in zip(name, df["isin"].astype(str))]
    df["holdco_note"] = df["isin"].astype(str).map(K.KNOWN_HOLDCOS).fillna("")
    df["icb_missing"] = icb.eq("")

    stats = dict(stats)
    for flag, key, on in (("is_coop_cert", "dropped_cooperative_cert",
                           cfg.exclude_cooperative_certs),
                          ("is_investment_co", "dropped_investment_co",
                           cfg.exclude_investment_companies),
                          ("is_reit", "dropped_reit", cfg.exclude_reits and cfg.exclude_property),
                          ("is_holdco", "dropped_holdco", cfg.exclude_holdcos)):
        if on:
            stats[key] = int(df[flag].sum())
            df = df[~df[flag]]
    if not cfg.exclude_holdcos:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())
    stats["icb_missing"] = int(df["icb_missing"].sum())
    return df.copy(), stats


def keep_one_line_per_issuer(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Ordinary shares and certificates of one company: keep the traded line.

    Germany's invariant 1. Robertet lists its ordinary shares, its
    certificats d'investissement (CI, no vote) and its certificats de droit
    de vote (CDV) as three lines; all carry the same earnings, so the cheaper
    one would be scored against a cohort holding its sibling. The most traded
    line survives; ties go to the ordinary share.
    """
    df = df.copy()
    df["issuer"] = df["name"].map(K.issuer_key)
    df["n_lines"] = df.groupby("issuer")["issuer"].transform("size")
    adv = pd.to_numeric(df.get("adv_local"), errors="coerce").fillna(-1.0)
    df["_a"] = adv
    df["_o"] = (~df.get("is_class_line", pd.Series(False, index=df.index))
                .fillna(False).astype(bool)).astype(int)
    df = df.sort_values(["issuer", "_a", "_o"], ascending=[True, False, False])
    dup = df.duplicated(subset=["issuer"], keep="first")
    dropped = df.loc[dup, "ticker"].tolist()
    if dropped:
        log.info("one line per issuer: dropped %s", ", ".join(dropped))
    df = df[~dup].drop(columns=["_a", "_o"])
    return df, {"dropped_second_line": len(dropped)}


# ---------------------------------------------------------------------------
# Control and pyramids
# ---------------------------------------------------------------------------
def apply_control(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Closely held: insiders and strategic holders own a majority.

    Poland read single controllers from GPW's register; France publishes no
    free register, and Yahoo's heldPercentInsiders lumps every strategic
    holder together (see config_fr.CONTROL_MIN_INSIDERS), so this says the
    free float is the minority - not who controls. A flag, never a gate
    unless asked.
    """
    df = df.copy()
    ins = pd.to_numeric(df.get("insiders_pct"), errors="coerce")
    df["ctrl_pct"] = ins * 100.0
    df["is_controlled"] = ins >= K.CONTROL_MIN_INSIDERS
    stats = {"insiders_known": int(ins.notna().sum()),
             "flagged_majority_insiders": int(df["is_controlled"].sum())}
    if cfg.exclude_controlled:
        stats["dropped_controlled"] = int(df["is_controlled"].sum())
        df = df[~df["is_controlled"]]
    return df, stats


def _latest(row: pd.Series, key: str) -> float:
    for i in (3, 2, 1):
        v = pd.to_numeric(row.get(f"{key}_y{i}"), errors="coerce")
        if pd.notna(v):
            return float(v)
    return np.nan


def flag_pyramids(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """A listed parent that consolidates a listed subsidiary.

    Christian Dior owns ~42% of LVMH with control and consolidates it, so the
    two file the same revenue, the same EBITDA, and largely the same profit.
    Both in one screen is Germany's two-lines-of-one-company problem one level
    up: the same earnings are counted twice, and the parent - which carries a
    standing holding discount - is scored against a cohort that holds its own
    subsidiary. Japan (親子上場) and Germany (Traton, Porsche AG) listed this as
    a known gap because no free shareholder data names the parent.

    The filings name it, but revenue alone does not: the first run matched
    on revenue within 1% and "found" ten pairs among 76 names, every one a
    coincidence (Accor and Sopra Steria, Orange and Schneider). A pair now
    needs all three of
      * revenue within PYRAMID_REVENUE_TOL and operating profit within
        PYRAMID_OP_TOL, same currency and fiscal year - one income statement
      * a parent whose equity is at least PYRAMID_MIN_MI_SHARE minority
        interests (Dior's minorities are LVMH's other shareholders, ~58%)
      * a child carrying far fewer (PYRAMID_MI_GAP below the parent)
    Two unrelated companies match one of these by chance; not all three.
    The parent is dropped by default and the subsidiary tagged.
    """
    df = df.copy()
    df["pyramid_parent_of"] = ""
    df["pyramid_child_of"] = ""
    rev = df.apply(lambda r: _latest(r, "rev"), axis=1)
    op = df.apply(lambda r: _latest(r, "op"), axis=1)
    fy = df.get("fin_years", pd.Series("", index=df.index)).fillna("").astype(str) \
           .str.split(",").str[-1]
    ccy = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    mi = pd.to_numeric(df.get("lf_mi"), errors="coerce").fillna(0.0)
    mi_share = mi / (eq + mi)
    idx = [i for i in df.index if pd.notna(rev[i]) and rev[i] > 0]
    pairs = []
    for a_pos, a in enumerate(idx):
        for b in idx[a_pos + 1:]:
            if fy[a] != fy[b] or ccy[a] != ccy[b] or not fy[a]:
                continue
            if abs(rev[a] - rev[b]) / max(rev[a], rev[b]) > K.PYRAMID_REVENUE_TOL:
                continue
            if not (pd.notna(op[a]) and pd.notna(op[b]) and op[a] * op[b] > 0
                    and abs(op[a] - op[b]) / max(abs(op[a]), abs(op[b])) <= K.PYRAMID_OP_TOL):
                continue
            parent, child = (a, b) if mi_share[a] >= mi_share[b] else (b, a)
            if (mi_share[parent] >= K.PYRAMID_MIN_MI_SHARE
                    and mi_share[parent] - mi_share[child] >= K.PYRAMID_MI_GAP):
                pairs.append((parent, child))
    for p, c in pairs:
        df.at[p, "pyramid_parent_of"] = str(df.at[c, "name"])
        df.at[c, "pyramid_child_of"] = str(df.at[p, "name"])
        df.at[p, "is_holdco"] = True
        log.info("pyramid: %s consolidates %s (revenue %.0f vs %.0f)",
                 df.at[p, "name"], df.at[c, "name"], rev[p], rev[c])
    parents = df["pyramid_parent_of"].ne("")
    stats = {"pyramid_pairs": len(pairs)}
    if cfg.exclude_pyramid_parents:
        stats["dropped_pyramid_parent"] = int(parents.sum())
        df = df[~parents]
    return df, stats


# ---------------------------------------------------------------------------
# The currency repair
# ---------------------------------------------------------------------------
# A reporting currency whose rate to the euro is within this factor of 1
# cannot be told apart from the euro by magnitude at all. CHF (~1.07) is the
# case: one tiny Paris line. Everything inside this band is refused.
FX_MIN_SEPARATION = 1.08
# Poland's outer band: a ratio further than this from BOTH hypotheses is not
# evidence of either (STMicro's EPS ratio is 0.06 - a rounded EPS of 0.03).
FX_TEST_BAND = 1.6
# Between the two hypotheses the test takes the nearer one, unless the ratio
# sits so close to the midpoint that the margin is under this share of the
# distance between them. Not a tolerance around each hypothesis: Yahoo's
# trailing EPS sums four quarters at four exchange rates, so its ratio
# scatters +/-7% around the spot rate (Maurel & Prom 0.828, Vallourec 0.898
# against 0.888) - a tolerance tight enough to separate euros from dollars
# would refuse half of them.
FX_MIN_MARGIN = 0.25


def _nearer(k: pd.Series, r: pd.Series) -> pd.Series:
    """'quote' where k is nearer r, 'reporting' where nearer 1, '' where it is
    near neither, near the midpoint, or untestable."""
    lk = np.log(k.where(k > 0))
    lr = np.log(r.where(r > 0))
    d_rep, d_quo = lk.abs(), (lk - lr).abs()
    band = np.log(FX_TEST_BAND)
    clear = (d_rep - d_quo).abs() >= FX_MIN_MARGIN * lr.abs()
    out = np.where(clear & (d_quo < d_rep) & (d_quo < band), "quote",
                   np.where(clear & (d_rep < d_quo) & (d_rep < band), "reporting", ""))
    return pd.Series(out, index=k.index)


def repair_foreign_reporters(df: pd.DataFrame, eur_per: dict,
                             q_equity: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Yahoo's multiples on euro lines of companies that report in dollars.

    Poland's repair, measured again in Paris (2026-10-06), and Paris is not
    Warsaw. Yahoo's market cap, enterprise value and EPS are in euros on every
    USD reporter, its EBITDA and net income in dollars - Poland's split. But
    its BOOK VALUE differs by company: against the latest quarterly equity,
    TotalEnergies, Viridien, Maurel & Prom and STMicro carry a book value
    already converted to euros (ratio 0.86-0.89 against a rate of 0.888), so
    their P/B is right as Yahoo states it, while Vallourec's is in dollars
    (1.03) and its P/B is ~11% too low. Assuming Poland's behaviour would have
    "repaired" TotalEnergies' correct P/B into a wrong one. So, as Japan
    taught, every field is tested per row. With r = EUR per reporting unit:

      EPS  k = EPS x shares / net income                 ~r: euro (keep)    ~1: reporting
      BPS  k = BPS x shares / latest QUARTERLY equity    ~1: reporting      ~r: euro
           (no quarterly figure: the filed annual, then Yahoo's ROE)
      EV   EV / market cap in [0.25, 4]                  euro EV; EBITDA / r

    EV/EBITDA is a euro EV over dollar EBITDA on every one of them:
    TotalEnergies 5.10 -> 5.74. The book test reads the quarter because the
    filed annual drifts by about half the euro-dollar distance (TotalEnergies
    0.957 on the annual, 0.882 on the quarter). A test that lands nearer the
    midpoint than FX_MIN_MARGIN refuses the multiple it vouches for.
    """
    df = df.copy()
    q_equity = q_equity or {}

    def n(c: str) -> pd.Series:
        if c not in df.columns:
            return pd.Series(np.nan, index=df.index)
        return pd.to_numeric(df[c], errors="coerce")
    fin = df.get("fin_ccy", pd.Series("", index=df.index)).fillna("").astype(str)
    quote = df.get("currency", pd.Series("EUR", index=df.index)).fillna("EUR").astype(str)
    foreign = fin.ne("") & fin.ne(quote) & quote.eq("EUR")
    r = fin.map(lambda c: eur_per.get(c, np.nan)).astype(float)
    df["fx_rep_to_eur"] = r.where(foreign, 1.0)
    df["fx_note"] = ""
    stats = {"foreign_reporters": int(foreign.sum())}
    if not foreign.any():
        return df, stats

    untestable = foreign & ~(np.abs(np.log(r)) > np.log(FX_MIN_SEPARATION))
    live = foreign & ~untestable

    eps, bps, sh = n("trailing_eps"), n("book_value_ps"), n("yf_shares")
    ni, roe = n("yf_ni"), n("yf_roe")
    ev, mcap, ebitda = n("yf_ev"), n("market_cap_local"), n("yf_ebitda")

    eps_basis = _nearer((eps * sh / ni).where(ni.abs() > 0), r)
    eps_rep = live & eps_basis.eq("reporting")
    eps_bad = live & eps_basis.eq("") & eps.notna()
    eps_eur = eps.where(~eps_rep, eps * r)

    qeq = df["ticker"].map(q_equity).astype(float)
    by_quarter = _nearer((bps * sh / qeq).where((qeq > 0) & (sh > 0)), r)
    eq = n("lf_equity")
    lsh = n("lf_shares") * n("share_basis_g").fillna(1.0)
    by_filing = _nearer((bps * lsh / eq).where((eq > 0) & (lsh > 0)), r)
    by_roe = _nearer(((eps_eur / bps) / roe).where((roe != 0) & (bps > 0)), r) \
        .map({"quote": "reporting", "reporting": "quote", "": ""})
    has_q = (qeq > 0) & (sh > 0) & bps.notna()
    has_filing = (eq > 0) & (lsh > 0) & bps.notna()
    bps_basis = by_quarter.where(has_q, by_filing.where(has_filing, by_roe))
    bps_rep = live & bps_basis.eq("reporting")
    bps_bad = live & bps_basis.eq("") & bps.notna()

    ev_eur = live & (ev / mcap).between(0.25, 4.0)

    df["trailing_eps"] = eps_eur.where(~eps_bad)
    df["trailing_pe"] = n("trailing_pe").where(~eps_rep, n("trailing_pe") / r).where(~eps_bad)
    df["book_value_ps"] = bps.where(~bps_rep, bps * r).where(~bps_bad)
    df["price_to_book"] = n("price_to_book").where(~bps_rep, n("price_to_book") / r) \
                                            .where(~bps_bad)
    evx = (ev / (ebitda * r)).where(ebitda > 0)
    df["ev_to_ebitda"] = n("ev_to_ebitda").where(~foreign, evx.where(ev_eur))
    for c in ("trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps", "book_value_ps"):
        df.loc[untestable, c] = np.nan

    notes = []
    for i in df.index[foreign]:
        if untestable[i]:
            notes.append((i, "fx basis untestable"))
            continue
        bits = []
        if eps_bad[i]:
            bits.append("P/E refused")
        if bps_rep[i]:
            bits.append("P/B repaired")
        elif bps_bad[i]:
            bits.append("P/B refused")
        bits.append("EV/EBITDA repaired" if ev_eur[i] and pd.notna(evx[i])
                    else "EV/EBITDA refused")
        notes.append((i, ", ".join(bits)))
    for i, t in notes:
        df.at[i, "fx_note"] = t

    stats.update({
        "fx_untestable": int(untestable.sum()),
        "fx_eps_on_reporting_basis": int(eps_rep.sum()),
        "fx_pe_refused": int(eps_bad.sum()),
        "fx_pb_repaired": int(bps_rep.sum()),
        "fx_pb_refused": int(bps_bad.sum()),
        "fx_ev_repaired": int((ev_eur & evx.notna()).sum()),
    })
    log.info("currency repair: %d foreign reporters - %s", int(foreign.sum()),
             ", ".join(f"{df.at[i, 'symbol'] if 'symbol' in df else i}: {t}" for i, t in notes))
    return df, stats

def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Low-P/B flags.

    Korea had a live policy catalyst attached to almost exactly this screen -
    KRX publicly identifies firms whose PBR sits in the bottom 20% of their
    industry. France has no counterpart: no disclosure regime is keyed to
    valuation. What closes French discounts is usually a bid - and, for a
    controlled company, the controller's squeeze-out at 90% - neither
    computable from this data.

    So these two columns are kept for continuity and for sorting, not as a
    catalyst.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """ROE derived from EPS and BPS rather than taken as a vendor field.

    Same reasoning as Korea: the ratio is then consistent with the very P/E
    and P/B being screened on. It is unit-free only once EPS and BPS are in
    the same currency - Yahoo hands out euro EPS against dollar BPS for foreign
    reporters, so this must run after repair_foreign_reporters.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def flag_ttm_vs_filed(df: pd.DataFrame) -> pd.DataFrame:
    """Profitable over the trailing twelve months, loss-making in the last
    filed year. A flag, never a gate.

    The peer and absolute screens use Yahoo's trailing multiples in every
    market, and Yahoo's trailing twelve months is the sum of the last four
    quarters - one-offs included. Germany's K+S is the case that found this:
    two quarters of very large non-operating gains turned a filed loss of
    EUR 1.1bn into TTM net income of +EUR 1.06bn and a P/E of 2.6.

    It is not a sign error - the quarters genuinely sum that way - so the
    trailing figure is left alone and the row is marked instead. The test is
    deliberately narrow: TTM profit against a normalized filed LOSS. A ratio
    test (TTM P/E far below filed P/E) would also catch genuine turnarounds,
    which is noise, not a warning.
    """
    df = df.copy()
    pe = pd.to_numeric(df.get("trailing_pe"), errors="coerce")
    lf = pd.to_numeric(df.get("lf_ni"), errors="coerce")
    df["ttm_vs_filed_loss"] = pe.notna() & (lf < 0)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require survivors to actually earn something.

    Runs after scoring, never before: it narrows `passes` and leaves
    avg_discount alone, so the peer cohorts still contain the low-ROE names
    that make them representative. Missing ROE fails.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    Identical in structure to the Korea version, including the financials
    carve-out: EV/EBITDA is suppressed for banks and insurers, so a strict
    both-metrics rule would exclude every French bank and insurer however far
    below book it traded.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_div_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def restate_info_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Yahoo's .info per-share fields on today's share basis, after a split.

    The peer and absolute screens read P/E, P/B, EPS, book value and yield
    straight from .info. Yahoo restates those for splits on its own schedule,
    and not consistently: after Johnson Matthey's Aug 2026 4-for-3
    consolidation its bookValue was already on today's 125.9m shares (16.01 =
    FY2026 equity / 125.9m) while its filed statements were still on the old
    167.9m; in the Japan build bookValue was restated while dividendRate was
    not. So the basis is tested per row, never assumed either way.

    `share_basis_g` comes from build_statement_record: the split / consolidation
    factor since the latest filing that today's market cap demands (1 for
    almost everyone). Where it is not 1, filed equity over (book value x filed
    shares) says which basis .info is on: ~1 means the old one, ~g the new.

      old basis   EPS, book value and yield divided by g; P/E and P/B
                  multiplied by it. ROE, their ratio, does not move.
      new basis   per-share fields are right; the dividend cannot be checked
                  the same way (in Japan it lagged when book value did not),
                  so the yield is left missing rather than trusted.
      neither     P/E, P/B and yield missing - no basis to vouch for.

    EV/EBITDA needs nothing: Yahoo builds EV from today's market cap.
    """
    df = df.copy()
    g = pd.to_numeric(df.get("share_basis_g"), errors="coerce").fillna(1.0)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    sh = pd.to_numeric(df.get("lf_shares"), errors="coerce")
    bv = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    k = eq / (bv * sh)
    moved = np.abs(np.log(g)) > 0.02
    tol = np.log(1.25)
    old = moved & (np.abs(np.log(k)) < tol)
    new = moved & ~old & (np.abs(np.log(k / g)) < tol)
    unclear = moved & ~old & ~new

    for c, op in (("trailing_eps", "div"), ("book_value_ps", "div"), ("div_yield", "div"),
                  ("trailing_pe", "mul"), ("price_to_book", "mul")):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.where(~old, v / g if op == "div" else v * g)
    if "div_yield" in df.columns:
        df.loc[new, "div_yield"] = np.nan
    for c in ("trailing_pe", "price_to_book", "div_yield"):
        if c in df.columns:
            df.loc[unclear, c] = np.nan

    df["split_note"] = np.select([old, new, unclear],
                                 ["info restated for split", "yield unverified after split",
                                  "info basis unclear"], default="")
    stats = {"info_restated_for_split": int(old.sum()),
             "info_yield_unverified": int(new.sum()),
             "info_basis_unclear": int(unclear.sum())}
    if moved.any():
        log.info("split basis: %d .info rows restated, %d yields unverified, %d unclear",
                 *stats.values())
    return df, stats


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def add_history_now(df: pd.DataFrame) -> pd.DataFrame:
    """Today's P/E, P/B and EV/EBITDA on the SAME basis as the history.

    Each historical year is that year-end market value over that year's filed
    totals (providers_fr.build_statement_record). Today's value has to be
    built the same way - today's market value over the latest filing - or the
    comparison measures the gap between two definitions rather than a change
    in valuation.

    That is not hypothetical. yfinance's own trailingPE divides by the last
    twelve months, including interims: the UK build measured Shell at 10.5
    against 15.2 on the filed-year basis, a 31% gap that would read as a 31%
    discount to history on its own. Korea learned the same lesson on
    EV/EBITDA, where mixing providers put only 18 of 30 within +/-25%; its fix
    was to rebuild the current value from the history's own source, which is
    what this does.

    So these three columns feed the own-history screen ONLY. The peer and
    absolute screens keep yfinance's figures, because they compare companies
    with each other on a common vendor definition, not a company with itself.

    Out-of-bounds values become NaN here rather than in the screen, so the
    dashboard receives exactly what Python tested and cannot disagree with it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    mv = n("market_cap_local") * n("fx_now")      # quote-major -> reporting ccy
    ni, eq, eb = n("lf_ni"), n("lf_equity"), n("lf_ebitda")
    mi = n("lf_mi").fillna(0.0)
    df["per_now"] = (mv / ni).where(ni > 0)
    df["pbr_now"] = (mv / eq).where(eq > 0)
    df["evx_now"] = ((mv + n("lf_debt") - n("lf_cash") + mi) / eb).where(eb > 0)
    df.loc[_fin_mask(df), "evx_now"] = np.nan          # invariant 6
    for col, (_, bound_key) in zip(("per_now", "pbr_now", "evx_now"), HIST_METRICS):
        lo, hi = K.METRIC_BOUNDS[bound_key]
        df[col] = df[col].where((df[col] >= lo) & (df[col] <= hi))
    return df


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own filed history - the third screen.

    A port of the Korea build's apply_history_screen, held to the same rules:
    the benchmark is a median (invariant 4); a non-positive or out-of-bounds
    multiple is missing, never cheap, in history as well as today
    (invariant 2), so a loss year drops out of the benchmark rather than
    dragging it; EV/EBITDA is skipped for financials (invariant 6); fewer than
    `hist_min_years` usable years is no benchmark; the ROE floor applies.

    One difference, and it is a data limit rather than a choice: Yahoo carries
    FOUR filed years for French companies, as for UK and German ones, where
    WiseReport gave Korea five, so the benchmark is a four-year median.
    """
    df = add_history_now(df)
    fin = _fin_mask(df)

    disc_cols = []
    for key, bound_key in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[bound_key]
        vals = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index)) \
                 .map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(
                lambda v, i=i: v[i] if i < len(v) and v[i] is not None else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
        cur = df[f"{key}_now"]
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the failing ones - the same
    # rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    note = df.get("hist_note", pd.Series("", index=df.index)).fillna("")
    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        # Every name a guard refused a benchmark, by reason - so a data problem
        # shows up as a count in the funnel rather than as names quietly
        # missing from the third screen.
        "hist_share_break": int((note == "share-count break").sum()),
        "hist_basis_mismatch": int((note == "price/share basis mismatch").sum()),
        "hist_stale_filing": int((note == "stale filings").sum()),
        "hist_no_reporting_ccy": int((note == "no reporting currency").sum()),
        "hist_normalized_earnings": int((df.get("hist_earnings", pd.Series("", index=df.index))
                                         == "normalized").sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats




def fr_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "symbol", "isin", "name", "name_exchange", "board", "market",
            "sector", "industry", "subsector", "icb", "icb_industry", "subtype",
            "yf_industry", "country", "market_cap_usd", "mcap_source",
            "adv_usd", "close_local", "currency", "fx_rep_to_eur", "fx_note",
            "is_controlled", "ctrl_pct", "is_holdco", "holdco_note",
            "pyramid_parent_of", "pyramid_child_of"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank"]
    cols += ["hist_years", "hist_note", "hist_earnings", "share_basis_g", "split_note",
             "per_now", "pbr_now", "evx_now",
             "hist_n_valid", "hist_n_pass", "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
    cols += ["fin_years", "fin_n", "fin_ccy"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout", "ttm_vs_filed_loss",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
