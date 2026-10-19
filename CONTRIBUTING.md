# Contributing

Use Python 3.10+ in an isolated environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python tools/test_offline.py
```

Keep state assembly in `runtime.py`, provider selection in `providers.py`, and
relationship/name rules in their existing domain modules. Reuse those entry
points rather than adding a second implementation in CLI/demo/tests. New behavior
needs a focused regression for the concrete failure and an explanation of its
user-visible effect. Documentation defaults are tested in `test_product_cli.py`.

The default test suite is offline and blocks socket connections. Mock all QQ and
model transports. Real probes are separate: `regular probe probe_cache --allow-paid`
requires your own credentials and consumes quota. Never attach secrets, raw chat
archives or private OpenIDs to an issue. Include `regular --version`, Python/platform,
and redacted `regular doctor --json` output. Review paths and bot aliases before
sharing diagnostics. Keep Chinese and English quick starts aligned when changing
operator behavior. Build with `python -m build`; release/tag/upload is a separate
maintainer action.

Product mechanisms draw on [NoneBot's Console quick start](https://nonebot.dev/docs/quick-start),
[Errbot's text-mode initialization and daemon examples](https://errbot.readthedocs.io/en/latest/user_guide/setup.html),
and [AstrBot's init/run CLI](https://docs.astrbot.app/en/use/cli.html).
The project keeps its small domain modules rather than adding those frameworks.
