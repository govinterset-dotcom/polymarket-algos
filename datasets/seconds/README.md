# Spot seconds (Parquet)

Place `btc_seconds_YYYY-MM-DD_YYYY-MM-DD.parquet` files here.

Typical columns: `ts_sec`, `close` (verify with `pyarrow`).

See [../README.md](../README.md) and [../MANIFEST.md](../MANIFEST.md).

These files are **10–45 MB** each — prefer **Git LFS** or **Release** assets rather than normal git blobs if the host limits file size.
