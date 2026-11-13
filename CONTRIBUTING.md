# Contributing

Thanks for taking an interest in this project. Bug reports, reproducibility checks, documentation improvements, and carefully tested research contributions are welcome.

## Before opening an issue

- Search existing issues to avoid duplicates.
- Identify the strategy (`comeback`, `premove`, or `rozi`) and the exact file/version.
- State whether the observation comes from historical replay, paper mode, or live execution.
- Include timeframe, date range, configuration, and relevant log excerpts.
- Remove wallet addresses, private keys, API credentials, and other sensitive information from logs.

## Reporting a bug

Please include:
1. Python version and operating system.
2. Installation steps and command used to run the strategy.
3. Relevant environment variables ( redact secrets; include only non-sensitive settings).
4. Expected behavior and actual behavior.
5. A minimal reproducible example or sanitized journal excerpt, if possible.

Never attach a real `.env` file or secrets.

## Proposing a strategy or parameter change

A useful research contribution should document:
- the hypothesis and signal definition;
- dataset dates, markets, and timeframes;
- all parameters and filters;
- entry-price and fill assumptions;
- fees, slippage, and resolution methodology;
- number of trades, win rate, net PnL, return on turnover, drawdown, and loss streaks;
- out-of-sample results, where available.

Avoid presenting a result from a parameter set selected on the same sample as independent validation. Clearly distinguish replay, paper trading, and live fills.

## Pull requests

- Keep changes focused and explain the reason for each change.
- Preserve the default paper-trading safety mode.
- Do not add credentials, private keys, account identifiers, or personal trading data.
- Update the README and example configuration when behavior or parameters change.
- Test the affected strategy before submitting.

By contributing, you agree that your contribution may be distributed under the repository's eventual project license. If no license has been selected yet, please discuss licensing before submitting substantial code.
