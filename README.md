# Business Analyst Bot

A bot that continuously finds, screens and analyses small businesses, and drafts
offers structured so that **no cash leaves your pocket at closing**. The purchase
price is carried by the seller, and the business's own cash flow services the note.

It does three things, in order of how much they cost to run:

1. **Screens cheaply.** Every listing is scored deterministically — no model calls —
   on seller-financing posture, debt coverage, price discipline, durability, owner
   independence, industry fit and seller motivation. Most listings die here.
2. **Underwrites the structure.** For anything that survives, it solves for the
   largest price the cash flow can carry at your target DSCR, builds a seller note
   with a standby and interest-only ramp, and stress-tests the result against an
   overstated SDE.
3. **Analyses what's left.** Only the best-scoring deals get the eight analyst
   playbooks, because that is the only step that spends money.

## Quick start

```bash
git clone https://github.com/BuildingForward/Business-Analyst
cd Business-Analyst

# Screen the bundled sample deals. No API key, no spend, no network.
python3 -m business_analyst screen --source data/sample_listings.csv

# See exactly what would be sent to the model, without calling it.
python3 -m business_analyst run --source data/sample_listings.csv --provider dry-run

# The real thing.
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python3 -m business_analyst run --source data/sample_listings.csv --provider anthropic
```

Nothing but `anthropic` is required, and only for the last step. Everything else
is the standard library.

## What it produces

For the bundled sample data, 12 listings reduce to 7 candidates:

| Deal | Industry | Ask | SDE | Mult | Score | Offer | Cash | DSCR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Coastal Pest Defense | pest control | $780,000 | $268,000 | 2.9x | 80.9 | $662,933 | $0 | 1.50x |
| Sparkle Commercial Cleaning | commercial cleaning | $450,000 | $185,000 | 2.4x | 77.8 | $398,397 | $0 | 1.50x |
| Nimbus Managed IT | managed it services | $1,450,000 | $395,000 | 3.7x | 73.9 | $1,067,705 | $0 | 1.50x |

and the five rejections each carry a reason:

```
Bayside Pizzeria:      SDE $88,000 below the $100,000 floor
                       Seller has ruled out carrying paper; no cash means no deal
Atlas Freight:         Priced at 5.4x SDE, above the 4.5x limit
Sunshine Storage:      Priced at 8.1x SDE, above the 4.5x limit
Petal & Stem Florist:  SDE $62,000 below the $100,000 floor
Harborview Dental:     Industry 'medical practice' is excluded from the thesis
```

Each surviving deal gets a memo (`reports/<name>-<id>.md`) with the facts, the
score breakdown, the proposed structure, a stress test, and the written analysis;
plus a draft LOI where the structure clears.

## How the offer is built

The structure is deliberately boring, because boring is what sellers sign:

- **Seller note for the whole price.** Default 6% over 7 years.
- **3 months of standby.** No payments at all, so you bank working capital before
  the first one is due. Interest accrues to principal during this window.
- **12 months interest-only.** A ramp while you learn the business.
- **10% holdback** against reps, warranties and the working-capital true-up.
- **Earnout** bridging the gap between the ask and what the cash flow supports,
  so the seller still reaches their number if the business performs.

The price is not a multiple pulled from the air. It is solved by bisection against
the actual amortisation schedule, for the largest principal that still clears your
target DSCR after paying the operator a salary. The price the bot offers and the
coverage it reports therefore cannot disagree.

```bash
python3 -m business_analyst stress --deal <deal-id>
```

```
Coastal Pest Defense - price held at $662,933

 haircut         SDE    debt svc    DSCR   free cash  covers
      0%     268,000     138,667    1.50      69,333  yes
     10%     241,200     138,667    1.31      42,533  yes
     20%     214,400     138,667    1.11      15,733  NO
     30%     187,600     138,667    0.92     -11,067  NO
```

The price and the note are held **fixed** while the seller's earnings claim is
discounted. That is the number that matters: sellers add back aggressively, and a
note that only covers at the seller's own figures is how a no-money-down deal
becomes a personal liability. The memo reports the exact breakeven.

## The eight playbooks

`full_analysis`, `market_research`, `competitor_teardown`, `pricing_audit`,
`segmentation`, `swot`, `gtm`, `growth_plan` — each filled from the listing's own
facts. Inspect any of them without running anything:

```bash
python3 -m business_analyst prompts            # list all eight
python3 -m business_analyst prompts --show swot
```

Every playbook inherits a system prompt that requires the model to separate
**disclosed** facts from **inferences**, to name the diligence question when a
figure is missing, and never to invent a market size. An analyst that fabricates
a number is worse than no analyst when you are personally signing a note.

By default a run uses the three-playbook triage set; `--playbook` selects others.

## Controlling spend

This is a bot that runs continuously, so the money guards are part of the design:

| Guard | Flag | Default |
| --- | --- | --- |
| Score below which a deal never reaches the model | `--threshold` | 55 |
| Deals analysed per cycle | `--max-deals` | 5 |
| Hard cap on model calls per run | `--max-calls` | 24 |
| Disk cache of responses | `--cache-dir` | `.llm-cache` |
| Which playbooks run | `--playbook` | triage set (3 of 8) |

Deals already in the database are never re-fetched, re-scored or re-analysed, and
identical prompts are served from cache. A second run over unchanged sources costs
nothing. Use `--provider dry-run` to see the prompts before spending anything.

## Running continuously

```bash
python3 -m business_analyst watch --source data/sample_listings.csv \
    --provider anthropic --interval 3600
```

Each cycle discovers, screens, analyses and writes reports. A failing source or a
failing playbook is logged and skipped rather than taking the run down, and every
cycle is recorded in the `runs` table.

## Sources

| Spec | Meaning |
| --- | --- |
| `path/to/file.csv` | CSV with flexible column naming |
| `path/to/file.json` | JSON array, or `{"listings": [...]}` |
| `rss:<url>` | RSS 2.0 or Atom feed |
| `json-api:<url>` | JSON HTTP endpoint |

Column names are matched loosely, so a broker's export usually works unedited:
`Asking Price`, `ask`, `price` and `list price` all map to the same field, and
`$1.2M`, `1200000` and `1.2m` all parse to the same number.

**On scraping:** there is deliberately no HTML scraper for the big marketplaces.
Those break constantly and generally violate the sites' terms of service. Use a
feed or export you are entitled to, or add a source of your own against a site
whose terms you have read.

## Tuning the thesis

```bash
python3 -m business_analyst run --source deals.csv \
    --salary 80000 \        # operator salary taken before any debt service
    --target-dscr 1.75 \    # underwrite tighter
    --min-dscr 1.35 \       # floor below which a deal is not viable
    --rate 0.07 --term 10 \ # seller note terms
    --standby 6 --interest-only 18
```

`python3 -m business_analyst industries` prints the screening table — typical SDE
multiples, whether sellers in that industry commonly carry paper, capital
intensity and owner dependence.

## Commands

| Command | What it does |
| --- | --- |
| `screen` | Fetch and score. No model calls. |
| `run` | One full pass: discover, screen, analyse, report. |
| `watch` | Run continuously on an interval. |
| `analyze --deal <id>` | Run playbooks against one stored deal. |
| `show --deal <id>` | Print a stored deal's memo. |
| `offer --deal <id>` | Draft an LOI. |
| `stress --deal <id>` | Stress the offer against an overstated SDE. |
| `list` | Stored deals by score, with stage. |
| `prompts` / `industries` | Inspect the playbooks and the industry table. |

## Tests

```bash
python3 -m unittest discover -s tests
```

118 tests, standard library only, no network and no API key. The finance module
is tested hardest: amortisation against known values, that a fully amortising note
actually reaches a zero balance, that the solved price reproduces the DSCR it was
solved for, and that the number printed in a memo is recomputable from the note
printed beside it.

## Caveats

- Industry multiples are priors for ranking and sanity-checking an ask. They are
  not a valuation.
- The bot underwrites the **seller's** figures. It quantifies how wrong those
  figures can be before the deal breaks; it cannot tell you whether they are true.
  That is what a quality-of-earnings analysis is for.
- The LOI is a drafting aid. Have an attorney in the relevant jurisdiction review
  it before it reaches a seller.
- Nothing here is legal, tax or investment advice.
