"""Configuration for the France (Euronext Paris) valuation screener.

Sixth market after Korea, the UK, Japan, Germany and Poland, and built by
combining what each of them had to learn. The quality floor, the discount
test and the history screen are the user's screening preferences and are
carried over unchanged. What is local:

  * classification - ICB, read per company off Euronext's own factsheet, as
    Japan, Germany and Poland read the operator's classification rather than
    a vendor's
  * universe hygiene - Euronext names the share class of every line (sub
    type), the venture-capital vehicles, and the home market of every
    multi-listed company, so three of the traps are exact lookups
  * the holding-company trap - France's version of the UK's investment trusts,
    which ICB does NOT separate (Eurazeo and Peugeot Invest file with Amundi,
    Christian Dior with LVMH). See KNOWN_HOLDCOS and fr_filters.flag_pyramids
  * the currency trap - Poland's, at a far smaller rate: TotalEnergies,
    Vallourec and Maurel & Prom quote in euros and report in dollars, and
    EUR/USD (~0.85) is too close to 1 for Poland's fixed test band
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

METRIC_LABELS = {
    "trailing_pe": "P/E",
    "price_to_book": "P/B",
    "ev_to_ebitda": "EV/EBITDA",
}

# Same reasoning as Korea: a zero or negative multiple means "no earnings" or
# "negative equity", never "cheap". STMicro's trailing P/E reads 1,725 on a
# rounded EPS of 0.03 - the upper bound is what removes it.
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}

# ---------------------------------------------------------------------------
# Euronext markets
# ---------------------------------------------------------------------------
# The download names every line's market(s), home market first: "Euronext
# Paris", "Euronext Growth Paris", "Euronext Amsterdam, Paris". Euronext Access
# (the unregulated MTF) is not read - nothing on it clears USD 600m.
MICS = ("XPAR", "ALXP")
BOARDS = {"Euronext Paris": "MAIN", "Euronext Growth Paris": "GROWTH"}

# ---------------------------------------------------------------------------
# ICB, from Euronext's factsheet
# ---------------------------------------------------------------------------
# Four levels: industry (2 digits), supersector (4), sector (6), subsector (8).
# Peers are ICB sector, falling back to ICB industry - Poland's shape (GPW
# sector -> macro-sector). Measured 2026-10-06 on 111 survivors: falling back
# to the supersector scored 71 of them, to the industry 92, with the same
# names passing bar one at the margin. See CLAUDE.md.
#
# Financials are ICB industry 30: banks, financial services, insurance. Real
# estate is its own industry (35) in ICB, so unlike Germany and Poland no
# carve-out is needed for invariant 6.
FINANCIAL_INDUSTRY_CODE = "30"

# Listed investment vehicles. Euronext's own sub type names venture-capital
# funds (Altamir, "Venture Capital Fund Shares"); ICB's two investment-vehicle
# subsectors catch the closed-end funds.
INVESTMENT_SUBTYPES = ("venture capital fund", "investment fund", "fund shares")
INVESTMENT_ICB_SUBSECTORS = ("30204000", "30205000")   # closed-end, open-end vehicles

# French REITs are SIICs, and ICB files every one of them under its REIT
# sector (351020): Unibail, Klepierre, Gecina, Covivio, Icade, Mercialys. IAS
# 40 runs their revaluations through earnings, so their P/E is the appraiser's
# year - the reason every earlier market excluded REITs and landlords.
# Developers (Nexity, Kaufman & Broad) are ICB 351010 and stay.
REIT_ICB_SECTOR = "351020"

# Share classes. Euronext's sub type says what each line is. A certificate
# (Robertet's CI and CDV lines) or a preference share is a second line of an
# issuer whose ordinary shares are also listed - two prices for one set of
# earnings, Korea's 우선주 trap. Germany's rule applies: one line per issuer,
# the most traded.
CLASS_SUBTYPES = ("depositary receipt", "certificate", "preferred", "preference")
# Words dropped from Euronext's names before lines are grouped by issuer:
# "ROBERTET CI", "ROBERTET CDV 87", "ARTOIS NOM.", "AB SCIENCE BSA".
CLASS_WORDS = {"CI", "CDV", "87", "NOM", "ADP", "PREF", "BSA", "DS", "SA", "SE", "NV",
               "ACT", "ORD", "P"}

# Cooperative investment certificates (CCI) - France's structurally cheap
# share class, and its biggest. Thirteen Caisses Regionales de Credit Agricole
# list a CCI: a non-voting claim on a cooperative bank whose members' shares
# never trade. On 2026-10-06 every one sat at 0.20-0.27x book, the bottom
# decile of the whole market, with median traded value of USD 30-350k - a
# permanent discount for no vote and no exit, exactly Korea's 우선주. Euronext
# files them as "Depositary Receipt / Certificate", which SES's fiduciary
# certificates (its only listed line, full economics) also are, so the class is
# the certificate sub type AND a cooperative-bank name.
COOPERATIVE_NAME_TOKENS = ("CRCAM", "CCI")
COOPERATIVE_YAHOO_WORDS = ("COOPERATIVE", "CAISSE REGIONALE")

# ---------------------------------------------------------------------------
# Holding companies - flags by default, never silent
# ---------------------------------------------------------------------------
# France's structurally cheap class. A listed holding trades at a standing
# discount to the value of its stakes, and ICB does not see it: Eurazeo and
# Peugeot Invest are "Asset Managers and Custodians" (with Amundi), Wendel
# "Diversified Financial Services", Bollore "Entertainment", Compagnie de
# l'Odet "Gas Distribution", Artois "Mortgage Finance" (measured 2026-10-06).
# The UK found the same thing and only the AIC register fixed it; France has
# no such register, so the names are listed here, by ISIN, with what they
# hold. A holding that CONSOLIDATES a listed subsidiary (Christian Dior over
# LVMH, Odet over Bollore) is also found from the data - see
# fr_filters.flag_pyramids - so this list matters for the ones that do not.
KNOWN_HOLDCOS = {
    "FR0000121204": "Wendel (Bureau Veritas and unlisted stakes)",
    "FR0000121121": "Eurazeo (private-equity portfolio)",
    "FR0000064784": "Peugeot Invest (Stellantis, Forvia and others)",
    "FR0000039299": "Bollore (UMG, Vivendi, Canal+, Havas, Louis Hachette)",
    "FR0000062234": "Compagnie de l'Odet (Bollore)",
    "FR0000076952": "Artois (Bollore group)",
    "FR001400SU99": "Moncey (Bollore group)",
    "FR0000130403": "Christian Dior (LVMH)",
    "FR0000061137": "Burelle (OPmobility)",
    "FR0000051393": "IDI (private-equity portfolio)",
}

# A parent consolidating a listed subsidiary files the subsidiary's income
# statement plus its own small activities: Dior and LVMH file the identical
# revenue. Revenue alone is not enough - among 76 survivors, ten unrelated
# pairs agreed within 1% (fr_filters.flag_pyramids) - so operating profit must
# agree too, and the parent's equity must be mostly other people's: the
# minorities of the subsidiary it consolidates.
PYRAMID_REVENUE_TOL = 0.01
PYRAMID_OP_TOL = 0.05
PYRAMID_MIN_MI_SHARE = 0.25
PYRAMID_MI_GAP = 0.15

# Closely held: Yahoo's heldPercentInsiders at a majority. For French
# companies Yahoo counts every strategic holder in it - a family, a parent, the
# state, an industrial partner - so it is NOT Poland's single controller:
# L'Oreal reads 57% (Bettencourt 35% + Nestle 20%), Thales 56% (the state +
# Dassault), Credit Agricole 69% (its regional banks' holding). 68 of 153 size
# survivors reach it (2026-10-06). A flag that the free float is the minority,
# never a gate unless asked. The free-data
# counterpart of Poland's register read; no free source names the French
# state's stakes (the APE's own site refuses automated requests), so there is
# no state flag - a known gap, see CLAUDE.md.
CONTROL_MIN_INSIDERS = 0.50


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000
    # Measured, not inherited - see CLAUDE.md "The liquidity gate". Yahoo's
    # volume is Euronext Paris only (Cboe and Turquoise are not in it), so it
    # is a lower bound. At the cross-market USD 4m the screen loses Rubis,
    # SEB, Imerys, Eramet, Coface, Ipsos, BIC - ordinary SBF 120 floats; at
    # 1m what goes is what should: Odet, Lagardere, Fnac Darty, Peugeot
    # Invest, Robertet - controlled, or under offer.
    min_adv_usd: float = 1_000_000
    adv_lookback_days: int = 60                 # trading days

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0

    # --- Absolute value screen ---
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0
    abs_min_div_yield: float = 2.0

    # --- Own-history screen ---
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = ICB sector (6 digits); icb_industry = ICB industry (2 digits).
    # `sector` (the ICB supersector) is what the dashboard shows beside it.
    peer_keys: tuple[str, ...] = ("industry",)
    fallback_peer_keys: tuple[str, ...] = ("icb_industry",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- France-specific universe hygiene ---
    exclude_investment_companies: bool = True
    exclude_reits: bool = True
    exclude_property: bool = True           # same switch as the other markets: SIICs
    exclude_secondary: bool = True          # home market not Paris (ArcelorMittal, Solvay)
    exclude_cooperative_certs: bool = True  # Credit Agricole regional banks' CCIs
    exclude_pyramid_parents: bool = True    # Dior over LVMH: one set of earnings, twice
    flag_holdcos: bool = True
    exclude_holdcos: bool = False           # flagged by default, not dropped
    exclude_controlled: bool = False

    exclude_sectors: tuple[str, ...] = ()

    # --- Fetching ---
    max_workers: int = 2
    request_delay: float = 0.25
    cache_dir: str = ".fr_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
REIT_TOKENS = ("REIT", "SIIC")
HOLDCO_TOKENS = ("HOLDING", "HLDG", "PARTICIPATIONS")


def plain(s: str) -> str:
    """Upper-case, accents stripped, punctuation to spaces."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s.upper()).split())


def issuer_key(name: str) -> str:
    """Groups the listed lines of one issuer: 'ROBERTET', 'ROBERTET CI' and
    'ROBERTET CDV 87' are one company. French ISINs carry no shared prefix
    across classes (FR0000039091 vs FR0000045601), so unlike Germany the name
    is the whole key. Punctuation goes before class words, Germany's
    Draegerwerk lesson."""
    toks = [t for t in plain(name).split() if t not in CLASS_WORDS]
    return " ".join(toks[:2])


def board_of(market: str) -> str:
    """'MAIN' / 'GROWTH' from Euronext's market string, by its home venue."""
    home = str(market or "").split(",")[0].strip()
    return BOARDS.get(home, "")


def is_secondary(market: str) -> bool:
    """Home market elsewhere: 'Euronext Amsterdam, Paris' (ArcelorMittal),
    'Euronext Brussels, Paris' (Solvay). Euronext writes the home venue first;
    Airbus, STMicro and Stellantis are 'Euronext Paris' and stay."""
    return board_of(market) == ""


def is_financial(sector: str = "", industry: str = "", icb: str = "") -> bool:
    """ICB industry 30. Called with the supersector name too, so the
    dashboard's (sector, industry) call works: every financial supersector is
    one of the three below."""
    if icb:
        return str(icb).startswith(FINANCIAL_INDUSTRY_CODE)
    return str(sector).strip() in ("Banks", "Financial Services", "Insurance")


def is_investment_company(subtype: str, icb: str) -> bool:
    st = str(subtype or "").lower()
    return (any(t in st for t in INVESTMENT_SUBTYPES)
            or str(icb or "")[:8] in INVESTMENT_ICB_SUBSECTORS)


def is_class_line(subtype: str) -> bool:
    st = str(subtype or "").lower()
    return any(t in st for t in CLASS_SUBTYPES)


def is_cooperative_cert(subtype: str, name: str, yahoo_name: str = "") -> bool:
    if not is_class_line(subtype):
        return False
    toks = plain(name).split()
    y = plain(yahoo_name)
    return (any(t in toks for t in COOPERATIVE_NAME_TOKENS)
            or any(w in y for w in COOPERATIVE_YAHOO_WORDS))


def is_reit(name: str, icb: str = "") -> bool:
    n = plain(name)
    return str(icb or "").startswith(REIT_ICB_SECTOR) or any(
        t in n.split() for t in REIT_TOKENS)


def is_holdco(name: str, isin: str = "") -> bool:
    n = plain(name)
    return str(isin) in KNOWN_HOLDCOS or any(t in n.split() for t in HOLDCO_TOKENS)
