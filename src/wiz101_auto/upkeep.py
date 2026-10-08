"""Out-of-combat maintenance: potions, wisps, dialogue, stray popups."""

from __future__ import annotations

import asyncio
import math
import time

from loguru import logger
from wizwalker import XYZ, Keycode

from . import ui
from .collect import away_from, landmarks, spread_points
from .config import QuestConfig, UpkeepConfig
from .travel_data import hops_from_hub
from .wisps import ANY, BOTH, HEALTH, MANA, WispMemory, usable, wisp_kind


def wisp_hub(zone: str, need=None) -> bool:
    """A hub with no wisps of the kind needed: most hubs have none, but
    Celestia's has mana wisps (130 spots seen) and none of its other zones
    do; taking every hub as empty, the wizard sat at 0% mana in it."""
    return is_hub_zone(zone) and wisp_memory().count(zone, BOTH if need is None else need) < 3


def is_hub_zone(zone: str) -> bool:
    """A world's hub (the Oasis, the Commons, the Basilica): never any wisps."""
    from .farm import FARM_HEAL_ZONE
    from .travel_data import is_world_hub

    return zone == FARM_HEAL_ZONE or is_world_hub(zone)


async def is_free(client) -> bool:
    """Not loading, not fighting and not in a dialogue."""
    try:
        return not (
            await client.is_loading()
            or await client.in_battle()
            or await ui.is_visible(client, ui.ADVANCE_DIALOG)
        )
    except Exception:
        return False


async def wait_until_free(client, timeout: float = 60.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await is_free(client):
            return True
        await asyncio.sleep(0.25)
    return False


async def wait_for_loading(client, appear_timeout: float = 2.0):
    """Give a loading screen a moment to appear, then wait for it to finish."""
    loop = asyncio.get_running_loop()
    end = loop.time() + appear_timeout
    while loop.time() < end and not await client.is_loading():
        await asyncio.sleep(0.1)
    while await client.is_loading():
        await asyncio.sleep(0.2)


async def max_health(client) -> int:
    """Max health as the game uses it. The stats' base + bonus can read high
    (2548 while the game showed 1866 full: the bot kept hunting wisps at
    "73%"); the combat read (state/my_stats.json, saved each fight) is the
    game's own number, so the lower of the two is taken."""
    max_hp = await client.stats.max_hitpoints()
    try:
        import json
        from pathlib import Path

        fought = int(json.loads(Path("state", "my_stats.json").read_text(encoding="utf-8"))["max_health"])
    except Exception:
        return max_hp
    return min(max_hp, fought) if fought > 0 else max_hp


HEALTH_READ_SECONDS = 20.0  # how long to wait out a loading screen for a real reading


async def health_mana(client) -> tuple[float, float]:
    """Health and mana ratios. During a zone load the game reports a max of
    0: that read as full (a heal trip said 'healed to 100%' 11 s after
    leaving at 44%, and the wizard went at Ullik with 74%). Wait for a real
    reading; none: 0 (heal, rather than fight hurt)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + HEALTH_READ_SECONDS
    while True:
        hp, max_hp = await client.stats.current_hitpoints(), await max_health(client)
        mana, max_mana = await client.stats.current_mana(), await client.stats.max_mana()
        # (Health over its max is real: 2545/2523 with a buff. Requiring
        # hp <= max waited out the whole read every time, and at 0% mana the
        # heal never got going before the watchdog restarted the step.)
        if max_hp > 0 and max_mana > 0 and 0 < hp and 0 <= mana:
            return min(hp / max_hp, 1.0), min(mana / max_mana, 1.0)
        if loop.time() >= end:
            return (hp / max_hp if max_hp > 0 else 0.0), (mana / max_mana if max_mana > 0 else 0.0)
        await asyncio.sleep(0.5)


async def potion_ok(client, tripped: bool = False) -> bool:
    """Potions only when nothing else heals (the player's rule): inside a
    dungeon we can't leave to heal and come back to (the Death Realm, a team
    dungeon) or one a heal trip already failed from. Everywhere else the
    wisps, or a mark and the hub, do it; the potions are kept for that."""
    from .dungeons import DungeonMemory, no_return
    from .teamup import is_team_up_zone

    zone = await client.zone_name() or ""
    if is_team_up_zone(zone):
        return False  # its own rule (team_potion): after fights, and one kept for the final boss
    if no_return(zone):
        return True
    in_dungeon = zone in DungeonMemory.load().dungeons or "/interiors/" in zone.lower()
    return tripped and in_dungeon


# A team dungeon with no healing between fights (the Waterworks, the
# player's rule): a potion after a fight only below this much health, and the
# last one kept for right before the final boss (Sylster Glowstorm).
TEAM_POTION_BELOW = 0.35
TEAM_POTIONS_KEPT = 1  # for the final fight
FINAL_POTION_BELOW = 0.8  # before the final boss: the kept potion goes in below this


def team_potion(hp: float, charges: float, before_final: bool = False) -> bool:
    """Drink now in a team dungeon? After a fight: below TEAM_POTION_BELOW
    with more than the kept one left; before the final fight: below
    FINAL_POTION_BELOW with any left."""
    if charges < 1.0:
        return False
    if before_final:
        return hp < FINAL_POTION_BELOW
    return hp < TEAM_POTION_BELOW and charges >= TEAM_POTIONS_KEPT + 1


async def maintain(client, cfg: UpkeepConfig):
    hp, mana = await health_mana(client)
    from .teamup import is_team_up_zone

    if is_team_up_zone(await client.zone_name() or ""):
        # No heal trips or wisp runs in there (the team goes on): the potion rule.
        charges = await client.stats.potion_charge()
        if cfg.use_potions and team_potion(hp, charges):
            logger.info(f"drinking a potion after the fight (hp {hp:.0%}, {charges:.0f} left; "
                        f"keeping {TEAM_POTIONS_KEPT} for the final boss)")
            await ui.click(client, ui.POTION_BUTTON)
            await asyncio.sleep(1.0)
        return

    if cfg.collect_wisps and hp < cfg.wisp_health_ratio:
        sprinter = client  # SprintyClient (bot.new_handler)
        try:
            wisps = await sprinter.get_health_wisps()
            if mana < 0.5:
                wisps += await sprinter.get_mana_wisps()
            for wisp in await sprinter.find_safe_entities_from(wisps):
                await client.teleport(await wisp.location())
                await asyncio.sleep(0.3)
        except Exception as exc:
            logger.debug(f"wisp collection failed: {exc}")
        hp, mana = await health_mana(client)

    if (cfg.use_potions and (hp < cfg.potion_health_ratio or mana < cfg.potion_mana_ratio)
            and await potion_ok(client)):
        if await client.stats.potion_charge() >= 1.0:
            logger.info(f"drinking potion (hp {hp:.0%}, mana {mana:.0%})")
            await ui.click(client, ui.POTION_BUTTON)
            await asyncio.sleep(1.0)
        elif hp < 0.25:
            logger.warning("low health and no potions; the next fight may be lost")


_memory: WispMemory | None = None


def wisp_memory() -> WispMemory:
    global _memory
    if _memory is None:
        _memory = WispMemory.load()
    return _memory


def _pt(xyz) -> tuple[float, float, float]:
    return (xyz.x, xyz.y, xyz.z)


async def scan_wisps(client) -> list:
    """Visible health and mana wisps; their positions are remembered for later,
    along with wisp stand-ins (fixed markers where a taken wisp respawns)."""
    try:
        health = await client.get_health_wisps()
        mana = await client.get_mana_wisps()
        wisps = health + mana
        seen = [(w, HEALTH) for w in health] + [(w, MANA) for w in mana]
        for e in await client.get_base_entities_with_vague_name("StandIn"):
            name = (await (await e.object_template()).object_name()) or ""
            if "wisp" in name.lower():
                seen.append((e, wisp_kind(name)))
        if seen:
            zone = await client.zone_name() or "?"
            added = 0
            for w, kind in seen:
                added += wisp_memory().record(zone, [_pt(await w.location())], kind)
            if added:
                logger.debug(f"remembered {added} new wisp spot(s) in {zone}")
                wisp_memory().save()
        return wisps
    except Exception as exc:
        logger.debug(f"wisp scan failed: {exc}")
        return []


async def mob_positions(client) -> list[tuple[float, float, float]]:
    """Places to keep clear of: enemies, and fights already going on (duel
    circles: another player's fight pulls in whoever lands beside it)."""
    from .collect import duel_circles

    try:
        mobs = [_pt(await m.location()) for m in await client.get_mobs()]
    except Exception:
        mobs = []
    return mobs + await duel_circles(client)


UNREACHABLE_WISP_MINUTES = 15.0
_unreachable: dict[tuple[str, tuple[float, float, float]], float] = {}  # (zone, spot) -> when


def _is_unreachable(zone: str, p: tuple[float, float, float]) -> bool:
    now = time.monotonic()
    return any(
        z == zone and now - t < UNREACHABLE_WISP_MINUTES * 60 and math.dist(p, q) < 60
        for (z, q), t in _unreachable.items()
    )


async def collect_wisps(client, cfg: UpkeepConfig, *, limit: int = 6) -> int:
    """Teleport onto nearby health/mana wisps that aren't close to mobs. Returns how many were taken.

    A wisp still there after landing on it can't be picked up (e.g. outside the
    playable map): it's skipped for a while, forgotten as a spawn spot, and the
    wizard goes back to where it was."""
    try:
        zone = await client.zone_name() or "?"
        await scan_wisps(client)  # remember every spawn spot seen
        # The game won't let a full-health wizard take a health wisp (or a
        # full-mana one a mana wisp), so only go for the kinds we can use.
        hp, mana = await health_mana(client)
        wisps = []
        if hp < 0.99:
            wisps += await client.get_health_wisps()
        if mana < 0.99:
            wisps += await client.get_mana_wisps()
        if not wisps:
            return 0
        safe = await client.find_safe_entities_from(wisps, safe_distance=cfg.wisp_safe_distance)
        if not safe:
            return 0
        start = await client.body.position()
        spots = [_pt(await w.location()) for w in safe]
        spots = [p for p in spots if not _is_unreachable(zone, p)]
        spots = sorted(spots, key=lambda p: math.dist(p, _pt(start)))[:limit]
        taken = 0
        stranded = False
        for spot in spots:
            before = await health_mana(client)
            await client.teleport(XYZ(*spot))
            await asyncio.sleep(0.4)
            remaining = [_pt(await w.location()) for w in await scan_wisps(client)]
            after = await health_mana(client)
            if after[0] >= 0.99 and after[1] >= 0.99:
                break  # topped up; whatever is left isn't unreachable, just unneeded
            # Only judge a wisp unreachable if both kinds were still needed (a
            # full-health wizard can't take a health wisp even when it's reachable).
            needed_both = before[0] < 0.99 and before[1] < 0.99
            gained = after[0] > before[0] or after[1] > before[1]
            if needed_both and not gained and any(math.dist(spot, r) < 60 for r in remaining):
                logger.info(f"wisp at ({spot[0]:.0f}, {spot[1]:.0f}) can't be collected; skipping it")
                _unreachable[(zone, spot)] = time.monotonic()
                if wisp_memory().forget(zone, spot):
                    wisp_memory().save()
                stranded = True
            else:
                taken += 1
        if stranded:
            await client.teleport(start)
            await asyncio.sleep(0.5)
        return taken
    except Exception as exc:
        logger.debug(f"wisp collection failed: {exc}")
        return 0


FROZEN_FAILS_BEFORE_RELOG = 3  # heal moves in a row failing on should_update: the game took no moves
_frozen = [0]


async def _note_frozen(client, exc: Exception | None):
    """Heal moves failing on WizWalker's `should_update`: the game stopped
    taking moves (06:24 in Celestia's hub: every wisp visit timed out at 0%
    mana for minutes, and the quest loop's relog never saw it, the errors
    being caught here). Three in a row: relog."""
    if exc is None or "should_update" not in str(exc):
        _frozen[0] = 0
        return
    _frozen[0] += 1
    if _frozen[0] >= FROZEN_FAILS_BEFORE_RELOG:
        _frozen[0] = 0
        logger.warning("the game takes no moves while healing: relogging")
        await _relog_out(client)


async def visit_known_spot(client, cfg: UpkeepConfig, zone: str, need=BOTH) -> bool:
    """Teleport to a remembered spawn point of a wisp kind we need (away from
    mobs) and grab what's there."""
    try:
        got = await _visit_known_spot(client, cfg, zone, need)
        await _note_frozen(client, None)
        return got
    except Exception as exc:  # e.g. WizWalker's ExceptionalTimeout while a popup blocks the game
        logger.debug(f"wisp spot visit failed: {exc!r}")
        await _note_frozen(client, exc)
        return False


async def _visit_known_spot(client, cfg: UpkeepConfig, zone: str, need) -> bool:
    me = _pt(await client.body.position())
    spot = wisp_memory().next_spot(
        zone, me, await mob_positions(client), safe_distance=cfg.wisp_safe_distance, need=need
    )
    if spot is None:
        return False
    wisp_memory().mark_visited(zone, spot)
    logger.info(f"checking a known {wisp_memory().kind_of(zone, spot)} wisp spawn point")
    await client.teleport(XYZ(*spot))
    await asyncio.sleep(0.4)
    if await collect_wisps(client, cfg):
        wisp_memory().found(zone, spot)
    elif wisp_memory().missed(zone, spot):
        logger.info("no wisp at that spot three times running: forgetting it")
        wisp_memory().save()
    return True


async def sweep_for_wisps(client, cfg: UpkeepConfig) -> int:
    """Hop across the zone's landmarks (on the map, away from mobs) to discover
    its wisp spawns; wisps only load near the wizard."""
    start = await client.body.position()
    # Only skip landmarks right next to mobs: busy streets (Unicorn Way) would
    # otherwise leave nothing to search. Wisps near mobs are still skipped.
    spots = away_from(await landmarks(client), await mob_positions(client), SWEEP_MOB_DISTANCE)
    points = spread_points(spots, _pt(start), WISP_SWEEP_SPACING)[:WISP_SWEEP_MAX]
    zone = await client.zone_name() or "?"
    before = len(wisp_memory().spots.get(zone, []))
    logger.info(f"searching {zone} for wisp spawn points ({len(points)} spots)")
    for p in points:
        if not await is_free(client):
            break
        await client.teleport(XYZ(*p))
        await asyncio.sleep(0.4)
        await scan_wisps(client)
    found = len(wisp_memory().spots.get(zone, [])) - before
    logger.info(f"found {found} new wisp spawn point(s)")
    return found


async def can_move(client) -> bool:
    """Tap forward and back: a wizard wedged in a wall or building barely moves."""
    start = _pt(await client.body.position())
    moved = 0.0
    for key in (Keycode.W, Keycode.S):
        await client.send_key(key, 0.5)
        await asyncio.sleep(0.2)
        moved = max(moved, math.dist(start, _pt(await client.body.position())))
    return moved > STUCK_MOVE_DISTANCE


async def unstick(client, frozen: bool = False) -> bool:
    """If the wizard can't walk (clipped into geometry after a teleport), move it
    to the nearest on-map landmark it can walk from. True if it was stuck.
    `frozen`: teleports are being refused too: straight to the relog."""
    try:
        if not await is_free(client):
            return False  # fighting, a dialogue or a cutscene: standing still is normal
        if await can_move(client):
            return False
        me = _pt(await client.body.position())
        if frozen:
            logger.warning(f"wizard frozen at ({me[0]:.0f}, {me[1]:.0f}): can't walk or teleport; relogging")
            return await _relog_out(client)
        logger.warning(f"wizard seems stuck at ({me[0]:.0f}, {me[1]:.0f}): can't walk; moving to a landmark")
        spots = away_from(await landmarks(client), await mob_positions(client), 400.0)
        spots = sorted((p for p in spots if math.dist(p, me) > 150), key=lambda p: math.dist(p, me))
        for p in spots[:8]:
            await client.teleport(XYZ(*p))
            await asyncio.sleep(0.4)
            if await can_move(client):
                logger.success(f"unstuck: now at ({p[0]:.0f}, {p[1]:.0f})")
                return True
        logger.warning("still stuck after trying nearby landmarks")
        return await _relog_out(client)
    except Exception as exc:
        logger.debug(f"unstick failed: {exc!r}")
        if "should_update" in str(exc):
            return await _relog_out(client)  # the game takes no moves at all
        return False


async def _relog_out(client) -> bool:
    """Nothing frees the wizard (a duel circle 'battle' with 0 opponents):
    log out to character select and back in. True (it acted)."""
    from .relog import relog

    if not await relog(client):
        logger.warning("ALERT: main quest stuck: the wizard can't move; relog failed")
    return True


async def move_to_safety(client, safe_distance: float = 1500.0, why: str = "to rest") -> bool:
    """If an enemy (or a fight going on) is close, teleport to the nearest
    spot with none around: landmarks, or walkway points at floor height
    (cameras and other path markers float off the walkable map)."""
    from .collect import floor_points, path_points

    try:
        me = await client.body.position()
        hazards = await mob_positions(client)
        if all(math.dist(p, _pt(me)) > safe_distance for p in hazards):
            return False
        spots = await landmarks(client) + floor_points(await path_points(client), me.z)
        candidates = away_from(spots, hazards, safe_distance)
        if not candidates:
            return False
        spot = min(candidates, key=lambda p: math.dist(p, _pt(me)))
        logger.info(f"enemies close by: moving somewhere clear {why}")
        await client.teleport(XYZ(*spot))
        await asyncio.sleep(0.4)
        return True
    except Exception as exc:
        logger.debug(f"could not find a safe spot: {exc}")
        return False


WATCH_DISTANCE = 1500.0  # waiting in place: an enemy this close gets us moving
BUSY_MOVES = 2  # moving away this often in one wait: too busy to wait here


async def watchful_wait(client, seconds: float) -> int:
    """Wait without standing still in an enemy's path: every second, if an
    enemy (or a fight going on) is within WATCH_DISTANCE, move somewhere clear
    (patrols walked right into the wizard while it waited for wisps). Stops
    early once it has had to move BUSY_MOVES times (the street is too busy to
    wait in: Hyde Park's patrols caught it anyway) or a fight starts. Returns
    how many times it moved (BUSY_MOVES or more: leave)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    moves = 0
    while loop.time() < end:
        if await client.in_battle():
            return BUSY_MOVES
        near = await mob_positions(client)
        me = await client.body.position()
        if any(math.dist(p, _pt(me)) < WATCH_DISTANCE for p in near):
            moves += 1
            if moves >= BUSY_MOVES:
                return moves
            await move_to_safety(client, REST_SAFE_DISTANCE, "(an enemy is coming)")
        await asyncio.sleep(1.0)
    return moves


SWEEP_MOB_DISTANCE = 1000.0  # hopping next to a mob starts a fight
WISP_SWEEP_SPACING = 2500.0  # wisps load within roughly this range
WISP_SWEEP_MAX = 16
STUCK_MOVE_DISTANCE = 25.0  # walking 0.5s moves ~100+; less means wedged in geometry
WISP_GAIN = 0.03  # smallest health/mana ratio gain that means a wisp was taken
REST_WORLDS = {"Aquila"}  # no easily reached wisps: resting (regeneration) is allowed here
RESPAWN_WAITS = 1  # waits for empty wisp spots to refill before carrying on / healing elsewhere
WISP_RESPAWN_WAIT = 15.0  # empty wisp spots in a wisp zone: wait this long, then go round again
REST_SAFE_DISTANCE = 2000.0  # resting spot: no enemy (or duel circle) this close
FRUITLESS_VISITS = 3  # empty wisp spots in a row before going elsewhere to heal
# Interiors recovery gave up on (walk out on the quest path instead).
_leaving_interior: set[str] = set()
REST_PROBE_SECONDS = 60.0  # resting this long without gaining anything: no wisps here, heal elsewhere
BARREN_SECONDS = 1800.0  # how long a zone that gave nothing is skipped as a place to heal
_barren: dict[str, float] = {}  # zone -> when resting there gave nothing
_farm_safe_heal_swept: set[str] = set()


_healed_in: dict[str, str] = {}  # world -> the zone where the last heal there worked


def note_healed(zone: str):
    """A heal worked here: this world's heals go here first next time (after
    every defeat in Nastrond it tried Gloomgrove and Wolfsthal first, empty,
    a minute each, and healed at once in Hrundle Fjord)."""
    if zone and not is_hub_zone(zone) and "/interiors/" not in zone.lower():
        _healed_in[zone.split("/", 1)[0]] = zone


def heal_preferences(preferred) -> list[str]:
    """The zones to heal in first: where the last heal worked, then config's."""
    return list(dict.fromkeys([*_healed_in.values(), *preferred]))


def _heal_prefs(cfg) -> list[str]:
    return heal_preferences(cfg.heal_zones)


def note_barren(zone: str, now: float | None = None):
    import time as _time

    _barren[zone] = _time.monotonic() if now is None else now


def barren_zones(now: float | None = None) -> set[str]:
    import time as _time

    now = _time.monotonic() if now is None else now
    return {z for z, t in _barren.items() if now - t < BARREN_SECONDS}


def _dungeon_zones() -> set[str]:
    """Zones of learned dungeons (their rooms), never a place to go heal."""
    try:
        from .dungeons import DungeonMemory

        mem = DungeonMemory.load()
        return set(mem.dungeons) | set(mem.bosses.values())
    except Exception:
        return set()


def best_wisp_zone(
    current_zone: str,
    spots: dict | None = None,
    preferred: list[str] = (),
    need=BOTH,
    kinds: dict | None = None,
    avoid: set[str] = frozenset(),
    hops=None,
) -> str | None:
    """Where to recover: a preferred heal zone in the same world when health is
    needed, else the zone with the most remembered spots of the needed wisp
    kind (Unicorn Way as a Wizard City fallback)."""
    if spots is None:
        spots, kinds = wisp_memory().spots, wisp_memory().kinds
    kinds = kinds or {}
    world = current_zone.split("/", 1)[0]
    in_world = [z for z in preferred if z.split("/", 1)[0] == world and z != current_zone and z not in avoid]
    if HEALTH in need and in_world:
        return in_world[0]
    same_world = [
        (sum(usable(kinds.get(z, {}).get(tuple(p), ANY), need) for p in pts), z)
        for z, pts in spots.items()
        if z.split("/", 1)[0] == world
    ]
    # The easiest to reach first (fewest gates by `hops`: from the world's hub,
    # where a heal trip starts), then the most wisps. Hubs never have wisps.
    def reach(z: str) -> int:
        n = hops(current_zone, z) if hops else None
        return 99 if n is None else n

    zones = sorted(((reach(z), -n, z) for n, z in same_world if n >= 3))
    dungeons = _dungeon_zones()
    for _, _, z in zones:
        # Never walk into a dungeon for its wisps: that skipped the Team Up
        # check and took the wizard into Mount Olympus alone.
        if z in dungeons or "/interiors/" in z.lower():
            continue
        # (Only zones with 3+ spots of the needed kind are here, hubs too:
        # Celestia's has the world's mana wisps.)
        if z != current_zone and z not in avoid:
            return z
    if in_world:
        return in_world[0]
    unicorn = "WizardCity/WC_Streets/WC_Unicorn"
    if world == "WizardCity" and current_zone != unicorn and unicorn not in avoid:
        return "WizardCity/WC_Streets/WC_Unicorn"
    return None


CLOSE_ENOUGH = 0.10  # within this much of the fight thresholds counts when no wisps help


def close_enough(cfg: UpkeepConfig, hp: float, mana: float) -> bool:
    return hp >= cfg.min_health_to_fight - CLOSE_ENOUGH and mana >= cfg.min_mana_to_fight - CLOSE_ENOUGH


async def heal_in_room(client, cfg: UpkeepConfig, rounds: int = 4) -> bool:
    """Heal from this room's wisps only (in view, then its learned spawn
    points), never going anywhere else: a team dungeon, where leaving means
    losing the team. True if it gained anything."""
    zone = await client.zone_name() or ""
    hp0, mana0 = await health_mana(client)
    for _ in range(rounds):
        hp, mana = await health_mana(client)
        if not cfg.needs_recovery(hp, mana):
            break
        took = await collect_wisps(client, cfg)
        if not took and not await visit_known_spot(client, cfg, zone, needed_wisps(cfg, hp, mana)):
            break
        await asyncio.sleep(0.5)
        if await client.zone_name() != zone:
            break
    hp, mana = await health_mana(client)
    if hp > hp0 or mana > mana0:
        logger.info(f"healed from this room's wisps: {hp0:.0%} -> {hp:.0%} health, "
                    f"{mana0:.0%} -> {mana:.0%} mana")
        return True
    return False


def needed_wisps(cfg: UpkeepConfig, hp: float, mana: float) -> frozenset[str]:
    """Which wisp kinds recovery still needs: health alone while health is
    too low to fight (a mana wisp's spot kept the bot at 41% health for
    minutes, every visit a little mana), else whatever is short."""
    if hp < cfg.min_health_to_fight:
        return frozenset({HEALTH})
    need = set()
    if hp < cfg.rest_until_health:
        need.add(HEALTH)
    if mana < cfg.rest_until_mana:
        need.add(MANA)
    return frozenset(need or BOTH)


async def recover(
    client, cfg: UpkeepConfig, controller, go_to_zone=None, trip=None, mark=None,
    safe_heal_zone: str | None = None,
) -> bool:
    """Make sure the wizard is healthy before engaging anything.

    With `mark` (async, True if it marked the spot): mark first, then heal in
    this zone (wisps in view, remembered spots, a sweep) and teleport back to
    where it started. If this zone lacks what is needed (no health wisps, or
    mana still short), `trip(marked=..., force=...)` heals from the world hub
    and Recalls to the mark (True if it went).

    Returns True when it's fine to carry on questing, False if something
    (a fight, dialogue, loading) interrupted the recovery.
    """
    hp, mana = await health_mana(client)
    if not cfg.needs_recovery(hp, mana):
        return True
    zone_now = await client.zone_name() or ""
    if zone_now in _leaving_interior:
        return True  # already found nothing here; the quest is walking us out
    _leaving_interior.clear()
    logger.info(f"health {hp:.0%}, mana {mana:.0%}: recovering before going on")
    marked = bool(mark and await mark())
    start_pos = await client.body.position()

    async def back_to_start():
        """Healed in this zone: go back to where healing began."""
        here = await client.body.position()
        if await client.zone_name() == zone_now and math.dist(_pt(start_pos), _pt(here)) > 400:
            await client.teleport(start_pos)
            await asyncio.sleep(0.5)

    loop = asyncio.get_running_loop()
    started = loop.time()
    last_report = started
    rested = False
    rest_start: tuple[float, float, float] | None = None  # (when, hp, mana) resting began
    moved_on = False  # already left a zone that gave nothing
    tripped = False  # tried a heal trip through the hub
    travelled = False
    fruitless = 0  # remembered spots visited in a row without gaining anything
    respawn_waits = 0  # waits for the wisps here to come back
    emptied: set[str] = set()  # zones whose wisps didn't come back
    grabbed_in_view = False  # past the fight thresholds: wisps in view taken once
    swept: set[str] = set()
    while True:
        await controller.checkpoint()
        if not await is_free(client):
            return False
        hp, mana = await health_mana(client)
        if cfg.recovered(hp, mana):
            logger.success(f"recovered to {hp:.0%} health, {mana:.0%} mana")
            note_healed(await client.zone_name() or "")
            await back_to_start()
            return True
        if not cfg.needs_recovery(hp, mana):
            # Fit to fight again: take the wisps in view, but no visiting spots,
            # waiting for respawns or trips for the last few percent (standing
            # around for those is slow and where patrols caught the wizard).
            if cfg.collect_wisps and not grabbed_in_view and await collect_wisps(client, cfg):
                grabbed_in_view = True
                await asyncio.sleep(0.3)
                continue
            logger.success(f"healed to {hp:.0%} health, {mana:.0%} mana: good enough, going on")
            note_healed(await client.zone_name() or "")
            await back_to_start()
            return True

        if cfg.collect_wisps:
            # 1. wisps in view  2. remembered spawn points  3. search the zone once
            if await collect_wisps(client, cfg):
                await asyncio.sleep(0.5)
                now_hp, now_mana = await health_mana(client)
                if now_hp > hp or now_mana > mana:
                    continue

        # A potion only when stuck in a dungeon with no other way to heal.
        low = hp < cfg.potion_health_ratio or mana < cfg.potion_mana_ratio
        if (cfg.use_potions and low and await client.stats.potion_charge() >= 1.0
                and await potion_ok(client, tripped)):
            logger.info(f"drinking potion (hp {hp:.0%}, mana {mana:.0%}): no other way to heal here")
            await ui.click(client, ui.POTION_BUTTON)
            await asyncio.sleep(1.5)
            continue

        if cfg.collect_wisps:
            zone = await client.zone_name() or "?"
            need = needed_wisps(cfg, hp, mana)
            farm_safe_heal_zone = safe_heal_zone is not None and zone == safe_heal_zone
            # Northguard is normally treated as an empty hub. In farm mode,
            # search it once for wisps, but never wander back among Savarstaad mobs.
            hub = wisp_hub(zone, need) and not farm_safe_heal_zone
            known_elsewhere = (
                not farm_safe_heal_zone
                and best_wisp_zone(zone, preferred=_heal_prefs(cfg), need=need,
                                   avoid=barren_zones(), hops=hops_from_hub) is not None
            )
            if hub:
                fruitless = FRUITLESS_VISITS
            if not hub and await visit_known_spot(client, cfg, zone, need):
                rested = False
                now_hp, now_mana = await health_mana(client)
                # Passive regeneration ticks up a little on every visit; only a real
                # wisp (a few % at once) counts as finding something.
                # (Only what's needed counts: mana from a mana wisp's spot while
                # health is what's low isn't progress.)
                gained = now_hp - hp >= WISP_GAIN or (MANA in need and now_mana - mana >= WISP_GAIN)
                fruitless = 0 if gained else fruitless + 1
                if fruitless < FRUITLESS_VISITS:
                    continue
            if (zone not in swept and not hub and not known_elsewhere
                    and (not farm_safe_heal_zone or zone not in _farm_safe_heal_swept)):
                # Only where no wisp zone is known yet: the sweep hops across
                # landmarks (NPCs, objects) to discover the spawns.
                swept.add(zone)
                if farm_safe_heal_zone:
                    _farm_safe_heal_swept.add(zone)
                if await sweep_for_wisps(client, cfg):
                    continue
            # The wisps visited above may have done the job (14% -> 88% in
            # Hyde Park): look again before leaving the zone to heal.
            hp, mana = await health_mana(client)
            if not cfg.needs_recovery(hp, mana):
                logger.info(f"healed here to {hp:.0%} health, {mana:.0%} mana; no trip needed")
                note_healed(await client.zone_name() or "")
                return True
            wisp_zone = not hub and wisp_memory().count(zone, need) >= 3
            in_time = loop.time() - started < cfg.rest_max_minutes * 60
            busy = zone in barren_zones()  # patrols made waiting here impossible
            if (wisp_zone and fruitless >= FRUITLESS_VISITS and in_time and not busy
                    and respawn_waits >= RESPAWN_WAITS):
                # Waited twice and the same spots stayed empty (GH_Wolf: 3 spots
                # checked every 15 s for minutes at 54%): heal elsewhere and
                # come back (the player: not carrying on half healed).
                logger.info(f"no wisps came back after {respawn_waits} waits: healing elsewhere")
                busy = True
                # (Not back here for a while: the next heal tried the same empty
                # zones again, a minute each, after every defeat.)
                note_barren(zone)
                # (It said so every 9 s in Mirkholm Keep and stayed: the trip
                # was already used. Another zone, not this one, once more.)
                if zone not in emptied:
                    emptied.add(zone)
                    travelled = False
            if wisp_zone and fruitless >= FRUITLESS_VISITS and in_time and not busy:
                respawn_waits += 1
                # A street with wisps whose spots are empty right now: they
                # respawn. Wait here and go round them again, rather than back
                # to the hub (it went hub <-> Hyde Park, then rested).
                logger.info(f"wisps here are respawning; waiting {WISP_RESPAWN_WAIT:.0f}s to go round again")
                await move_to_safety(client, REST_SAFE_DISTANCE)
                if await watchful_wait(client, WISP_RESPAWN_WAIT) >= BUSY_MOVES:
                    # Patrols keep coming: waiting here ends in a fight.
                    note_barren(zone)
                    hp, mana = await health_mana(client)
                    if hp >= cfg.min_health_to_fight or close_enough(cfg, hp, mana):
                        logger.info(f"too many patrols in {zone} to wait for wisps; "
                                    f"{hp:.0%} health, {mana:.0%} mana: carrying on")
                        await back_to_start()
                        return True
                    logger.info(f"too many patrols in {zone} to wait for wisps; healing elsewhere")
                fruitless = 0
                continue
            poor_zone = hub or not wisp_zone or busy
            if trip and not tripped and poor_zone:
                # This zone lacks what is needed (health wisps, or mana after
                # healing here): the world hub, then Recall to the mark.
                tripped = True
                if await trip(marked=marked):
                    return True
            if go_to_zone and not travelled and poor_zone:
                # No wisps to be had here right now (e.g. the hub after a defeat):
                # go heal where they spawn instead of waiting for respawns.
                travelled = True
                fruitless = 0
                dest = best_wisp_zone(zone, preferred=_heal_prefs(cfg), need=need,
                                      avoid=barren_zones() | emptied, hops=hops_from_hub)
                if dest:
                    what = " and ".join(sorted(need))
                    logger.info(f"no {what} wisps in {zone}; going to {dest} for them")
                    if await go_to_zone(dest):
                        respawn_waits = 0
                        continue
                if "interiors" in zone.lower():
                    # A dungeon/building with no wisps and no known way out: resting
                    # here takes minutes. Let the quest path walk out, heal outside.
                    logger.info(f"no wisps or route out of {zone}; following the quest out to heal")
                    _leaving_interior.add(zone)
                    return True
            if fruitless >= FRUITLESS_VISITS and close_enough(cfg, hp, mana):
                # Nothing to be had around here and we're nearly there: waiting for
                # regeneration costs minutes that questing puts to better use.
                logger.info(f"no wisps here; {hp:.0%} health, {mana:.0%} mana is enough to go on")
                await back_to_start()
                return True

        if close_enough(cfg, hp, mana):
            # Nothing to pick up nearby, and resting regenerates little or
            # nothing: this close to the threshold, questing on is better.
            logger.info(f"nothing to heal with here; {hp:.0%} health, {mana:.0%} mana is enough to go on")
            await back_to_start()
            return True
        zone = await client.zone_name() or "?"
        in_time = loop.time() - started < cfg.rest_max_minutes * 60
        if in_time and not wisp_hub(zone) and wisp_memory().count(zone, needed_wisps(cfg, hp, mana)) >= 3:
            # Wisps beat resting: wait for the next ones to come off cooldown.
            if await watchful_wait(client, WISP_RESPAWN_WAIT / 2) >= BUSY_MOVES:
                note_barren(zone)
                logger.info(f"too many patrols in {zone} to wait for wisps; carrying on")
                await back_to_start()
                return True
            continue
        if not rested and zone.split("/", 1)[0] in REST_WORLDS:
            # Worlds without easy wisps (Aquila): regenerate standing clear of enemies.
            await move_to_safety(client, REST_SAFE_DISTANCE)
            rested = True
            rest_start = (loop.time(), hp, mana)
        if not rested:
            # No resting for slow regeneration (the player's rule): wisps only.
            # Go where they are; nowhere known: carry on and heal at the next.
            dest = best_wisp_zone(zone, preferred=_heal_prefs(cfg), need=needed_wisps(cfg, hp, mana),
                                  avoid=barren_zones(), hops=hops_from_hub)
            if dest and dest != zone and go_to_zone and await go_to_zone(dest):
                logger.info(f"went to {dest} for wisps")
                continue
            if trip and not tripped:
                # Nowhere to walk to for wisps (inside the Emperor's Palace,
                # back by Recall at 2% health, it 'carried on' into Jade Oni):
                # the hub, then Recall back to the mark.
                tripped = True
                if await trip(marked=marked):
                    return True
            logger.info(f"no wisps to heal with ({hp:.0%} health, {mana:.0%} mana); carrying on")
            await back_to_start()
            return True
        elif rest_start and loop.time() - rest_start[0] > REST_PROBE_SECONDS and not moved_on:
            gained = hp - rest_start[1] >= 0.01 or mana - rest_start[2] >= 0.01
            if not gained:
                # A minute of rest gave nothing: no wisps (and no regeneration)
                # here. Heal where we know we can instead of waiting.
                zone = await client.zone_name() or "?"
                note_barren(zone)
                moved_on = True
                waited = f"{REST_PROBE_SECONDS:.0f}s"
                logger.info(f"nothing recovered in {waited} in {zone}; going somewhere to heal")
                if trip and await trip(force=True, marked=marked):
                    return True
                dest = best_wisp_zone(zone, preferred=_heal_prefs(cfg), need=needed_wisps(cfg, hp, mana),
                                      avoid=barren_zones(), hops=hops_from_hub)
                if dest and go_to_zone and await go_to_zone(dest):
                    logger.info(f"went to {dest} to heal")
                    rested, rest_start = False, None
                    continue
                logger.info("no known place to heal; carrying on")
                return True

        elapsed = loop.time() - started
        if elapsed > cfg.rest_max_minutes * 60:
            if not cfg.needs_recovery(hp, mana):
                return True
            # Never end the session over it: carry on and heal at the next
            # chance (wisps on the way, a heal trip, a level-up).
            logger.warning(
                f"could not recover (health {hp:.0%}, mana {mana:.0%}) within "
                f"{cfg.rest_max_minutes:g} min; carrying on"
            )
            return True
        if loop.time() - last_report > 60:
            logger.info(f"resting: health {hp:.0%}, mana {mana:.0%}; waiting for regeneration or wisps")
            last_report = loop.time()
        controller.allow_idle(10)
        await clear_popups(client)  # e.g. the minigame picker, opened by walking past its sign
        await watchful_wait(client, 5)


_CLOSE_NAMES = (
    "Exit",
    "exit",
    "Close",
    "close",
    "btnClose",
    "CloseButton",
    "Close_Button",
    "btnExit",
    "Cancel",
)


async def close_crowns_shop(client) -> bool:
    """Close any open Crowns shop / offer window. Only top-level windows are
    checked, so this is cheap enough to run every step."""
    for parent in (client.root_window, await client.get_world_view_window()):
        try:
            children = await parent.children()
        except Exception:
            continue
        for w in children:
            try:
                name = await w.name() or ""
                low = name.lower()
                # (The Crown Shop itself is 'PermanentShopModalWindow'.)
                if ("crown" not in low and "permanentshop" not in low) or not await w.is_visible():
                    continue
            except Exception:
                continue
            logger.warning(f"crowns window {name!r} is open; closing it")
            for close_name in _CLOSE_NAMES:
                for btn in await w.get_windows_with_name(close_name):
                    try:
                        if await btn.is_visible():
                            await client.mouse_handler.click_window(btn)
                            await asyncio.sleep(0.5)
                            return True
                    except Exception:
                        pass
            await client.send_key(Keycode.ESC, 0.1)
            await asyncio.sleep(1.5)
            try:
                still = await w.is_visible()
            except Exception:
                still = False
            if still:
                # Its X is drawn in a Flash panel (no button window): click
                # the top-right corner where it sits.
                r = await w.scale_to_client()
                x = int(r.x2 - (r.x2 - r.x1) * SHOP_X_FROM_RIGHT)
                y = int(r.y1 + (r.y2 - r.y1) * SHOP_X_FROM_TOP)
                logger.info(f"crowns window still open (Esc did nothing); clicking its X at ({x}, {y}) "
                            f"of ({r.x1}, {r.y1})-({r.x2}, {r.y2})")
                await ui.button_click(client, x, y)
                await asyncio.sleep(0.8)
            return True
    return False


# The Crown Shop's X: the window is the whole screen; on a 1760x990
# screenshot the X sat at (1497, 30). (A click at the screen's corner opened
# the Friends list instead.)
SHOP_X_FROM_RIGHT = 1 - 1497 / 1760
SHOP_X_FROM_TOP = 30 / 990


async def reconnect_if_asked(client) -> bool:
    """"Problem: Unable to find your zone on server (connect too long?)" with
    Reconnect / Quit: press Reconnect. True if it did."""
    from .relog import _find_button

    try:
        button = await _find_button(client.root_window, ("reconnect",))
    except Exception:
        return False
    if button is None:
        return False
    logger.warning("the game lost its connection ('Problem' box): pressing Reconnect")
    await ui.click_center(client, button)
    await asyncio.sleep(5.0)
    await wait_for_loading(client)
    return True


async def close_friends_list(client) -> bool:
    """The Online Friends list (opened by a stray click) covers the right of
    the screen: close it. True if it did."""
    try:
        for w in await client.root_window.get_windows_with_name("NewFriendsListWindow"):
            if not await w.is_visible():
                continue
            for btn in await w.get_windows_with_name("btnClose"):
                if await btn.is_visible():
                    logger.info("closing the Friends list")
                    await ui.click_center(client, btn)
                    await asyncio.sleep(0.5)
                    return True
    except Exception:
        pass
    return False


# "Really Skip the Tutorial?" (its title); the caption modal_text reads:
SKIP_TUTORIAL_CAPTION = "only click yes if you've been here before"


async def skip_tutorial(client) -> bool:
    """A game tutorial (the Archmastery one in the Dueling Arena after
    Malistaire, 'Complete Archmastery Tutorial in Arena'): the bot can't play
    it and the wizard stood there unable to move for 20 minutes. Press SKIP
    TUTORIAL, then Yes on "Really Skip the Tutorial?". True if it did."""
    try:
        box = await ui.modal_box(client)
        if box is not None and SKIP_TUTORIAL_CAPTION in (await ui.modal_text(box)).lower():
            logger.info("skipping the tutorial: Yes")
            return await ui.modal_click(client, box, "leftButton")
        for w in await client.root_window.get_windows_with_name("TutorialWindow"):
            if not await w.is_visible():
                continue
            for btn in await w.get_windows_with_name("SkipButton"):
                if await btn.is_visible():
                    logger.info("a tutorial: pressing SKIP TUTORIAL")
                    await ui.click_center(client, btn)
                    await asyncio.sleep(1.5)
                    box = await ui.modal_box(client)
                    if box is not None and SKIP_TUTORIAL_CAPTION in (await ui.modal_text(box)).lower():
                        await ui.modal_click(client, box, "leftButton")
                    return True
    except Exception as exc:
        logger.debug(f"skip_tutorial: {exc!r}")
    return False


async def clear_popups(client):
    if await reconnect_if_asked(client):
        return
    await skip_tutorial(client)
    await close_crowns_shop(client)
    await close_friends_list(client)
    if await ui.click_named(client, "btnPetLevelClose"):
        # "Sir Buster has leveled up to Adult!" covered the cards mid-fight:
        # every cast and discard missed for a round and a half.
        logger.info("closed the pet level-up window")
        await asyncio.sleep(0.5)
    await ui.close_chat(client)
    await ui.dismiss_notice(client)
    await dismiss_endorsement(client)
    await ui.click(client, ui.CANCEL_CHEST_REROLL)
    if await ui.is_visible(client, ui.MINIGAME_EXIT):
        logger.info("closing the minigame picker")
        await ui.click(client, ui.MINIGAME_EXIT)
    if await ui.is_visible(client, ui.MISSING_AREA_RETRY):
        await ui.click(client, ui.MISSING_AREA_RETRY)


async def dismiss_endorsement(client) -> bool:
    """Choose Friendly on the post-fight endorsement popup, then verify it closed."""
    if not await ui.is_visible(client, ui.ENDORSEMENT):
        return False
    logger.info("endorsing the wizard we fought with (Friendly) to close the window")
    if await ui.click(client, ui.ENDORSE_FRIENDLY):
        await asyncio.sleep(0.5)
    if await ui.is_visible(client, ui.ENDORSEMENT):
        logger.warning("endorsement window stayed open after Friendly; closing it")
        await ui.click(client, ui.ENDORSE_CLOSE)
        await asyncio.sleep(0.3)
    if await ui.is_visible(client, ui.ENDORSEMENT):
        logger.warning("endorsement window is still open")
        return False
    return True


async def endorsement_loop(client, controller):
    """Dismiss endorsement prompts even when no quest step is running."""
    while not controller.stopped.is_set():
        await controller.checkpoint()
        if not await client.in_battle():
            await dismiss_endorsement(client)
        await asyncio.sleep(1.0)


class DialoguePolicy:
    """Whether quest offers should be accepted right now.

    Offers from the NPC the quest helper sent us to are the story line and
    must be accepted; offers from anyone else are side quests.
    """

    def __init__(self):
        self._accept_until = 0.0
        self.accepted = 0  # quests accepted: the game tracks a new one, so the quester re-ranks

    def accept_offers_for(self, seconds: float = 30.0):
        self._accept_until = time.monotonic() + seconds

    @property
    def accepting(self) -> bool:
        return time.monotonic() < self._accept_until


async def dialogue_loop(client, cfg: QuestConfig, controller, policy: DialoguePolicy | None = None):
    """Advance NPC dialogue as it appears. Runs for the whole session."""
    policy = policy or DialoguePolicy()
    last_offer, tries = "", 0
    while not controller.stopped.is_set():
        try:
            if not controller.paused and await ui.is_visible(client, ui.ADVANCE_DIALOG):
                offer = await ui.is_visible(client, ui.DECLINE_QUEST)
                if offer and (cfg.accept_side_quests or policy.accepting):
                    text = await ui.text_at(client, ui.DIALOG_TEXT)
                    tries = tries + 1 if text == last_offer else 0
                    last_offer = text
                    if tries == 0:
                        logger.info(f"accepting quest: {text[:80]}")
                        if policy is not None:
                            policy.accepted += 1
                    # The offer's accept button doesn't always take the usual
                    # (left-shifted) click: cycle through other ways of pressing it.
                    # (Never Enter: it opens the chat box and swallows later keys.)
                    how = tries % 3
                    if how == 0:
                        if not await ui.click(client, ui.ADVANCE_DIALOG):
                            await client.send_key(Keycode.SPACEBAR)
                    elif how == 1:
                        w = await ui.window_at(client, ui.ADVANCE_DIALOG)
                        if w is not None:
                            await ui.click_center(client, w)
                    else:
                        await client.send_key(Keycode.SPACEBAR)
                    if tries in (1, 2):
                        logger.debug(f"quest offer still open; accepting another way ({how})")
                    await asyncio.sleep(0.6)
                elif offer:
                    text = await ui.text_at(client, ui.DIALOG_TEXT)
                    logger.info(f"declining side quest: {text[:80]}")
                    await client.send_key(Keycode.ESC)
                    await asyncio.sleep(0.1)
                    await client.send_key(Keycode.ESC)
                else:
                    # A storyline offer has no Decline, only Accept: Space closed
                    # it unaccepted (Thornton Lewis's Explorer101, every talk for
                    # 11 hours), so an Accept button is clicked.
                    label = (await ui.text_at(client, ui.ADVANCE_DIALOG)).strip().lower()
                    if "accept" in label:
                        text = await ui.text_at(client, ui.DIALOG_TEXT)
                        logger.info(f"accepting a storyline quest: {text[:80]}")
                        if policy is not None:
                            policy.accepted += 1
                        if not await ui.click(client, ui.ADVANCE_DIALOG):
                            await client.send_key(Keycode.SPACEBAR)
                        await asyncio.sleep(0.6)
                    else:
                        await client.send_key(Keycode.SPACEBAR)
        except Exception as exc:
            logger.trace(f"dialogue loop: {exc}")
        await asyncio.sleep(0.3)
