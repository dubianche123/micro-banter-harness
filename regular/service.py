"""Generate service definitions from resolved paths; never install/start a service."""
from __future__ import annotations
import argparse
from pathlib import Path
import plistlib
import sys

LABEL = "io.github.the-regular"


def launchd(directory, python):
    directory = Path(directory).expanduser().resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(Path(python).expanduser().absolute()), "-m", "regular", "--directory", str(directory), "run"],
        "WorkingDirectory": str(directory),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(directory / "data" / "service.log"),
        "StandardErrorPath": str(directory / "data" / "service.log"),
    }


def _systemd_quote(value):
    # systemd's documented quoted string escaping; no shell or XML substitution.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'


def systemd(directory, python):
    directory = Path(directory).expanduser().resolve()
    command = " ".join(_systemd_quote(v) for v in (Path(python).expanduser().absolute(), "-m", "regular", "--directory", directory, "run"))
    return f"""[Unit]
Description=The Regular QQ companion
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={_systemd_quote(directory)}
ExecStart=:{command}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("launchd", "systemd"))
    parser.add_argument("--directory", default=str(Path.cwd()))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.kind == "launchd":
        (Path(args.directory).expanduser().resolve() / "data").mkdir(parents=True, exist_ok=True)
        with output.open("wb") as stream:
            plistlib.dump(launchd(args.directory, args.python), stream, sort_keys=False)
    else:
        output.write_text(systemd(args.directory, args.python), encoding="utf-8")
    print(f"Generated {output}; not installed or started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
