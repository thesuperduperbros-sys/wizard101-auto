"""Looking for quests to pick up: talk once to each named NPC nearby.

A yellow "!" over an NPC means a quest on offer, but nothing in the game's
memory tells a quest giver from any other NPC (same behaviors). So in the main
quest's world the bot walks up to each named NPC near it once (ambient
townsfolk, "MB-AmbLady-I", are skipped) and talks: the dialogue loop accepts
whatever is offered. More quests in the book means kills and turn-ins made on
the way count twice. Who was asked is kept in state/npc_talked.json and asked
again after an hour (new quests open up as others are done), and entering a
zone not checked for an hour asks everyone in it.

Only a world with a guide (docs/sidequests/<World>.txt, from the player:
quest giver, then quest line, "(after finishing “X”)" for what must come
first) is asked at all, and only the givers who still have something to hand
out: a quest of theirs that isn't in
the quest book, isn't in docs/CompletedQuests.txt, isn't skipped, and whose
"after finishing" quest is done.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from loguru import logger
from wizwalker import XYZ, Keycode

from . import ui
from .upkeep import is_free, mob_positions, wait_until_free

TALKED_PATH = Path("state") / "npc_talked.json"
ASK_AGAIN_HOURS = 1.0
ZONE_CHECKS_PATH = Path("state") / "npc_zone_checks.json"
ZONE_RECHECK_SECONDS = 3600.0  # entering a zone not swept this long: ask every named NPC in it
GIVER_RANGE = 2500.0  # NPCs this close are worth a quick word
CHECK_SECONDS = 20.0  # how often to look for someone new to ask
MOB_CLEARANCE = 700.0  # never walk up to an NPC standing by enemies


def is_named_npc(object_name: str, display: str, behaviors: list[str]) -> bool:
    """A named NPC who may hand out quests: has NPC behavior and a name, and
    isn't ambient scenery ("MB-AmbWalker10") or a trigger."""
    obj = (object_name or "").lower()
    parts = re.split(r"[-_ ]+", obj)
    ambient = any(_AMBIENT.match(p) for p in parts)
    return (
        "NPCBehavior" in behaviors
        and bool((display or "").strip())
        and not ambient
        and not obj.startswith("dynatrigger")
    )


def looks_like_person(name: str) -> bool:
    """A display name that reads like a person ("Thornton Lewis", "The
    Archivist"), not an object id ("CL-Chest-Common-001", "DS_WispHealth")
    or a one-word pick-up ("Ore")."""
    # (Not a door's label: "To Teleporter Hub" sent the bot to Triton Avenue.)
    return (" " in name.strip() and not re.search(r"[_\d]", name) and "-" not in name
            and name[:1].isupper() and not name.startswith("To ")
            and not set(name.lower().split()) & _OBJECT_WORDS)


# Words that make a display name a thing, not a person (the side-quest hunt
# visited 'Cantrip Ritual Chest' and 'Duel Circle').
_OBJECT_WORDS = frozenset({"chest", "circle", "sign", "signpost", "statue", "door", "gate", "portal",
                           "table", "cauldron", "barrel", "crate", "banner", "totem", "teleporter",
                           "fountain", "pedestal", "lever", "brazier", "obelisk", "switch"})


_AMBIENT = re.compile(r"^amb(?!rose)")  # "AmbLady", "AmbWalker10"; not Headmaster Ambrose

GUIDE_DIR = Path("docs") / "sidequests"
MENU_QUESTS = 3  # quest options taken from one NPC's menu
NO_PROMPT_RETRIES = 3  # walks to an NPC that showed no talk prompt before giving up
# Never asked for quests (the player's call): Prospector Zeke's are hunts for
# hidden things (Stray Cat Strut's cats) the bot can't find.
SKIP_GIVERS = frozenset({"prospectorzeke"})
COMPLETED_PATH = Path("docs") / "CompletedQuests.txt"
BOOK_PATH = Path("state") / "quest_book.json"
_QUEST_LINE = re.compile(r"^\*?\s*(?P<name>[^(]+?)\s*\(\s*\d+\s*(gold|xp)", re.I)
_AFTER = re.compile(r"after finishing\s*[“\"]\s*\*?\s*(?P<q>[^”\"]+?)\s*[”\"]", re.I)


@dataclass(frozen=True)
class GuideQuest:
    giver: str
    name: str
    after: str | None = None  # a quest that must be done first
    main: bool = False  # under a "(MAIN QUEST)" heading: the story, done in guide order
    area: str = ""  # the heading it's under ("VILLAGE OF SORROW"): where the giver stands
    goals: list[str] = field(default_factory=list, compare=False)  # its "- ..." lines, in order


def heading_like(line: str) -> bool:
    return line.isupper()


def parse_guide(text: str) -> list[GuideQuest]:
    """Quests of a guide: a quest line ("Name(170 XP)...") right under its
    giver's name; headings (upper case, "(SIDE QUEST)") and goals ("-...")
    are not givers."""
    out, prev, main, area = [], "", False, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "(MAIN QUEST)" in line.upper():
            main = True
        elif "(SIDE QUEST)" in line.upper():
            main = False
        heading = re.sub(r"\((MAIN|SIDE) QUEST\)", "", line, flags=re.I).strip()
        if heading and heading.isupper() and not heading.startswith("("):
            area = heading.replace("’", "'")
        m = _QUEST_LINE.match(line)
        if (m and prev and not prev.startswith(("-", "(")) and not prev.isupper()
                and not _QUEST_LINE.match(prev)):
            after = _AFTER.search(line)
            out.append(GuideQuest(prev, m["name"].strip(), after["q"].strip() if after else None, main, area))
        elif line.startswith("-") and out and not heading_like(line):
            out[-1].goals.append(line.lstrip("- ").strip())
        prev = line
    return out


def norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def same_quest(a: str, b: str) -> bool:
    """The guide's spelling vs the game's ("Gate Crushers"/"Gate Crashers",
    "Mail Calll"), without "More Crazy Cats" matching "Crazy Cats"."""
    a, b = norm(a), norm(b)
    return a == b or (min(len(a), len(b)) >= 6 and SequenceMatcher(None, a, b).ratio() >= 0.9)


def pending_givers(guide: list[GuideQuest], have: set[str], done: set[str]) -> dict[str, list[str]]:
    """Giver (normalized name) -> their quests we can still get: not held or
    skipped (`have`), not done, and what must come first is done (an "after"
    that isn't a quest of the guide, e.g. "The Ironworks", counts as done)."""
    names = [q.name for q in guide]

    def known(x: str, pool) -> bool:
        return any(same_quest(x, y) for y in pool)

    # Done without being logged (CompletedQuests.txt is recent): what a held or
    # done quest came after, and every story quest before the furthest one.
    done = set(done)
    for _ in range(len(guide)):
        more = {q.after for q in guide if q.after and known(q.name, have | done) and q.after not in done}
        if not more:
            break
        done |= more
    story = [q for q in guide if q.main]
    reached = max((i for i, q in enumerate(story) if known(q.name, have | done)), default=-1)
    if reached > 0:  # (-1: none reached yet; story[:-1] would mark all but the last)
        done |= {q.name for q in story[:reached]}
    out: dict[str, list[str]] = {}
    for q in guide:
        if norm(q.giver) in SKIP_GIVERS:
            continue
        if known(q.name, have) or known(q.name, done):
            continue
        if q.after and known(q.after, names) and not known(q.after, done):
            continue
        out.setdefault(norm(q.giver), []).append(q.name)
    return out


def load_guide(world: str) -> list[GuideQuest] | None:
    try:
        return parse_guide((GUIDE_DIR / f"{world}.txt").read_text(encoding="utf-8"))
    except OSError:
        return None


def _book_and_done() -> tuple[set[str], set[str]]:
    have: set[str] = set()
    try:
        have = {q["name"] for q in json.loads(BOOK_PATH.read_text(encoding="utf-8")).get("quests", [])}
    except Exception:
        pass
    try:
        done = {ln.strip() for ln in COMPLETED_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except OSError:
        done = set()
    return have, done


class QuestGivers:
    def __init__(self, quester):
        self.q = quester
        self.client = quester.client
        self._last_check = 0.0
        self._no_prompt: dict[str, int] = {}  # NPCs walked to without a talk prompt
        self.main_sweep_zone = ""  # where the next main quest's giver is asked for (sweep_now)
        self._talked: dict[str, float] = {}
        self._zone = ""
        self._sweeping = False  # asking every named NPC in this zone, one after another
        try:
            self._talked = json.loads(TALKED_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        self._guides: dict[str, list[GuideQuest] | None] = {}
        self._zone_checks: dict[str, float] = {}
        try:
            self._zone_checks = json.loads(ZONE_CHECKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _swept(self, zone: str):
        self._zone_checks[zone] = time.time()
        try:
            ZONE_CHECKS_PATH.parent.mkdir(exist_ok=True)
            ZONE_CHECKS_PATH.write_text(json.dumps(self._zone_checks, indent=1), encoding="utf-8")
        except Exception:
            pass

    def sweep_now(self, zone: str):
        """Ask every NPC of `zone` again at once, the ones asked within the hour
        too: a main quest was just handed in and the next one isn't in the
        book (its giver is usually close by, and was asked before it had it)."""
        if not zone:
            return
        # Asked even in a world without a side-quest list: it's the main
        # story's giver ('Going Portal' after 'Foe of Foes', by Milos
        # Bookwyrm in the Atheneum, in Dragonspyre, which has no list).
        self.main_sweep_zone = zone
        self._zone_checks.pop(zone, None)
        self._talked = {k: v for k, v in self._talked.items() if not k.startswith(f"{zone}|")}
        self._zone = ""  # the next ask_nearby starts the sweep
        self._last_check = 0.0

    def _key(self, zone: str, name: str) -> str:
        return f"{zone}|{name}"

    def _asked_recently(self, zone: str, name: str) -> bool:
        when = self._talked.get(self._key(zone, name))
        return when is not None and time.time() - when < ASK_AGAIN_HOURS * 3600

    def _remember(self, zone: str, name: str):
        self._talked[self._key(zone, name)] = time.time()
        try:
            TALKED_PATH.parent.mkdir(exist_ok=True)
            TALKED_PATH.write_text(json.dumps(self._talked, indent=1), encoding="utf-8")
        except Exception:
            pass

    def wanted_givers(self, zone: str) -> dict[str, list[str]] | None:
        """Givers with quests still to get in this zone's world, per its guide;
        None without a guide (ask every named NPC)."""
        from .questlist import world_of_zone

        world = world_of_zone(zone) or zone.split("/", 1)[0]
        if world not in self._guides:
            self._guides[world] = load_guide(world)
        guide = self._guides[world]
        if guide is None:
            return None
        if self.q.cfg.side_quest_world and world.casefold() == self.q.cfg.side_quest_world.casefold():
            guide = [q for q in guide if not q.main]
        from .setbacks import ALWAYS_SKIP

        have, done = _book_and_done()
        have |= set(getattr(self.q.setbacks, "skipped", set())) | ALWAYS_SKIP
        return pending_givers(guide, have, done)

    def visit_target(self, world: str) -> tuple[str, str] | None:
        """Nothing left to do in `world`: the first giver on the player's list
        who still has a quest for us, not asked in the last hour, with the zone
        they stand in (from the list's area heading). (npc, zone) or None."""
        from .quest import objective_zone

        guide = load_guide(world)
        wanted = self.wanted_givers(f"{world}/") or {}
        for q in guide or []:
            if norm(q.giver) not in wanted or not q.area:
                continue
            zone = objective_zone(f"Talk To {q.giver} in {q.area}")
            if zone and not self._asked_recently(zone, q.giver):
                return q.giver, zone
        return None

    def hunt_target(self, place: str, zones: dict[str, dict], enemies: set[str]) -> tuple[str, str] | None:
        """Nothing left to do in `place` (a world, or a zone prefix like
        Wintertusk's "Grizzleheim/GH_HFjord") and no side-quest list for it: a
        named person seen there (the entity map, `zones`) not asked in the
        last hour, in a zone not swept for quests in the last hour, the zone
        with the most of them first. (npc, zone) or None: then grinding."""
        best: tuple[int, str, str] | None = None
        for zone, names in zones.items():
            if not zone.startswith(place + "/") or "/interiors/" in zone.lower():
                continue
            if time.time() - self._zone_checks.get(zone, 0.0) < ZONE_RECHECK_SECONDS:
                continue
            people = [n for n in names if looks_like_person(n) and n not in enemies
                      and norm(n) not in SKIP_GIVERS and not self._asked_recently(zone, n)]
            if people and (best is None or len(people) > best[0]):
                best = (len(people), people[0], zone)
        return (best[1], best[2]) if best else None

    async def _candidates(self, zone: str, reach: float = GIVER_RANGE) -> list[tuple[float, str, XYZ]]:
        from .names import lang_name
        from .questlist import world_of_zone

        me = await self.client.body.position()
        mobs = await mob_positions(self.client)
        wanted = self.wanted_givers(zone)
        world = world_of_zone(zone) or zone.split("/", 1)[0]
        guide = self._guides.get(world) or []
        listed = {norm(q.giver) for q in guide}  # givers the player's list knows about
        out = []
        for e in await self.client.get_base_entity_list():
            try:
                template = await e.object_template()
                if not template:
                    continue
                code = await template.display_name()
                display = await lang_name(self.client, code) if code else ""
                if not display or self._asked_recently(zone, display) or norm(display) in SKIP_GIVERS:
                    continue
                who = norm(display)
                if wanted is not None and who in listed and who not in wanted:
                    continue  # the list says they have nothing left for us
                pos = await e.location()
                d = math.dist((pos.x, pos.y), (me.x, me.y))
                if d > reach:
                    continue
                if wanted is not None and who not in listed and d > GIVER_RANGE:
                    # Not on the (partial) list: asked when passing close, not
                    # sought out (a Hametsu Village giver was walked past).
                    continue
                if not is_named_npc(await template.object_name(), display, await e.list_behavior_names()):
                    continue
                if any(math.dist((pos.x, pos.y), m[:2]) < MOB_CLEARANCE for m in mobs):
                    continue  # an enemy (or beside one)
                out.append((d, display, pos))
            except Exception:
                continue
        return sorted(out, key=lambda c: c[0])

    async def ask_nearby(self) -> bool:
        """Talk to the nearest named NPC not asked yet (accepting any quest
        offered), then step back. True if it went to one."""
        if not self._sweeping and time.monotonic() - self._last_check < CHECK_SECONDS:
            return False
        self._last_check = time.monotonic()
        zone = await self.client.zone_name() or ""
        world = self.q._main_world
        from .quest import FALLBACK_SIDE_PLACES, same_world

        places = FALLBACK_SIDE_PLACES.get(world, ())
        fallback = self.q._grinding and any(zone.startswith(p + "/") for p in places)
        # (The story's own sweep counts in any world: _main_world still named
        # an older one, and Zafaria's hub NPCs were never asked for its next quest.)
        story_sweep = bool(zone) and zone == self.main_sweep_zone
        off_world = not world or (
            not same_world(zone.split("/", 1)[0], world) and not fallback
        )
        if not zone or (off_world and not story_sweep) or await self.q._in_dungeon(zone):
            return False
        from .questlist import world_of_zone

        guide_world = world_of_zone(zone) or zone.split("/", 1)[0]
        if (load_guide(guide_world) is None and zone != self.main_sweep_zone
                and not self.q._grinding):
            # Only where the player gave a side-quest list (docs/sidequests/<World>.txt):
            # elsewhere NPCs aren't asked at all (Wizard City's, on the way through),
            # unless there's nothing else to do (the player: side quests give far
            # more experience than grinding).
            return False
        if zone != self._zone:
            # A new zone: not swept for an hour, ask everyone in it (quests
            # unlock as the story moves on; the 2500 range alone missed them).
            self._zone = zone
            self._sweeping = time.time() - self._zone_checks.get(zone, 0.0) > ZONE_RECHECK_SECONDS
            if self._sweeping:
                wanted = self.wanted_givers(zone)
                who = ""
                if wanted is not None:
                    who = (f" (guide: {sum(map(len, wanted.values()))} quests from {len(wanted)} "
                           f"givers left in {zone.split('/')[0]}: {', '.join(sorted(wanted))})")
                logger.info(f"checking the NPCs of {zone.split('/')[-1]} for new quests{who}")
        if not await is_free(self.client):
            return False
        found = await self._candidates(zone, float("inf") if self._sweeping else GIVER_RANGE)
        if not found:
            if zone == self.main_sweep_zone:
                self.main_sweep_zone = ""  # everyone there asked once
            if self._sweeping:
                self._sweeping = False
                self._swept(zone)
                logger.info(f"asked every NPC in {zone.split('/')[-1]} for quests")
            return False
        _d, name, pos = found[0]
        self._remember(zone, name)  # once, whatever happens
        me = await self.client.body.position()
        back = XYZ(me.x, me.y, me.z)
        logger.info(f"asking {name} for quests ({_d:.0f} away)")
        # The quester's approach (teleport near, inch in on foot until the talk
        # prompt shows): landing 200 short left the Marleybone hub's quest
        # givers out of range ("no talk prompt").
        await self.q.travel(pos, npc=True)
        await asyncio.sleep(0.3)
        for nudge in (None, (Keycode.S, 0.2), (Keycode.W, 0.3), (Keycode.W, 0.3)):
            if nudge:
                await self.client.send_key(*nudge)
                await asyncio.sleep(0.2)
            if await ui.is_visible(self.client, ui.NPC_RANGE):
                break
        else:
            # Still no prompt (Milos Bookwyrm behind his desk): walk right up
            # to the NPC, as the quest step's talk does.
            try:
                await self.client.goto(pos.x, pos.y)
            except Exception:
                pass
            await asyncio.sleep(0.4)
        prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
        if "talk" in prompt:
            if self.q.dialogue:
                self.q.dialogue.accept_offers_for(20)
            accepted = self.q.dialogue.accepted if self.q.dialogue else 0
            await self.client.send_key(Keycode.X, 0.1)
            await asyncio.sleep(1.5)
            taken: set[str] = set()
            for _ in range(MENU_QUESTS):
                # A menu (several quests, or quests and a shop): each quest
                # option once ('Don't Fall In' at Milos Bookwyrm, beside
                # 'Warkeeper'; closing the menu never took it).
                if not await self.q.services.is_open():
                    break
                if not await self.q.services.choose_quest(taken):
                    await self.q.services.close()
                    break
                await asyncio.sleep(1.0)
                await wait_until_free(self.client, timeout=20)
                await ui.close_menus(self.client)
                await self.client.send_key(Keycode.X, 0.1)  # the menu again, for the next one
                await asyncio.sleep(1.5)
            if await self.q.services.is_open():
                await self.q.services.close()
            await wait_until_free(self.client, timeout=20)
            await ui.close_menus(self.client)
            if self.q.dialogue and self.q.dialogue.accepted > accepted:
                logger.success(f"picked up a quest from {name}")
                self.q._ranked_for = None
                self.q._last_rank = -1e9  # re-rank: maybe it's a quick errand
        else:
            logger.info(f"no talk prompt at {name} ({prompt!r})")
            fails = self._no_prompt.get(name, 0) + 1
            self._no_prompt[name] = fails
            if fails < NO_PROMPT_RETRIES:
                # Not asked after all (Milos Bookwyrm: the teleport by him
                # was refused): again on a later pass.
                self._talked.pop(self._key(zone, name), None)
        if await is_free(self.client):
            await self.client.teleport(back)
        return True
