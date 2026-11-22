# Full algorithm sources

Due to GitHub API payload limits in automated pushes, the complete `.py` sources for all research modules are shipped as base64 parts:

1. Ensure `archive/part_00.b64` … `archive/part_19.b64` are present.
2. Run:

```bash
python3 reconstruct_algos.py
```

This writes `all_algos_src.zip` and extracts every algorithm folder (except `ineff`) with full source, Builder constants, README, and `.env.example`.

Folders already on `main` with README + env are valid; the archive fills in any missing full `.py` bodies.
