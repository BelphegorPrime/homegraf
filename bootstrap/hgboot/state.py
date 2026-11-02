"""What the bootstrap has done and the credentials it created, in one JSON file (mode 0600) on a volume."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile


class State:
    def __init__(self, path: str | os.PathLike):
        self.path = pathlib.Path(path)
        self.data: dict = json.loads(self.path.read_text()) if self.path.is_file() else {}

    def get(self, section: str, key: str | None = None, default=None):
        value = self.data.get(section, {})
        return value if key is None else value.get(key, default)

    def set(self, section: str, key: str, value) -> None:
        self.data.setdefault(section, {})[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def write_env_file(path: str | os.PathLike, values: dict[str, str]) -> None:
    """KEY=value lines, mode 0600, replaced atomically (homegraf reads it at start: HOMEGRAF_ENV_FILE)."""
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: a value for an env file cannot contain a line break")
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    with os.fdopen(fd, "w") as f:
        f.write("# Written by the bootstrap of the homegraf stack; read by homegraf at start (HOMEGRAF_ENV_FILE).\n")
        for key, value in values.items():
            f.write(f"{key}={value}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
