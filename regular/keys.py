"""One reader for provider key files, shared by configuration and setup."""
import logging
from pathlib import Path


def read_api_keys(path, default_key=""):
    path = Path(path)
    if not path.exists():
        return [default_key] if default_key else []
    try:
        with path.open(encoding="utf-8") as stream:
            keys = [line.strip() for line in stream if line.strip() and not line.startswith("#")]
    except Exception:
        logging.getLogger("regular.config").warning("Could not read provider key file %s", path.name)
        return [default_key] if default_key else []
    return keys or ([default_key] if default_key else [])
