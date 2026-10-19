"""Small operator entry point; no QQ/model imports until a command needs them."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
import time

from . import __version__


def load_config(directory):
    os.environ["REGULAR_HOME"] = str(Path(directory).expanduser().resolve())
    return importlib.import_module("config")


def initialize(directory, *, interactive=False):
    """Create a private config once. Never overwrite it or display its secrets."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ".env"
    values = {"QQ_APP_ID": "", "QQ_APP_SECRET": "", "BOT_NAMES": "",
              "AI_PROVIDER": "openai", "OPENAI_BASE_URL": "", "OPENAI_API_KEY": "",
              "OPENAI_MODELS": "", "ZHIPU_API_KEY": "", "GEMINI_API_KEY": "",
              "FALLBACK_PROVIDER": "", "OWNER_CLAIM_PHRASE": "", "SSL_VERIFY": "true",
              "VISION_ENABLED": "false", "VISION_PROVIDER": "openai", "VISION_MODEL": ""}
    if target.exists():
        print(f"Config already exists; kept unchanged: {target}")
        return 0
    if interactive:
        values["QQ_APP_ID"] = input("QQ App ID: ").strip()
        values["QQ_APP_SECRET"] = getpass.getpass("QQ App secret (hidden): ")
        values["BOT_NAMES"] = input("Bot names (comma separated): ").strip()
        provider = input("Provider [openai/zhipu/gemini, default openai]: ").strip().lower() or "openai"
        if provider not in ("openai", "zhipu", "gemini"):
            raise ValueError("Provider must be openai, zhipu or gemini")
        values["AI_PROVIDER"] = provider
        values["VISION_PROVIDER"] = provider
        if provider == "openai":
            values["OPENAI_BASE_URL"] = input("OpenAI-compatible base URL (include /v1 if required): ").strip()
            values["OPENAI_MODELS"] = input("Chat model IDs (ordered, comma separated): ").strip()
        values[f"{provider.upper()}_API_KEY"] = getpass.getpass("Provider API key (hidden): ")
        values["OWNER_CLAIM_PHRASE"] = getpass.getpass("Owner claim phrase (hidden; blank disables): ")
    # JSON double-quoted strings are valid dotenv values, including escaped newlines.
    text = "# Created by regular init. Keep this file private.\n"
    text += "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()) + "\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Config already exists; kept unchanged: {target}")
        return 0
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    (directory / "data").mkdir(exist_ok=True, mode=0o700)
    print(f"Created {target} (0600). Fill credentials, then run regular doctor.")
    return 0


def _path_writable(path):
    path = Path(path)
    while not path.exists():
        parent = path.parent
        if parent == path:
            break
        path = parent
    return os.access(path, os.W_OK)


def _doctor_result(rows, network, as_json):
    result = {"ok": not any(r["state"] == "error" for r in rows), "network": network, "checks": rows}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"[{row['state'].upper()}] {row['check']}: {row['detail']}")
        print("No model inference was made. Missing QQ credentials are expected before init/configuration.")
    return 0 if result["ok"] else 1


def doctor(config, *, network=False, as_json=False, dependency_error=None):
    """Inspect only config, dependencies and paths unless --network is explicit."""
    rows = []
    def add(name, state, detail):
        rows.append({"check": name, "state": state, "detail": detail})
    add("python", "ok" if sys.version_info >= (3, 10) else "error", sys.version.split()[0])
    dependencies_ready = True
    for module in ("dotenv", "botpy", "openai", "httpx", "aiohttp", "certifi"):
        present = importlib.util.find_spec(module) is not None
        dependencies_ready = dependencies_ready and present
        add(f"dependency.{module}", "ok" if present else "error", "installed" if present else "Install project dependencies: pip install .")
    if config is None:
        add("configuration", "error", f"Settings unavailable: missing dependency {dependency_error or 'unknown'}. Install project dependencies before retrying.")
        return _doctor_result(rows, False, as_json)
    for error in config.validate():
        add("configuration", "error", error)
    add("bot_names", "ok" if config.BOT_NAMES else "warning",
        ", ".join(config.BOT_NAMES) if config.BOT_NAMES else "No aliases; only platform nickname/mentions will trigger replies")
    add("owner_claim", "ok" if config.OWNER_CLAIM_PHRASE != "认主" else "warning",
        "disabled" if not config.OWNER_CLAIM_PHRASE else
        ("public legacy phrase; replace it" if config.OWNER_CLAIM_PHRASE == "认主" else "configured (hidden)"))
    add("tls", "ok" if config.SSL_VERIFY else "warning", "verified" if config.SSL_VERIFY else "verification disabled")
    if "AI_DAILY_BUDGET" in os.environ:
        add("legacy_budget_name", "warning", "AI_DAILY_BUDGET is not read; use DAILY_BUDGET (old documentation used the wrong name)")
    for name in ("STATE_FILE", "OWNER_FILE", "STATUS_FILE", "LOG_FILE", "ARCHIVE_DIR", "MEMORY_DIR"):
        path = getattr(config, name)
        add(f"path.{name}", "ok" if _path_writable(path) else "error", path)
    state = Path(config.STATE_FILE)
    if state.exists():
        try:
            value = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            add("state_json", "ok", "readable JSON object")
        except (OSError, ValueError) as error:
            add("state_json", "error", type(error).__name__)
    if config.LEGACY_DATA_LAYOUT:
        add("data_layout", "warning", "Existing root data is retained. Back up before explicitly setting DATA_DIR; no automatic migration.")
    else:
        add("data_layout", "ok", config.DATA_DIR)
    capabilities = None
    if dependencies_ready:
        try:
            import providers
            capabilities = providers.configuration()
        except ImportError as error:
            dependencies_ready = False
            add("provider_dependencies", "error", f"Cannot import provider dependencies: {error.name or 'unknown'}")
    else:
        add("provider_capabilities", "warning", "Not inspected while dependencies are missing")
    if capabilities is not None:
        for warning in capabilities.get("migration_warnings", []):
            add("provider_migration", "warning", warning)
        if capabilities.get("request_policy"):
            add("request_policy", "ok", json.dumps(capabilities["request_policy"], ensure_ascii=False))
        if capabilities.get("ignored_settings"):
            add("provider_migration", "warning", "Legacy settings no longer affect requests: " + ", ".join(capabilities["ignored_settings"]))
        for name in capabilities["provider_chain"]:
            provider = capabilities["providers"][name]
            add(f"provider.{name}", "ok" if provider["key_count"] else "warning",
                f"{provider['key_count']} key(s); chat={','.join(provider['models'])}; "
                f"digest={','.join(provider['digest_models'])}; judge={','.join(provider['judge_models'])}; access unverified")
        vision = capabilities["vision"]
        enabled = config.VISION_ENABLED and vision["configured"]
        add("vision", "ok" if enabled or not config.VISION_ENABLED else "warning",
            f"{vision['provider']}/{vision['model']}; configured; access unverified" if enabled else
            ("disabled by configuration" if not config.VISION_ENABLED else "unavailable: vision provider key missing; text chat remains available"))
    add("qq_permissions", "manual", "QQ console: enable C2C, add sandbox members, and allow all group messages. Local checks cannot verify these switches.")
    if network and not dependencies_ready:
        add("network", "warning", "Skipped: dependencies must be installed first")
    if network and dependencies_ready:
        import httpx
        names = dict.fromkeys(n for n in (config.AI_PROVIDER, config.FALLBACK_PROVIDER) if n)
        with httpx.Client(verify=config.SSL_VERIFY, timeout=10.0) as client:
            for name in names:
                preset = config.PROVIDER_PRESETS[name]
                keys = config.load_api_keys(name)
                if not preset["base_url"]:
                    add(f"network.{name}", "warning", "base URL missing; skipped")
                    continue
                if not keys:
                    add(f"network.{name}", "warning", "no configured key; skipped")
                    continue
                try:
                    response = client.get(preset["base_url"].rstrip("/") + "/models",
                                          headers={"Authorization": f"Bearer {keys[0]}"})
                    add(f"network.{name}", "ok" if response.is_success else "error",
                        f"metadata HTTP {response.status_code}; no inference; does not prove model quota/capabilities")
                except (httpx.HTTPError, httpx.InvalidURL) as error:
                    add(f"network.{name}", "error", type(error).__name__)
    return _doctor_result(rows, network and dependencies_ready, as_json)


def status(config, *, as_json=False):
    path = Path(config.STATUS_FILE)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
            raise ValueError("unsupported snapshot schema")
        age = max(0, time.time() - float(snapshot["updated_at"]))
    except FileNotFoundError:
        print(json.dumps({"available": False, "path": str(path)}) if as_json else
              f"No runtime snapshot at {path}. Start regular run to publish status.")
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        if as_json:
            print(json.dumps({"available": False, "path": str(path), "error": "invalid_snapshot"}))
        else:
            print("Invalid/unreadable runtime snapshot; run regular doctor.", file=sys.stderr)
        return 1
    result = {"available": True, "age_seconds": round(age, 1), "stale": age > 120, **snapshot}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Runtime: {snapshot.get('lifecycle', 'unknown')} | snapshot age {age:.0f}s" + (" (stale)" if age > 120 else ""))
        print(f"Gateway: {snapshot.get('gateway_connected', 'unknown')} | PID: {snapshot.get('pid', 'unknown')}")
        for name in ("providers", "budget", "tasks"):
            if name in snapshot:
                print(f"{name}: {json.dumps(snapshot[name], ensure_ascii=False)}")
    return 1 if age > 120 or snapshot.get("lifecycle") in ("stopped", "error") else 0


async def demo(messages):
    # The shared adapter owns routing/state/budget/sending; this CLI has no chat rules.
    from regular.demo import DemoSession
    identity = {"user_id": "alice", "group_id": "demo-group"}
    async with DemoSession() as session:
        help_text = "Try 查好感 / 掷骰子 / 群史记; /user NAME and /group ID switch identity; /quit exits."
        print("Local demo: no QQ connection or model requests. " + help_text)

        async def dispatch(message):
            command, _, value = message.strip().partition(" ")
            if command in ("/quit", "/exit"):
                return True
            if command == "/help":
                print(help_text)
            elif command in ("/user", "/group"):
                value = value.strip()
                if not value:
                    print(f"Usage: {command} " + ("NAME" if command == "/user" else "ID"))
                else:
                    identity["user_id" if command == "/user" else "group_id"] = value
                    print(f"Now speaking as {identity['user_id']} in {identity['group_id']}.")
            elif message.strip():
                print(await session.reply(message, **identity))
            return False

        if messages:
            for message in messages:
                print(f"{identity['user_id']}@{identity['group_id']}> {message}")
                if await dispatch(message):
                    break
            return 0
        while True:
            try:
                message = input(f"{identity['user_id']}@{identity['group_id']}> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if await dispatch(message):
                break
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="regular", description="The Regular: local demo, configuration, diagnostics and QQ runtime")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--directory", default=os.getcwd(), help="deployment directory containing .env (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a private .env without overwriting existing config")
    init.add_argument("--interactive", action="store_true", help="prompt for credentials with hidden secret input")
    diag = sub.add_parser("doctor", help="check local configuration; no network by default")
    diag.add_argument("--network", action="store_true", help="GET provider metadata, never generate model output")
    diag.add_argument("--json", action="store_true")
    stat = sub.add_parser("status", help="read runtime's structured snapshot")
    stat.add_argument("--json", action="store_true")
    sub.add_parser("run", help="validate and connect the QQ runtime")
    dm = sub.add_parser("demo", help="offline interactive demo through the production message handler")
    dm.add_argument("messages", nargs="*", help="optional messages for a noninteractive demo")
    probe = sub.add_parser("probe", help="run an explicitly authorized paid research probe")
    probe.add_argument("name", help="file stem in tests/probes, e.g. probe_cache")
    probe.add_argument("--allow-paid", action="store_true", help="authorize real model requests and quota consumption")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return initialize(args.directory, interactive=args.interactive)
        if args.command == "demo":
            return asyncio.run(demo(args.messages))
        try:
            config = load_config(args.directory)
        except ImportError as error:
            if args.command == "doctor":
                return doctor(None, network=args.network, as_json=args.json, dependency_error=error.name)
            raise
        if args.command == "doctor":
            return doctor(config, network=args.network, as_json=args.json)
        if args.command == "status":
            return status(config, as_json=args.json)
        if args.command == "run":
            errors = config.validate()
            if errors:
                for error in errors:
                    print(error, file=sys.stderr)
                print("Run regular init --interactive and regular doctor.", file=sys.stderr)
                return 1
            import bot
            bot.run_bot()
            return 0
        if not args.allow_paid:
            parser.error("probe requires --allow-paid; it makes real model requests")
        source_root = Path(__file__).resolve().parent.parent
        candidate = source_root / "tests" / "probes" / f"{args.name}.py"
        if Path(args.name).name != args.name or not candidate.is_file():
            parser.error("unknown probe; inspect tests/probes in the source checkout")
        previous_argv = sys.argv
        try:
            # Probe scripts have their own argv semantics (some treat every arg
            # as a model); never leak operator CLI flags into their requests.
            sys.argv = [str(candidate)]
            runpy.run_path(str(candidate), run_name="__main__")
        finally:
            sys.argv = previous_argv
        return 0
    except (OSError, ValueError) as error:
        print(f"{type(error).__name__}: command failed; check paths/configuration with regular doctor", file=sys.stderr)
        return 1
