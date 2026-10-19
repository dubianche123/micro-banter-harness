"""Run self-contained tests with outbound socket connections forbidden."""
from pathlib import Path
import os
import socket
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def deny_connect(*args, **kwargs):
    raise AssertionError("Network forbidden in offline tests; mock the provider/transport")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="regular-tests-") as directory:
        os.environ["REGULAR_HOME"] = directory
        os.environ["DATA_DIR"] = directory
        os.environ["AI_PROVIDER"] = "zhipu"
        os.environ["FALLBACK_PROVIDER"] = ""
        os.environ["SSL_VERIFY"] = "true"
        # Empty values also prevent a developer's .env or account from affecting CI.
        for name in ("QQ_APP_ID", "QQ_APP_SECRET", "ZHIPU_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
            os.environ[name] = ""
        socket.socket.connect = deny_connect
        socket.socket.connect_ex = deny_connect
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        raise SystemExit(not result.wasSuccessful())
