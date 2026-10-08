"""Wires everything together and runs the bot against one game client."""

from __future__ import annotations

import asyncio
import contextlib
import time

from loguru import logger
from wizwalker import ClientHandler
from wizwalker.errors import PatternFailed
from wizwalker.extensions.wizsprinter import SprintyClient

from . import lifetime
from .bossfarm import BossFarmer
from .combat.fighter import Fighter
from .config import Config
from .dungeon_heal import DungeonHealer
from .gear import GearManager
from .petdance import PetDancer
from .potions import PotionShopper
from .progression import Progression
from .quest import Quester
from .safety import BotStopped, Controller
from .trainer import SpellTrainer
from .upkeep import (
    DialoguePolicy,
    dialogue_loop,
    health_mana,
    is_free,
    maintain,
    max_health,
    move_to_safety,
    recover,
    scan_wisps,
)
from .watchdog import Watchdog

HOOK_TIMEOUT = 90
CONNECT_TIMEOUT = 300.0  # the whole way into the world (reconnect, Play, hooks)
DEATH_HEALTH_RATIO = 0.1
GRIND_SHOWN_SECONDS = 90.0  # the status says "grinding" this long after the last grind step
DEFEAT_MOVE_DISTANCE = 1500.0  # a defeat puts you back at the zone's start (or another zone)
CAMP_POSITION_TOLERANCE = 600.0


async def return_to_farm_camp(client, quester, zone: str, position) -> bool:
    """Return to the saved farm position, traveling back to its zone if needed."""
    current_zone = await client.zone_name() or ""
    if current_zone != zone:
        logger.info(f"returning to farm zone {zone} from {current_zone or 'unknown zone'}")
        if not await quester.go_to_zone(zone) or await client.zone_name() != zone:
            logger.warning(f"could not return to farm zone {zone}")
            return False
    here = await client.body.position()
    if here.distance(position) <= CAMP_POSITION_TOLERANCE:
        return True
    logger.info(f"returning to farm camp position in {zone}")
    from .safe_teleport import allow_engage

    allow_engage(client)
    await client.teleport(position)
    await asyncio.sleep(0.5)
    here = await client.body.position()
    if here.distance(position) > CAMP_POSITION_TOLERANCE:
        logger.warning("could not return to the saved farm camp position")
        return False
    return True


async def go_to_farm_recovery_zone(client, quester) -> bool:
    """Go to Northguard before farm-mode recovery, away from Savarstaad patrols."""
    from .farm import FARM_HEAL_ZONE

    zone = await client.zone_name() or ""
    if zone == FARM_HEAL_ZONE:
        return True
    logger.info(f"going to Northguard for health/mana recovery from {zone or 'unknown zone'}")
    if not await quester.go_to_zone(FARM_HEAL_ZONE) or await client.zone_name() != FARM_HEAL_ZONE:
        logger.warning("could not reach Northguard for health/mana recovery")
        return False
    return True


def new_handler() -> ClientHandler:
    # Clients are created as SprintyClients so they also have WizSprinter's
    # entity helpers (closest mob, wisps, safe spots).
    return ClientHandler(client_cls=SprintyClient)


def release_mouse_buttons(client) -> None:
    """Send the game a left and right button-up. A click is button-down, a
    short wait, button-up: a stop landing in between left the game thinking
    the button was still held, and it ignored the player's own clicks."""
    import ctypes

    try:
        send = ctypes.windll.user32.SendMessageW
        send(client.window_handle, 0x0202, 0, 0)  # WM_LBUTTONUP
        send(client.window_handle, 0x0205, 0, 0)  # WM_RBUTTONUP
    except Exception as exc:
        logger.debug(f"could not release the mouse buttons: {exc!r}")


async def mouse_on_pause(client, controller, mouseless: bool):
    """Paused, the game gets the player's mouse back: the mouseless hook (the
    bot clicks without moving the real cursor) is switched off, and on again
    on resume (the player couldn't use the mouse while paused)."""
    if not mouseless:
        return
    from wizwalker.memory.hooks import MouselessCursorMoveHook

    hooks = client.hook_handler

    def hook_on() -> bool:
        return hooks._check_if_hook_active(MouselessCursorMoveHook)

    released = False
    try:
        while not controller.stopped.is_set():
            if controller.paused and not released:
                release_mouse_buttons(client)
                # The hook itself, not one reference to it: with a deck
                # change (DeckBuilder) also holding it, leaving one reference
                # kept it on and the player never got the mouse back.
                if hook_on():
                    await hooks.deactivate_mouseless_cursor_hook()
                released = True
                logger.info("paused: the mouse is yours")
            elif not controller.paused and released:
                if client.mouse_handler._ref_count > 0 and not hook_on():
                    await hooks.activate_mouseless_cursor_hook()
                released = False
                logger.info("resumed: the bot has the mouse again")
            await asyncio.sleep(0.3)
    finally:
        if released and client.mouse_handler._ref_count > 0 and not hook_on():
            # (The shutdown closes the managed mouseless once: put the hook
            # back so that close is balanced.)
            try:
                await hooks.activate_mouseless_cursor_hook()
            except Exception:
                pass


async def close_handler(handler: ClientHandler):
    """Unhook from the game. Each client is closed separately and failures are
    logged, so one bad unhook doesn't leave the rest of the game patched."""
    for client in list(handler.clients):
        release_mouse_buttons(client)
        try:
            await client.close()
        except Exception as exc:
            logger.opt(exception=exc).error(
                "unhooking failed; restart Wizard101 before running the bot again"
            )


WORLD_HOOKS = ("player_struct", "player_stat_struct", "current_client", "current_render_context")


async def connect(handler: ClientHandler):
    from .gamerestart import bot_game_pid, remember_game

    clients = handler.get_new_clients()
    if not clients:
        raise SystemExit("No Wizard101 window found. Start the game and log in to your wizard first.")
    # The player may play another copy (another account): only the bot's own
    # game is hooked (hooks patch the game's memory), never the focused one.
    pid = bot_game_pid()
    mine = [c for c in clients if c.process_id == pid]
    if mine:
        client = mine[0]
    elif len(clients) == 1:
        client = clients[0]
    else:
        raise SystemExit(
            f"{len(clients)} Wizard101 windows are open and none is known as the bot's "
            "(state/game_client.json).\nClose the other copy, or put the bot's game process id in "
            "state/game_client.json, then start again.")
    if len(clients) > 1:
        logger.info(f"{len(clients)} game windows open; using the bot's own (process {client.process_id})")
    remember_game(client.process_id, client.window_handle)
    logger.info("activating hooks (can take a few seconds; move your wizard a step if it stalls)")
    try:
        hooks = client.hook_handler
        await hooks.activate_all_hooks(wait_for_ready=False)
        _click_left_of_center(client)
        # The window hook fills in at character select too (the player hooks
        # only in the world): a relog cut short left it there, and every
        # restart then timed out. Press Play first, then wait for the rest.
        await asyncio.wait_for(hooks._wait_for_value(hooks._base_addrs["current_root_window"], None),
                               timeout=HOOK_TIMEOUT)
        from .relog import at_character_select, play_from_character_select
        from .upkeep import reconnect_if_asked

        async with client.mouse_handler:
            if await reconnect_if_asked(client):  # "Problem: Unable to find your zone" (lost connection)
                await asyncio.sleep(3.0)
        if await at_character_select(client):
            async with client.mouse_handler:
                await play_from_character_select(client)
        await asyncio.wait_for(
            asyncio.gather(*(
                hooks._wait_for_value(hooks._base_addrs[name], None)
                for name in WORLD_HOOKS
            )),
            timeout=HOOK_TIMEOUT,
        )
    except PatternFailed:
        await close_handler(handler)
        raise SystemExit(
            "\nCould not hook into the game: its memory still holds changes from an earlier bot "
            "session that did not shut down cleanly.\n"
            "FIX: fully close Wizard101 (exit to desktop), start it again, log in, then rerun.\n"
            "To avoid this, stop the bot with Ctrl+Shift+Q (or Ctrl+C) instead of closing its window."
        ) from None
    except TimeoutError:
        raise SystemExit(
            f"Hooks did not activate within {HOOK_TIMEOUT}s. Make sure your wizard is loaded into the "
            "world (not the login or character screen), walk a step while it starts, and try running "
            "as Administrator."
        ) from None
    logger.success("connected to the game")
    from .safe_teleport import install

    install(client)  # every teleport lands clear of enemies (unless meant to start a fight)
    return client


def _client_origin(hwnd: int, awareness: int):
    """The client area's top-left in screen coordinates as a thread with the
    given DPI awareness context sees it (-1 unaware, -4 per-monitor v2)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pt = wintypes.POINT(0, 0)
    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(awareness))
    try:
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
    return pt.x, pt.y


def window_on_screen(hwnd: int) -> float:
    """Share of the game window's area that lies on some monitor (1.0 if it
    can't be read). The window once sat below the left monitor (3% on
    screen): out of the player's sight."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return 1.0
        area = max(1, (r.right - r.left) * (r.bottom - r.top))
        monitors: list[tuple[int, int, int, int]] = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def add(_h, _dc, m, _lp):
            monitors.append((m.contents.left, m.contents.top, m.contents.right, m.contents.bottom))
            return 1

        user32.EnumDisplayMonitors(None, None, proc(add), 0)
        covered = 0
        for left, top, right, bottom in monitors:
            w = min(r.right, right) - max(r.left, left)
            h = min(r.bottom, bottom) - max(r.top, top)
            if w > 0 and h > 0:
                covered += w * h
        return min(1.0, covered / area)
    except Exception:
        return 1.0


def dpi_click_offset(hwnd: int) -> tuple[int, int]:
    """What to add to a client position so the game sees the click there.

    WizWalker makes the bot DPI-aware, so the cursor position it writes is in
    real pixels; the game is DPI-unaware and turns it back into client
    coordinates with its *scaled* origin. On a monitor not at 100% (the game
    sat on a 125% monitor left of a 100% main one) the two origins differ:
    (-2384, 120) real vs (-2419, 96) scaled, so the game saw every click 35px
    right and 24px down. That missed thin buttons (Pass, Flee, message-box
    Yes/No: 41px tall) and made cards need a click "left of center". The fix is
    the scaled origin minus the real one, measured per click (windows move)."""
    try:
        sx, sy = _client_origin(hwnd, -1)
        rx, ry = _client_origin(hwnd, -4)
    except Exception:
        return 0, 0
    return sx - rx, sy - ry


SHUTDOWN_WAIT = 10.0  # seconds the tasks get to end before the game is unhooked anyway


def obey_controller(client, controller):
    """Every teleport, key press and click waits while paused and stops the
    step once stopped (Controller.checkpoint). The player paused the bot and
    it kept walking a door; then stopped it and it still went on: long loops
    inside a step never reached a checkpoint."""
    def gate(fn):
        async def wrapped(*args, **kwargs):
            await controller.checkpoint()
            return await fn(*args, **kwargs)
        wrapped.__wrapped__ = fn
        return wrapped

    client._controller = controller  # (smoothwalk releases W while paused)
    teleport = gate(client.teleport)

    async def noted_teleport(*args, **kwargs):
        client._last_teleport_at = time.monotonic()  # (a move of ours: not a defeat's)
        return await teleport(*args, **kwargs)

    client.teleport = noted_teleport
    client.send_key = gate(client.send_key)
    mouse = client.mouse_handler
    mouse.click = gate(mouse.click)
    # (Every mouse move too: paused mid Team Up, a window click moved the
    # cursor with the mouseless hook off and the step died on HookNotActive.)
    for name in ("click_window", "set_mouse_position"):
        if hasattr(mouse, name):
            setattr(mouse, name, gate(getattr(mouse, name)))


def _click_left_of_center(client):
    """Aim clicks where the game will see them: shift every cursor position by
    the DPI offset (see dpi_click_offset), and click windows at their center."""
    mouse = client.mouse_handler
    set_position = mouse.set_mouse_position
    offset_logged: list = []

    async def set_mouse_position(x, y, *args, **kwargs):
        if x >= 0 and y >= 0:  # (-100, -100) parks the cursor outside the window
            try:
                dx, dy = dpi_click_offset(client.window_handle)
            except Exception:
                dx, dy = 0, 0
            if (dx, dy) != (0, 0) and offset_logged != [(dx, dy)]:
                offset_logged[:] = [(dx, dy)]
                logger.info(f"correcting clicks by ({dx}, {dy}) px for display scaling")
            x, y = x + dx, y + dy
        return await set_position(x, y, *args, **kwargs)

    async def click_window(window, **kwargs):
        r = await window.scale_to_client()
        await mouse.click(int((r.x1 + r.x2) / 2), int((r.y1 + r.y2) / 2), **kwargs)

    mouse.set_mouse_position = set_mouse_position
    mouse.click_window = click_window


def _team_zone(zone: str) -> bool:
    from .teamup import is_team_up_zone

    return is_team_up_zone(zone or "")


async def combat_loop(client, fighter: Fighter, cfg: Config, controller: Controller, adapter=None):
    while not controller.stopped.is_set():
        await controller.checkpoint()
        if await client.in_battle():
            fight_zone = await client.zone_name() or ""  # a defeat moves us elsewhere
            fight_spot = await client.body.position()
            await fighter.handle_combat()
            fight_over = time.monotonic()
            await asyncio.sleep(1.5)
            hp = await client.stats.current_hitpoints()
            max_hp = await max_health(client)
            moved = await client.zone_name() != fight_zone or (
                (await client.body.position()).distance(fight_spot) > DEFEAT_MOVE_DISTANCE
            )
            if getattr(client, "_last_teleport_at", 0.0) > fight_over:
                # We moved ourselves (won on 6%, then "moving somewhere clear"
                # within the wait): not the game sending a defeated wizard back.
                moved = False
            # (Fleeing moves us away too, but isn't a defeat.)
            if not fighter.fled and (hp <= 1 or (max_hp and hp / max_hp < DEATH_HEALTH_RATIO and moved)):
                # Losing a fight sends you back (elsewhere) with a sliver of
                # health; winning on a sliver leaves you standing where you fought.
                controller.record_death(fight_zone)
                if adapter is not None and not _team_zone(fight_zone):
                    adapter.on_fight(list(fighter.last_enemy_names))
                    adapter.on_defeat(list(fighter.last_enemy_names), fighter.last_bosses)
            elif await is_free(client):
                if adapter is not None and not fighter.fled:
                    adapter.on_fight(list(fighter.last_enemy_names))
                    adapter.on_win(list(fighter.last_enemy_names))
                await scan_wisps(client)
                await maintain(client, cfg.upkeep)
        await asyncio.sleep(0.3)


async def status_loop(client, controller: Controller, fighter: Fighter, quester, watchdog):
    """Publish a heartbeat to state/status.json for `wiz101-auto status`."""
    from . import gamerestart
    from .service import write_status

    started = time.time()
    last_offscreen_alert = 0.0
    loading_since = hung_since = 0.0  # a loading screen / "Not Responding" window since
    while not controller.stopped.is_set():
        # A frozen game (a loading screen for minutes, the window not
        # responding): the supervisor restarts it and logs back in.
        now = time.monotonic()
        try:
            loading = await client.is_loading()
        except Exception:
            loading = False
        loading_since = (loading_since or now) if loading else 0.0
        hung_since = (hung_since or now) if gamerestart.window_hung(client.window_handle) else 0.0
        frozen = ""
        if loading_since and now - loading_since > gamerestart.FREEZE_SECONDS:
            frozen = f"a loading screen for {now - loading_since:.0f}s"
        elif hung_since and now - hung_since > gamerestart.HUNG_SECONDS:
            frozen = f"the game window not responding for {now - hung_since:.0f}s"
        if frozen:
            logger.warning(f"ALERT: the game looks frozen ({frozen}); stopping for a game restart")
            gamerestart.request(f"game frozen: {frozen}")
            controller.stop(f"game frozen ({frozen})")
            break
        info = {"state": "paused" if controller.paused else "running", "uptime_s": int(time.time() - started)}
        shown = window_on_screen(client.window_handle)
        info["window_on_screen"] = round(shown, 2)
        if shown < 0.5 and time.monotonic() - last_offscreen_alert > 600:
            last_offscreen_alert = time.monotonic()
            logger.warning(f"ALERT: the game window is {100 - shown * 100:.0f}% off screen (you may not see "
                           "it); the bot still plays, but move it back onto a monitor to watch it")
        try:
            info.update(
                zone=await client.zone_name(),
                level=await client.stats.reference_level(),
                health=f"{await client.stats.current_hitpoints()}/{await max_health(client)}",
                in_battle=await client.in_battle(),
            )
        except Exception as exc:
            info["read_error"] = repr(exc)
        info.update(fights=fighter.fights, deaths=controller.deaths)
        info["deaths_total"] = lifetime.load().get("deaths", 0)
        from .farm import Farm

        farm = Farm.load()
        if farm.active or farm.runs:
            from .farm import load_looted, target_status

            info["farm"] = {
                "dungeon": farm.name, "runs": farm.runs, "active": farm.active,
                "targets": target_status(farm.name, load_looted()),
            }
        if quester:
            info.update(
                objective=quester._last_progress[0],
                objective_age_s=int(time.monotonic() - quester._last_progress_time),
                objectives_completed=quester.objectives_completed,
                # Grinding only when it did grind work lately: the flag stayed on
                # while the pinned main quest's objective was being done, and the
                # stream page said "grinding" over a quest (the player).
                activity="grinding for experience" if quester._grinding and (
                    time.monotonic() - getattr(quester, "_ground_at", -1e9) < GRIND_SHOWN_SECONDS
                ) else "questing",
            )
        if watchdog:
            info["watchdog_nudges"] = watchdog.nudges
        write_status(**info)
        await asyncio.sleep(5)


STUCK_TIMEOUTS_BEFORE_RELOG = 3  # quest steps failing in a row on WizWalker's should_update


async def quest_loop(quester: Quester, controller: Controller):
    stuck = 0  # steps in a row that failed because the game ignored our moves
    while not controller.stopped.is_set():
        await controller.checkpoint()
        try:
            await quester.run_step()
            stuck = 0
        except BotStopped:
            raise
        except Exception as exc:
            logger.opt(exception=exc).warning("quest step failed; retrying")
            if "should_update" in str(exc):
                # Standing on a duel circle whose fight never started, the game
                # stopped taking moves: log out to character select and back.
                stuck += 1
                from .quest import WALK_IN_QUIET

                cutscene = time.monotonic() < getattr(quester, "_walked_in_at", -1e9) + WALK_IN_QUIET
                if (stuck >= STUCK_TIMEOUTS_BEFORE_RELOG and not cutscene
                        and not await quester.client.in_battle()):
                    from .relog import relog

                    stuck = 0
                    controller.allow_idle(180)
                    try:
                        if not await relog(quester.client):
                            logger.warning("ALERT: main quest stuck: the wizard can't move; relog failed")
                    finally:
                        controller.end_idle()
            await asyncio.sleep(2.0)
        await asyncio.sleep(0.5)


async def farm_loop(client, cfg: Config, controller: Controller, progression: Progression, quester):
    """Camp in the wizard's starting zone and fight only its target mob."""
    from .backpack import BackpackSeller
    from .bossfarm import mobs_named
    from .farm import FARM_HEAL_ZONE, is_farm_zone, is_target_mob

    backpack_seller = BackpackSeller(quester)
    camp_zone = await client.zone_name() or ""
    camp_position = await client.body.position() if camp_zone else None
    logger.info(f"camping for {cfg.farm_mob} in current zone {camp_zone or 'unknown'}; will not travel")
    if camp_zone and not is_farm_zone(camp_zone, cfg.farm_zone):
        logger.warning(
            f"current zone {camp_zone} differs from configured farm zone {cfg.farm_zone}; "
            "staying here as requested"
        )
    last_wrong_zone = None
    return_to_camp = False
    waiting_for_safe_recovery = False

    async def restore_camp() -> bool:
        nonlocal return_to_camp
        if camp_position is None:
            logger.warning(
                f"cannot return to farm camp in {camp_zone or 'unknown'}: no camp position was saved"
            )
            return False
        return_to_camp = not await return_to_farm_camp(client, quester, camp_zone, camp_position)
        return not return_to_camp

    while not controller.stopped.is_set():
        await controller.checkpoint()
        zone = await client.zone_name() or ""
        if not camp_zone and zone:
            camp_zone = zone
            camp_position = await client.body.position()
            logger.info(f"farm camp established in {camp_zone}")
        if zone != camp_zone:
            if waiting_for_safe_recovery and zone == FARM_HEAL_ZONE and await is_free(client):
                await maintain(client, cfg.upkeep)
                recovered = await recover(
                    client, cfg.upkeep, controller, safe_heal_zone=FARM_HEAL_ZONE
                )
                hp, mana = await health_mana(client)
                if recovered and not cfg.upkeep.needs_recovery(hp, mana):
                    waiting_for_safe_recovery = False
                    return_to_camp = True
                else:
                    logger.warning(
                        f"still needs recovery in Northguard ({hp:.0%} health, {mana:.0%} mana); "
                        "waiting here instead of returning to farm"
                    )
                    await asyncio.sleep(max(15.0, cfg.farm_seconds_between_fights))
                continue
            if backpack_seller.pending_return and camp_position and await is_free(client):
                backpack_seller.pending_return = not await backpack_seller.return_to_camp(
                    camp_zone, camp_position
                )
                await asyncio.sleep(max(15.0, cfg.farm_seconds_between_fights))
                continue
            if return_to_camp and camp_position and await is_free(client):
                await restore_camp()
                await asyncio.sleep(cfg.farm_seconds_between_fights)
                continue
            if zone != last_wrong_zone:
                logger.warning(
                    f"outside farm camp ({camp_zone}); waiting in "
                    f"{zone or 'unknown zone'} without traveling"
                )
                last_wrong_zone = zone
            await asyncio.sleep(cfg.farm_seconds_between_fights)
            continue
        last_wrong_zone = None
        if await is_free(client):
            if return_to_camp and not await restore_camp():
                await asyncio.sleep(cfg.farm_seconds_between_fights)
                continue
            hp, mana = await health_mana(client)
            needs_recovery = cfg.upkeep.needs_recovery(hp, mana) or (
                cfg.upkeep.collect_wisps and hp < cfg.upkeep.wisp_health_ratio
            )
            if needs_recovery:
                if not await go_to_farm_recovery_zone(client, quester):
                    return_to_camp = (
                        camp_position is not None and await client.zone_name() != camp_zone
                    )
                    await asyncio.sleep(cfg.farm_seconds_between_fights)
                    continue
                return_to_camp = camp_position is not None
            await maintain(client, cfg.upkeep)
            recovered = await recover(
                client, cfg.upkeep, controller, safe_heal_zone=FARM_HEAL_ZONE
            )
            if not recovered:
                waiting_for_safe_recovery = await client.zone_name() == FARM_HEAL_ZONE
                return_to_camp = camp_position is not None and not waiting_for_safe_recovery
                if waiting_for_safe_recovery:
                    await asyncio.sleep(max(15.0, cfg.farm_seconds_between_fights))
                continue
            hp, mana = await health_mana(client)
            if cfg.upkeep.needs_recovery(hp, mana):
                waiting_for_safe_recovery = await client.zone_name() == FARM_HEAL_ZONE
                return_to_camp = camp_position is not None and not waiting_for_safe_recovery
                logger.warning(
                    f"recovery did not reach the fight threshold ({hp:.0%} health, {mana:.0%} mana)"
                )
                if waiting_for_safe_recovery:
                    await asyncio.sleep(max(15.0, cfg.farm_seconds_between_fights))
                continue
            if camp_position:
                zone_after_recovery = await client.zone_name() or ""
                here = await client.body.position()
                if zone_after_recovery != camp_zone or here.distance(camp_position) > CAMP_POSITION_TOLERANCE:
                    return_to_camp = True
                if return_to_camp and not await restore_camp():
                    await asyncio.sleep(cfg.farm_seconds_between_fights)
                    continue
            if camp_position and await backpack_seller.tick(camp_zone, camp_position):
                if await client.zone_name() != camp_zone:
                    return_to_camp = True
                await asyncio.sleep(max(15.0, cfg.farm_seconds_between_fights))
                continue
            await progression.tick()
            pet = getattr(quester, "pet", None)
            if pet:
                controller.allow_idle(1800)
                try:
                    if await pet.tick():
                        return_to_camp = camp_position is not None
                        continue
                except Exception as exc:
                    logger.opt(exception=exc).warning("pet dance trip failed; continuing farm")
                finally:
                    controller.end_idle()
            targets = [
                position
                for name, position in await mobs_named(client)
                if is_target_mob(name, cfg.farm_mob)
            ]
            if targets:
                me = await client.body.position()
                target = min(targets, key=me.distance)
                from .safe_teleport import allow_engage

                allow_engage(client)
                await client.teleport(target)
            else:
                # Waiting at a spawn is intentional; the watchdog's stall
                # recovery would otherwise move the wizard away from camp.
                controller.allow_idle(20)
                logger.debug(f"no {cfg.farm_mob} nearby")
        await asyncio.sleep(cfg.farm_seconds_between_fights)


async def run(cfg: Config):
    s = cfg.safety
    controller = Controller(s.stop_key, s.pause_key, s.max_hours)
    logger.info(f"mode={cfg.mode}; {s.stop_key}=stop, {s.pause_key}=pause/resume")

    handler = new_handler()
    client = None
    tasks: list[asyncio.Task] = []
    stack = contextlib.AsyncExitStack()
    try:
        try:
            client = await asyncio.wait_for(connect(handler), CONNECT_TIMEOUT)
        except TimeoutError:
            # Stuck getting in (after pressing Reconnect on a lost connection it
            # waited seven hours, the servers down, no watcher running yet):
            # the supervisor restarts the game (every 15 min until it's in).
            from . import gamerestart

            gamerestart.request(f"could not get into the world within {CONNECT_TIMEOUT / 60:.0f} min")
            raise SystemExit(f"could not get into the world within {CONNECT_TIMEOUT / 60:.0f} min; "
                             "asked for a game restart") from None
        obey_controller(client, controller)
        client._walk = cfg.movement.walk  # (safe_teleport: walk instead of teleporting)
        client._walk_only = tuple(cfg.movement.walk_only)
        if s.mouseless:
            # Managed mode: helpers like DeckBuilder nest `async with mouse_handler`
            # and must not switch mouseless off underneath us.
            await stack.enter_async_context(client.mouse_handler)

        c = cfg.combat
        try:
            # The simulator's enemies and hit rates, from every fight logged so far (~0.1 s).

            from .combat.calibrate import activity_logs, write_stats

            write_stats(activity_logs())
        except Exception as exc:
            logger.debug(f"enemy stats not refreshed: {exc!r}")
        fighter = Fighter(client, c.strategy, max_discards=c.max_discards, flee_below=c.flee_below,
                          rollouts=c.rollouts)
        dialogue = DialoguePolicy()
        adapter = None
        if c.adapt_deck:
            from .deck_adapt import DeckAdapter

            adapter = DeckAdapter()  # a boss deck after a loss, the general deck after the win
        tasks = [
            asyncio.create_task(controller.watch(), name="safety"),
            asyncio.create_task(mouse_on_pause(client, controller, s.mouseless), name="mouse"),
            asyncio.create_task(combat_loop(client, fighter, cfg, controller, adapter), name="combat"),
            asyncio.create_task(dialogue_loop(client, cfg.quest, controller, dialogue), name="dialogue"),
        ]
        progression = Progression(client, cfg.progression)
        await progression.start()
        quester = None
        if cfg.mode == "quest":
            quester = Quester(client, cfg.quest, controller, progression, cfg.upkeep, dialogue)
            quester.deck_adapter = adapter
            if adapter is None:
                from .deck_keeper import DeckKeeper

                quester.deck_keeper = DeckKeeper()  # the one deck, put back when it drifts
            if cfg.gear_checks:
                quester.gear = GearManager(client, progression.school or "")
                quester.gear.before_check = lambda: move_to_safety(client, 1200.0, "before checking gear")
                quester.gear.level_up_only = not cfg.gear_checks_new_items
                if quester.gear.level_up_only:
                    logger.info("gear checks after level-ups only (gear_checks_new_items: false)")
            else:
                logger.info("gear checks are off (gear_checks: false)")
                from .gear import GearMemory

                # New loot is still noted (the farm's targets come from it);
                # items asked for in state/gear.json "restore" (the player's
                # pick, e.g. the Reshuffle amulet) still go on.
                quester.gear = GearManager(client, progression.school or "")
                quester.gear.loot_only = True
                quester.gear.restore_only = bool(GearMemory().restore)
            if cfg.progression.enabled:
                quester.trainer = SpellTrainer(quester, progression, cfg.progression.train_levels)
            quester.healer = DungeonHealer(quester, cfg.upkeep)
            quester.pet = PetDancer(quester, cfg.pet)
            quester.potions = PotionShopper(quester, cfg.upkeep.use_potions and cfg.upkeep.buy_potions)
            quester.fighter = fighter
            if cfg.quest.flee_unneeded_fights:
                fighter.unneeded_fight = quester.unneeded_fight
            fighter.may_flee = quester.may_flee
            tasks.append(asyncio.create_task(quest_loop(quester, controller), name="quest"))
            from .prompt_watch import prompt_loop

            # The right person's talk prompt: X at once, not at the step's next look.
            tasks.append(asyncio.create_task(prompt_loop(client, quester, controller), name="prompt"))
            from .gatewatch import gate_watch

            # Every gate walked through, the player's too (paused), into doors.json.
            tasks.append(asyncio.create_task(gate_watch(client, quester.doors, controller), name="gates"))
        watchdog = None
        if s.stall_seconds > 0 and cfg.mode == "quest":
            watchdog = Watchdog(
                client,
                controller,
                quester,
                stall_seconds=s.stall_seconds,
                battle_stall_seconds=s.battle_stall_seconds,
            )
            tasks.append(asyncio.create_task(watchdog.run(), name="watchdog"))
        if cfg.mode == "boss":
            boss_quester = Quester(client, cfg.quest, controller, progression, cfg.upkeep, dialogue)
            farmer = BossFarmer(boss_quester, cfg.boss_farm, cfg.upkeep, controller)
            tasks.append(asyncio.create_task(farmer.run(), name="boss"))
        if cfg.mode == "farm":
            farm_quester = Quester(client, cfg.quest, controller, progression, cfg.upkeep, dialogue)
            farm_quester.pet = PetDancer(farm_quester, cfg.pet)
            tasks.append(
                asyncio.create_task(
                    farm_loop(client, cfg, controller, progression, farm_quester), name="farm"
                )
            )

        from .upkeep import endorsement_loop

        tasks.append(
            asyncio.create_task(endorsement_loop(client, controller), name="endorsement")
        )
        tasks.append(
            asyncio.create_task(status_loop(client, controller, fighter, quester, watchdog), name="status")
        )
        stop_waiter = asyncio.create_task(controller.stopped.wait())
        done, _ = await asyncio.wait([*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not stop_waiter and t.exception() and not isinstance(t.exception(), BotStopped):
                logger.opt(exception=t.exception()).error(f"{t.get_name()} task crashed")
                controller.stop(f"{t.get_name()} crashed")
        controller.stop(controller.stop_reason or "finished")

        summary = f"fights: {fighter.fights}, deaths: {controller.deaths}"
        if quester:
            summary += f", objectives completed: {quester.objectives_completed}"
        logger.info(f"session over ({controller.stop_reason}). {summary}")
        from .service import write_status

        write_status(state="stopped", reason=controller.stop_reason, summary=summary)
        return controller.stop_reason
    finally:
        for t in tasks:
            t.cancel()
        # (Bounded: a step that swallowed its cancel kept playing after "session
        # over" at 13:39, so the hooks never came out and stop had to kill the
        # process. Whatever is still running after this, we unhook anyway.)
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT) if tasks else (set(), set())
        if pending:
            logger.warning(f"{len(pending)} task(s) still running at shutdown "
                           f"({', '.join(t.get_name() for t in pending)}); unhooking anyway")
        try:
            await stack.aclose()
        except Exception:
            pass
        await close_handler(handler)
