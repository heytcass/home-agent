"""Configuration, read from one TOML file written by the NixOS module.

Secrets are never in the file: each member's Matrix token is read from the
path in `token_file` (a sops-nix secret), and the Anthropic key comes from
the environment.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Member:
    name: str  # chief, fleet, ...
    user_id: str  # full Matrix user id
    token_file: str
    model: str
    workdir: str = ""  # where this member's Agent SDK runs happen
    socket: str = ""  # the member worker's Unix socket; empty means not configured

    def token(self) -> str:
        return Path(self.token_file).read_text().strip()


@dataclass
class Config:
    homeserver: str
    room_id: str
    tom: str  # Tom's Matrix user id
    alerts_user: str  # the account room-say posts as
    db_path: str
    memory_dir: str  # the git checkout holding lessons
    members_dir: str  # member definitions (read-only to the members)
    ollama_url: str = ""
    embed_model: str = "bge-m3"
    decision_model: str = ""  # empty disables the local router
    router_model: str = "claude-haiku-5-5"
    door_socket: str = ""  # the door's Unix socket; empty means no deploys, restarts, or reboots
    door_timeout_s: float = 4200  # longest wait for the door: a build, a switch, and its revert window
    hosts: list[str] = field(default_factory=lambda: ["suzuka", "spa", "silverstone", "monza"])  # in roll order
    alert_field: str = "alert"  # the key in an alert event's content that holds its labels
    daily_cap_usd: float = 3.00
    monthly_cap_usd: float = 95.00
    per_run_cap_usd: float = 0.75
    max_turns: int = 40
    run_timeout_s: float = 1800  # longest one member run may last, not counting time a deploy waits on a host
    run_idle_s: float = 600  # longest a worker may say nothing
    members: dict[str, Member] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get("STAFF_CONFIG", "/etc/home-staff/config.toml")
        raw = tomllib.loads(Path(path).read_text())
        members = {n: Member(name=n, **m) for n, m in raw.pop("members", {}).items()}
        return cls(members=members, **raw)
