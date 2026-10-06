# France (Euronext Paris) Relative Valuation Screener

**Live:** https://shkon1215-netizen.github.io/france-screener/ · rebuilt each
weekday after the Euronext Paris close

Finds Paris-listed stocks trading at a discount to their sector peers on P/E,
P/B and EV/EBITDA, and, independently, stocks that are cheap in absolute
terms or cheap against their own filed history. Each row also carries three
years of revenue, EBITDA and net profit.

**Defaults:** market cap ≥ USD 600M · median daily traded value ≥ USD 1M on
Euronext Paris · ≥20% below peer median on ≥2 of 3 metrics · ROE ≥ 5%.

This is the sixth market, after [Korea](https://github.com/shkon1215-netizen/korea-screener),
the UK, Japan, [Germany](https://github.com/shkon1215-netizen/germany-screener)
and [Poland](https://github.com/shkon1215-netizen/poland-screener). It is built
by combining what each earlier market had to learn. `screener.py` is the same
maths throughout. The data layer, the share-class hygiene and the
holding-company handling are specific to France.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python test_france.py    # offline logic check, no network needed
python check_setup.py    # tests every live call individually
python main_fr.py -v     # full run, ~7 minutes cold, seconds warm
```

```bash
python main_fr.py --skip-liquidity           # size alone gates
python main_fr.py --min-adv 4e6              # the cross-market liquidity bar
python main_fr.py --keep-pyramid-parents     # keep Christian Dior next to LVMH
python main_fr.py --include-secondary        # keep ArcelorMittal, Solvay, Aperam
python main_fr.py --include-cooperative      # see why the CCIs are off
python main_fr.py --exclude-holdcos          # drop Wendel, Eurazeo, Bolloré...
```

Results cache to `.fr_cache/` per session date, so re-runs are fast, and a
throttled Yahoo cannot silently shrink the universe.

## The dashboard

Every run writes a self-contained `fr_dashboard.html`. Open it directly, or run
`python serve.py` (on Windows, double-click `dashboard.cmd`) to make the
**Refresh** button work. The browser re-evaluates every threshold on the page.
Two things cannot be re-evaluated, and the page says so: the peer medians, and
companies below the run's market-cap floor.

## Where the data comes from

| | source |
|---|---|
| roster, home market of each line | Euronext's own equities download: Euronext Paris and Euronext Growth Paris |
| ICB classification, share class | Euronext's per-company factsheet (size survivors only) |
| multiples, EPS, book, yield, insider holding | Yahoo `.info` on the `.PA` line |
| liquidity | one batched Yahoo price download, 60-session median traded value |
| filed statements | Yahoo annual income statement and balance sheet, plus the latest quarterly balance sheet for dollar reporters |

## French traps this handles

**Cooperative certificates.** Thirteen Crédit Agricole regional banks list a
*certificat coopératif d'investissement*: a non-voting claim on a cooperative
bank whose members' shares never trade. Every one sits at 0.20–0.27× book.
They are excluded, the way Korea excludes preferred shares.

**Holding companies, which ICB cannot see.** Eurazeo and Peugeot Invest are
filed with Amundi, and Christian Dior with LVMH. Named holdings (Wendel,
Eurazeo, Peugeot Invest, the Bolloré cascade, Burelle, IDI) are tagged with
what they hold. A parent that *consolidates* a listed subsidiary is found
from the filings: identical revenue and operating profit, and a parent whose
equity is mostly minority interests. Dior over LVMH and Burelle over
OPmobility (below the liquidity gate) are both found this way. The parent is dropped so that one set of
earnings is not counted twice.

**Dollar reporters.** TotalEnergies, Vallourec, Maurel & Prom and STMicro
quote in euros and file in dollars. Yahoo's EV/EBITDA divides euros by
dollars on all of them. Its book value is already in euros for some
(TotalEnergies) and in dollars for others (Vallourec). Each field is tested
per company, then repaired or refused.

**Foreign secondaries.** Euronext names each line's home market first.
ArcelorMittal (Amsterdam) and Solvay (Brussels) are excluded. Airbus,
STMicro and Stellantis, whose home market is Paris, stay.

**SIICs** (French REITs) are excluded, because IAS 40 revaluations make their
P/E an appraisal. Developers (Nexity, Kaufman & Broad) stay.

Research tool, not investment advice.
