from pathlib import Path

import pytest

from wiz101_auto.combat.model import EffectKind, Target
from wiz101_auto.combat.reader import average_effects, map_effect
from wiz101_auto.config import load_config


@pytest.mark.parametrize(
    "effect,target,param,kind,tgt",
    [
        ("damage", "enemy_single", 100, EffectKind.DAMAGE, Target.ENEMY_SINGLE),
        ("damage", "enemy_team", 90, EffectKind.DAMAGE, Target.ENEMY_ALL),
        ("steal_health", "enemy_single", 80, EffectKind.STEAL, Target.ENEMY_SINGLE),
        ("heal", "self", 400, EffectKind.HEAL, Target.SELF),
        ("heal_over_time", "friendly_team", 300, EffectKind.HOT, Target.ALLY_ALL),
        ("modify_outgoing_damage", "self", 35, EffectKind.BLADE, Target.SELF),
        ("modify_outgoing_damage", "friendly_single", 35, EffectKind.BLADE, Target.ALLY_SINGLE),
        ("modify_outgoing_damage", "enemy_single", -25, EffectKind.WEAKNESS, Target.ENEMY_SINGLE),
        ("modify_incoming_damage", "enemy_single", 30, EffectKind.TRAP, Target.ENEMY_SINGLE),
        ("modify_incoming_damage", "self", -50, EffectKind.SHIELD, Target.SELF),
        ("modify_card_damage", "spell", 100, EffectKind.ENCHANT_DAMAGE, Target.SPELL),
        ("modify_card_accuracy", "spell", 10, EffectKind.ENCHANT_ACCURACY, Target.SPELL),
        ("stun", "enemy_single", 1, EffectKind.STUN, Target.ENEMY_SINGLE),
        ("reshuffle", "self", 0, EffectKind.OTHER, Target.SELF),
    ],
)
def test_map_effect(effect, target, param, kind, tgt):
    e = map_effect(effect, target, param)
    assert e.kind is kind and e.target is tgt


def test_average_effects_for_random_spells():
    groups = [[map_effect("damage", "enemy_single", v)] for v in (80, 100, 120)]
    (avg,) = average_effects(groups)
    assert avg.value == 100 and avg.kind is EffectKind.DAMAGE


def test_example_config_loads():
    cfg = load_config(Path(__file__).parent.parent / "config.example.yaml")
    assert cfg.mode == "farm"
    assert not cfg.progression.enabled
    assert cfg.safety.max_hours == 0
    assert cfg.pet.auto and not cfg.pet.stop_at_goal
    assert 0 < cfg.combat.strategy.heal_threshold < 1


def test_no_config_defaults_to_couch_potato_farm():
    cfg = load_config(None)
    assert cfg.mode == "farm"
    assert cfg.farm_zone == "Grizzleheim/GH_Hero"
    assert cfg.farm_mob == "Troubled Warrior"
    assert not cfg.progression.enabled
    assert cfg.safety.max_hours == 0
    assert cfg.pet.auto and not cfg.pet.stop_at_goal
    assert cfg.pet.auto
    assert not cfg.pet.stop_at_goal


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("combat:\n  strategy:\n    heal_treshold: 0.5\n")
    with pytest.raises(ValueError, match="heal_treshold"):
        load_config(p)


def test_wrong_type_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("quest:\n  teleport: maybe\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_myth_preset_loads():
    cfg = load_config(Path(__file__).parent.parent / "configs" / "myth.yaml")
    assert cfg.progression.school == "Myth"


def test_couch_potato_preset_targets_savarstaad_warriors():
    cfg = load_config(Path(__file__).parent.parent / "configs" / "couch_potato.yaml")
    assert cfg.mode == "farm"
    assert cfg.farm_zone == "Grizzleheim/GH_Hero"
    assert cfg.farm_mob == "Troubled Warrior"
    assert not cfg.progression.enabled
    assert cfg.safety.max_hours == 0


@pytest.mark.parametrize(
    "text,codes",
    [
        ("ctrl+shift+q", (0x11, 0x10, ord("Q"))),
        ("Ctrl + ]", (0x11, 0xDD)),
        ("F9", (0x78,)),
        ("alt+1", (0x12, ord("1"))),
        ("ctrl+backtick", (0x11, 0xC0)),
    ],
)
def test_parse_hotkey(text, codes):
    from wiz101_auto.safety import parse_hotkey

    assert parse_hotkey(text) == codes


@pytest.mark.parametrize("bad", ["", "ctrl+", "ctrl+shift", "hyper+q", "ctrl+nope"])
def test_parse_hotkey_rejects(bad):
    from wiz101_auto.safety import parse_hotkey

    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_bad_hotkey_in_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("safety:\n  stop_key: ctrl+nope\n")
    with pytest.raises(ValueError, match="nope"):
        load_config(p)


def test_npc_menu_ranking_prefers_objective_words():
    from wiz101_auto.npc import rank_options

    labels = ["Unicorn Way Bounty", "Sergeant Muldoon: Olde Town", "Shop"]
    assert rank_options(labels, "Talk to Sergeant Muldoon in Olde Town")[0] == 1
    assert rank_options(["A", "B"], "whatever") == [0, 1]  # stable when nothing matches


def test_needs_recovery_threshold():
    from wiz101_auto.config import UpkeepConfig

    cfg = UpkeepConfig(min_health_to_fight=0.8)
    assert cfg.needs_recovery(33 / 425)
    assert not cfg.needs_recovery(0.9)
    myth = load_config(Path(__file__).parent.parent / "configs" / "myth.yaml")
    assert myth.upkeep.min_health_to_fight == 0.85


def test_controller_idle_window():
    import asyncio

    from wiz101_auto.safety import Controller

    async def main():
        c = Controller("ctrl+shift+q", "ctrl+shift+p", 0)
        assert c.idle_until == 0
        c.allow_idle(30)
        assert c.idle_until > 0
        c.allow_idle(1)  # never shortens an existing window
        assert c.idle_until > __import__("time").monotonic() + 20
        c.end_idle()
        assert c.idle_until == 0

    asyncio.run(main())


def test_stall_defaults():
    from wiz101_auto.config import SafetyConfig

    assert SafetyConfig().stall_seconds == 15.0


def test_per_school_maps_the_stat_vector_and_normalises_percents():
    from wiz101_auto.combat.reader import per_school

    assert per_school([0.0, 0.0, 0.0, 0.4], 0.05)["myth"] == 0.45
    assert per_school([10.0, -20.0])["ice"] == -0.2


def test_recovery_covers_mana():
    from wiz101_auto.config import UpkeepConfig

    cfg = UpkeepConfig()
    assert cfg.needs_recovery(1.0, 0.0)
    assert not cfg.needs_recovery(0.9, 0.7)
    assert cfg.needs_recovery(0.9, 0.6)  # mana is refilled at 65% or less
    assert not cfg.recovered(1.0, 0.5) and cfg.recovered(1.0, 0.9)


def test_heal_where_it_last_worked_first():
    from wiz101_auto import upkeep

    upkeep._healed_in.clear()
    prefs = ["WizardCity/WC_Streets/WC_Unicorn", "Grizzleheim/GH_Hero"]
    assert upkeep.heal_preferences(prefs) == prefs
    upkeep.note_healed("Grizzleheim/GH_HFjord/GH_HFjord")
    upkeep.note_healed("Grizzleheim/GH_MainHub")  # (a hub: never)
    got = upkeep.heal_preferences(prefs)
    assert got[0] == "Grizzleheim/GH_HFjord/GH_HFjord" and "Grizzleheim/GH_Hero" in got
    upkeep._healed_in.clear()


def test_a_hub_with_known_wisps_counts():
    from wiz101_auto import upkeep

    spots = {"Celestia/CL_Hub": [(i * 500.0, 0.0, 0.0) for i in range(5)],
             "Celestia/CL_Z05_The_Floating_Land": [(i * 500.0, 0.0, 0.0) for i in range(5)]}
    kinds = {"Celestia/CL_Hub": {s: "mana" for s in map(tuple, spots["Celestia/CL_Hub"])},
             "Celestia/CL_Z05_The_Floating_Land":
                 {s: "health" for s in map(tuple, spots["Celestia/CL_Z05_The_Floating_Land"])}}
    got = upkeep.best_wisp_zone("Celestia/CL_Z02_Crab_Realm", spots, need={upkeep.MANA}, kinds=kinds,
                                hops=lambda a, b: 1)
    assert got == "Celestia/CL_Hub"
