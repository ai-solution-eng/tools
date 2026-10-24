# OLD_RTXPRO6000x1

Per-level latency / throughput (unique-nonce requests, no cache reuse):

TTFT (ms): P50/P95/P99/P100 ascending;  tokens/s: higher is better so percentiles are inverted (P100 = slowest).

| ctx | users | task | failed | TTFT P50 (ms) | TTFT P95 (ms) | TTFT P99 (ms) | TTFT P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |
|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | mixed | 0 | 335.5 | 699.9 | 732.3 | 740.4 | 36.6 | 36.6 | 36.6 | 36.6 |
| 0 | 4 | mixed | 0 | 353.7 | 522.7 | 524.9 | 525.4 | 33.9 | 33.8 | 33.8 | 33.8 |
| 0 | 32 | mixed | 0 | 3789.4 | 130206.0 | 161996.9 | 162095.0 | 21.3 | 21.2 | 21.2 | 21.1 |
| 65536 | 1 | mixed | 0 | 12739.2 | 12773.1 | 12776.1 | 12776.8 | 31.9 | 31.9 | 31.9 | 31.9 |
| 65536 | 4 | mixed | 0 | 12821.9 | 46463.3 | 48793.8 | 49376.4 | 23.4 | 20.9 | 20.9 | 20.9 |
| 65536 | 32 | mixed | 62 | 260063.9 | 380315.0 | 420611.5 | 425155.9 | 19.8 | 18.3 | 17.9 | 17.8 |

## Derived: aggregate prefill throughput

Computed from the TTFT column above, not measured separately:

    aggregate prefill tok/s = users * ctx / (TTFT turn1 P100 in seconds)

All `users` requests fire at once, so the last first-token (P100) marks
when all `users * ctx` prompt tokens have been prefilled.  This is an
AGGREGATE rate: it is flat-to-rising with concurrency, because more users each
prefilling the same prompt is more total work in a similar window shape.
Dividing it by `users` gives the per-user rate, which falls ~1/users.

It does NOT mean the hardware is at peak: at ctx=32768 a single user already
saturates prefill, and the achieved FLOPS sit around 30% of the 4x H200 BF16
peak, limited by memory bandwidth and FP4/FP8 expert weights rather than FMA.

Caveats:

* `ctx` is the *nominal* padding target.  The padding rule assumes 4
  chars/token while real tokenizers average ~5.3, so the true prompt is
  roughly 25% shorter and these figures are ~25% high.
* P100 is a single sample (the slowest request).  A level whose P100/P50
  exceeds 2 is straggler-dominated and carries a leading `~`:
  treat it as a floor, not a measurement.

| ctx | users | task | P100 TTFT (ms) | P100/P50 | aggregate prefill tok/s |
|:---|---:|:---|---:|---:|---:|
| 65536 | 1 | mixed | 12777 | 1.00 | 5,129 |
| 65536 | 4 | mixed | 49376 | 3.85 | ~5,309 |
| 65536 | 32 | mixed | 425156 | 1.63 | 4,933 |
