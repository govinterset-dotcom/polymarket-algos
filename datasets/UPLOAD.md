# Publishing large datasets

Full path JSONL and seconds parquet are multi‑MB. The GitHub web UI and a normal `git push` (ideally with **Git LFS**) are the reliable ways to publish them.

## One-shot script (copy-paste)

On your machine, put all dump files in one folder (example: `~/polydata`), then:

```bash
# 1) clone
cd ~
git clone https://github.com/govinterset-dotcom/polymarket-algos.git
cd polymarket-algos

# 2) optional but recommended for large files
git lfs install
git lfs track "datasets/paths/*.jsonl"
git lfs track "datasets/seconds/*.parquet"
git add .gitattributes

# 3) copy UNIQUE files only (adjust SRC)
SRC=~/polydata
mkdir -p datasets/paths datasets/seconds

cp "$SRC/rozi15s_path-3.jsonl"   datasets/paths/
cp "$SRC/rozi_path.jsonl"         datasets/paths/
cp "$SRC/rozi15_path.jsonl"       datasets/paths/
cp "$SRC/rozi15s_path.jsonl"      datasets/paths/
cp "$SRC/comeback_path.jsonl"     datasets/paths/

cp "$SRC/btc_seconds_2026-10-01_2026-10-27.parquet" datasets/seconds/
cp "$SRC/btc_seconds_2026-10-13_2026-10-29.parquet" datasets/seconds/
cp "$SRC/btc_seconds_2026-10-27_2026-11-09.parquet" datasets/seconds/
cp "$SRC/btc_seconds_2026-10-27_2026-11-10.parquet" datasets/seconds/
cp "$SRC/btc_seconds_2026-10-27_2026-11-12.parquet" datasets/seconds/
cp "$SRC/btc_seconds_2026-11-11_2026-11-16.parquet" datasets/seconds/

# skip duplicates: *-2.parquet, comeback_path-2, "comeback_path (1)*"

# 4) commit + push
git add datasets/ .gitattributes
git status
git commit -m "data: add unique path JSONL and BTC seconds parquet"
git push origin main
```

Login: use a [Personal Access Token](https://github.com/settings/tokens) with `repo` scope when Git asks for a password.

## Without LFS

Create a **Release** on GitHub → tag e.g. `datasets-2026-11` → attach the same files. Keep `MANIFEST.md` in sync.

## Why the agent cannot upload the full archive

Automation commits go through a size-limited API channel. Docs and small text files work; ~10–45 MB binaries do not. Your local `git push` / Release UI does not have that limit the same way.
