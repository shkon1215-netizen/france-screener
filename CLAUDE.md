# CLAUDE.md — France (Euronext Paris) Valuation Screener

Sixth market after Korea, the UK, Japan, Germany and Poland, built by
combining what each one learned. Same maths (`screener.py`, Germany's copy:
EUR), same three screens (vs peers, outright, vs own history), same
thresholds except the liquidity gate. Universe: Euronext Paris + Euronext
Growth Paris (630 lines on 2026-10-06).

## Run order

```bash
python test_france.py     # offline, must pass
python check_setup.py     # eight live checks
python main_fr.py -v      # full run, ~7 min cold (Yahoo .info x630, factsheets x180), seconds warm
python serve.py           # dashboard with working Refresh (dashboard.cmd)
python sync_dashboard_from_korea.py ../Korea/dashboard.py   # dashboard.py is generated
```

## Where each piece came from

| piece | from | French change |
|---|---|---|
| roster from the operator | Germany (workbook), Poland (GPW search) | Euronext's CSV download, one request |
| operator's classification | Japan, Germany, Poland | ICB from Euronext's factsheet, size survivors only |
| ISIN symbol resolution, fast_info market cap | Germany | none |
| foreign secondaries | Germany (index membership) | Euronext's home-venue field. The index feed is encrypted and is not read |
| one line per issuer | Germany (Vorzug) | Robertet's CI / CDV certificates, grouped by name |
| investment vehicles by operator's label | Poland (sector 180) | Euronext sub type "Venture Capital Fund Shares" + ICB 30204000/30205000 |
| REIT / landlord exclusion | Korea, Germany | ICB REIT sector 351020 (SIICs) |
| currency repair | Poland | test band scaled to the rate; quarterly equity for the book test |
| splits, TTM-vs-filed flag | UK, Germany | none |
| ownership flag | Poland (register) | Yahoo heldPercentInsiders. Means "closely held", not a single controller |
| holdco handling | UK (AIC register) | named list + data-driven pyramid test |

## French invariants

1. **Cooperative investment certificates are excluded** (`is_cooperative_cert`).
   13 Crédit Agricole regional banks list a CCI at 0.20–0.27× book,
   USD 30–350k a day. This is France's 우선주 (Korea's preferred shares).
   Euronext's sub type ("Depositary Receipt / Certificate") alone is not
   enough, because SES's certificates (its only line) carry the same label.
   The rule is certificate sub type AND a `CRCAM` / `CCI` / cooperative name.
2. **A parent consolidating a listed subsidiary is dropped** (`flag_pyramids`).
   It needs all of: revenue within 1%, operating profit within 5%, same
   currency and fiscal year, parent equity ≥25% minorities, and the child
   ≥15pp fewer. **Revenue alone is not enough.** The first run matched on
   revenue within 1% and "found" 10 pairs among 76 names, every one a
   coincidence (Accor/Sopra Steria, Orange/Schneider). Real pairs found:
   Christian Dior → LVMH, and Burelle → OPmobility (only with
   `--skip-liquidity`: Burelle trades under USD 1m a day).
3. **Holding companies are tagged, never silently kept as operating companies.**
   ICB files Eurazeo and Peugeot Invest with Amundi, Wendel as "Diversified
   Financial Services", Bolloré as "Entertainment", Odet as "Gas
   Distribution" and Artois as "Mortgage Finance". There is no French AIC
   register, so `KNOWN_HOLDCOS` names them by ISIN (verified against
   Euronext's list). Add to it rather than invent a heuristic. The UK proved
   that no heuristic separates holdings from asset managers.
4. **The currency repair tests every field per row, and Paris is not Warsaw.**
   On USD reporters Yahoo's EPS, market cap and EV are in euros, and EBITDA
   and net income are in dollars (Poland's split). Book value differs by
   company: against the latest quarterly equity, TotalEnergies, Viridien,
   M&P and STMicro come out euro (k 0.86–0.89, r 0.888) and Vallourec dollar
   (k 1.03). Assuming Poland's behaviour would have broken TotalEnergies'
   correct P/B. The test takes the nearer hypothesis unless the margin is
   under 25% of their separation, because TTM EPS sums four quarters at four
   rates and its ratio scatters ±7% (M&P 0.828). Poland's fixed 1.6× band
   would have refused all three multiples of the CAC 40's largest company.
5. **Foreign secondary = Euronext names another home market first.**
   ArcelorMittal (CAC 40, but "Euronext Amsterdam, Paris"), Solvay and Aperam
   are out. Airbus, STMicro, Stellantis and Technip Energies are
   "Euronext Paris" and stay. `--include-secondary` keeps them all.
6. **Financials = ICB industry 30.** Real estate is ICB industry 35, so it
   keeps EV/EBITDA with no carve-out.
7. **Peers: ICB sector → ICB industry** (Poland's sector → macro shape). On
   2026-10-06, 111 survivors: sector→supersector scored 71, sector→industry
   92, and the passing set was the same bar one at the margin. Banks have
   only 3 in their sector, so they benchmark against Financials on P/E and
   P/B.
8. Germany's invariants 2, 4, 5, 7, 8, 11, 12 and 14 hold unchanged.

## Liquidity gate: USD 1m (not 4m), measured 2026-10-06

| ADV floor | names | relative / absolute / history / any |
|---|---|---|
| none | 153 | 24 / 10 / 9 / 34 |
| USD 1m (default) | 111 | 17 / 7 / 6 / 24 |
| USD 2m | 90 | 11 / 6 / 6 / 17 |
| USD 4m | 76 | 9 / 4 / 6 / 15 |

(The none/2m/4m rows were measured with the earlier supersector fallback.)
Yahoo's volume is Euronext Paris only; Cboe and Turquoise are not in it, so
this is a lower bound, as with Germany's Xetra figure. Germany kept 4m
because what it removed was near-zero free float. In Paris, 1–4m is ordinary
SBF 120 floats (Rubis, SEB, Imerys, Eramet, Coface, Ipsos, BIC, Rémy
Cointreau). What goes under 1m is what should go: Odet, Lagardère, Fnac
Darty, Peugeot Invest, Robertet, controlled or under offer.

## Calibration, measured rather than inherited

153 names past the size gate, hygiene applied, 2026-10-06:

| | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| P/E | 8.57 | 11.02 | **16.21** | 26.11 | 36.26 |
| P/B | 0.66 | 0.91 | **1.46** | 2.83 | 5.17 |
| EV/EBITDA | 4.84 | 6.40 | **8.53** | 12.97 | 19.29 |
| ROE % | −5.34 | 2.80 | **9.45** | 13.84 | 19.34 |
| Yield % | 0.00 | 0.70 | **2.53** | 4.34 | 6.51 |

At the default gate (111 names), **P/B < 1 passes 25%** (exactly p25) and
**EV/EBITDA < 8 passes 32%**. EV < 7 would put it at 23% and change no
final pass: of the 18 names below book and below 8× (or financials below
book), the ROE floor leaves 8, fair P/B 7 and the dividend floor 7. So
Korea's 1.0 / 8.0 stay, as Germany kept them. Cost of equity stays 10%.

**Board dimension does not matter here.** Across all priced lines median P/B
is 1.10 Main vs 1.05 Growth. Only 3 Growth names clear USD 600m, so this is
one run and one page.

## Results, reference run (2026-10-06 close)

```
630 lines (328 Main, 292 Growth, 10 home elsewhere)
  574 priced (5 market caps via fast_info)
  181 cleared USD 600m
      -12 CCIs, -12 SIICs, -1 VC vehicle, -10 secondaries, -3 fund quotes
  112 cleared USD 1m/day (-1 Robertet CI); -1 Christian Dior (parent of LVMH)
   17 cheap vs peers (after ROE ≥ 5%)
    7 cheap outright (3 banks via the P/B + ROE carve-out)
    6 cheap vs own history (4 found by nothing else)
   24 passing at least one screen
```

Browser verdict at default thresholds equals Python's: 24 / 17 / 7 / 6, and
every per-test count (P/B < 1 28, EV < 8 35, fair 16, yield 61, carve-out 3;
history 9 / 19 / 8 → 6).

## Known gaps

1. **No state flag.** The French state's stakes (Orange, Engie, Renault,
   Thales, Safran, Airbus, ADP, Air France-KLM, FDJ, Eramet, Eutelsat) are
   published by the APE, whose site refuses automated requests (403). The
   closely-held flag catches some of them (ADP, Thales) for a different
   reason.
2. **The closely-held flag is coarse.** Yahoo's insider figure lumps family,
   state and industrial partners (L'Oréal 57% = Bettencourt + Nestlé). 33
   of 112 survivors carry it.
3. **Non-consolidating holdcos depend on a list.** A new listed holding
   company will not be tagged until it is added to `KNOWN_HOLDCOS`.
4. **ADV is Euronext Paris only.** See the liquidity section.
5. Four filed years for the history screen; trailing multiples only.

## Likely first failures

- **Yahoo throttles silently.** The run refuses to screen under half priced.
  The cache per session date fills gaps on re-run.
- **Euronext changes the factsheet layout.** `check_setup.py` asserts
  TotalEnergies → 60101000 and a CRCAM → certificate. A failed parse leaves
  ICB empty, counted as `icb_missing` in the funnel, not guessed.
- **The statements cache stores derived records.** Bump
  `STATEMENTS_CACHE_VERSION` when `build_statement_record` changes. The
  factsheet cache has its own `FACTSHEET_CACHE_VERSION`.

## Keeping in step with Korea

```bash
python sync_dashboard_from_korea.py ../Korea/dashboard.py
```

Every substitution must match exactly once; misses exit 1. Then grep for
Korean text, KOSPI, PER/PBR and `ticker`, and confirm in a browser that the
page's verdict equals the Python funnel at default thresholds.

## Publishing

`.github/workflows/screen.yml` runs at 17:00 UTC on weekdays (after the
Euronext Paris close in both CET and CEST), builds `site/`, and deploys to
GitHub Pages, with a retry after a pause for Yahoo throttling. The page is
unlisted (`noindex` plus a blanket `robots.txt`), but the repo is public,
which free Pages requires. No screen output is committed.

Live: https://shkon1215-netizen.github.io/france-screener/.

**Euronext and Yahoo both answer GitHub's runners**, confirmed on the first
run, 2026-10-06: roster 630, 574 priced, 181 factsheets fetched with no
failures, no retry needed. The funnel matched the local run line for line
(181 / 112 / 24, with 17 / 7 / 6). If the roster or factsheet step ever fails
in CI while working locally, suspect Euronext blocking the runner range
before a code change.

A docs-only push does not trigger a run (`paths-ignore: **.md`); start one with
`gh workflow run screen.yml`.

Research tool, not investment advice.
