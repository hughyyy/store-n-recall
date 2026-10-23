"""Environment-driven configuration with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Default store location when STORE_N_RECALL_DB is unset. Honors XDG_DATA_HOME
# per the XDG base directory spec; an empty XDG_DATA_HOME means unset.
_DEFAULT_DB_REL = os.path.join("store-n-recall", "facts.db")


@dataclass(frozen=True)
class Config:
    db_path: str
    default_project: str | None = None
    agent: str = "unknown"

    def resolve_project(self, project: str | None) -> str:
        """Project resolution: the param wins, then the env fallback, else error.

        There is deliberately no cwd-derived default — the MCP process cwd is
        wherever the client launched it, not the agent's repo.
        """
        if project is not None and project.strip():
            return project.strip()
        if self.default_project:
            return self.default_project
        raise FactError(
            "project required: pass `project` or set STORE_N_RECALL_PROJECT"
        )


class FactError(Exception):
    """A user-facing error: invalid arguments, unknown ids, scope problems."""


def _xdg_data_home() -> str | None:
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    return raw or None


def load_config() -> Config:
    db = os.environ.get("STORE_N_RECALL_DB", "").strip()
    if not db:
        home = os.path.expanduser("~")
        if _xdg_data_home():
            base = _xdg_data_home()
        elif home:
            base = os.path.join(home, ".local", "share")
        else:
            raise FactError(
                "cannot locate a data directory: set STORE_N_RECALL_DB explicitly"
            )
        db = os.path.join(base, _DEFAULT_DB_REL)
    else:
        db = os.path.expanduser(db)

    project = os.environ.get("STORE_N_RECALL_PROJECT", "").strip() or None
    agent = os.environ.get("STORE_N_RECALL_AGENT", "").strip() or "unknown"
    return Config(db_path=db, default_project=project, agent=agent)
