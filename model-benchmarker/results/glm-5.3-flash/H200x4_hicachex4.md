# H200x4_hicachex4

Per-level latency / throughput (multiturn: shared prefix reused across each user's turns):

TTFT (ms): P50/P95/P99/P100 ascending;  tokens/s: higher is better so percentiles are inverted (P100 = slowest).

TTFT turn1 = first request (full prefill);  TTFT-post = turns 2+ (shared prefix reused — shows prefix-cache benefit).

| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) | TTFT turn1 P99 (ms) | TTFT turn1 P100 (ms) | TTFT-post P50 (ms) | TTFT-post P95 (ms) | TTFT-post P99 (ms) | TTFT-post P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |
|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | coding | 0 | 825.5 | 825.5 | 825.5 | 825.5 | 1240.8 | 1864.4 | 1931.9 | 1948.7 | 159.1 | 127.5 | 125.3 | 124.8 |
| 0 | 1 | creative | 0 | 745.7 | 745.7 | 745.7 | 745.7 | 1739.4 | 2032.7 | 2053.4 | 2058.6 | 82.8 | 74.3 | 72.9 | 72.5 |
| 0 | 1 | mixed | 0 | 740.2 | 740.2 | 740.2 | 740.2 | 2023.4 | 13680.9 | 15303.3 | 15708.9 | 200.1 | 117.8 | 114.6 | 113.8 |
| 0 | 4 | coding | 0 | 17130.0 | 17135.1 | 17135.6 | 17135.7 | 1079.8 | 1375.4 | 1383.1 | 1385.0 | 123.2 | 104.4 | 104.2 | 104.2 |
| 0 | 4 | creative | 0 | 813.0 | 818.8 | 819.4 | 819.5 | 1231.6 | 1764.9 | 1870.2 | 1896.5 | 107.7 | 76.5 | 75.0 | 74.7 |
| 0 | 4 | mixed | 0 | 750.7 | 751.7 | 751.7 | 751.7 | 1994.4 | 3793.6 | 3814.1 | 3819.3 | 119.3 | 91.7 | 86.9 | 85.7 |
| 0 | 32 | coding | 0 | 3672.8 | 6341.2 | 7529.1 | 7533.5 | 1160.1 | 1896.7 | 2132.8 | 2461.2 | 63.7 | 51.7 | 49.4 | 48.1 |
| 0 | 32 | creative | 0 | 16677.9 | 16730.9 | 16750.8 | 16759.1 | 1410.1 | 2426.5 | 15680.9 | 16010.6 | 51.0 | 38.7 | 35.8 | 35.6 |
| 0 | 32 | mixed | 0 | 2001.2 | 4809.2 | 4891.6 | 4897.3 | 23277.7 | 140306.1 | 153460.5 | 156459.2 | 70.3 | 48.0 | 46.1 | 45.4 |
| 0 | 64 | coding | 0 | 5600.3 | 11331.6 | 11413.7 | 11415.3 | 1650.5 | 72388.9 | 77942.1 | 84787.1 | 45.1 | 37.3 | 34.0 | 31.6 |
| 0 | 64 | creative | 0 | 5250.4 | 11012.9 | 11527.0 | 11532.7 | 23100.4 | 69630.4 | 81104.2 | 97446.3 | 42.2 | 30.5 | 27.8 | 22.6 |
| 0 | 64 | mixed | 8 | 5030.0 | 10799.3 | 11233.9 | 11241.9 | 253739.4 | 598962.7 | 892644.0 | 898083.5 | 58.2 | 36.2 | 27.7 | 1.0 |
| 32768 | 1 | coding | 0 | 2125.2 | 2125.2 | 2125.2 | 2125.2 | 1295.2 | 1338.6 | 1339.3 | 1339.5 | 133.2 | 120.2 | 119.2 | 119.0 |
| 32768 | 1 | creative | 0 | 1746.7 | 1746.7 | 1746.7 | 1746.7 | 1351.1 | 1718.0 | 1762.1 | 1773.1 | 91.7 | 86.8 | 86.4 | 86.3 |
| 32768 | 1 | mixed | 0 | 1942.6 | 1942.6 | 1942.6 | 1942.6 | 1451.0 | 1931.7 | 1995.0 | 2010.8 | 247.9 | 129.0 | 116.7 | 113.6 |
| 32768 | 4 | coding | 0 | 4276.4 | 5447.8 | 5538.2 | 5560.8 | 1319.2 | 1580.7 | 1666.0 | 1687.4 | 132.9 | 102.3 | 102.2 | 102.2 |
| 32768 | 4 | creative | 0 | 3981.1 | 5052.2 | 5127.0 | 5145.8 | 1282.6 | 1846.5 | 2069.8 | 2125.6 | 108.2 | 73.3 | 72.9 | 72.8 |
| 32768 | 4 | mixed | 0 | 4483.0 | 5535.3 | 5598.9 | 5614.8 | 2105.9 | 2795.8 | 3552.9 | 3742.2 | 156.9 | 87.5 | 85.7 | 85.2 |
| 32768 | 32 | coding | 0 | 20886.0 | 37592.6 | 38780.1 | 39010.3 | 1486.7 | 18987.6 | 26367.5 | 33757.8 | 61.1 | 46.3 | 43.1 | 41.3 |
| 32768 | 32 | creative | 0 | 21156.3 | 37875.6 | 38955.4 | 39137.0 | 1235.8 | 1983.1 | 2442.1 | 3199.4 | 60.9 | 39.8 | 38.4 | 36.9 |
| 32768 | 32 | mixed | 0 | 21113.1 | 37305.5 | 38455.1 | 38652.9 | 132188.2 | 179709.4 | 186861.1 | 200148.5 | 68.3 | 45.3 | 5.9 | 0.2 |
| 32768 | 64 | coding | 0 | 39671.3 | 225592.0 | 230357.6 | 231570.8 | 114645.0 | 149591.6 | 154933.5 | 160302.6 | 49.1 | 31.9 | 25.1 | 1.6 |
| 32768 | 64 | creative | 0 | 39228.4 | 442742.2 | 462986.6 | 472336.4 | 210414.1 | 284323.7 | 405694.0 | 554283.3 | 55.1 | 31.2 | 21.6 | 1.7 |
| 32768 | 64 | mixed | 35 | 42108.9 | 567692.8 | 571373.3 | 571665.7 | 294398.6 | 876166.8 | 895592.2 | 901006.3 | 59.6 | 30.0 | 0.8 | 0.6 |
| 512000 | 1 | coding | 0 | 23897.3 | 23897.3 | 23897.3 | 23897.3 | 2749.7 | 12550.3 | 13899.3 | 14236.5 | 112.6 | 106.9 | 106.5 | 106.4 |
| 512000 | 1 | creative | 0 | 22643.4 | 22643.4 | 22643.4 | 22643.4 | 3317.3 | 3605.3 | 3618.9 | 3622.3 | 86.6 | 82.7 | 82.1 | 82.0 |
| 512000 | 1 | mixed | 0 | 22812.4 | 22812.4 | 22812.4 | 22812.4 | 2839.1 | 4455.4 | 4667.0 | 4719.9 | 107.9 | 105.4 | 105.1 | 105.0 |

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
| 32768 | 1 | coding | 2125 | 1.00 | 15,419 |
| 32768 | 1 | creative | 1747 | 1.00 | 18,760 |
| 32768 | 1 | mixed | 1943 | 1.00 | 16,868 |
| 32768 | 4 | coding | 5561 | 1.30 | 23,571 |
| 32768 | 4 | creative | 5146 | 1.29 | 25,472 |
| 32768 | 4 | mixed | 5615 | 1.25 | 23,344 |
| 32768 | 32 | coding | 39010 | 1.87 | 26,879 |
| 32768 | 32 | creative | 39137 | 1.85 | 26,792 |
| 32768 | 32 | mixed | 38653 | 1.83 | 27,128 |
| 32768 | 64 | coding | 231571 | 5.84 | ~9,056 |
| 32768 | 64 | creative | 472336 | 12.04 | ~4,440 |
| 32768 | 64 | mixed | 571666 | 13.58 | ~3,668 |
| 512000 | 1 | coding | 23897 | 1.00 | 21,425 |
| 512000 | 1 | creative | 22643 | 1.00 | 22,611 |
| 512000 | 1 | mixed | 22812 | 1.00 | 22,444 |
