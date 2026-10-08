"""Pet dance game grinding (the `pet` command).

`pet [--games N]` asks the running bot to go to the Pet Pavilion in Wizard
City at its next free moment, play the dance game on the Wizard City track
until the pet is out of energy (or N games), feed the first snack offered
after each win, then Recall back to where it was and go on questing.

The dance moves come straight from memory: a hook on the code that reads
the pet's dance sequence (from Deimos, github.com/Deimos-Wizard101,
GPL-3.0: the hook by peechez in src/dance_game_hook.py, the game flow from
src/auto_pet.py). The moves are 'a'..'d' for up/right/down/left and are
sent as W/D/S/A key presses.
"""

from __future__ import annotations

import asyncio
import ctypes
import re
import time
from pathlib import Path

from loguru import logger
from wizwalker import XYZ, Keycode
from wizwalker.memory import HookHandler, SimpleHook

from . import ui
from .upkeep import is_free, wait_for_loading

PET_REQUEST = Path("state") / "pet.request"  # `pet`: games to play ("0": until the energy runs out)
PET_PARK = "WizardCity/WC_Streets/Interiors/WC_PET_Park"
GAME_IDLE_SECONDS = 180.0  # a dance game and its rewards: no watchdog recovery meanwhile
DANCE_SIGIL = XYZ(-4450.58, -994.90, -8.04)  # the dance game's sigil in the Pet Pavilion
DANCE_TITLE = "Dance Game"  # the sigil's prompt title
NPC_TITLE = ["WorldView", "NPCRangeWin", "wndTitleBackground", "NPCRangeTxtTitle"]
MOVES = str.maketrans("abcd", "WDSA")
ROUNDS = 5  # rounds in a dance game
DANCE_MOVE_DELAY = 0.03  # small gap so consecutive move inputs are not merged
HOOK_SETTLE = 5.0  # the hook misses turns when a game starts right after it's placed
QUEST_FIRST_MAX = 1800.0  # energy full: the quest it's on finished first, waited for at most this long
MAX_GAMES = 200  # a ceiling for "until the energy runs out"
WM_KEYDOWN, WM_KEYUP = 0x100, 0x101
TRACK_BUTTON = re.compile(r"btnTrack(\d+)$")


class DanceGameMovesHook(SimpleHook):
    """Copies the dance game's move string pointer to an export (peechez, via Deimos)."""

    pattern = rb"\x48\x8B\xD8\x48\x39\x70\x10\x76.\x8B\xC6"
    instruction_length = 7
    exports = [("dance_game_moves", 8)]
    noops = 2

    async def bytecode_generator(self, packed_exports):
        return (
            b"\x48\x8B\xD8"  # mov rbx, rax
            b"\x48\x8B\x00"  # mov rax, [rax]
            b"\x48\xA3" + packed_exports[0][1]  # mov [export], rax
            + b"\x48\x8B\xC3"  # mov rax, rbx
            b"\x48\x39\x70\x10"  # cmp [rax+10], rsi (the instruction replaced)
        )


async def activate_dance_hook(handler: HookHandler):
    if handler._check_if_hook_active(DanceGameMovesHook):
        return
    await handler._check_for_autobot()
    hook = DanceGameMovesHook(handler)
    await hook.hook()
    handler._active_hooks[DanceGameMovesHook] = hook
    handler._base_addrs["dance_game_moves"] = hook.dance_game_moves


async def deactivate_dance_hook(handler: HookHandler):
    if not handler._check_if_hook_active(DanceGameMovesHook):
        return
    hook = handler._get_hook_by_type(DanceGameMovesHook)
    del handler._active_hooks[DanceGameMovesHook]
    await hook.unhook()
    handler._base_addrs.pop("dance_game_moves", None)


def decode_moves(raw: bytes) -> str:
    """The hook's 8 bytes -> the keys to press ('acbd' -> 'WSDA')."""
    return raw.partition(b"\0")[0].decode(errors="ignore").translate(MOVES)


async def read_moves(handler: HookHandler) -> str:
    addr = handler._base_addrs.get("dance_game_moves")
    if not addr:
        return ""
    try:
        return decode_moves(await handler.read_bytes(addr, 8))
    except Exception:
        return ""


def post_keys(window_handle: int, keys: str, delay: float = DANCE_MOVE_DELAY):
    """Key presses straight to the game window (works with it in the background)."""
    user32 = ctypes.windll.user32
    for key in keys:
        user32.PostMessageW(window_handle, WM_KEYDOWN, ord(key), 0)
        user32.PostMessageW(window_handle, WM_KEYUP, ord(key), 0)
        if delay > 0:
            time.sleep(delay)


async def available_tracks(window) -> list[int]:
    """Find visible, numbered track choices in the dance-game selector."""
    found: set[int] = set()
    pending = [window]
    while pending:
        current = pending.pop()
        try:
            name = await current.name()
            match = TRACK_BUTTON.fullmatch(name or "")
            if match and await current.is_visible():
                found.add(int(match.group(1)))
            pending.extend(await current.children())
        except Exception:
            continue
    return sorted(found)


def first_number(text: str) -> int | None:
    """'Energy: 12/45' -> 12."""
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def games_requested() -> int | None:
    """None: no request; 0: until the energy runs out; N: N games."""
    try:
        text = PET_REQUEST.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    n = first_number(text)
    return n if n is not None else 0


async def _visible(root, *names):
    """The first visible window named names[-1] under one named names[-2]
    under ... (each searched anywhere below the one before), else None."""
    current = root
    for name in names:
        found = None
        try:
            for w in await current.get_windows_with_name(name):
                if await w.is_visible():
                    found = w
                    break
        except Exception:
            return None
        if found is None:
            return None
        current = found
    return current


async def _text(root, *names) -> str:
    w = await _visible(root, *names)
    if w is None:
        return ""
    try:
        return ui._TAGS.sub("", await w.maybe_text() or "").strip()
    except Exception:
        return ""


async def _click(client, *names) -> bool:
    w = await _visible(client.root_window, *names)
    if w is None:
        return False
    try:
        await client.mouse_handler.click_window(w)
        return True
    except Exception as exc:
        logger.debug(f"pet: click {names[-1]} failed: {exc}")
        return False


async def _hidden(root, *names) -> bool:
    return await _visible(root, *names) is None


async def _wait_for(check, timeout: float, every: float = 0.15) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await check():
            return True
        await asyncio.sleep(every)
    return False


STAGES = ("baby", "teen", "adult", "ancient", "epic", "mega", "ultra")
PET_STATE = Path("state") / "pet.json"  # the equipped pet as last seen: kind, stage
PET_WINDOWS = Path("state") / "pet_windows.txt"  # every text of the pet-game windows (for mapping)
ENERGY_CHECK_SECONDS = 60.0


def stage_in(texts: list[str]) -> str | None:
    """The furthest pet stage named in `texts` ('...is now an Adult!')."""
    found = [st for st in STAGES for t in texts if re.search(rf"\b{st}\b", t, re.I)]
    return max(found, key=STAGES.index) if found else None


def kind_in(texts: list[str], kinds) -> str | None:
    """A pet kind from `kinds` (lower case) named in `texts`."""
    for k in kinds:
        if any(re.search(rf"\b{re.escape(k)}\b", t, re.I) for t in texts):
            return k
    return None


def goal_reached(kind: str | None, stage: str | None, goals: dict, default_goal: str) -> bool:
    goal = (goals.get((kind or "").lower()) or default_goal).lower()
    if not stage or goal not in STAGES or stage not in STAGES:
        return False
    return STAGES.index(stage) >= STAGES.index(goal)


def load_pet() -> dict:
    import json

    try:
        return json.loads(PET_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_pet(data: dict):
    import json

    try:
        PET_STATE.parent.mkdir(exist_ok=True)
        PET_STATE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


async def _all_texts(window, depth: int = 0) -> list[str]:
    out: list[str] = []
    if window is None or depth > 10:
        return out
    try:
        text = ui._TAGS.sub("", await window.maybe_text() or "").strip()
        if text:
            out.append(text)
        for child in await window.children():
            out += await _all_texts(child, depth + 1)
    except Exception:
        pass
    return out


def in_pet_game(zone: str) -> bool:
    """The pet game's own zone ("ThePhantomZoneWorld/PetGameDance")."""
    return "petgame" in (zone or "").lower()


class PetDancer:
    """Pet trips from inside the bot (the one process hooked in): a `pet`
    request, or (cfg.auto) whenever the wizard's energy is full, until the
    pet reaches its goal stage."""

    def __init__(self, quester, cfg=None):
        from .config import PetConfig

        self.q = quester
        self.client = quester.client
        self.cfg = cfg or PetConfig()
        self.feed = self.cfg.feed
        self._next_check = 0.0
        self._dumped: set[str] = set()
        self._next_track = 0
        self._wait_quest: str | None = None  # energy full: the quest to finish first
        self._wait_since = 0.0

    def done(self) -> bool:
        pet = load_pet()
        return self.cfg.stop_at_goal and goal_reached(
            pet.get("kind"), pet.get("stage"), self.cfg.goals, self.cfg.default_goal
        )

    async def energy(self) -> tuple[int | None, int | None]:
        """(the wizard's energy now, its maximum): pet games cost energy."""
        now = first_number(await ui.named_text(self.client, "textEnergy"))
        try:
            most = await self.client.stats.energy_max()
        except Exception:
            most = None
        return now, most

    async def tick(self) -> bool:
        """Call while free. True if it made the trip."""
        wanted = games_requested()
        if wanted is not None:
            try:
                await self.trip(wanted)
            finally:
                PET_REQUEST.unlink(missing_ok=True)
            return True
        if not self.cfg.auto or time.monotonic() < self._next_check:
            return False
        self._next_check = time.monotonic() + ENERGY_CHECK_SECONDS
        if self.cfg.stop_at_goal and self.done():
            return False
        now, most = await self.energy()
        if now is None or not most or now < most:
            self._wait_quest = None
            return False
        # The player: finish the quest it's on first, then the pet (at most
        # QUEST_FIRST_MAX: a stuck quest doesn't keep the pet waiting).
        if self._wait_quest is None:
            self._wait_quest = getattr(self.q, "_active_quest", None) or ""
            self._wait_since = time.monotonic()
            if self._wait_quest:
                logger.info(f"pet: energy full ({now}/{most}): the dance game after {self._wait_quest!r}")
        open_now = {q.name for q in getattr(self.q, "_last_quests", []) or []}
        if (self._wait_quest and self._wait_quest in open_now
                and time.monotonic() - self._wait_since < QUEST_FIRST_MAX):
            return False
        self._wait_quest = None
        pet = load_pet()
        what = f"{pet.get('kind') or 'pet'} {pet.get('stage') or ''}".strip()
        logger.info(f"pet: energy full ({now}/{most}): off to the dance game ({what})")
        await self.trip(0)
        return True

    async def _learn(self, window_name: str):
        """Read the pet's kind and stage from a pet-game window's texts."""
        w = await _visible(self.client.root_window, window_name)
        if w is None:
            return
        texts = await _all_texts(w)
        if window_name not in self._dumped and texts:
            self._dumped.add(window_name)
            try:
                with PET_WINDOWS.open("a", encoding="utf-8") as f:
                    when = time.strftime("%Y-%m-%d %H:%M")
                    f.write(f"--- {window_name} ({when})\n" + "\n".join(texts) + "\n")
            except OSError:
                pass
        pet = load_pet()
        kind = kind_in(texts, list(self.cfg.goals))
        stage = stage_in(texts)
        changed = False
        if kind and kind != pet.get("kind"):
            pet["kind"], changed = kind, True
        old = pet.get("stage")
        if stage and stage != old and (window_name == "PetLevelUpWindow" or old not in STAGES
                                       or STAGES.index(stage) > STAGES.index(old)):
            pet["stage"], changed = stage, True
        if changed:
            save_pet(pet)
            logger.info(f"pet: {pet.get('kind') or 'the pet'} is {pet.get('stage') or '?'}")

    async def trip(self, wanted: int):
        start = await self.client.zone_name() or ""
        resumed = in_pet_game(start)
        if resumed:
            # Back in a game already (a restart mid-dance): play on from here,
            # then back to the mark the trip made (the player: it sat stuck in
            # the dance game after a restart).
            logger.info("pet: in the dance game already: playing on")
            marked = bool(self.q._mark)
        elif start != PET_PARK:
            marked = await self.q._mark_here("travel", require_clear=False)
            note = " (marked here)" if marked else ""
            logger.info(f"pet: going to the Pet Pavilion for the dance game{note}")
            if not await self._go_to_pavilion():
                logger.warning("pet: could not reach the Pet Pavilion")
                return
        else:
            marked = False
        games, why = 0, "done"
        await activate_dance_hook(self.client.hook_handler)
        await asyncio.sleep(HOOK_SETTLE)
        try:
            limit = wanted or MAX_GAMES
            while games < limit:
                # A game stands still for the watchdog (no quest progress): it
                # cancelled one mid-dance and hopped the wizard away.
                self.q.controller.allow_idle(GAME_IDLE_SECONDS)
                result = await self.play_one()
                if result != "won":
                    why = result
                    break
                games += 1
                logger.success(f"pet: dance game {games} won")
                if self.done():
                    pet = load_pet()
                    why = f"{pet.get('kind') or 'the pet'} reached {pet.get('stage')}: its goal"
                    logger.success(f"pet: {why}")
                    break
        finally:
            self.q.controller.end_idle()
            await self._close_all()
            await deactivate_dance_hook(self.client.hook_handler)
        logger.info(f"pet: {games} game(s) played; stopped: {why}")
        if marked and self.q._mark and await is_free(self.client):
            await self.q._recall(self.q._mark.zone)

    async def _go_to_pavilion(self) -> bool:
        from .trainer import home_to_ravenwood

        zone = await self.client.zone_name() or ""
        # Another world, or a Wizard City room no gate leads out of: Go Home,
        # out of the dorm into Ravenwood; then the gates (Commons, Pavilion).
        if not zone.startswith("WizardCity/") or "/interiors/" in zone.lower():
            if not await home_to_ravenwood(self.q):
                return False
        await self.q.go_to_zone(PET_PARK)
        await wait_for_loading(self.client)
        return await self.client.zone_name() == PET_PARK

    async def _on_sigil(self) -> bool:
        """Stand on the sigil until its prompt shows."""
        for _ in range(4):
            if await ui.text_at(self.client, NPC_TITLE) == DANCE_TITLE:
                return True
            await self.client.teleport(DANCE_SIGIL)
            await asyncio.sleep(1.0)
            if await ui.text_at(self.client, NPC_TITLE) == DANCE_TITLE:
                return True
            await self.client.send_key(Keycode.S, 0.2)
            await self.client.send_key(Keycode.W, 0.3)
            await asyncio.sleep(0.6)
        return False

    async def play_one(self) -> str:
        """One game, from the sigil to the reward screen closed: 'won', or
        why it stopped ('no energy', 'no snacks', 'no sigil', 'no game')."""
        root = self.client.root_window
        if await _visible(root, "PetGameDance"):
            # A game under way (resumed): dance it out, then its rewards.
            if not await self.dance():
                return "no game"
            return await self.collect()
        await self._finish_rewards()  # (a reward page left open from the last game)
        if not await _visible(root, "PetGameTracks"):
            if not await self._on_sigil():
                return "no sigil"
            for _ in range(10):
                await self.client.send_key(Keycode.X, 0.1)
                if await _wait_for(lambda: _visible(root, "PetGameTracks"), 1.5):
                    break
            else:
                return "no game"
        await self._learn("PetGameTracks")
        cost = first_number(await _text(root, "PetGameTracks", "txtEnergyCost"))
        have = first_number(await _text(root, "PetGameTracks", "txtYourEnergy"))
        logger.info(f"pet: energy {have} (a game costs {cost})")
        if cost is not None and have is not None and have < cost:
            return "no energy"
        # (Clicks while the scroll still unrolls are lost: the game never
        # started on the other window. Settle first.)
        await asyncio.sleep(1.5)
        tracks_window = await _visible(root, "PetGameTracks")
        tracks = await available_tracks(tracks_window) if tracks_window else []
        if not tracks:
            logger.warning("pet: no visible dance tracks were found")
            return "no game"
        start = self._next_track % len(tracks)
        order = tracks[start:] + tracks[:start]
        selected = None
        for track in order:
            if not await _click(self.client, "PetGameTracks", f"btnTrack{track}"):
                continue
            await asyncio.sleep(0.15)
            if not await _click(self.client, "PetGameTracks", "btnNext"):
                continue
            if await _wait_for(lambda: _hidden(root, "PetGameTracks"), 4):
                selected = track
                break
            logger.info(f"pet: track {track} did not start; trying the next available track")
        if selected is None:
            logger.warning("pet: none of the visible dance tracks would start")
            return "no game"
        self._next_track = (tracks.index(selected) + 1) % len(tracks)
        logger.info(f"pet: started dance track {selected}")
        if not await self.dance():
            return "no game"
        return await self.collect()

    async def dance(self) -> bool:
        root = self.client.root_window
        if not await _wait_for(lambda: _visible(root, "PetGameDance", "txtAction"), 30):
            logger.warning("pet: the dance game didn't start")
            return False
        for n in range(ROUNDS):
            # The pet shows the moves, then "Go!": the player's turn.
            async def go():
                return "Go!" in await _text(root, "PetGameDance", "txtAction")

            async def not_go():
                return not await go()

            await _wait_for(not_go, 15)
            if not await _wait_for(go, 30):
                logger.warning(f"pet: no 'Go!' in round {n + 1}")
                return False
            await asyncio.sleep(1.5)
            moves = await read_moves(self.client.hook_handler)
            logger.info(f"pet: round {n + 1}: {moves or '(no moves read)'}")
            post_keys(self.client.window_handle, moves)
        await asyncio.sleep(3.0)
        return True

    async def collect(self) -> str:
        """The reward screen: Next, the first snack and Feed Pet, Finish."""
        root = self.client.root_window
        if not await _wait_for(lambda: _visible(root, "PetGameRewards"), 30):
            return "no game"
        await self._learn("PetGameRewards")
        await _click(self.client, "PetGameRewards", "btnNext")  # Next
        await asyncio.sleep(1.5)
        await self._close_level_up()
        result = "won"
        if self.feed:
            if await _click(self.client, "PetGameRewards", "chkSnackCard0"):
                await asyncio.sleep(0.6)
                await _click(self.client, "PetGameRewards", "btnNext")  # Feed Pet
                await asyncio.sleep(1.0)
                await self._close_level_up()
            else:
                result = "no snacks"
        await self._finish_rewards()
        return result

    async def _finish_rewards(self):
        """Through the reward pages to the end: Finish (btnBack) when shown,
        else Next (a page of the pet's improved stats has only Next: leaving
        it open blocked the next game)."""
        root = self.client.root_window
        for _ in range(40):
            if not await _visible(root, "PetGameRewards"):
                return
            await self._close_level_up()
            if not await _click(self.client, "PetGameRewards", "btnBack"):
                await _click(self.client, "PetGameRewards", "btnNext")
            await asyncio.sleep(0.6)

    async def _close_level_up(self):
        root = self.client.root_window
        for _ in range(10):
            if not await _visible(root, "PetLevelUpWindow"):
                return
            await self._learn("PetLevelUpWindow")
            logger.success("pet: the pet leveled up")
            await _click(self.client, "PetLevelUpWindow", "btnPetLevelClose")
            await asyncio.sleep(0.3)

    async def _close_all(self):
        for _ in range(10):
            root = self.client.root_window
            if await _visible(root, "PetGameTracks"):
                await _click(self.client, "PetGameTracks", "btnBack")
            elif await _visible(root, "PetGameRewards"):
                await self._finish_rewards()
            else:
                return
            await asyncio.sleep(0.4)
