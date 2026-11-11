# Methodology

The original sections below describe the numerical/synthetic framework. For actual historical
quote replay, BTC inverse premium/settlement accounting, chronological selection, execution lag
and cost assumptions, see [historical protocol](historical_protocol.md). The historical hedger
uses observed option mid IV; the SVI experiment is separate, not an integrated SVI hedger.

## 1. Conventions and instrument scope

The core pricer treats a European option as an option on a forward with quote-currency value

```text
C = D [F N(d1) - K N(d2)]
P = D [K N(-d2) - F N(-d1)]
d1 = [ln(F/K) + 0.5 sigma^2 T] / [sigma sqrt(T)]
d2 = d1 - sigma sqrt(T)
```

where `D` is the discount factor and time uses ACT/365. For spot hedging, the simulator maps
`F = S exp((r-q)T)` and uses spot delta/gamma. Vega is reported per `1.00` absolute volatility,
not per volatility point.

Deribit BTC/ETH inverse option premiums are quoted in base currency. The REST adapter converts a
two-sided premium to a contemporaneous USD equivalent by multiplying by the separately requested
spot index. It retains the option's `underlying_price` as the expiry forward. With Deribit's
reported `r = ln(F/index)/T`, the discount factor is `D = index/F`, so the USD-equivalent inverse
formula is consistent with Black-76. The adapter does not relabel the source premium as an
exchange-native USD quote.

## 2. Quote validation and implied volatility

For option sign `phi` (`+1` call, `-1` put), Black-76 bounds are

```text
lower = D max(phi(F-K), 0)
upper = D F  for calls
upper = D K  for puts
```

The IV solver rejects prices outside those bounds. A price at the lower bound maps to zero
volatility; a price at the upper bound has no finite implied volatility and is rejected. Interior
prices use Brent's bracketed method, with an expanding upper-volatility bracket and explicit
iteration/tolerance limits.

Chain cleaning then applies, in order:

1. finite, positive inputs and non-expired maturity;
2. a strictly positive, non-crossed two-sided quote;
3. strict model-independent mid-price bounds;
4. maximum relative bid/ask spread and maximum absolute log-moneyness;
5. solvable positive mid IV and minimum normalized vega.

No rejected quote is silently replaced with mark IV. Accepted observations are weighted using IV
spread and open interest, with a cap that prevents one nominally tight quote from dominating a
slice.

## 3. Put-call parity forward estimate

Matched calls and puts imply

```text
F_i = K_i + (C_i - P_i) / D.
```

The estimator combines strike-level values with a spread-weighted median and reports median
absolute dispersion. A minimum of three matched strikes is required. This robust estimator is
available as a cross-check; Deribit snapshot calibration uses the supplied option underlying.

## 4. Raw-SVI surface

Each expiry is fitted in total-variance space:

```text
w(k) = a + b [rho(k-m) + sqrt((k-m)^2 + sigma^2)]
k = ln(K/F)
implied_vol = sqrt(w(k)/T).
```

Calls and puts at the same strike are aggregated by calibration weight. At least five distinct
strikes are required. The objective uses a robust soft-L1 loss, multiple deterministic starting
points, and penalties for:

- negative total variance, including the raw-SVI analytical minimum;
- left/right Lee wing slopes above 2;
- negative Gatheral-Jacquier density factor `g(k)`;
- negative total-variance differences versus the preceding expiry.

The post-fit diagnostic recomputes variance, `g(k)`, and adjacent calendar spreads on 401 points
over `k in [-1.5, 1.5]`. Strict calibration raises rather than publishing a surface when any metric
is below tolerance. The finite grid is disclosed because it is a control, not a global theorem.

Between expiries, total variance is linearly interpolated. Before the first and after the last
expiry, the nearest implied volatility is held constant, which makes total variance proportional
to time. Production extrapolation should instead be governed by an explicit long/short-term model.

## 5. Event-driven hedging and accounting

At each timestamp the simulator performs this sequence:

1. accrue the previous cash balance at the previous risk-free rate;
2. credit/debit continuous underlying carry on the previous spot hedge;
3. mark option and hedge PnL from the previous state to the current state;
4. calculate current option Greeks using only the current event;
5. ask the interval and/or delta-band policy whether a rebalance is due;
6. round the target hedge to the configured lot size;
7. execute buys at `spot * (1 + half_spread)` and sells at
   `spot * (1 - half_spread)`, then debit fees;
8. mark equity as `option value + hedge units * spot + cash`.

At exact expiry the option settles at intrinsic value, the hedge closes through the same cost
model, and option/hedge inventory is zeroed. An expiry observation is mandatory; the simulator
does not interpolate a settlement price using future information.

For each interval, option PnL is approximated by

```text
Delta dS + 0.5 Gamma dS^2 + Vega dVol + Theta dt + residual.
```

The residual contains cross-Greeks, smile dynamics, higher-order terms, and finite-event error. It
is not discarded. The terminal identity is

```text
terminal PnL
= option Greek terms + option residual + hedge PnL
 + financing/carry - execution costs.
```

The checked-in maximum absolute reconciliation error is below `7e-11` USD.

## 6. Dual-Currency Investment replication

The structured-product example is a stablecoin deposit with promised term redemption `A` and
conversion strike `K`. Its quote-currency-equivalent payoff is

```text
A                         if S_T >= K
(A/K) S_T                 if S_T < K
= A - (A/K) max(K-S_T, 0).
```

It is therefore a discounted cash redemption minus `A/K` European puts. The product module uses
that identity for price and Greeks, solves the coupon that makes model value equal principal, and
separately returns the actual settlement currency/amount. The coupon input is explicitly a
whole-term contractual rate, avoiding a hidden annualization convention. This simplified product
omits issuer credit, early termination, observation windows, and legal/business-day adjustments.

## 7. Reference experiment design

The offline surface benchmark uses a calendar-consistent raw-SVI family, small deterministic and
seeded IV noise, two-sided spreads, four expiries, 13 strikes per expiry, and both calls and puts.
Because the generating family is known, fit error and rejection behavior are measurable.

The hedge benchmark uses 250 common paths from a correlated lognormal-spot / mean-reverting
log-vol process. It compares:

- an unhedged short call;
- a four-hour delta hedge;
- a five-delta no-trade band.

The process is a scenario generator, not calibrated market dynamics. Its purpose is to expose how
hedge frequency trades transaction cost against terminal dispersion and tail loss.

## 8. Limitations and production extensions

- use synchronized WebSocket book/index/future data and exchange sequence numbers;
- construct discount and forward curves from liquid futures and parity diagnostics;
- calibrate bid and ask surfaces, not only a weighted mid surface;
- add surface time dynamics, jump risk, and vanna/volga hedging instruments;
- model perpetual or future hedges, funding, basis, margin, liquidation, and venue outages;
- replace proportional execution costs with depth-aware impact and order acknowledgements;
- add model-change approval, parameter stability limits, stale-data controls, and persistent
  market-data provenance.
