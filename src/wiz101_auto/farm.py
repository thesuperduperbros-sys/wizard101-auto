"""Farming a group dungeon: run it again and again with whatever team forms.

`state/farm.json` says which dungeon (its first room's zone id), which boss
ends a run, whether farming is on, and how many runs are done. The bot goes
to the dungeon's sigil, waits for a team (teamup.py), follows it through the
dungeon joining its fights, counts the run once the final boss is beaten and
leaves by the world hub button for the next one. `farm` / `farm --stop` on the
command line turn it on and off; the run count and the loot targets show on
the stream page (/stream). A stuck main quest turns it on by itself.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

FARM_FILE = Path("state") / "farm.json"

FARM_HEAL_ZONE = "Grizzleheim/GH_MainHub"

# What each farm is after (shown on the stream page, filled in as looted):
# (group, slot, item name).
TARGETS = {
    # The player (2026-10-04): the Waterworks' myth set, with a team, on foot
    # (movement.walk_only); the hood from Luska Charmbeak, the rest from
    # Sylster Glowstorm (docs/guides/Wizard City - Waterworks.md).
    "Waterworks": [
        ("Tricksy", "Hat", "Tricksy Hood"),
        ("Tricksy", "Robe", "Tricksy Cape"),
        ("Tricksy", "Shoes", "Tricksy Boots"),
    ],
    "Mount Olympus": [  # the player's Zeus set: farming stops once all are looted
        ("Zeus", "Hat", "Helmet of Zeus' Will"),
        ("Zeus", "Robe", "Zeus' Armor of Supremacy"),
        ("Zeus", "Shoes", "Boots of Zeus' Lore"),
    ],
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def is_target_mob(name: str, target: str) -> bool:
    return _norm(name) == _norm(target)


def is_farm_zone(zone: str, target_zone: str) -> bool:
    target_zone = target_zone.rstrip("/")
    return zone == target_zone or zone.startswith(f"{target_zone}/")


def target_status(farm_name: str, looted: dict) -> list[dict]:
    """The farm's targets, each with whether it has been looted (by name, in
    state/looted_gear.json)."""
    have = {_norm(n) for n in looted}
    return [
        {"group": group, "slot": slot, "name": name, "have": _norm(name) in have}
        for group, slot, name in TARGETS.get(farm_name, [])
    ]


def load_looted() -> dict:
    try:
        return json.loads((Path("state") / "looted_gear.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


@dataclass
class Farm:
    dungeon: str = "Aquila/AQ_Z01_MountOlympus"
    name: str = "Mount Olympus"
    final_boss: str = "Zeus Sky Father"
    active: bool = False
    runs: int = 0
    complete: bool = False  # every target looted: farming is done (a stuck main quest won't restart it)

    @classmethod
    def load(cls, path: Path | None = None) -> Farm:
        try:
            data = json.loads((path or FARM_FILE).read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

    def save(self, path: Path | None = None):
        path = path or FARM_FILE
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    def record_run(self, path: Path | None = None) -> int:
        self.runs += 1
        self.save(path)
        logger.success(f"{self.name} run {self.runs} done")
        return self.runs

    def targets_done(self, looted: dict) -> bool:
        status = target_status(self.name, looted)
        return bool(status) and all(t["have"] for t in status)

    def ends_run(self, boss_names: list[str]) -> bool:
        """The fight just won had the boss that ends a run."""
        return any(self.final_boss.lower() == n.lower() for n in boss_names)
