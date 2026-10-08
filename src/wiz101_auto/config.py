"""Bot configuration, loaded from YAML (see config.example.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .combat.brain import Strategy
from .deck_plan import DeckPolicy


@dataclass
class QuestConfig:
    enabled: bool = True
    teleport: bool = True  # teleport to objectives; False = walk (slower, lower detection risk)
    accept_side_quests: bool = True  # every quest is experience; areas are cleared in order
    photomancy: bool = True
    # Abort questing if the objective hasn't changed for this long.
    stuck_minutes: float = 12.0
    # Flee fights the tracked quest doesn't need (never bosses or fights indoors).
    # Fleeing costs all mana, but teleporting to mana wisps gets it back quickly.
    flee_unneeded_fights: bool = True


@dataclass
class UpkeepConfig:
    use_potions: bool = True
    buy_potions: bool = True  # out of potions: a trip to Hilda Brewer in the Commons for a full set
    potion_health_ratio: float = 0.5
    potion_mana_ratio: float = 0.2
    collect_wisps: bool = True
    wisp_health_ratio: float = 0.8  # grab nearby wisps below this after a fight
    # Before questing on: recover (potion, wisps, resting away from mobs)
    # whenever health is below min_health_to_fight, until rest_until_health.
    min_health_to_fight: float = 0.8
    rest_until_health: float = 0.95
    # Spells cost mana: at 0 every card is grayed out, so top mana up too.
    min_mana_to_fight: float = 0.65
    rest_until_mana: float = 0.85
    rest_max_minutes: float = 8.0  # give up and stop the bot if still too low after this
    wisp_safe_distance: float = 1500.0  # skip wisps (and wisp spots) this close to a mob (patrols move)
    # Zones to go heal in when the current one has no wisps (first same-world match wins).
    # (Savarstaad Pass: 7 health-wisp spots within 3000 of its gate; Mirkholm
    # Keep, picked before for having the most, has none that close.)
    heal_zones: list[str] = field(default_factory=lambda: [
        "WizardCity/WC_Streets/WC_Unicorn", "Grizzleheim/GH_Hero"])

    def needs_recovery(self, health_ratio: float, mana_ratio: float = 1.0) -> bool:
        return health_ratio < self.min_health_to_fight or mana_ratio < self.min_mana_to_fight

    def recovered(self, health_ratio: float, mana_ratio: float) -> bool:
        return health_ratio >= self.rest_until_health and mana_ratio >= self.rest_until_mana


@dataclass
class SafetyConfig:
    stop_key: str = "ctrl+shift+q"
    pause_key: str = "ctrl+shift+p"
    max_hours: float = 0.0  # 0 = no limit: run until stopped
    mouseless: bool = True  # clicks through a memory hook so your real mouse stays free
    stall_seconds: float = 15.0  # nothing changes for this long -> escalating recovery (0 = off)
    battle_stall_seconds: float = 120.0


@dataclass
class CombatConfig:
    strategy: Strategy = field(default_factory=Strategy)
    max_discards: int = 4  # per round; the hand refills at the next round, so discards draw new cards
    flee_below: float = 0.0  # 0 disables fleeing
    rollouts: bool = True  # play each move out in the simulator and take the best (not a fight-ending move)
    adapt_deck: bool = True  # a lost fight: search a deck for those enemies, switch; the general deck after


@dataclass
class ProgressionConfig:
    enabled: bool = False
    school: str = ""  # blank = read from the game
    rebuild_on_start: bool = True
    check_minutes: float = 30.0  # re-read the spellbook this often (0 = only on events)
    auto_train: bool = True  # experimental: train spells when the trainer window opens
    remind_to_train: bool = True
    # Levels at which the school professor has new spells: the bot goes to train.
    train_levels: list[int] = field(default_factory=lambda: [1, 5, 8, 10, 16, 20, 22, 26, 33, 38, 42, 50])
    deck: DeckPolicy = field(default_factory=DeckPolicy)


@dataclass
class BossFarmConfig:
    boss: str = ""  # e.g. "Alicane Swiftarrow" (its dungeon is learned while questing)
    until_item: str = ""  # stop once the backpack holds this, e.g. "Humongofrog"
    max_runs: int = 0  # 0 = keep going


@dataclass
class DeckSearchConfig:
    """Cards the combat simulator's deck search never puts in a deck (read
    by combat/deckopt.py): named cards, whole schools (death: the simulator
    overrates their hits), and exceptions to the schools (Feint). Minion
    summons are never allowed (the player's rule)."""

    banned: list[str] = field(
        default_factory=lambda: ["Dark Sprite", "Vampire", "Blinding Light", "Earthquake"])
    banned_schools: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)


@dataclass
class PetConfig:
    """Pet dance-game grinding (petdance.py). `auto`: whenever the wizard's
    energy is full, mark, go to the Pet Pavilion and play until it runs out,
    then Recall back. When `stop_at_goal` is true, a pet stops at its goal
    stage (`goals`, by pet kind, lower case), or `default_goal`."""

    auto: bool = True
    stop_at_goal: bool = False
    goals: dict = field(default_factory=lambda: {"bloodbat": "adult", "fellhound": "adult"})
    default_goal: str = "mega"
    feed: bool = True  # feed the first snack offered after each win


@dataclass
class MovementConfig:
    # The player: look like someone playing, walking instead of teleporting.
    walk: bool = False  # teleports within a zone become walks (teleport only when walking fails)
    # Zone id prefixes where the bot never teleports, only walks (the Waterworks with a team).
    walk_only: list[str] = field(default_factory=list)


@dataclass
class Config:
    mode: str = "farm"  # quest | fight | farm | boss
    farm_seconds_between_fights: float = 2.0
    farm_zone: str = "Grizzleheim/GH_Hero"
    farm_mob: str = "Troubled Warrior"
    log_file: str = "wiz101-auto.log"
    gear_checks: bool = True  # try on gear after level-ups and new items (slow: minutes per check)
    gear_checks_new_items: bool = True  # false: only after level-ups (new loot is still logged)
    quest: QuestConfig = field(default_factory=QuestConfig)
    upkeep: UpkeepConfig = field(default_factory=UpkeepConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    combat: CombatConfig = field(default_factory=CombatConfig)
    progression: ProgressionConfig = field(default_factory=ProgressionConfig)
    boss_farm: BossFarmConfig = field(default_factory=BossFarmConfig)
    deck_search: DeckSearchConfig = field(default_factory=DeckSearchConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)


def _merge(obj: Any, data: dict[str, Any], path: str = "") -> Any:
    known = {f.name: f for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{path}{key} must be a mapping")
            _merge(current, value, f"{path}{key}.")
        else:
            if isinstance(current, float) and isinstance(value, int):
                value = float(value)
            if current is not None and not isinstance(value, type(current)):
                raise ValueError(f"{path}{key} should be {type(current).__name__}, got {value!r}")
            setattr(obj, key, value)
    return obj


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path:
        p = Path(path)
        if p.exists():
            _merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        else:
            raise FileNotFoundError(p)
    from .safety import parse_hotkey

    parse_hotkey(cfg.safety.stop_key)  # fail early on a typo, not mid-session
    parse_hotkey(cfg.safety.pause_key)
    if cfg.mode not in ("quest", "fight", "farm", "boss"):
        raise ValueError(f"mode must be quest, fight, farm or boss, not {cfg.mode!r}")
    if cfg.mode == "boss" and not cfg.boss_farm.boss:
        raise ValueError("mode: boss needs boss_farm.boss (the boss's name)")
    if cfg.mode == "farm" and (not cfg.farm_zone.strip() or not cfg.farm_mob.strip()):
        raise ValueError("mode: farm needs non-empty farm_zone and farm_mob")
    return cfg
