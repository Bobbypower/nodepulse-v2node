# v2node patch candidates

The default build applies the patches directly under `ops/v2node/patches/`. Keep experimental patches in this directory so the default build and rolling release exclude them.

## Legacy traffic delivery guard

`0010-traffic-legacy-delivery-guard.patch` is the original candidate moved from `ops/v2node/patches/`. Its SHA256 is `724818f68a77177e548428f455e79fa345bfc92e76be7304735ce9887fc8d4b6`.

The patch drains sampled traffic counters atomically, reports unsuccessful HTTP statuses and suppresses the legacy traffic POST's transport retries. Unknown outcomes can still leave unreported traffic. Lossless retry requires a durable sender queue and a receiver receipt committed with the billing transaction.

For isolated review, apply it after the existing v2node patch series to upstream `cf513e1f0ba5f22eef6b40e65c22e3e9856d86b2`, using the patched Xray pin `e087e74a1bf25b46f2776d1a3b53a68f6018d654`. The Windows Go 1.27.1 synthetic tests passed. A Linux build and real-node acceptance are still pending. Keep the locked runtime package separate from this candidate.
