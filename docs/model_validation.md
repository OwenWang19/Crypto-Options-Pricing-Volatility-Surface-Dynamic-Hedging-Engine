# Model validation controls

| Risk | Control | Failure behavior |
| --- | --- | --- |
| Premium violates no-arbitrage bounds | Explicit discounted lower/upper bounds | IV inversion raises |
| Root solver returns a plausible wrong IV | Price-to-IV-to-price round trips over stress grid | Test fails |
| Analytic Greeks contain sign/scale errors | Central-difference delta, gamma, vega, volga | Test fails |
| Bad chain observations contaminate fit | Ordered validation with rejection counts | Observation excluded and reason recorded |
| One tight quote dominates calibration | Median-normalized weights with hard cap | Influence bounded |
| Negative SVI variance | Analytical/grid penalty plus post-fit check | Strict surface raises |
| Butterfly arbitrage | Gatheral-Jacquier density-factor penalty and independent grid scan | Strict surface raises |
| Calendar arbitrage | Sequential total-variance penalty and adjacent-slice grid scan | Strict surface raises |
| Calls/puts imply inconsistent forwards | Spread-weighted parity median and dispersion | Diagnostic exposes inconsistency |
| Hedge uses future settlement information | Exact-expiry event required; chronological event loop | Simulation raises on missing expiry |
| Hidden transaction cost | Bid/ask execution and fees flow through cash | Ledger and summary record cost |
| PnL attribution drops residual | Residual is an explicit ledger field | Terminal reconciliation remains complete |
| State/accounting drift | Terminal PnL identity checked path by path | Integration test fails |
| Policy comparison uses different markets | Seeded common paths for all policies | Experiment construction prevents mismatch |
| REST payload changes | Instrument/summary join and conversion test | Adapter test or skip count exposes breakage |
| Structured payoff is mis-specified | State-by-state DCI cash-minus-put replication | Product test fails |

## Governance notes

The static-arbitrage diagnostic is deliberately described as grid-based. Soft penalties improve
the optimizer's feasible-region behavior, while the independent strict check prevents publishing
a surface that violates the tested domain. Neither mechanism is represented as a mathematical
proof over all strikes.

Reference outputs include a run manifest, slice parameters, fitted/market IV pairs, rejection
counts, hedge summary, and one complete event ledger. These files are intended to make results
reproducible and reviewable rather than dependent on screenshots.
