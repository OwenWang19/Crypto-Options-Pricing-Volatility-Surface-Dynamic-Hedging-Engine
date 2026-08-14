# Resume bullets

Use only metrics from the checked-in seed-17 reference run, and retain the word "synthetic" in an
interview or project explanation.

## Compact version

- Built a Python crypto-options research engine with bounded Black-76 IV inversion, liquidity-
  weighted raw-SVI calibration, static-arbitrage diagnostics, and event-driven delta hedging.
- Implemented self-financing cash/spot accounting with bid-ask costs, fees, carry, exact-expiry
  settlement, and Greek PnL attribution; reconciled 250 path-level ledgers to below `7e-11` USD.
- On seeded synthetic spot/volatility scenarios, reduced short-call terminal PnL dispersion by
  92.2% using a five-delta hedge band while executing 78% fewer trades than four-hour hedging;
  reported costs and tail risk without making a historical-return claim.

## Interview framing

The strongest discussion is not the 92% number. Explain why:

1. the unhedged option naturally has much larger directional dispersion;
2. a common-path comparison isolates policy differences;
3. frequent hedging lowers discretization risk but increases cost;
4. Greek attribution is approximate while the cash-account identity is exact;
5. grid-based no-arbitrage diagnostics and a synthetic path generator have clear limitations.
