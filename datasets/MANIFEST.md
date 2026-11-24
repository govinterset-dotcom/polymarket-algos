# Dataset manifest (research archive)

Files collected for path + seconds research. Publish into `datasets/paths/` and `datasets/seconds/` (or GitHub Release assets) as storage allows.

### Coverage (path)

Unique markets (windows) in **path**: **~12 600**  
Of which **BTC 5m ≈ 7 000**, **BTC 15m ≈ 3 200** (plus ETH/SOL/XRP in some dumps).

| size (bytes) | sha256-16 | file |
|-------------:|-----------|------|
| 42413983 | e7d0c53298e9ec0f | btc_seconds_2026-10-01_2026-10-27.parquet |
| 42413983 | e7d0c53298e9ec0f | btc_seconds_2026-10-01_2026-10-27-2.parquet *(duplicate content)* |
| 27437855 | 3cdfa69c0c2345e3 | btc_seconds_2026-10-13_2026-10-29.parquet |
| 21344430 | 835ec2c082b96d8e | btc_seconds_2026-10-27_2026-11-09.parquet |
| 23315852 | c20e327dc9024440 | btc_seconds_2026-10-27_2026-11-10.parquet |
| 24759845 | 065a32b438a6f94e | btc_seconds_2026-10-27_2026-11-12.parquet |
| 24759845 | 065a32b438a6f94e | btc_seconds_2026-10-27_2026-11-12-2.parquet *(duplicate content)* |
| 10729812 | 0db5589bf793891b | btc_seconds_2026-11-11_2026-11-16.parquet |
| 18717234 | 64e0cc2a6906fdab | rozi_path.jsonl |
| 8414044 | 3579aa89606957d4 | rozi15_path.jsonl |
| 15432228 | 096e5185261347d3 | rozi15s_path.jsonl |
| 17379946 | ebbeb1ff27679afa | rozi15s_path-2.jsonl |
| 21946613 | 36d72457271042e2 | rozi15s_path-3.jsonl |
| 8174275 | 6b3af3077bb52c5c | comeback_path.jsonl |
| 8174275 | 6b3af3077bb52c5c | comeback_path-2.jsonl *(duplicate content)* |
| 4338517 | 0d85eec774843708 | comeback_path (1).jsonl |
| 37349217 | bc47119c1bfce686 | lotto01_probe.jsonl |

Deduplicate by hash before publishing. Prefer `rozi15s_path-3.jsonl` as the longest 15m path set when available.

**Upload note:** binaries are too large for casual API commits; use `git lfs` or a GitHub **Release** named e.g. `datasets-2026-11` and keep this manifest in sync.
