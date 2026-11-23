# Publishing large datasets

Path JSONL and seconds parquet files are **multi‑MB to ~45 MB**. GitHub soft-limits and API commits are awkward for bulk binaries.

## Recommended

1. **Git LFS** (best for ongoing updates):

```bash
git lfs install
git lfs track "datasets/paths/*.jsonl"
git lfs track "datasets/seconds/*.parquet"
# add files, commit, push
```

2. **GitHub Release** (simple one-shot archive):

- Create release e.g. `datasets-2026-11`
- Attach deduplicated files from `MANIFEST.md`
- Keep `MANIFEST.md` hashes matching the release assets

## Deduplicate first

Several local dumps share the same content hash (see `MANIFEST.md`). Publish **one** file per unique hash.

## After upload

Update `MANIFEST.md` and this note’s release tag if you use Releases so AI agents and humans know where to download.
