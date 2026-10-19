"""Operator behavior: safe initialization, portable services and truthful diagnostics."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from regular import cli, service

ROOT = Path(__file__).resolve().parent.parent


def config_json(directory, *fields, env=None):
    values = {k: v for k, v in os.environ.items() if not k.startswith(("QQ_", "ZHIPU_", "GEMINI_", "OPENAI_", "VISION_", "OWNER_", "AI_", "FALLBACK_", "DATA_", "SSL_", "STATE_", "ARCHIVE_", "MEMORY_", "LOG_", "STATUS_", "BANTER_"))}
    values.update(REGULAR_HOME=str(directory), PYTHONPATH=str(ROOT))
    if env:
        values.update(env)
    code = "import config,json; print(json.dumps({n:getattr(config,n) for n in " + repr(fields) + "}))"
    result = subprocess.run([sys.executable, "-c", code], env=values, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class ProductConfigTest(unittest.TestCase):
    def test_safe_defaults_and_central_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            data = config_json(directory, "SSL_VERIFY", "STATE_FILE", "ARCHIVE_DIR", "AI_PROVIDER", "VISION_ENABLED", "VISION_MODEL", "VISION_PROVIDER")
            self.assertTrue(data["SSL_VERIFY"])
            self.assertEqual(data["AI_PROVIDER"], "openai")
            self.assertTrue(data["VISION_ENABLED"])
            self.assertEqual(data["VISION_PROVIDER"], "openai")
            self.assertEqual(data["VISION_MODEL"], "")
            self.assertEqual(data["STATE_FILE"], str((Path(directory) / "data/state.json").resolve()))
            self.assertEqual(data["ARCHIVE_DIR"], str((Path(directory) / "data/archive").resolve()))
            self.assertFalse((Path(directory) / "data").exists())

    def test_blank_disables_provider_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            data = config_json(directory, "FALLBACK_PROVIDER", env={"FALLBACK_PROVIDER": ""})
            self.assertEqual(data, {"FALLBACK_PROVIDER": ""})

    def test_old_layout_is_retained_without_moving_data(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "state.json"
            old.write_text('{"previous": true}')
            data = config_json(directory, "STATE_FILE", "LEGACY_DATA_LAYOUT")
            self.assertEqual(data["STATE_FILE"], str(old.resolve()))
            self.assertTrue(data["LEGACY_DATA_LAYOUT"])
            self.assertEqual(old.read_text(), '{"previous": true}')

    def test_explicit_data_and_old_per_file_override(self):
        with tempfile.TemporaryDirectory() as directory:
            data = config_json(directory, "STATE_FILE", "OWNER_FILE", env={"DATA_DIR": "var", "STATE_FILE": "kept/state.json"})
            self.assertEqual(data["STATE_FILE"], str((Path(directory) / "kept/state.json").resolve()))
            self.assertEqual(data["OWNER_FILE"], str((Path(directory) / "var/owner.txt").resolve()))

    def test_dotenv_quotes_comments_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text('BOT_NAMES="name#one,name two" # comment\nQQ_APP_ID=from-file\n')
            data = config_json(directory, "BOT_NAMES", "QQ_APP_ID", env={"QQ_APP_ID": "from-environment"})
            self.assertEqual(data["BOT_NAMES"], ["name#one", "name two"])
            self.assertEqual(data["QQ_APP_ID"], "from-environment")

    def test_model_chains_can_be_configured_without_source_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            data = config_json(directory, "PROVIDER_PRESETS", env={"ZHIPU_MODELS": "first, second,first", "ZHIPU_MODELS_DIGEST": "digest"})
            self.assertEqual(data["PROVIDER_PRESETS"]["zhipu"]["models"], ["first", "second"])
            self.assertEqual(data["PROVIDER_PRESETS"]["zhipu"]["models_digest"], ["digest"])

    def test_documented_defaults_match_example(self):
        fields = ("BANTER_PROBABILITY", "OWNER_MENTION_PROBABILITY", "BANTER_COOLDOWN_SECONDS", "DAILY_BUDGET", "AI_MAX_ATTEMPTS", "LOG_LEVEL")
        with tempfile.TemporaryDirectory() as directory:
            actual = config_json(directory, *fields)
        lines = (ROOT / ".env.example").read_text().splitlines()
        examples = dict(line[2:].split("=", 1) for line in lines if line.startswith("# ") and "=" in line and line[2:].split("=", 1)[0] in fields)
        for field in fields:
            self.assertIn(field, examples)
            self.assertEqual(str(actual[field]), str(type(actual[field])(examples[field])))
        for readme in ("README.md", "README_EN.md"):
            self.assertNotIn("AI_DAILY_BUDGET", (ROOT / readme).read_text())


class DemoConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_identity_controls_use_the_shared_demo_adapter(self):
        calls = []
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def reply(self, message, **identity):
                calls.append((message, identity))
                return "ok"
        with patch("regular.demo.DemoSession", Session), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await cli.demo(["查好感", "/user bob", "/group friends", "查好感", "/user", "/quit", "not reached"]), 0)
        self.assertEqual(calls, [("查好感", {"user_id": "alice", "group_id": "demo-group"}),
                                 ("查好感", {"user_id": "bob", "group_id": "friends"})])


class InitializationTest(unittest.TestCase):
    def test_init_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            cli.initialize(directory)
            target = Path(directory) / ".env"
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            target.write_text("precious-existing-config")
            cli.initialize(directory)
            self.assertEqual(target.read_text(), "precious-existing-config")

    def test_generic_init_asks_for_explicit_endpoint_and_models(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            with patch("builtins.input", side_effect=["123", "老群友", "", "https://gateway.example/v1", "first,second"]), patch("getpass.getpass", side_effect=["qq-secret", "model-secret"]) as secrets:
                cli.initialize(directory, interactive=True)
            from dotenv import dotenv_values
            values = dotenv_values(Path(directory) / ".env")
            self.assertEqual(values["AI_PROVIDER"], "openai")
            self.assertEqual(values["OPENAI_BASE_URL"], "https://gateway.example/v1")
            self.assertEqual(values["OPENAI_MODELS"], "first,second")
            self.assertEqual(values["VISION_ENABLED"], "true")
            self.assertNotIn("VISION_PROVIDER", values)
            self.assertNotIn("VISION_MODEL", values)
            self.assertNotIn("OWNER_CLAIM_PHRASE", values)
            self.assertEqual(secrets.call_count, 2)
            actual = config_json(directory, "VISION_PROVIDER", "VISION_MODEL")
            self.assertEqual(actual, {"VISION_PROVIDER": "openai", "VISION_MODEL": "first"})
            self.assertNotIn("model-secret", output.getvalue())

    def test_interactive_secret_roundtrip_without_echo(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            secret = 'private #value with "quote" and \\ path'
            with patch("builtins.input", side_effect=["123", "阿王", "zhipu"]), patch("getpass.getpass", side_effect=[secret, secret]):
                cli.initialize(directory, interactive=True)
            self.assertNotIn(secret, output.getvalue())
            actual = config_json(directory, "QQ_APP_SECRET", "PROVIDER_PRESETS", "VISION_PROVIDER", "VISION_MODEL")
            self.assertEqual(actual["QQ_APP_SECRET"], secret)
            self.assertEqual(actual["PROVIDER_PRESETS"]["zhipu"]["api_key"], secret)
            self.assertEqual(actual["VISION_PROVIDER"], "zhipu")
            self.assertEqual(actual["VISION_MODEL"], "glm-4.7")


class ServiceTest(unittest.TestCase):
    def test_plist_roundtrip_arbitrary_directory_and_venv(self):
        directory = '/tmp/a user/Code & <friends> "好"'
        python = '/tmp/a user/.venv/bin/python'
        data = plistlib.loads(plistlib.dumps(service.launchd(directory, python)))
        self.assertEqual(data["WorkingDirectory"], str(Path(directory).resolve()))
        self.assertEqual(data["ProgramArguments"][0], python)
        self.assertEqual(data["EnvironmentVariables"], {"PYTHONUNBUFFERED": "1"})
        self.assertNotIn("proxy", repr(data).lower())

    def test_systemd_quotes_spaces_percent_and_quotes(self):
        text = service.systemd('/tmp/space name/100%"x$HOME', '/tmp/venv $name/bin/python')
        self.assertIn('100%%\\"x$HOME', text)
        working = next(line for line in text.splitlines() if line.startswith('WorkingDirectory='))
        self.assertIn('$HOME', working)
        self.assertNotIn('$$HOME', working)
        self.assertIn('ExecStart=:"/tmp/venv $name/bin/python" "-m" "regular"', text)
        self.assertNotIn("root", text)

    def test_default_launchd_script_only_generates(self):
        text = (ROOT / "tools/launchd/install.sh").read_text()
        self.assertIn('${1:-generate}', text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pmset", text)
        self.assertNotIn("192.168.", text)


class StatusTest(unittest.TestCase):
    def test_fresh_and_stale_status_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            config = SimpleNamespace(STATUS_FILE=str(path))
            data = {"schema_version": 1, "updated_at": time.time(), "pid": 12, "lifecycle": "ready", "gateway_connected": True, "providers": {}, "budget": {"day": "2026-10-19", "used": 2, "limit": 3, "remaining": 1}}
            path.write_text(json.dumps(data))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.status(config, as_json=True), 0)
            self.assertFalse(json.loads(output.getvalue())["stale"])
            data["updated_at"] -= 3600
            path.write_text(json.dumps(data))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.status(config, as_json=True), 1)
            self.assertTrue(json.loads(output.getvalue())["stale"])

    def test_absent_status_does_not_claim_running(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.status(SimpleNamespace(STATUS_FILE=str(Path(directory) / "absent")), as_json=True), 1)
            self.assertFalse(json.loads(output.getvalue())["available"])

    def test_doctor_local_never_creates_clients_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "from unittest.mock import patch; from regular.cli import main; import sys; guard=patch('httpx.AsyncClient', side_effect=AssertionError('no client')); guard.start(); main(['--directory'," + repr(directory) + ", 'doctor','--json']); assert 'bot' not in sys.modules; assert 'runtime' not in sys.modules"
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "REGULAR_HOME": directory, "DATA_DIR": directory, "ZHIPU_API_KEY": "", "GEMINI_API_KEY": ""}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["network"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_doctor_explains_private_claim_and_inherited_vision(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
                   "QQ_APP_ID": "fake", "QQ_APP_SECRET": "fake", "AI_PROVIDER": "openai",
                   "OPENAI_BASE_URL": "https://model.invalid/v1", "OPENAI_API_KEY": "fake-secret",
                   "OPENAI_MODELS": "chat-model,backup-model"}
            result = subprocess.run([sys.executable, "-m", "regular", "--directory", directory,
                                     "doctor", "--json"], env=env, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row["check"]: row for row in json.loads(result.stdout)["checks"]}
            self.assertIn("first non-empty private message", rows["owner_claim"]["detail"])
            self.assertIn("usage scope", rows["qq_permissions"]["detail"])
            self.assertEqual(rows["qq_permissions"]["state"], "manual")
            self.assertIn("openai/chat-model", rows["vision"]["detail"])
            self.assertIn("inherited", rows["vision"]["detail"])
            self.assertIn("unverified", rows["vision"]["detail"])
            self.assertNotIn("fake-secret", result.stdout)

    def test_doctor_network_includes_the_separate_vision_endpoint_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
                   "QQ_APP_ID": "fake", "QQ_APP_SECRET": "fake", "AI_PROVIDER": "openai",
                   "OPENAI_BASE_URL": "https://chat.invalid/v1", "OPENAI_API_KEY": "fake-chat",
                   "OPENAI_MODELS": "text-model", "VISION_PROVIDER": "gemini",
                   "VISION_MODEL": "image-model", "GEMINI_API_KEY": "fake-vision"}
            code = """from unittest.mock import patch
from types import SimpleNamespace
from regular.cli import main
urls=[]
class MetadataClient:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **kwargs):
        urls.append(url)
        return SimpleNamespace(is_success=True, status_code=200)
with patch('httpx.Client', MetadataClient):
    assert main(['--directory', DIRECTORY, 'doctor', '--network', '--json']) == 0
assert urls == ['https://chat.invalid/v1/models', 'https://generativelanguage.googleapis.com/v1beta/openai/models'], urls
""".replace("DIRECTORY", repr(directory))
            result = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["network"])
            rows = {row["check"]: row for row in report["checks"]}
            self.assertEqual(rows["network.gemini"]["state"], "ok")
            self.assertIn("gemini/image-model", rows["vision"]["detail"])
            self.assertIn("explicit", rows["vision"]["detail"])

    def test_doctor_missing_provider_dependency_keeps_json_and_skips_import(self):
        with tempfile.TemporaryDirectory() as directory:
            code = """import builtins, importlib.util, json
from regular.cli import main
real_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name: None if name == 'openai' else real_spec(name)
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'providers':
        raise AssertionError('missing dependency must skip provider import')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
assert main(['--directory', DIRECTORY, 'doctor', '--json']) == 1
""".replace("DIRECTORY", repr(directory))
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["ok"])
            self.assertTrue(any(row["check"] == "dependency.openai" and row["state"] == "error" for row in report["checks"]))

    def test_doctor_config_dependency_failure_is_json(self):
        missing = ModuleNotFoundError("test missing dotenv", name="dotenv")
        with patch("regular.cli.load_config", side_effect=missing), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["doctor", "--json"]), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertTrue(any(row["check"] == "configuration" and "dotenv" in row["detail"] for row in report["checks"]))

    def test_paid_probe_isolates_operator_arguments_and_restores_them(self):
        captured = []
        original = ["regular", "--directory", "/tmp/instance", "probe", "probe_cache", "--allow-paid"]
        with patch.object(sys, "argv", original), patch("regular.cli.load_config", return_value=object()), patch("runpy.run_path", side_effect=lambda *a, **k: captured.append(list(sys.argv))):
            self.assertEqual(cli.main(["probe", "probe_cache", "--allow-paid"]), 0)
            self.assertIs(sys.argv, original)
        self.assertEqual(captured, [[str(ROOT / "tests/probes/probe_cache.py")]])

    def test_probe_requires_explicit_paid_flag_before_loading(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()), patch("regular.cli.load_config", return_value=object()), patch("runpy.run_path") as run:
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--directory", directory, "probe", "probe_cache"])
            self.assertEqual(raised.exception.code, 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
