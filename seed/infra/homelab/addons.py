"""Building blocks for new apps: ready-made services (database, cache) added to the compose skeleton.

Each block is a compose file in addons/compose.<id>.yaml (so Renovate keeps its image current) with the metadata in an
`x-addon` key; the text below `services:` is appended to the app's compose file with __APP__ replaced by the app ID.
"""
from __future__ import annotations

import dataclasses
import pathlib

import yaml

DIR = pathlib.Path(__file__).parent / "addons"


@dataclasses.dataclass(frozen=True)
class Addon:
    id: str
    label: str
    description: str
    secrets: tuple[str, ...]          # variables the block needs, generated unless the caller says otherwise
    app_env: dict[str, str]           # environment added to the app's service so it can reach the block
    services: str                     # the block's service definitions, indented for the app's `services:`
    ui: bool = False                  # a tool for people (e.g. a database UI): the app neither waits for it nor uses it
    requires_any: tuple[str, ...] = ()  # needs at least one of these blocks next to it
    needs_port: bool = False          # publishes a port on the server (__PORT__): a free one is picked
    needs_host: bool = False          # names the address people reach it at (__HOST__)
    url_path: str = "/"               # where its web page starts (the RustFS console answers 403 at /)
    vpn: bool = False                 # the app runs in this block's network (network_mode), its port moves here
    exclusive: bool = False           # cannot be combined with other blocks
    needs_app_port: bool = False      # carries the app's published port (__HOST_PORT__, __CONTAINER_PORT__)

    def render(self, app_id: str, port: int | None = None, db: str = "", host: str | None = None,
               app_port: tuple[int, int] | None = None) -> str:
        """__APP__ = app ID, __PORT__ = the published port, __DB__ = the block from requires_any this one works with,
        __HOST__ = the server's address, __HOST_PORT__/__CONTAINER_PORT__ = the app's own port (host, container)."""
        if self.needs_port and port is None:
            raise ValueError(f"{self.id} needs a host port")
        if self.needs_host and not host:
            raise ValueError(f"{self.id} needs the server's address")
        if self.needs_app_port and app_port is None:
            raise ValueError(f"{self.id} needs the app's port")
        out = (self.services.replace("__APP__", app_id).replace("__PORT__", str(port)).replace("__DB__", db)
               .replace("__HOST__", host or ""))
        return out.replace("__HOST_PORT__", str(app_port[0] if app_port else "")).replace("__CONTAINER_PORT__", str(app_port[1] if app_port else ""))

    def info(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description, "secrets": list(self.secrets),
                "image": yaml.safe_load(self.services.replace("__PORT__", "0").replace("__DB__", "db").replace("__HOST__", "h")
                                      .replace("__HOST_PORT__", "1").replace("__CONTAINER_PORT__", "1"))[self.id]["image"],
                "ui": self.ui, "requiresAny": list(self.requires_any), "publishesPort": self.needs_port, "urlPath": self.url_path, "exclusive": self.exclusive, "vpn": self.vpn}


def catalog(directory: pathlib.Path = DIR) -> dict[str, Addon]:
    out = {}
    for path in sorted(directory.glob("compose.*.yaml")):
        text = path.read_text()
        meta = yaml.safe_load(text)["x-addon"]
        _, _, services = text.partition("\nservices:\n")
        addon_id = path.name[len("compose."):-len(".yaml")]
        out[addon_id] = Addon(addon_id, meta["label"], meta["description"], tuple(meta.get("secrets") or ()),
                              dict(meta.get("app_env") or {}), services.rstrip("\n") + "\n", bool(meta.get("ui")),
                              tuple(meta.get("requires_any") or ()), "__PORT__" in services, "__HOST__" in services,
                              str(meta.get("url_path") or "/"), bool(meta.get("vpn")), bool(meta.get("exclusive")),
                              "__CONTAINER_PORT__" in services)
    return out
