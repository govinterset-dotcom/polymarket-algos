# dozi

**Companion / early impulse**

## Strategy

Related impulse family with taker entries and volume filters. Monitors book early–mid window and takes when delta and volume conditions align. Designed as a companion to gozi-style late impulses.

## Files

| File | Role |
|------|------|
| `dozi.py` | Full strategy source |
| `dozi.env.example` | Config template — copy to `dozi.env` |

## Quick start

```bash
cd dozi
cp dozi.env.example dozi.env
# edit PAPER=true and keys if needed
python dozi.py
```

Paper mode is the default. Live trading requires `PRIVATE_KEY` and explicit disable of paper flags.

## License

Same as repository root (Polymarket Builder Attribution Source-Available License 1.0).
