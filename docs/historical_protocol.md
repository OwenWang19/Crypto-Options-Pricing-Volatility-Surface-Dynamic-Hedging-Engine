# Historical protocol — frozen before examining strategy outcomes

Created 2026-11-11. This is a retrospective research protocol, not an independently
registered experiment or a claim that the historical period was previously unseen.

## Data and population

- Cryptarbitrage's hourly Deribit BTC inverse-option panel, linked by Deribit Insights:
  2024-01-13 through 2024-07-27. Pin the original Parquet SHA-256.
- Binance official BTCUSDT one-minute spot archives, January–July 2024; verify their
  published SHA-256 files. Use OPEN only, not a contemporaneous candle's closing price.
- Official Deribit BTC/USD delivery prices, with pagination and explicit date coverage.
- Do not redistribute the source Parquet: public access does not establish a data license.

## Selection, timing and comparison

1. Each Friday at 09:00 UTC select the call and put nearest their expiry forward from
   information already recorded by that time; expiry is the following Friday 08:00 UTC.
   Require positive two-sided quotes, positive open interest, relative spread <= 25%,
   and a source snapshot no more than 70 minutes old. Tie-break by instrument name.
2. Freeze the selected instruments. Attempt entry at their next available snapshot,
   no later than two hours after the selection time. Short at the observed bid minus
   an additional 1% of bid premium, not at theoretical value or mark. Missing entry
   quotes produce an explicit skipped opportunity, not a replacement instrument.
3. Underlying hedges execute at the next strictly later whole minute OPEN, plus
   adverse slippage and fees. Delta comes only from an already-recorded option mid.
   No use of the next option snapshot to calculate the current hedge.
4. Sell one BTC option per case. Call and put cases are separate experiments, NOT
   independent samples; group them by entry week for statistics.
5. Compare unhedged, hourly, four-hour, and 0.02/0.05/0.10 BTC delta-band policies.
   No continuous-monitoring claim: options are observed approximately hourly.
6. Calibration period: positions settled by 2024-04-19 08:00 UTC. Temporal holdout:
   new selections from 2024-04-19 09:00 UTC. Fix the chosen band using calibration
   data only; all cost/latency sensitivities remain visible, not a new winner search.
7. Never discard an entered case because it lost money, lost two-sided quotes, or
   developed a data gap. Missing signal quotes freeze the previous delta; stale
   signals stop rebalancing after the configured limit and are counted. A held
   position can always reach the official payoff without future option quotes.

## Accounting and costs

- BTC-denominated option premium is converted to a USD-equivalent at observed spot
  on entry; the conversion incurs the same adverse spot cost assumption. BTC delivery
  liability is computed with the OFFICIAL delivery price and converted using the
  independently observed expiry Binance spot, not the delivery TWAP itself.
- Report USD-equivalent strategy PnL without a speculative BTC collateral investment.
  A USD cash account can finance spot inventory; baseline financing rate 0%, with
  explicit 5%/10% annual borrowing-cost sensitivities. This is not a margin simulator.
- Option fee: min(0.0003 BTC, 12.5% of premium) per contract. Weekly ITM delivery fee:
  min(0.00015 BTC, 12.5% of intrinsic BTC). These are declared fee assumptions,
  not an assertion of a fully versioned historical customer fee schedule.
- Spot baseline: 5 bp fee plus 1 bp adverse slippage per transaction; stress at
  10+2 and 20+5 bp. No fee-tier, maker-rebate or zero-impact advantage is assumed.
- USD/USDT parity is an explicit cross-venue conversion assumption. No measured
  historical depeg, L2 impact, available option size, margin calls or liquidation.

## Validation and uncertainty

- Premium minus settlement minus fees plus spot hedge PnL plus financing must equal
  terminal cash to numerical tolerance; retain every event and cash flow.
- Report case count, distinct week count, per-case and paired weekly hedge PnL,
  normalized PnL, hedge costs, stale events, drawdowns and tail outcomes. No capital
  return, annualized Sharpe or leverage claim without an actual margin/capital model.
- Use paired entry-week block bootstrap intervals for dispersion differences. An
  interval spanning zero is inconclusive. Weekly blocks do not eliminate all serial
  dependence; the six-month sample cannot establish rare-tail safety.
- Run real-data SVI calibration and held-out-strike validation separately. Calls and
  puts sharing a strike stay in the SAME split. Failed fits and diagnostics count.
  This does not establish that an SVI hedger was tested: primary delta uses observed
  mid IV with frozen-IV Greeks.

## Diagnostic-driven surface refinement (after the initial all-quote experiment)

The first all-call/put run is retained in `artifacts/historical/svi_all_quotes_baseline/`:
10/27 surfaces passed the grid gate, 6 failed the gate, and 11 did not converge. This
is a negative model-validation result, not silently discarded data. The primary hedge
and its parameter selection do not use those surfaces and are unchanged.

For the final surface experiment use out-of-the-money calls when K >= F and puts
when K <= F, with normalized vega >= 0.001. This avoids subtracting intrinsic value
from coarse deep-ITM BTC quotes to infer tiny time values. This refinement is a
post-diagnostic modelling decision, NOT an independently preregistered result.
Retain any remaining failures and do not claim that the synthetic 3.58 vol-bp error
transfers to market data. Group call/put duplicates by strike in the validation
split even when OTM selection usually leaves only one option type per strike.
