"""Follows the in-game quest helper (the quest arrow) until stopped.

Each `step()`:
  1. reads the quest objective text and the quest marker position,
  2. travels to the marker (teleport with bounce detection, or walking),
  3. interacts with whatever is there (NPC, door, dungeon sigil, object),
  4. for "Defeat ..." objectives, pulls the nearest mob if no fight started.

Combat itself is handled concurrently by the Fighter task.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from wizwalker import XYZ, Keycode

from . import ui
from .bring_out import BringOut
from .collect import (
    Collector,
    away_from,
    collect_item_name,
    floor_points,
    landmarks,
    loose_names,
    matches_item,
    path_points,
    spread_points,
)
from .config import QuestConfig
from .deck import close_spellbook
from .dungeon_heal import DUNGEON_MANA_TRIP
from .dungeons import DungeonEntry, DungeonMemory
from .entitymap import DoorMemory, EntityMap
from .entitymap import scan as scan_entities
from .farm import Farm
from .givers import QuestGivers
from .marks import RETURN_KINDS, Mark, load_mark, recall_is_faster, save_mark, should_travel_mark
from .npc import ServicesMenu
from .questlist import SIDE_WORLDS, CompletionTracker, load_quest_list, norm
from .safe_teleport import allow_close_landing, allow_engage, teleport_aborted, walk_zone
from .setbacks import ALWAYS_SKIP, DEFEATS_TO_DEFER, MAIN_DEFEATS_TO_DEFER, Setbacks
from .teamup import TEAM_UP_NAMES, is_team_up_zone
from .travel_data import (
    find_zone_gate,
    gate_behind,
    gate_kind,
    gate_toward,
    hops_to_place,
    last_zone_jump,
    learn_gate,
    note_zone_jump,
    objective_zone,
    press_x_gate,
    quest_spots,
    ride_gate,
    zone_hops,
    zones_near,
)
from .upkeep import (
    clear_popups,
    heal_in_room,
    health_mana,
    is_free,
    mob_positions,
    move_to_safety,
    recover,
    scan_wisps,
    unstick,
    wait_for_loading,
    wait_until_free,
    wisp_memory,
)
from .wisps import sweep_points

INTERACT_RANGE = 750.0
# Marks go down only before a dungeon (its sigil) and before a heal trip; the
# travel and fight marks kept replacing the one that mattered.
TRAVEL_AND_FIGHT_MARKS = False
DEFEAT_NO_MARK_SECONDS = 120.0  # right after a defeat the wizard stands in the hub: no heal marks
NPC_INCH_RANGE = 1500.0  # a refused teleport this close to an NPC: step closer; farther off, it's a door
EXPOSED_RADIUS = 1200.0  # standing still (menus, marking) this close to an enemy invites a fight
BOUNCE_DISTANCE = 20.0
WISP_SCAN_SECONDS = 30.0
STATUS_EVERY_SECONDS = 20.0
SWITCH_QUEST_AFTER = 4  # same objective, this many interactions without change
MAX_QUEST_SLOTS = 6
MAX_BOOK_PAGES = 30  # the quest book, read to its last page (was 5: quests went missing)
RANK_QUESTS_EVERY = 120.0  # at most this often: quest-book rankings (on objective changes)
NO_MAIN_ALERT_SECONDS = 1800.0  # "no main quest in the book" alert at most this often
# Quest book window paths (mapped by Deimos).
QUEST_LIST = ["WorldView", "DeckConfiguration", "wndQuestList"]
BOOK_FIELDS = {
    "txtName", "txtWorld", "txtGoal", "txtGoalObjective1", "txtZone", "txtReward1Amount",
    "imgEncounter", "txtGoalCounter", "imgActivityQuestType", "LeftMainline", "imgActiveQuest",
}
QUEST_BOOK_ALL = [*QUEST_LIST, "QuestLogAllButton"]
# (Landings beside a refused marker end up to ~300 off: the object there, or
# the marker reached, within this.)
MARKER_REACHED = 800.0
FALL_DROP = 600.0  # a walk that drops this much went off a ledge
USE_OBJECT_RANGE = 200.0  # already this close to a "Use X" object: no need to announce the trip
RECALL_KINDS = ("travel", "room", "dungeon")  # marks a travel Recall may use
CIRCLE_NEAR_MARKER = 1500.0  # a duel circle this close to a Defeat marker is the fight's spot
OBJECT_REACH_SLACK = 120.0  # landed this near a hop spot around an object: we stood beside it
GUARD_RANGE = 1500.0  # enemies this close to a lever are its guards: fought before pulling it
FLOOR_HEIGHT_STEP = 1000.0  # spots this far apart in height are on different floors
# Dungeons where a pulled lever needs time (Counterweight East: the
# counterweight must reach the top before Sprockets spawns): zone -> seconds.
LEVER_WAITS = {"Marleybone/MB_BigBen/MB_CounterweightEast": 8.0}
# Dungeons whose boss the bot leaves to the player once the levers are pulled.
HAND_OVER_BOSS_ZONES = {
    "Marleybone/MB_BigBen/MB_CounterweightEast",
    "Marleybone/MB_BigBen/MB_CounterweightWest",
}
BOSS_SPAWN_WAIT = 8.0  # after the last lever, before walking up to the boss
GATE_FRONT = 250.0  # land this far in front of the gate the last lever opened
GATE_BEYOND = 400.0  # and walk this far past it
NEAR_START = 2500.0  # already this close to where the walk up starts: walk from here
FIGHT_START_WAIT = 25.0  # standing still after walking into a fight's circle
CIRCLE_KEEP_AWAY = 1200.0  # searches and hops never land this close to a duel circle
CIRCLE_WALK_FROM = 1100.0  # land this far from it, then walk in
TEAM_JOIN_FROM = 500.0  # joining a teammate's fight: land this far from the circle, walk in
TEAM_FOLLOW = 600.0  # farther than this from the nearest teammate: catch up
TEAM_FOLLOW_WALKING = 300.0  # on foot (walk-only zones): this close behind the lead teammate
# Team dungeons only ever entered for the farm (Mount Olympus is the story's
# too): with farming off, the bot leaves them.
FARM_ONLY_DUNGEONS = ("WizardCity/Gauntlets/WC_Triton_Gauntlet1/",)
DOOR_SPOT_FAR = 800.0  # a learned door walk starting farther off than this: start near the door
DOOR_SPOT_NEAR = 300.0  # ...this far short of it
SPIRAL_PAGES = 6  # pages of the Spiral Map's world list looked through for a world
MAP_GO_TRIES = 3  # Go To World pressed this many times without leaving: close the map
DUNGEON_SETTLE = 15.0  # after a fight in a dungeon: no heal trip out for this long (cutscenes)
BOSS_SPAWN_WAIT = 60.0  # with a boss to beat next: up to this long for him to appear before a heal trip
PET_RESUME_SECONDS = 600.0  # in the Pet Pavilion: the trip resumed at most this often
NO_FIGHT_HEAL_BELOW = 0.35  # a step with no fight: heal only below this health...
NO_FIGHT_MANA_BELOW = 0.15  # ... or this mana
ENGAGE_RESET_MISSES = 6  # tries at a boss that start nothing: leave the dungeon and enter a fresh copy
ENGAGE_WALK_TRIES = 3  # walk-ins at a boss that start nothing: then landings on him in between
HUNT_EXHAUSTED_SECONDS = 3600.0  # after the NPC hunt ran dry: other worlds' side quests for this long
GATE_CATCH_UP = 250.0  # catching up with the team: teleport this far short of their gate, walk in
TEAM_GATE_NEAR = 1500.0  # on foot: a known gate this near where the team vanished is where they went
TEAM_LOST_WALKING = 3.0  # on foot: the lead out of sight this long went through a door: after them
ROOM_ALONE_ADVANCE = 10.0  # come into a room with no teammate in it: this long, then on to the next room
KNOWN_DOOR_NEAR = 900.0  # a remembered door walk this near the marker is the way to it
TEAM_MARKER_NEAR = 400.0  # this near the quest marker in a team dungeon: arrived, wait there
ROOM_MOB_CLEAR = 1500.0  # on to the next room only with no enemy this near us, the landing or the gate
TEAM_BEHIND = 250.0  # ... landing this far behind them
TEAM_WAIT_TICK = 1.0  # seconds between looks for a teammate's fight
RAVENWOOD = "WizardCity/WC_Ravenwood"
WORLD_TREE = "WizardCity/WC_Ravenwood_Teleporter"  # inside Bartleby: the Spiral Map's world gate
DETOUR_GAP_FILE = Path("state") / "detour_gap.json"  # {"zone", "asked"}: where the detour's quest ended
DETOUR_ASK_SECONDS = 3600.0  # its NPCs asked again after this
# {"zone", "asked"}: where the main story was last worked on (its NPCs asked for its next quest)
MAIN_STORY_ZONE_FILE = Path("state") / "main_story_zone.json"
VISIT_FILE = Path("state") / "visit_npc.json"  # {"npc", "zone"}: go and talk to them
SPIRAL_WORLD_NAMES = {"WizardCity": "wizard city", "Krokotopia": "krokotopia", "Marleybone": "marleybone",
                      "MooShu": "mooshu", "DragonSpire": "dragonspyre", "Celestia": "celestia",
                      "Grizzleheim": "grizzleheim"}
CYCLOPS_LANE = "WizardCity/WC_Streets/WC_Cyclops"
AQUILA_PORTAL = (-10241.0, 8219.0, 0.0)  # its "press X" prompt goes to Aquila (the hub, by Silenus)
BARTLEBY_MOUTH = (31.0, 1854.0, 56.0)  # WC_BartlebyMouth_Door: into the World Tree (to Aquila)
TEAM_APPROACH = 2500.0  # farther than this from the boss's marker: go closer
TEAM_STANDOFF = 1300.0  # ... stopping this far from it (the team starts the fight)
TEAM_CIRCLE_NEAR = 1500.0  # a duel circle this near the marker is the boss's fight
TEAM_RERANK_SECONDS = 120.0  # in a team dungeon: read the quest book on entering, then this often
# Where hard-to-find things are, from the player: item (letters only, lower
# case) -> (zone, (x, y, z) of a landmark, radius to search around it).
FIND_HINTS: dict[str, tuple[str, tuple[float, float, float], float]] = {}
MINIGAME_WORLD = "ThePhantomZoneWorld"  # minigames' zones (Shockalock): never where a quest is
SEEK_SPOTS = 12  # remembered spots of a Defeat target looked at, nearest first
SEEK_NEAR = 600.0  # a remembered spot closer than this: already looked
SEEK_CLEAR = 1200.0  # no other kind of enemy this close to a spot we go to
LONE_TARGET_CLEARANCE = 800.0  # going after an enemy: no other kind this close to it
ENGAGE_BACKOFF = 400.0  # landing on it started no fight: walk in from this far, times the misses
ENGAGE_BACKOFF_MAX = 2400.0
WALKED_CLOSE = 250.0  # a walked path ended this near its target: arrived
WALK_IN_MIN = 600.0  # a walk-in starts at least this far from the boss
MAIN_LOSSES_NO_LADDER = 5  # a story fight lost this often with one fixed deck: side quests, then again
MAIN_LOSSES_RETRY_SECONDS = 3600.0  # (or at the next level-up)
MAIN_LOSSES_RETRY_SAME_LEVEL = 2  # back after the hour at the same level: this many, then wait again
DETOUR_DEFEATS = 15  # a detour world's fight lost this often waits (the main story meanwhile)
DETOUR_RETRY_SECONDS = 3 * 3600.0  # (or a level-up; an hour meant two more deaths an hour to Jotun's trio)
# Bosses whose fight is much easier with others beaten first in side dungeons
# (the player: Jotun fights with his brothers Ullik and Grettir unless they're
# beaten in Helgrind Warren and Winterdeep Warren, the sigils either side of
# Nidavellir's Entrance Hall; then he's soloable). boss -> [(brother, zone of
# the sigil, the sigil)].
# Bosses that appear only once their dungeon's tasks are done (Sylster
# Glowstorm: after the Waterworks' four levers and the Drain Valve). Not
# being in view isn't "the wrong copy": the bot left the Waterworks for it.
LATE_BOSSES = {norm("Sylster Glowstorm")}

PRE_BOSSES = {
    "jotun": [
        ("Ullik", "Grizzleheim/GH_AbandCity/GH_EntranceHall", (-3010.0, 8610.0, -199.0)),
        ("Grettir", "Grizzleheim/GH_AbandCity/GH_EntranceHall", (3032.0, 8660.0, -199.0)),
    ],
}
PRE_BOSSES_FILE = Path("state") / "prereq_bosses.json"  # brothers beaten (names)
PRE_BOSS_ROOMS_FILE = Path("state") / "prereq_rooms.json"  # brother -> his warren's zone


def load_pre_boss_rooms() -> dict[str, str]:
    try:
        return dict(json.loads(PRE_BOSS_ROOMS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def pending_pre_bosses(target: str, beaten: set[str]) -> list[tuple[str, str, tuple]]:
    """The side bosses still to beat before `target` (PRE_BOSSES)."""
    plan = PRE_BOSSES.get(norm(target), [])
    done = {norm(b) for b in beaten}
    return [p for p in plan if norm(p[0]) not in done]


def load_pre_bosses_beaten() -> set[str]:
    try:
        return set(json.loads(PRE_BOSSES_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


WARREN_SCOUT_SPACING = 1200.0  # scouting a brother's warren: hops this far apart
WARREN_SCOUT_BATCH = 8  # ... this many per step
# Bosses whose defeat opens the way to a brother (the player: in Helgrind
# Warren the Runed Annihilator and another boss, then the gate to Ullik).
# Grendel Sapscar (with a Grendel Witch Doctor) after the Annihilator.
WARREN_GATE_BOSSES = {"Ullik": ("Runed Annihilator", "Grendel Sapscar")}
WARREN_BLOCKED_TRIES = 2  # walks to the brother that started nothing: he's behind a locked gate
WARREN_SCOUT_ROUNDS = 2  # full scouts of a warren before fighting through it
WARREN_USE_RANGE = 1500.0  # objects this near a scouting hop are used
WALK_IN_QUIET = 120.0  # seconds after a walk-in with no stuck checks (the boss's cutscene)
# Outdoor zones with enemies, per world (until a fight there is won).
GRIND_FALLBACK = {"Celestia": "Celestia/CL_Z02_Crab_Realm", "Grizzleheim": "Grizzleheim/GH_Wolf"}
# Where to grind, highest level first.
GRIND_WORLDS = ["Celestia", "DragonSpire", "MooShu", "Marleybone", "Krokotopia", "Grizzleheim", "WizardCity"]
MARK_SAFE_RADIUS = 1500.0  # a (non-dungeon) mark only this far from every enemy
WALK_IN_LEGS = 4  # walking in from a dungeon's entrance: stops to look for the person
KNOWN_SPOT_TRIES = 3  # visits to a spot where a collect item was seen, per objective
FROZEN_REFUSALS = 2  # teleports refused even after a long wait, in a row: is the wizard frozen?
TELEPORT_SETTLE = 0.4  # after a jump (the safe-teleport wrapper already waits for arrival)
TALK_QUIET_SECONDS = 1.5  # a conversation is over after this long with no dialogue
DOOR_LEARN_RANGE = 3000.0  # a last landing this near the marker before a zone change: its door's way in
TP_SPOT_NEAR = 1500.0  # a refused teleport: a saved good spot this near the target is tried first
STILL_RANGE = 40.0  # a teammate that moved less than this between two looks is standing still
STILL_WINDOW = 12.0  # ... looks this close together count (a whole step takes ~5 s: at 4 s none ever did)
BOSS_WATCH = 20.0  # by the boss's circle: watching this long, every second, for a teammate to start it
DOOR_NEAR = 900.0  # this close to a door marker: walk through it (travel stops short)
DOOR_TRIES = 3  # a team door walk that changes nothing this often: look elsewhere
TEAM_LOST_AFTER = 8.0  # no teammate in sight this long: go after them
TEAM_TRACK_AHEAD = 900.0  # following their tracks: walk this far on past where they were last seen
TEAM_TRACK_TRIES = 2  # ... this many times per spot, then the known doors
TEAM_GONE_AFTER = 240.0  # had a team but none seen for this long (searching the rooms): they left; leave
TEAM_TALK_TRIES = 3  # a talk step in a team dungeon: tries before it's taken as waiting on the team
TEAM_ALONE_QUEST = 20.0  # a quest dungeon on the team list, no teammate in sight this long: out, Team Up
TEAM_ALONE_AFTER = 180.0  # no teammate seen at all since entering, this long: alone (leave, wait for a team)
TEAM_DOOR_NEAR = 2000.0  # a door this near where they were last seen is the way they went
FLOOR_BELOW_MAX = 1500.0  # how far under a raised fight to look for the floor to walk up from
ON_GROUND = 500.0  # an approach spot this close to a known ground point is on the map
APPROACH_TRIES = 8  # approach spots tried around a door
NPC_SEARCH_DEPTH = 2  # doors from the quest's zone, then the doors behind those
NPC_NEAR_MARKER = 900.0  # a named NPC this close to a prompt-less marker is who to talk to
DOOR_RANGE = 300.0  # at the marker with no prompt: probably a doorway
DOOR_OVERSHOOT = 200.0
WALK_STEP_SECONDS = 0.25  # walking into a door in short steps, checking the zone after each
WALK_MAX_STEPS = 40
APPROACH_DISTANCES = (250.0, 450.0, 700.0)
SIGIL_RANGE = 150.0  # a dungeon sigil this close to the marker is the way in
# Standing at a "press X to enter" prompt: a sigil this close is the one (a
# 4-player sigil's circles spread far from its center).
SIGIL_NEAR_RANGE = 800.0
SIGIL_WAIT_TICKS = 30  # half-seconds to stand still after one X at an entry prompt
STUCK_CHECK_AFTER = 20.0  # seconds on one objective before checking we can still walk
STUCK_CHECK_EVERY = 30.0
UNREACHED_BEFORE_FIGHT = 2  # failed approaches to an in-dungeon marker before fighting to open a gate
FLEES_BEFORE_FIGHTING = 2  # after fleeing the same enemies this often on one objective, fight
TURN_IN_TALK_SECONDS = 180.0  # a quest gone this soon after a talk: that talk handed it in
NAME_RECHECK_SECONDS = 30.0  # a collect/use name with nothing like it in the zone: look again
WANTED_SCAN_SECONDS = 8.0  # how often to look for wanted collect items in view
WINS_COUNT_AS_PROGRESS = 5  # won fights without the objective moving that still count
STUCK_RETRY_SECONDS = 1800.0  # a quest set aside for being stuck (not beaten) is tried again after this
GRIND_RERANK_SECONDS = 90.0  # while grinding, re-read the quest book this often
STUCK_TIMES_TO_FARM = 3  # a main quest found stuck this often: farm Aquila instead
ALERT_REPEAT_SECONDS = 3600.0  # the same main quest is alerted about at most hourly
STALL_SWITCH_SECONDS = 180.0  # no objective change and no won fight: follow another quest
# Attempts at one approach (per objective and zone) before it's skipped for
# the next one; when every approach is used up the quest is set aside.
APPROACH_LIMITS = {"talk_marker": 2, "marker_x": 2, "walk": 2, "teleporter": 3, "sweep": 2, "inch": 2,
                   "walk_in": 1, "reenter": 1, "lone_wait": 5, "boss_room_door": 3,
                   "use_walk": 2, "collect_marker": 3, "marker_travel": 2,
                   "spirit_portal": 2, "go_to_spot": 2, "known_door": 2, "find_marker": 8, "collect_sigil": 2,
                   "zone_first": 3, "fight_for_item": 2, "hub_button": 2, "locked_door_early": 8,
                   "zone_boss": 4, "locked_door_late": 8, "door_key": 3, "door_key_use": 2}
BOSS_ON_CIRCLE = 500.0  # an enemy this near a duel circle's center stands on it (a boss)
EXIT_LEARN_SECONDS = 5.0  # out of a dungeon this soon after a landing: that landing was its exit
ALIAS_AFTER_FIGHT = 60.0  # a Defeat counter that moves this soon after a fight was moved by it
WANTED_FIGHT_SECONDS = 30.0  # a fight started on purpose within this long isn't fled
# Doors that want an item first, from the player: target (lower case) ->
# (the item, the zones it's found in, in the order to try them).
DOOR_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    # "The door is locked. Find the crystal.": at the end of the Howling Cave
    # or the Dragon's Maw (its boss), off the Great Spyre where the Oni roam.
    "malistaire drake": ("Crystal", (
        "DragonSpire/DS_A3_Kings/DS_A3Z3_Volcano/DS_Volcano_Cave1",
        "DragonSpire/DS_A3_Kings/DS_A3Z3_Volcano/DS_Volcano_Cave3",
        "DragonSpire/DS_A3_Kings/DS_A3Z3_Volcano/DS_Volcano_Cave2",
    )),
}
ZONE_BOSS_RANGE = 1500.0  # enemies this near a zone's duel circle are its boss and guards
SPIRAL_TRIP_SECONDS = 90.0  # one go at the trip to another world (dorm, World Tree, gate, map)
GATE_STAND = 150.0  # land this far from the World Tree's gate: its "Press X" prompt shows
PORTAL_NEAR_MARKER = 3000.0  # a spirit portal this close to a Defeat marker leads to the fight
CANDLE_RANGE = 3000.0  # ritual candles around the portal
COLLECT_MARKER_RANGE = 2500.0  # an item to collect not in view, the marker farther: go to the marker
COLLECT_DOOR_NEAR = 1200.0  # an item this close to the marker: the marker is the item, not a door
def use_spots() -> list[tuple[float, float]]:
    """Where to stand to use an object, as offsets from it: on it, a few
    feet off, then rings of 8 at 120, 200 and 300."""
    out = [(0.0, 0.0), (0.0, -60.0), (0.0, 60.0), (-60.0, 0.0), (60.0, 0.0)]
    for r in (120.0, 200.0, 300.0):
        out += [(r * math.cos(i * math.pi / 4), r * math.sin(i * math.pi / 4)) for i in range(8)]
    return out


def _real_marker(m: XYZ) -> bool:
    """A quest marker that points somewhere: (0, 0, z) is none (the Town
    Dojo's marker sat on the room's center, under Ting Yin, for Usunoki)."""
    return abs(m.x) > 1 or abs(m.y) > 1


KNOWN_DOOR_NEAR_MARKER = 1500.0  # a learned door this close to a Defeat marker leads to the target
MOMENTUM_SECONDS = 180.0  # a quest whose objective moved on this recently is kept over a re-ranking
LAND_BESIDE_RADII = (150.0, 300.0)  # a refused teleport onto a marker in this zone: rings around it
_FIND = re.compile(r"(?i)^\s*find\s+(.+?)(?:\s+in\s+[A-Z].*)?\s*$")
_GO_TO = re.compile(r"(?i)^\s*go\s+to\s")
_USE_OBJECT = re.compile(
    r"(?i)^\s*(?:use|burn|light|activate|open|ring|pull|learn|read|study|examine|inspect|touch)\s")
WALK_LEG = 1500.0  # teleport hops toward a far marker, a look for the target after each
WALK_LEGS = 25
RECALL_WAIT = 12.0  # seconds after clicking Recall for the zone to change
RECALL_RETRY_SECONDS = 600.0  # after a travel recall fails (cooldown, refused), walk for a while
MARKER_WALK_RANGE = 800.0  # this close to the marker with the named enemy missing: walk onto it
MARKER_WALK_BACK = 350.0  # how far to back off before walking onto the marker
SIGIL_WAIT = 25.0  # the countdown after pressing X is ~10s
SIGIL_LEAVE_MOB_DISTANCE = 1000.0  # re-arm spots must be this clear of mobs
SIGIL_LEAVE = 3000.0  # the prompt re-arms only after leaving this far (~20m in game)
FAR_SWEEP_SPACING = 3000.0  # pickups load within roughly this range
FIND_AT_MARKER = 500.0  # this near a Find objective's marker: a prompt there is the way on
COMPANY_RANGE = 1200.0  # another enemy this near a boss joins its fight: the AoE deck
NOT_SAME_MOB = 5.0  # (the boss itself)
RECALL_KINDS = (*RETURN_KINDS, "room")  # marks a defeat or heal trip Recalls back to
RECALL_TRIES = 3  # failed Recalls to the mark before giving it up
RECALL_PENDING_FILE = Path("state") / "recall_pending.json"  # a defeat's Recall to the dungeon mark is due
SCOUT_MAX = 60  # squares visited from under the map when scouting a zone for an item
SCOUT_SETTLE = 1.0  # seconds for things to load after each hop
FAR_SWEEP_MAX = 25
ENTITY_SCAN_SECONDS = 30.0  # how often to note what's around (for the entity map)
KNOWN_SPOTS_FIRST = 6  # remembered spots tried before a zone sweep
MARKER_WAY_RANGE = 600.0  # at a fight's marker with the enemy absent: look for an X prompt there
PUZZLE_NEAR = 2000.0  # this close to the marker with its 'Use X' missing: a switch puzzle
PRESS_X_TRIES = 4  # X presses at a prompt before moving on
TRACK_TRIES = 3  # clicks on a quest's track button before giving up for this ranking
COLLECT_SEARCH_DEPTH = 2  # search zones up to this many gates from the objective's place
ENEMY_SWEEP_SPACING = 2500.0  # enemies load within roughly this range


@dataclass
class QuestEntry:
    slot: int
    name: str
    activity: bool = False  # spell/activity quest (new spells, training)
    mainline: bool = False
    active: bool = False
    reward: int = 0  # first reward amount shown in the quest book
    world: str = ""  # area shown in the book, e.g. "Triton Avenue"
    hops: int | None = None  # gate hops from where the wizard is (None = unknown)
    goal: str = ""  # current objective shown in the book, e.g. "Talk To Private Stillson"
    zone: str = ""  # the book's world name for the area, e.g. "Marleybone"
    target: str = ""  # the step's target as the book shows it ("Ms. Conrail"), no verb
    fight: bool = False  # the book shows the encounter icon: this step is a fight
    counted: bool = False  # the step has a counter ("0 of 3"): collect/defeat several


UNKNOWN_HOPS = 5  # an area we can't route to counts as fairly far


# Wizard City's street areas in the order the story opens them. Quests are
# cleared area by area, earliest first; hubs (Commons, Ravenwood, Shopping
# District, Olde Town...) aren't listed and count as the current area.
AREA_ORDER = (
    "unicorn way",
    "cyclops lane",
    "firecat alley",
    "triton avenue",
    "olde town",
    "haunted cave",
    "firefly forest",
    "colossus boulevard",
    "sunken city",
    "golem court",
    "storm tower",
    "lost city",
)


def hub_is_closer(walk: int | None, via: int | None) -> bool:
    """The hub button beats walking: the hub is fewer gates from the place
    than this zone is (the player's rule). From a zone with no known route
    (an interior), only when the place is the hub itself."""
    if via is None:
        return False
    if walk is None:
        return via == 0
    return via < walk


def area_rank(world: str) -> int | None:
    """Position of a quest's area in AREA_ORDER (None: a hub or unknown place)."""
    w = world.lower()
    return next((i for i, a in enumerate(AREA_ORDER) if a in w), None)


def _area_of(q: QuestEntry, order: dict) -> int | None:
    """A listed quest belongs to its list area (the book's "world" only shows
    where its current step is); others to the area the book shows."""
    listed = order.get(norm(q.name))
    return area_rank(listed.area if listed else q.world)


def quest_rank(q: QuestEntry, current_area: int = 0, order: dict | None = None) -> tuple:
    """Higher is better. Spell quests make the wizard stronger; then the main
    story (the book's flag); then the earliest
    area is cleared first; within an area, quests without a fight (talk, go to,
    collect) are quick experience; quests from docs/QuestList.txt go in list
    order; nearer beats farther; the tracked quest wins ties so the bot doesn't
    flip between equals."""
    order = order or {}
    hops = UNKNOWN_HOPS if q.hops is None else q.hops
    area = _area_of(q, order)
    area = current_area if area is None else area
    easy = bool(q.goal) and not is_combat_objective(q.goal)
    listed = order.get(norm(q.name))
    position = -listed.index if listed else -10_000
    # The book's main-story flag beats list order: a side quest on the Spiral
    # Tracker list ('No Entry') was followed ahead of 'Weird Science'.
    return (q.activity, q.mainline, -area, easy, position, -hops, q.active, q.reward)


def _side_world(world: str | None) -> bool:
    """A side world (Grizzleheim, Wintertusk...), by name or zone prefix."""
    from .questlist import SIDE_WORLDS, world_of_zone

    if not world:
        return False
    return world in SIDE_WORLDS or world_of_zone(world) in SIDE_WORLDS


def zone_world(name: str | None) -> str | None:
    """A world as zone names spell it: the book's "Dragonspyre" is
    "DragonSpire" (compared as-is, a wizard in Malistaire's Lair was 'out of
    the main world' and went by the dorm, which resets a dungeon)."""
    if not name:
        return name
    key = name.replace(" ", "").lower()
    for prefix, label in SPIRAL_WORLD_NAMES.items():
        if key in (prefix.lower(), label.replace(" ", "")):
            return prefix
    return name


def investigable(object_name: str, behaviors: list[str]) -> bool:
    """An object an 'Investigate X' objective may want used: selectable in
    the world (WizardSelectBehavior), not a person or the wizard."""
    return ("WizardSelectBehavior" in behaviors and "NPCBehavior" not in behaviors
            and "WizardCharacterBehavior" not in behaviors and object_name != "Player Object")


def quest_world(q: QuestEntry) -> str | None:
    """The world ("Krokotopia") a quest's area is in, from the book's area name."""
    zone = objective_zone(q.world) if q.world else None
    if zone:
        return zone.split("/", 1)[0]
    return zone_world(q.zone.replace(" ", "")) or None


def in_side_world(q: QuestEntry) -> bool:
    """A quest of an optional side world (Grizzleheim, Wintertusk, ...): the
    game marks their story as main-story, but the bot's main story is the
    arcs' worlds ('Face Your Fate' in Savarstaad Pass took over from
    Dragonspyre)."""
    world = quest_world(q)
    return any(same_world(world, w) for w in SIDE_WORLDS)


def same_world(a: str | None, b: str | None) -> bool:
    """World ids and the book's names differ in spaces/case ("WizardCity", "Wizard City")."""
    if not a or not b:
        return False
    a, b = zone_world(a), zone_world(b)
    return a.replace(" ", "").lower() == b.replace(" ", "").lower()


def choose_side_quest_in_world(
    quests: list[QuestEntry], world: str, side_quest_names: set[str], set_aside: set[str],
) -> QuestEntry | None:
    """Pick an available quest from the side-quest guide for one requested world."""
    candidates = [
        q for q in quests
        if norm(q.name) in side_quest_names
        and q.name not in set_aside
        and same_world(quest_world(q), world)
    ]
    return choose_quest(candidates)


# The player's order when the main story is stuck (2026-10-04): the story,
# then this world's side quests (in the book, then asked for), then side
# quests in these places (zone prefixes), and grinding only after all that.
# Side quests elsewhere when the story's world has none left (the player,
# 2026-10-06: Avalon's, then Zafaria's; then the Wysteria story).
FALLBACK_SIDE_PLACES = {"Celestia": ("Grizzleheim/GH_HFjord",),  # Wintertusk
                        "Avalon": ("Zafaria/ZF_Z00_Hub",)}


def quest_zone(q: QuestEntry) -> str:
    """The zone of a quest's area from the book ("Hrundle Fjord" ->
    "Grizzleheim/GH_HFjord/GH_HFjord"), "" if unknown."""
    return (objective_zone(q.world) if q.world else None) or ""


def choose_quest(
    quests: list[QuestEntry],
    set_aside: set[str] = frozenset(),
    order: dict | None = None,
    world: str | None = None,
    fallback: tuple[str, ...] = (),
    anywhere: bool = False,
) -> QuestEntry | None:
    """Which quest to track. Only the main story (and spell/class quests, which
    teach spells) while one of those can be worked on; side quests only when
    every main quest is set aside (e.g. a boss that keeps winning), to gain a
    level before trying again. Within that pool: spell quests first, then the
    earliest area, easy objectives first, listed quests in list order."""
    order = order or {}
    available = [q for q in quests if q.name not in set_aside]
    # The main story: flagged in the book, spell/class quests, or on the quest list.
    # (A side world's quest on the list only as the book or a detour says:
    # Wysteria's 'Exchange Student' and 'The Spiral Cup' were followed toward
    # Pigswick Academy, which isn't on the detour, all night.)
    main = [q for q in available
            if q.mainline or q.activity or (norm(q.name) in order and not in_side_world(q))]
    if not main and available:
        # Filling in with side quests while the main story waits for a level:
        # stay in this world (no trips back to Wizard City), finish the tracked
        # one before picking another, and prefer the biggest reward (experience).
        here = [q for q in available if quest_world(q) == world] if world else available
        for place in fallback:
            if here:
                break
            # (Nothing left in this world: the player's next places, e.g.
            # Wintertusk's side quests after Celestia's.)
            here = [q for q in available if quest_zone(q).startswith(place + "/")]
        if not here and anywhere:
            # This world's people all asked (`anywhere`): the side quests left in
            # the book in other worlds beat grinding (the player: experience).
            here = [q for q in available if quest_zone(q) and "/" in quest_zone(q)]
        if not here:
            return None  # nothing worth doing in this world: the caller grinds there
        active = next((q for q in here if q.active), None)
        if active:
            return active
        return max(
            here,
            key=lambda q: (
                q.reward,
                bool(q.goal) and not is_combat_objective(q.goal),  # quick, no fight
                -(UNKNOWN_HOPS if q.hops is None else q.hops),
            ),
        )
    quests = main or available or quests
    if not quests:
        return None
    areas = [a for a in (_area_of(q, order) for q in quests) if a is not None]
    current = min(areas) if areas else 0
    return max(quests, key=lambda q: quest_rank(q, current, order))


# Objectives with no fight: talking to someone (a turn-in), going somewhere,
# using or finding something. "Collect" is left out (items often drop from
# enemies, and one can take many fights).
ERRAND_VERBS = (
    "talk", "speak", "go", "use", "find", "explore", "visit", "read", "locate", "return", "deliver",
)
ERRAND_MAX_HOPS = 1  # this zone or the next: a detour, not a trip
# An errand with no known route ('Mail Call' in a Post Office) could be far:
# never worth leaving the main quest for.
ERRAND_UNKNOWN_HOPS = 99


def is_errand(goal: str) -> bool:
    """A quick step with no fight ('Talk To Sergeant Major Talbot')."""
    words = (goal or "").strip().lower().split()
    return bool(words) and words[0] in ERRAND_VERBS and not is_combat_objective(goal)


def quest_is_errand(q: QuestEntry) -> bool:
    """The quest's current step needs no fight: a worded goal ("Talk To X")
    judged by its verb, "Complete" (just hand it in), or the book's bare
    target ("Ms. Conrail") with no encounter icon and no counter."""
    goal = (q.goal or "").strip()
    if goal.lower() == "complete":
        return True
    if goal:
        return is_errand(goal)
    return bool(q.target) and not q.fight and not q.counted


# The player (2026-10-04): the main quest only, side quests only when it's
# stuck. Quick side-quest errands on the way (a turn-in, a talk) are off: one
# led on to 'Mane of Terror' and a defeat by its boss.
ERRAND_DETOURS = False
# The player (2026-10-05): a stuck main quest means side quests for experience,
# not farming (it switched the Waterworks farm on, which needs a team).
FARM_WHEN_STUCK = False


def errand_detour(
    quests: list[QuestEntry], chosen: QuestEntry | None, set_aside: set[str] = frozenset(),
    world: str | None = None, max_hops: int = ERRAND_MAX_HOPS,
) -> QuestEntry | None:
    """A side quest whose current step is a quick no-fight errand close by (a
    turn-in, a talk): worth doing before going on with the main quest. Only
    in `world`, the main quest's (furthest) world: no trips back to earlier
    worlds. The nearest first, then the biggest reward. None when there's none."""
    if chosen is None or not world:
        return None
    options = [
        q for q in quests
        if q is not chosen and not q.mainline and not q.activity and q.name not in set_aside
        and quest_is_errand(q) and _errand_hops(q) <= max_hops
        and same_world(quest_world(q), world)
    ]
    if not options:
        return None
    return min(options, key=lambda q: (_errand_hops(q), -q.reward, not q.active))


def _errand_hops(q: QuestEntry) -> int:
    return ERRAND_UNKNOWN_HOPS if q.hops is None else q.hops


TEAM_STATE = Path("state") / "team.json"
TEAM_STATE_FRESH = 600.0  # a teammate seen in a team dungeon this recently survives a restart


def _save_team_state(zone: str, _now: float) -> None:
    try:
        TEAM_STATE.write_text(json.dumps({"dungeon": zone.split("/")[0], "at": time.time()}),
                              encoding="utf-8")
    except OSError:
        pass


def _team_state_recent(zone: str) -> bool:
    try:
        data = json.loads(TEAM_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("dungeon") == zone.split("/")[0] and time.time() - data.get("at", 0) < TEAM_STATE_FRESH


def in_same_area(zone: str, first_room: str) -> bool:
    """A zone under the same area as a dungeon's first room (one of its rooms)."""
    area = first_room.rsplit("/", 1)[0]
    return zone.startswith(area + "/") and area.count("/") >= 1


def room_of(zone: str, where: str) -> bool:
    """Is `zone` a building of the area `where` ("Zafaria/Interiors/
    ZF_Z10_I03_Drum_House" of "Zafaria/ZF_Z10_Elephant_Graveyard": the same
    area code, ZF_Z10)?"""
    if "/interiors/" not in zone.lower():
        return False
    room, area = zone.rsplit("/", 1)[-1].split("_"), where.rsplit("/", 1)[-1].split("_")
    return (len(room) > 2 and len(area) > 2 and room[:2] == area[:2]
            and zone.split("/", 1)[0] == where.split("/", 1)[0])


def dungeon_quest(
    quests: list[QuestEntry], zone: str, zone_of, set_aside: set[str] = frozenset(),
    skipped: set[str] = frozenset(), entered_with: str | None = None,
    story_instances: set[str] = frozenset(),
) -> QuestEntry | None:
    """Inside a dungeon, a side quest set there (its book area is this dungeon,
    e.g. one handed out on entering) comes before the main quest: the main
    objective usually waits on it (a gate, a puzzle, an NPC to free)."""
    # A book area we can't map ("Counterweight East") still counts when the
    # main quest's area is the same one: that's the dungeon we're in.
    main_areas = {q.world for q in quests if q.mainline and q.world and zone_of(q.world) is None}
    story_areas = {q.world for q in quests if q.mainline and q.world}

    def named_here(area: str) -> bool:  # "Mount Olympus" in "Aquila/AQ_Z01_MountOlympus"
        key = area.replace(" ", "").replace("'", "").lower()
        if is_team_up_zone(zone) and area.strip().lower() in TEAM_UP_NAMES:
            return True  # any room of it (Aquila/Interiors/AQ_Z01_Apollo_Room)
        return len(key) > 4 and key in zone.replace("_", "").lower()

    # Set aside or not: we're in its dungeon now, which is what it waited on
    # (Into the Clouds, set aside while no team came, then the team took us in).
    # Skipped for good (No Entry: the Ironworks dungeon never ends) never is.
    never = set(skipped) | ALWAYS_SKIP
    local = [
        q for q in quests
        if not q.mainline and q.world and q.name not in never and not in_side_world(q)
        and (zone_of(q.world) == zone or named_here(q.world)
             or (zone_of(q.world) is None and q.world in main_areas)
             # The one the game tracked on entering ('Back to the Beginning' in
             # the Hall of Time, its area 'Grand Chasm Past'): the bot walked
             # to the portal home for the main quest and lost the instance.
             # Only that one: a quest the bot itself tracked later ('The
             # Secret History', after setting 'Fire Shield' aside in
             # Pyromancer's Tomb) isn't this dungeon's.
             or (q.active and (entered_with is None or q.name == entered_with))
             # A story quest the world's list marks INSTANCE (Queen Elissa's
             # Tomb: 'You Think You Can Drum' tracked inside, while the main
             # 'Tomb Sweet Tomb' waits on it to 'Save Prince Tziri'): the
             # tracked one, or one in the main quest's area.
             or (norm(q.name) in story_instances and (q.active or q.world in story_areas)))
    ]
    local.sort(key=lambda q: q.name in set_aside)  # ones not set aside first
    if not local:
        return None

    def late(q: QuestEntry) -> bool:
        # Waiting on a boss who comes only after the dungeon's own quest
        # (You Go First's Sylster Glowstorm: after Turn of the Wheel's levers).
        return norm(defeat_target(q.goal or "") or q.target or "") in LATE_BOSSES

    if len(local) > 1 and any(not late(q) for q in local):
        local = [q for q in local if not late(q)]
    return next((q for q in local if q.active), local[0])


def instance_names(listed: list) -> set[str]:
    """Normalized names of the story quests a world's list tags INSTANCE."""
    return {norm(q.name) for q in listed if "INSTANCE" in (t.split()[0].upper() for t in q.tags if t)}


def story_instance_quests(zone: str) -> set[str]:
    """The INSTANCE quests of the story list of the world we're in."""
    from .questlist import load_world_lists, world_of_zone

    try:
        lists = load_world_lists()
    except Exception:
        return set()
    world = world_of_zone(zone) or zone.split("/", 1)[0]
    return instance_names(lists.get(world, []))


EVADE_DISTANCE = 600.0  # an enemy this close to where we landed: move before it engages
MOB_CLEARANCE = 700.0  # landing closer than this to an enemy tends to start a fight
LANDING_RADII = (350.0, 600.0, 900.0, 1300.0, 1800.0)


def clear_of(p: XYZ, mobs: list[XYZ], clearance: float) -> bool:
    return all(math.dist((p.x, p.y), (m.x, m.y)) > clearance for m in mobs)


def safe_landing(target: XYZ, start: XYZ, mobs: list[XYZ], clearance: float) -> XYZ | None:
    """The nearest spot around `target` with no enemy within `clearance`,
    preferring the side we come from (the walk in then passes fewer enemies)."""
    dx, dy = start.x - target.x, start.y - target.y
    home = math.atan2(dy, dx) if (dx or dy) else 0.0
    for radius in LANDING_RADII:
        options = []
        for i in range(16):
            ang = home + i * math.pi / 8
            p = XYZ(target.x + radius * math.cos(ang), target.y + radius * math.sin(ang), target.z)
            if clear_of(p, mobs, clearance):
                options.append((abs(math.remainder(ang - home, 2 * math.pi)), p))
        if options:
            return min(options, key=lambda o: o[0])[1]
    return None


def is_known_enemy(name: str, stats_file: Path = Path("state") / "enemy_stats.json") -> bool:
    """Fought before (state/enemy_stats.json): its boss flag is then known."""
    want = norm(name)
    try:
        enemies = json.loads(stats_file.read_text(encoding="utf-8")).get("enemies", {})
    except (OSError, ValueError):
        return False
    return any(norm(n) == want for n in enemies)


def is_known_boss(name: str, stats_file: Path = Path("state") / "enemy_stats.json") -> bool:
    """A boss by the fights logged (state/enemy_stats.json), or one with
    brothers to beat first (PRE_BOSSES) or a brother himself."""
    want = norm(name)
    pre = {norm(k) for k in PRE_BOSSES} | {norm(b) for plan in PRE_BOSSES.values() for b, _z, _s in plan}
    if want in pre:
        return True
    try:
        enemies = json.loads(stats_file.read_text(encoding="utf-8")).get("enemies", {})
    except (OSError, ValueError):
        return False
    return any(norm(n) == want and v.get("boss") for n, v in enemies.items())


def defeat_target(objective: str) -> str | None:
    """The enemy a "Defeat X in Place (0 of 2)" objective names, else None."""
    m = re.match(r"^\s*defeat\s+(.+?)(?:\s+in\s+[^()]+)?(?:\s*\(\d+ of \d+\))?\s*$", objective, re.I)
    if not m:
        return None
    target = re.split(r"\s+and\s+", m.group(1), maxsplit=1)[0].strip()
    target = re.sub(r"^(any|a|an|the)\s+", "", target, flags=re.I)  # "Defeat Any Nirini"
    return target or None


def talk_target(objective: str) -> str | None:
    """The one a "Talk To Willie Marks in Willie's Clocktower" objective names."""
    m = re.match(r"^\s*talk\s+to\s+(.+?)(?:\s+in\s+.+)?\s*$", objective, re.I)
    return m.group(1).strip() if m else None


def locate_target(objective: str) -> str | None:
    """The one a "Locate Junho Shan in Hametsu Village" objective names."""
    m = re.match(r"^\s*locate\s+(.+?)(?:\s+in\s+.+)?\s*$", objective, re.I)
    return m.group(1).strip() if m else None


# ("Learn Mantra 2 in Ancient Burial Grounds": a tablet to read, walked at as a door for 6 minutes.)
_OPERATE = re.compile(
    r"^\s*(?:use|pull|push|press|activate|turn|flip|learn|read|study|examine|inspect|touch)\s+(.+?)"
    r"(?:\s+in\s+.+)?\s*$", re.I)


def operate_target(objective: str) -> str | None:
    """The object a "Use/Pull/Press X in Place" objective names."""
    m = _OPERATE.match(objective or "")
    return m.group(1).strip() if m else None


def is_hub(zone: str) -> bool:
    """A world's hub (the Oasis, the Commons, Dragonspyre's Basilica)."""
    from .travel_data import is_world_hub

    return is_world_hub(zone)


def defeat_names(objective: str) -> list[str]:
    """Names an enemy may have to count for a "Defeat X" objective, most
    specific first. "Defeat Any Sphinx Sokkwi" takes any Sokkwi (a Sokkwi
    Crusher counts): the last word, the creature kind, matches too."""
    target = defeat_target(objective)
    if not target:
        return []
    names = [target]
    words = target.split()
    if re.match(r"^\s*defeat\s+any\s", objective, re.I) and len(words) > 1:
        names.append(words[-1])
    # Enemies that counted for it before ('Defeat Spiders': the Ancient
    # Crystalweaver; nothing is named Spider, and the bot went from one old
    # sighting to another for 3 minutes).
    names += [n for n in defeat_aliases().get(target.lower(), []) if n not in names]
    return names


DEFEAT_ALIASES_FILE = Path("state") / "defeat_aliases.json"  # objective target -> enemies that counted


def defeat_aliases(path: Path = DEFEAT_ALIASES_FILE) -> dict[str, list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note_defeat_alias(target: str, enemies: list[str], path: Path = DEFEAT_ALIASES_FILE) -> list[str]:
    """A fight moved 'Defeat <target> (n of m)' on: its enemies whose names
    don't already say <target> are what <target> means. Returns the new ones."""
    data = defeat_aliases(path)
    key = target.lower().rstrip("s")
    if any(key in e.lower() for e in enemies if e):
        return []  # the target itself was in the fight: it counted, not the others (a Burning Flamewing)
    known = data.setdefault(target.lower(), [])
    new = [e for e in dict.fromkeys(enemies) if e and key not in e.lower() and e not in known]
    if new:
        known += new
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return new


def is_combat_objective(objective: str) -> bool:
    """Objectives met by fighting: 'Defeat X', 'Summon Myth Minion', 'Cast ...'."""
    first = objective.strip().lower().split(" ", 1)[0]
    return first in ("defeat", "summon", "cast")


def _norm_name(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalpha())


def same_object_name(name: str, want: str) -> bool:
    """`name` is the object `want` names, one or many: 'Use Grain Sacks (0 of
    3)' is three entities each called 'Grain Sack' (the bot swept Vestrilund
    for 'Grain Sacks' for 10 min with three Grain Sacks on its map)."""
    a, b = _norm_name(name), _norm_name(want)
    return a == b or a + "s" == b or b + "s" == a


def _name_words(name: str) -> list[str]:
    """'Contrivance Station' -> ['contrivance', 'station']; one or many alike."""
    words = [w for w in re.split(r"[^a-z]+", name.lower()) if len(w) >= 3]
    return [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def _words_alike(a: str, b: str) -> bool:
    """Same word, or the same stem ('contrivance'/'contrivances', 'tile'/'tiles')."""
    if a == b:
        return True
    n = min(len(a), len(b))
    return n >= 5 and a[:max(5, n - 2)] == b[:max(5, n - 2)]


def closest_name(want: str, names, used=()) -> str | None:
    """The name in `names` (things in the zone: seen on the map or in view)
    that the quest's `want` most likely means, or None. The player: when
    collect/use struggles, look up the name to go to at once instead of
    sweeping again; the full name first, then any word, reasoned out:

    1. the same name, one or many ('Grain Sacks' -> 'Grain Sack');
    2. a name containing the whole of it;
    3. shared words, each worth more the fewer names in the zone have it
       ('Contrivance' says more than 'Station', which all three stations
       share), similar spelling breaking ties; names in `used` (objects
       already used for earlier steps here) are ruled out first.
    """
    names = [n for n in dict.fromkeys(names) if n]

    def contains(n: str, w: str) -> bool:
        # (Containing the whole name, one or many: matches_item's first-word
        # rule took "Dulin's Hammer" for the NPC Dulin Helmsplitter.)
        a, b = _norm_name(n), _norm_name(w)
        return bool(b) and (b in a or b.rstrip("s") in a)

    hits = [n for n in names if same_object_name(n, want)] or [n for n in names if contains(n, want)]
    if hits:
        return min(hits, key=len)
    used_n = {_norm_name(u) for u in used}
    pool = [n for n in names if _norm_name(n) not in used_n]
    # "Dulin's Hammer" is a hammer: the owner's name isn't the thing (it
    # matched the NPC Dulin Helmsplitter).
    owners = {w.lower() for w in re.findall(r"([A-Za-z]+)['’]s(?![a-z])", want)}
    wanted = [w for w in _name_words(want) if w not in owners]
    if not pool or not wanted:
        return None
    words_of = {n: _name_words(n) for n in pool}

    def rarity(w: str) -> float:
        # How telling a word is here: shared by every name, it says little.
        df = sum(any(_words_alike(w, x) for x in ws) for ws in words_of.values())
        return math.log(1 + len(pool) / max(1, df))

    from difflib import SequenceMatcher

    best: tuple[float, float, str] | None = None
    for n, ws in words_of.items():
        score = sum(rarity(w) for w in wanted if any(_words_alike(w, x) for x in ws))
        if score <= 0:
            continue
        spelling = SequenceMatcher(None, _norm_name(want), _norm_name(n)).ratio()
        if best is None or (score, spelling) > best[:2]:
            best = (score, spelling, n)
    return best[2] if best else None


def in_known_dungeon_folder(zone: str, known) -> bool:
    """A room of a learned dungeon whose rooms share its first room's folder
    ("WizardCity/Gauntlets/WC_Triton_Gauntlet1/..."): the Waterworks' second
    room wasn't recognised after a restart in it, and a fight there was fled
    (out of the dungeon, its progress lost)."""
    folder = zone.rsplit("/", 1)[0]
    return folder.count("/") >= 2 and any(k.rsplit("/", 1)[0] == folder for k in known)


def fight_needed(objective: str, enemy_names: list[str], zone: str, has_boss: bool) -> bool:
    """Is this fight part of the quest? Conservative: bosses, fights inside
    buildings/dungeons and unclear cases count as needed."""
    if has_boss or "interiors" in zone.lower() or "/gauntlets/" in zone.lower() or not objective.strip():
        return True
    if not is_combat_objective(objective):
        return False
    wanted = defeat_names(objective)
    if not wanted:
        return True  # "Summon/Cast ...": any fight does
    for name in wanted:
        target = _norm_name(name).removesuffix("s")
        if len(target) < 3:
            return True
        if any(target in _norm_name(n) or _norm_name(n) in target for n in enemy_names):
            return True
    return False


def distance(a: XYZ, b: XYZ) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


QUEST_BOOK_FILE = Path("state") / "quest_book.json"
PIN_FILE = Path("state") / "quest_pin.json"


def load_pin() -> str:
    try:
        return json.loads(PIN_FILE.read_text(encoding="utf-8")).get("quest", "")
    except (OSError, ValueError, AttributeError):
        return ""


def save_pin(name: str):
    try:
        PIN_FILE.parent.mkdir(exist_ok=True)
        PIN_FILE.write_text(json.dumps({"quest": name}), encoding="utf-8")
    except OSError:
        pass


LAST_MAIN_FILE = Path("state") / "last_main.json"


def _load_last_main(field: int = 0):
    """(last main quest, zone of its last step); `field` 2: the zone a fight
    was last won in."""
    try:
        d = json.loads(LAST_MAIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    if field == 2:
        return str(d.get("win_zone", ""))
    return str(d.get("quest", "")), str(d.get("zone", ""))


def _save_last_main(quest: str | None = None, zone: str | None = None, win_zone: str | None = None):
    try:
        d = json.loads(LAST_MAIN_FILE.read_text(encoding="utf-8")) if LAST_MAIN_FILE.exists() else {}
    except (OSError, ValueError):
        d = {}
    for k, v in (("quest", quest), ("zone", zone), ("win_zone", win_zone)):
        if v is not None:
            d[k] = v
    try:
        LAST_MAIN_FILE.parent.mkdir(exist_ok=True)
        LAST_MAIN_FILE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _last_book_world() -> str | None:
    try:
        return json.loads(QUEST_BOOK_FILE.read_text(encoding="utf-8")).get("world") or None
    except (OSError, ValueError, AttributeError):
        return None


def _write_quest_book(quests: list[QuestEntry], chosen: QuestEntry | None, world: str | None):
    """The quest book as last read, for the dashboard (state/quest_book.json)."""
    try:
        data = {
            "time": time.time(),
            "world": world or "",
            "tracking": chosen.name if chosen else "",
            "tracking_area": chosen.world if chosen else "",
            "quests": [
                {"name": q.name, "area": q.world, "world": q.zone, "main": q.mainline, "spell": q.activity,
                 "goal": q.goal}
                for q in quests
            ],
        }
        QUEST_BOOK_FILE.parent.mkdir(exist_ok=True)
        QUEST_BOOK_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


class Quester:
    def __init__(self, client, cfg: QuestConfig, controller, progression=None, upkeep=None, dialogue=None):
        self.client = client
        self.dialogue = dialogue  # DialoguePolicy shared with the dialogue loop
        self.upkeep = upkeep
        self._last_wisp_scan = 0.0
        self.progression = progression
        self.services = ServicesMenu(client)
        self.lock = asyncio.Lock()  # one step (or watchdog action) at a time
        self._step_task: asyncio.Task | None = None
        self.collector = Collector(client)
        # Teleports land on known ground (safe_teleport reads this).
        client._ground_points = self._ground_for_teleport
        self.givers = QuestGivers(self)  # talks to named NPCs nearby once, for their quests
        self.cfg = cfg
        self.controller = controller
        self.sprinter = client  # SprintyClient (bot.new_handler)
        self._last_progress = (None, None)
        self._momentum: tuple[str, float] | None = None  # the quest that last moved on, and when
        self._last_progress_time = time.monotonic()
        self.objectives_completed = 0
        self.gear = None  # GearManager, set by the bot
        self.trainer = None  # SpellTrainer, set by the bot
        self._activity_quests: set[str] = set()  # spell quests seen in the book
        self._sigil_failed_at: XYZ | None = None  # sigil whose last try didn't start
        self._last_stuck_check = 0.0
        self.setbacks = Setbacks.load()
        self.quest_order = load_quest_list()  # docs/QuestList.txt
        self.completions = CompletionTracker()  # -> docs/CompletedQuests.txt
        self._active_quest: str | None = None  # tracked quest's name, from the quest book
        self._door_rooms: dict[str, str] = {}  # collect objective -> building entered by its marker's door
        self._seen_deaths = 0
        # A defeat happened since we marked a dungeon entrance / fight spot: kept
        # in state/recall_pending.json (a restart mid-Recall forgot it, and the
        # bot walked back to the Labyrinth's sigil: a fresh copy).
        self._recall_pending_flag = RECALL_PENDING_FILE.exists()
        self._last_defeat = -1e9
        self._last_win_zone = _load_last_main(2)  # where a fight was last won (to gain experience there)
        self._book_dumped = False  # quest book slot layout saved (state/quest_book_window.txt)
        self._boss_fights_seen = 0  # fighter.boss_fights already checked (a farm run's end)
        self._step_is_fight = False  # the tracked quest's step has the book's encounter icon
        self._book_names: set[str] = set()  # quests in the book at the last full read
        self._pin_new_from: set[str] | None = None  # after a visit: pin a quest not in this set
        self._floors_cleared: dict[tuple[str, str], set[int]] = {}  # dungeon floors checked for enemies
        self._boss_waited: set[tuple[str, str]] = set()  # (objective, zone) the spawn wait was done for
        # (zone, where, when, really seen) a teammate was last seen; False = our arrival spot
        self._mate_seen: tuple[str, XYZ, float, bool] | None = None
        self._team_alone_since: float | None = None  # in a team dungeon since (no teammate seen yet)
        self._room_pos = -1  # index in the dungeon's room order (teamup.ROOM_ORDER) this run
        self._team_with_us = False  # entered with a team, or saw a teammate in this dungeon
        self._mate_last_seen = 0.0  # when a teammate was last in sight
        self._prev_mates: tuple[float, list] = (0.0, [])  # (when, teammate positions) at the last look
        self._team_talks: dict[str, int] = {}  # team dungeon talk objective -> tries
        self._door_tries: dict[tuple[str, int, int], int] = {}  # team door walks per door
        self._standoff_tries: dict[tuple[str, int, int], int] = {}  # teleports toward a boss stop point
        self._prev_step: tuple[str, XYZ | None] | None = None  # (zone, quest marker) at the last step
        self._visit_tries = 0  # talk attempts for a visit_npc request
        self._team_ranked = -1e9  # last quest-book read inside a team dungeon
        self._background: dict[str, asyncio.Task] = {}  # scans running beside the step
        self._book_reader = "check"  # quest book: "check" (first page both ways), "fast" or "slow"
        self._no_main_alerted = -1e9  # last "no main quest in the book" alert
        # The last main-story quest seen in the book, and where its last step
        # was worked ("swept" once its NPCs were asked again); kept over
        # restarts (after one in Grizzleheim it asked the NPCs there instead
        # of those where 'Foe of Foes' ended).
        self._last_main, self._last_main_zone = _load_last_main()
        self._no_main_reads = 0  # full quest-book reads (alerted) with no main quest
        self._stuck_times: dict[str, int] = {}  # main quest -> times it was found stuck
        self._farm_alerted = 0.0  # last "can't get to the farmed dungeon" alert
        self._world_tree_zone = ""  # the World Tree's inside, once walked into from Ravenwood
        self._tree_tried: set[tuple[str, int, int]] = set()  # ways tried in there
        self._farm_run_done = False  # the farmed dungeon's final boss is beaten: leave
        self._entry_quest: tuple[str, str] = ("", "")  # (dungeon first room, quest tracked on entering)
        self._ranked_outside = False  # ranked quests outside a dungeon this session
        self._spell_first_logged = ""  # the class quest last put ahead of the pin
        self._chosen_entry: QuestEntry | None = None  # the tracked quest's book entry at the last ranking
        self._done_dungeon_logged = ""  # a finished dungeon whose own quest was skipped (logged once)
        self._route_written: list[str] | None = None  # the route last written for the stream page
        self._route_at = 0  # where on it we are
        self._loose_level: dict[str, int] = {}  # objective -> how loosely its item is searched for
        self._door_keys_found: set[str] = set()  # objectives whose door key (DOOR_KEYS) was picked up
        self._mate_trail: list[tuple[str, XYZ]] = []  # last teammate sightings (their direction of travel)
        self._track_tries: dict[tuple[str, int, int], int] = {}  # walks along their tracks, per spot
        self._mate_doors: set[tuple[str, int, int]] = set()  # doors already taken after the team
        self._npc_search: dict[str, dict] = {}  # objective -> door search state (a Talk To target not found)
        self._alerted: dict[str, float] = {}  # main quest -> last ALERT (monotonic)
        self._boss_deaths_seen = 0
        self._grinding = False  # every quest set aside: fight for experience until a level-up
        # The world the main quest is in (side quests stay there); after a
        # restart, the last ranking's (a restart in Grizzleheim, where an
        # auto-tracked side quest led, mustn't make that the world).
        self._main_world: str | None = zone_world(_last_book_world())
        self._fled: dict[tuple, int] = {}  # (objective, enemy names) -> times fled
        self._mainline: set[str] = set()  # main-story quests in the book (from the last ranking)
        self._wanted_items: dict[str, str] = {}  # item -> quest, from "Collect X" goals in the book
        self._last_wanted_scan = 0.0
        self._last_loot_scan = 0.0
        self.entity_map = EntityMap()  # what was seen where (targeted searches)
        self.doors = DoorMemory()  # where walking into a door worked
        self._last_entity_scan = 0.0
        self._attempts_at: dict[tuple[str, str, str], int] = {}  # (objective, zone, approach) -> tries
        self._swept_spots: dict[tuple[str, str], list] = {}  # (objective, zone) -> sweep spots visited
        self.bring_out = BringOut(self)  # a missing Talk To target: work the room until it shows
        self._puzzles_tried: set[tuple[str, str]] = set()  # (objective, zone) switch puzzles tried
        self._teleporters_tried: dict[tuple[str, str], set[str]] = {}  # (objective, zone) -> labels
        self._accepted_seen = 0  # DialoguePolicy.accepted at the last ranking
        self._pin: str | None = None  # the player's picked quest (None: not read yet this session)
        self._zones_searched: dict[str, set[str]] = {}  # collect objective -> zones swept for it
        self.fighter = None  # set by the bot: its fight count tells won fights apart
        self._fights_seen = 0
        self._deaths_at_fight = 0
        self._stall_switched_for = ""  # objective whose quest was set aside for stalling
        self._wins_since_progress = 0
        self._unreached: dict[tuple[str, str], int] = {}  # (objective, zone) -> failed approaches
        self._zone_before = ""  # for learning gates on arrival
        self._deaths_before = 0
        self._teleported = False  # a recall moved us: not a gate
        self._mark: Mark | None = load_mark()  # where the game's Mark is (dungeon sigil or travel spot)
        self._recall_blocked_until = 0.0  # after a refused/failed travel recall
        self.healer = None  # DungeonHealer, set by the bot
        self._dungeon: tuple[str, str] | None = None  # (outside zone, first room) of the dungeon we're in
        self._recalled_for = ""  # objective a travel recall was used for (once each)
        self._bad_gates: set[tuple[str, str]] = set()

    async def objective(self) -> str:
        texts = await ui.quest_goal_texts(self.client)
        if not texts:
            return await ui.quest_goal(self.client)
        try:
            goal = await self.client.goal_id()
        except Exception:
            goal = None
        if not hasattr(self, "_goal_texts"):
            self._goal_texts: dict = {}
        return ui.pick_goal(texts, goal, self._goal_texts)

    def _stop_if_asked(self, before: str, now: str):
        """state/stop_at.json {"objective": "...", "after": "..."}: stop the bot
        (once) when the objective turns into one containing `objective` from
        one containing `after` (right after the last Counterweight Lever:
        "Pull ..." -> "Defeat Sprockets ...", for the user to record from
        there)."""
        path = Path("state") / "stop_at.json"
        try:
            want = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        target, after = want.get("objective", "").lower(), want.get("after", "").lower()
        if target and target in now.lower() and (not after or after in before.lower()):
            path.unlink(missing_ok=True)
            self.controller.stop(f"reached {now!r} (state/stop_at.json)")

    def _in_background(self, kind: str, make) -> None:
        """Run `make()` (a coroutine factory) as a background task, one per kind
        at a time; errors are only logged."""
        task = self._background.get(kind)
        if task is not None and not task.done():
            return

        async def run():
            try:
                await make()
            except Exception as exc:
                logger.debug(f"background {kind} scan failed: {exc!r}")

        self._background[kind] = asyncio.create_task(run())

    def _learn_defeat_alias(self, old: str, new: str):
        """'Defeat X (0 of 2)' -> '(1 of 2)' right after a fight: its enemies
        count as X (state/defeat_aliases.json)."""
        target = defeat_target(old)
        fighter = self.fighter
        if not target or target != defeat_target(new) or not fighter or not fighter.last_enemy_names:
            return
        if time.monotonic() - fighter.combat_ended_at > ALIAS_AFTER_FIGHT:
            return
        bosses = set(getattr(fighter, "last_boss_names", []) or [])
        new_names = note_defeat_alias(target, [n for n in fighter.last_enemy_names if n not in bosses])
        if new_names:
            logger.info(f"{', '.join(new_names)} counted for {target!r}: hunting those for it from now on")

    async def _note_progress(self, objective: str, zone: str | None):
        key = (objective, zone)
        if objective and objective != self._last_progress[0] and self._active_quest:
            from .quest_steps import record

            record(self._active_quest, objective)  # the stream's quest card: steps done so far
        if key != self._last_progress:
            if self._last_progress[0] and objective != self._last_progress[0]:
                self.objectives_completed += 1
                logger.success(f"objective done -> now: {objective!r}")
                self._learn_defeat_alias(self._last_progress[0], objective)
                self._stop_if_asked(self._last_progress[0], objective)
                # The book's fight icon was for the old step (Katzenstein); the
                # next ranking reads the new one. A stale flag sent the bot out
                # of the lab to heal before talking to Grunk.
                self._step_is_fight = False
            if self._last_progress[0] and objective and self._active_quest:
                self._momentum = (self._active_quest, time.monotonic())  # mid-way through it
            self._last_progress = key
            self._last_progress_time = time.monotonic()
            self._attempts = 0
            self._wins_since_progress = 0
            return
        # A won fight counts as progress (drop hunts take many fights per item).
        fights = self.fighter.fights if self.fighter else 0
        if fights != self._fights_seen:
            won = self.controller.deaths == self._deaths_at_fight
            self._fights_seen, self._deaths_at_fight = fights, self.controller.deaths
            here = await self.client.zone_name() or ""
            outdoors = here and "interiors" not in here.lower() and not await self._in_any_dungeon(here)
            if won:
                names = self.fighter.last_enemy_names if self.fighter else []
                self.__dict__.setdefault("_won_names", set()).update(norm(n) for n in names)
                pre = {b for plan in PRE_BOSSES.values() for b, _z, _s in plan}
                beat = [b for b in pre if any(norm(b) == norm(n) for n in names)]
                if beat:
                    done = load_pre_bosses_beaten() | set(beat)
                    PRE_BOSSES_FILE.write_text(json.dumps(sorted(done)), encoding="utf-8")
                    logger.success(f"beat {', '.join(beat)}: one fewer at the boss's side later")
                if outdoors:
                    # (Outdoor wins only: grinding goes back there, and a
                    # dungeon's fights can pull its boss: the Runed Devestator.)
                    if here != self._last_win_zone:
                        _save_last_main(win_zone=here)
                    self._last_win_zone = here
                    self.__dict__.setdefault("_win_zones", {})[here.split("/", 1)[0]] = here
                self._wins_since_progress += 1
                # Up to a few won fights count as progress (a drop hunt needs
                # several); more without the objective moving means these
                # enemies aren't the ones that drop it.
                if self._wins_since_progress <= WINS_COUNT_AS_PROGRESS or self._grinding:
                    self._last_progress_time = time.monotonic()
                    return
        waited = time.monotonic() - self._last_progress_time
        if waited > STALL_SWITCH_SECONDS and self._stall_switched_for != objective:
            # Stuck without a way forward: follow the next best quest instead of
            # stalling; this one comes back after a level-up or an hour.
            self._stall_switched_for = objective
            quest = self._active_quest
            if quest and self._detour_stays(quest):
                logger.info(f"no progress on {objective!r} for {waited / 60:.0f} min; staying on {quest!r} "
                            "(the detour's 'stay')")
                self._last_progress_time = time.monotonic()
                quest = None
            here = await self.client.zone_name() or ""
            if quest and await self._in_dungeon(here) and await self._reenter_for_npc(objective, here):
                # Stuck inside a dungeon: a fresh copy first ('Explore King's
                # Tomb' led nowhere once the copy had broken).
                self._stall_switched_for = None
                self._last_progress_time = time.monotonic()
                return
            if quest:
                level = await self.client.stats.reference_level()
                self.setbacks.set_quest_aside(quest, objective, level, main=quest in self._mainline,
                                              stuck=True)
                self.setbacks.save()
                logger.warning(
                    f"no progress on {objective!r} for {waited / 60:.0f} min: setting {quest!r} aside "
                    "and following the next best quest"
                )
                if quest in self._mainline:
                    self._alert_main_stuck(quest, f"no progress on {objective!r} for {waited / 60:.0f} min")
                self._ranked_for = None
                self._last_rank = -1e9  # re-rank on this step
            return
        if waited > self.cfg.stuck_minutes * 60:
            # Never stop for it (the player: hours lost stopped): set aside
            # again and on with whatever else there is.
            self._stall_switched_for = None
            self._last_progress_time = time.monotonic() - STALL_SWITCH_SECONDS

    # --- movement ------------------------------------------------------------

    async def _position(self) -> XYZ:
        return await self.client.body.position()

    async def _zone_changed(self, zone: str | None) -> bool:
        await wait_for_loading(self.client, appear_timeout=0.8)
        if await self.client.zone_name() != zone:
            # Doors drop us wherever the game likes, and the new zone's enemies
            # take a moment to load: look a few times before moving on.
            for _ in range(3):
                if await self._clear_of_enemies() or await self.client.in_battle():
                    break
                await asyncio.sleep(0.8)
            return True
        return False

    async def _clear_of_enemies(self) -> bool:
        """Check where we ended up (after a teleport or a door): if an enemy is
        right there and no fight has started yet, hop to the nearest clear spot
        before it engages (Desert Golems patrol the Palace of Fire's entrance).
        True if it moved."""
        try:
            if await self.client.in_battle():
                return False
            me = await self._position()
            mobs = [XYZ(*m) for m in await mob_positions(self.client)]
            if not mobs or clear_of(me, mobs, EVADE_DISTANCE):
                return False
            spot = safe_landing(me, me, mobs, MOB_CLEARANCE)
            if spot is None:
                return False
            logger.info(f"enemies right where we landed; moving {distance(spot, me):.0f} away")
            await self.client.teleport(spot)
            await asyncio.sleep(0.5)
            return True
        except Exception as exc:
            logger.debug(f"clear-of-enemies check failed: {exc!r}")
            return False

    async def walk_through(self, target: XYZ, zone: str | None, overshoot: float = DOOR_OVERSHOOT) -> bool:
        """Walk straight at `target` and a little past it.

        Doors and zone exits only trigger when you walk into them; teleporting
        onto one gets rejected by the game and snaps you back.
        """
        # A "press X" spot (the Balance School's ladder): use it rather than
        # walking past it.
        if await self._press_x_here(zone):
            return True
        pos = await self._position()
        dx, dy = target.x - pos.x, target.y - pos.y
        length = math.hypot(dx, dy)
        if length < 1:
            return False
        beyond = XYZ(target.x + dx / length * overshoot, target.y + dy / length * overshoot, target.z)
        logger.debug(f"walking through objective ({length:.0f} units + {overshoot:.0f} overshoot)")
        # Walk in short steps and stop the moment the zone changes: one long
        # key press kept walking on the far side of the door, straight into the
        # Desert Golems at the Palace of Fire's entrance.
        from wizwalker.utils import calculate_perfect_yaw

        await self.client.body.write_yaw(calculate_perfect_yaw(pos, beyond))
        last = pos
        for _ in range(WALK_MAX_STEPS):
            await self.client.send_key(Keycode.W, WALK_STEP_SECONDS)
            if await self.client.zone_name() != zone or await self.client.is_loading():
                break
            if await ui.is_visible(self.client, ui.NPC_RANGE):
                prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
                if "talk" not in prompt:
                    return await self._press_x_here(zone, adjust=False)  # walked into a prompt: stop, use it
                # Someone standing by the door (Marla Stinger at the Death
                # school): not what we're walking to; keep going.
            now = await self._position()
            if distance(now, beyond) < 60 or distance(now, last) < 5:
                break  # there, or blocked by a wall
            last = now
        if await self._zone_changed(zone):
            if zone:
                self.doors.record(zone, (target.x, target.y, target.z), (pos.x, pos.y, pos.z),
                                  await self.client.zone_name() or None)
            return True
        return False

    async def _light_and_enter(self, zone: str, marker: XYZ) -> bool:
        """A spirit portal near the marker: press X at each candle around it,
        then at the portal. True if it found the portal (and tried)."""
        await scan_entities(self.client, zone, self.entity_map)
        here = (marker.x, marker.y, marker.z)
        is_portal = lambda n: "spiritworldportal" in n.lower().replace(" ", "")  # noqa: E731
        portals = self.entity_map.spots(zone, is_portal, here)
        portals = [pt for pt in portals if math.dist(pt[:2], here[:2]) < PORTAL_NEAR_MARKER]
        if not portals:
            return False
        portal = XYZ(*portals[0])
        candles = self.entity_map.spots(zone, lambda n: n.lower() in ("candle", "ritual candle"), portals[0])
        candles = [c for c in candles if math.dist(c[:2], portals[0][:2]) < CANDLE_RANGE]
        logger.info(f"a spirit portal by the marker: lighting {len(candles)} candle(s), then into the portal")
        for c in candles:
            if not await is_free(self.client):
                return True
            await self._use_object_at(XYZ(*c))
        if await is_free(self.client):
            logger.info("pressing X at the spirit portal")
            await self._use_object_at(portal)
        return True

    async def _land_beside(self, target: XYZ, mobs: list[XYZ]) -> bool:
        """Teleport to spots around `target` (150, then 300 away, 8
        directions; clear of enemies) until one takes. True once there."""
        for radius in LAND_BESIDE_RADII:
            for i in range(8):
                a = i * math.pi / 4
                spot = XYZ(target.x + radius * math.cos(a), target.y + radius * math.sin(a), target.z)
                if mobs and not clear_of(spot, mobs, MOB_CLEARANCE):
                    continue
                await self.client.teleport(spot)
                await asyncio.sleep(0.5)
                if distance(await self._position(), spot) < 100:
                    logger.info(f"the marker itself refused the teleport; landed {radius:.0f} beside it")
                    return True
        return False

    async def _object_at_marker(self, objective: str, target: XYZ) -> bool:
        """Is the object a 'Use X' names at the refused marker? Not there: the
        marker is a door on the way (pressing X at Frostmantle's Chamber's door
        did nothing, the room's enemies were fought as 'guards' and a landing
        left the fortress). Collect objectives can't be checked by name (the
        Ice Water is a 'GH_Jar'): yes."""
        name = operate_target(objective)
        if not name or not _USE_OBJECT.match(objective):
            return True
        pos = await self._npc_named(name, near=target)
        return pos is not None and distance(pos, target) < MARKER_REACHED

    async def _use_object_at(self, spot: XYZ) -> bool:
        """An object to use at `spot` (the Burial Ground Tablet: the teleport
        onto it lands inside it and is refused): land beside it, nudge until
        its X prompt shows, press it. True if it pressed. Sets
        self._object_reached when it stood beside the object at all."""
        self._object_reached = False
        for radius in (160.0, 260.0):
            for i in range(8):
                a = i * math.pi / 4
                near = XYZ(spot.x + radius * math.cos(a), spot.y + radius * math.sin(a), spot.z)
                if not await self._clear_spot(near):
                    continue
                await self.client.teleport(near)
                await asyncio.sleep(0.6)
                if distance(await self._position(), spot) < radius + OBJECT_REACH_SLACK:
                    self._object_reached = True
                for nudge in (None, (Keycode.W, 0.2), (Keycode.W, 0.2), (Keycode.S, 0.3)):
                    if nudge:
                        await self.client.send_key(*nudge)
                        await asyncio.sleep(0.25)
                    if await ui.is_visible(self.client, ui.NPC_RANGE):
                        await self.client.send_key(Keycode.X, 0.1)
                        await asyncio.sleep(1.5)
                        logger.info("pressed X at the object")
                        return True
        return False

    async def _press_x_here(self, zone: str | None, adjust: bool = True) -> bool:
        """Use a "press X" prompt at this spot (a ladder, a door that asks),
        pressing X a few times and waiting for the zone to change. With no
        prompt, `adjust` makes small moves first (a step back and forward, a
        small turn each way) looking for one. True once through."""
        nudges = (
            (Keycode.S, 0.15), (Keycode.W, 0.25), (Keycode.A, 0.12), (Keycode.D, 0.24), (Keycode.A, 0.12),
        ) if adjust else ()
        for nudge in (None, *nudges):
            if nudge is not None:
                await self.client.send_key(*nudge)
                await asyncio.sleep(0.25)
            if not await ui.is_visible(self.client, ui.NPC_RANGE):
                continue
            prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
            if "talk" in prompt:
                # Someone beside the door (Marla Stinger at the Death school):
                # a ladder or door never says "talk"; don't start her dialogue.
                continue
            entering = "to enter" in prompt
            if not entering and is_team_up_zone(zone or ""):
                continue  # a lever, not a door (the Waterworks' lever room: a wrong one is a fight)
            if entering:
                # A dungeon sigil (the Hyde Park safehouses): ONE press starts
                # a countdown that a second press or any step cancels.
                sigil = await self._sigil_at(await self._position(), SIGIL_NEAR_RANGE)
                if sigil is not None:
                    return await self._enter_by_sigil(sigil, zone)
            for _ in range(1 if entering else PRESS_X_TRIES):
                await self.client.send_key(Keycode.X, 0.1)
                for _ in range(SIGIL_WAIT_TICKS if entering else 6):
                    await asyncio.sleep(0.5)
                    if await self._zone_changed(zone) or await self.client.is_loading():
                        await wait_for_loading(self.client)
                        logger.info("used the 'press X' prompt here")
                        return True
                if not await ui.is_visible(self.client, ui.NPC_RANGE):
                    break
            if not await is_free(self.client):
                return True  # a dialogue or menu opened: the step takes it from here
        return await self._zone_changed(zone)

    async def _follow_path(self, path: list[XYZ], zone: str | None) -> bool | None:
        """Walk the waypoints. The collision grid doesn't know ledges: in
        Ravenscar the walk went off a cliff, the wizard fell under the map
        and walked on at z 0 for minutes (twice). A drop of FALL_DROP: back
        to the last spot on the ground, path dropped. True when walked, False
        after a fall, None when the zone changed or something else came up."""
        # One smooth walk (smoothwalk: W held, steered toward a point ahead),
        # not a stop and a sharp turn at every waypoint (the player: jerky).
        from .smoothwalk import walk_route

        async def busy() -> bool:
            return await self.client.in_battle() or not await is_free(self.client)

        how = await walk_route(self.client, path, zone, abort=busy)
        if how == "fell":
            logger.warning("fell off the walk path: back to the last spot on the ground")
            await asyncio.sleep(1.0)
            return False
        if how in ("zone", "aborted"):
            return None
        return True

    async def approach_and_walk(self, target: XYZ, zone: str | None) -> bool:
        """Teleport to a spot in front of `target` (on the side we came from), then walk in."""
        pos = await self._position()
        # A walked path around the walls (Edith Benchley in Celestia's Base
        # Camp: every teleport near her refused, and the bot gave up on her
        # four times). First when walking is the mode (movement.walk, or a
        # walk-only zone); else the teleports first and the walk if they fail
        # (the player: it walked the streets into a Haunted Minion's fight).
        from .walkmap import walk_path

        walking = getattr(self.client, "_walk", False) or any(
            (zone or "").startswith(p) for p in getattr(self.client, "_walk_only", ())) or walk_zone(
            self.client, zone or "")

        async def walk_there() -> bool:
            path = await walk_path(zone or "", await self._position(), target) if zone else None
            if not path or len(path) < 2:
                return False
            logger.info(f"walking a {len(path)}-waypoint path to it")
            if await self._follow_path(path, zone) is None:
                return True
            return await self._zone_changed(zone) or distance(await self._position(), target) < WALKED_CLOSE

        if walking and await walk_there():
            return True
        dx, dy = pos.x - target.x, pos.y - target.y
        length = math.hypot(dx, dy)
        base = math.atan2(dy, dx) if length > 1 else 0.0
        # Only spots on the map: near something known to stand on the ground
        # (it landed in the clouds off the Commons' edge beside the Nightside
        # door). The side we came from first, then the others.
        ground = await self._ground_points(zone or "", target)
        spots = []
        for turn in (0.0, math.pi / 4, -math.pi / 4, math.pi / 2, -math.pi / 2, math.pi):
            for back in APPROACH_DISTANCES:
                a = base + turn
                spot = XYZ(target.x + math.cos(a) * back, target.y + math.sin(a) * back, target.z)
                if not ground or any(math.dist((spot.x, spot.y), g[:2]) < ON_GROUND for g in ground):
                    spots.append(spot)
        for spot in spots[:APPROACH_TRIES]:
            if await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
                return True  # walked into a world gate: the map is the step's now (it sat a minute)
            if not await self._clear_spot(spot):
                continue  # an enemy stands there: landing on it starts a fight
            before = await self._position()
            await self.client.teleport(spot)
            await asyncio.sleep(TELEPORT_SETTLE)
            if await self._zone_changed(zone):
                return True
            if distance(await self._position(), before) <= BOUNCE_DISTANCE and distance(before, spot) > 50:
                continue  # this spot was rejected too; try further back
            if await self.walk_through(target, zone):
                return True
        # (No walked route outside walking zones: the player wants walking
        # only in the Waterworks; it walked Olde Town into stuck spots.)
        return False

    async def _ground_for_teleport(self, near: XYZ) -> list[tuple[float, float, float]]:
        from .tpspots import spots

        zone = await self.client.zone_name() or ""
        points = await self._landmarks() + await path_points(self.client) + spots().all(zone)
        return points + self.entity_map.spots(zone, lambda _n: True, (near.x, near.y, near.z))

    async def _ground_points(self, zone: str, near: XYZ) -> list[tuple[float, float, float]]:
        """Points known to be on the walkable map at `near`'s height: landmarks,
        walkway markers and spots things were seen at."""
        from .tpspots import spots

        points = await self._landmarks() + await path_points(self.client) + spots().all(zone)
        points += self.entity_map.spots(zone, lambda _n: True, (near.x, near.y, near.z))
        return [p for p in points if abs(p[2] - near.z) < 300 and math.dist(p[:2], (near.x, near.y)) < 3000]

    async def go_to_zone(self, dest: str, max_hops: int = 6) -> bool:
        """Walk the known gates to `dest`. True once there."""
        if is_team_up_zone(dest) and not is_team_up_zone(await self.client.zone_name() or ""):
            logger.info(f"not walking into {dest} alone (team-only dungeon)")
            return False
        await self._hub_shortcut(dest)
        for _ in range(max_hops):
            zone = await self.client.zone_name()
            if zone == dest:
                return True
            if not await is_free(self.client):
                return False  # a fight started on the way
            gate = gate_toward(zone, dest, self._bad_gates)
            if not gate:
                # No gate leads there (an instance: Khin-Pao in Kishibe's
                # MS_Plague2_T1): a door walked through before does. Go to
                # its zone by the gates, then through it.
                if await self._through_known_door(zone or "", dest, max_hops):
                    continue
                return False
            pos, next_zone = gate
            logger.info(f"heading to {dest}: gate to {next_zone}")
            kind = gate_kind(zone or "", next_zone)
            if press_x_gate(kind):
                # A boat, an NPC or a door that asks: stand there and press X.
                if not await self._use_x_gate(pos, zone or "", next_zone, ride_gate(kind)):
                    logger.warning(f"gate {zone} -> {next_zone} ({kind}) did not work; avoiding it")
                    self._bad_gates.add((zone, next_zone))
                await wait_for_loading(self.client)
                continue
            await self.travel(pos)
            if await self.client.zone_name() == zone and not await self.approach_and_walk(pos, zone):
                logger.warning(f"gate {zone} -> {next_zone} did not work; avoiding it")
                self._bad_gates.add((zone, next_zone))
            await wait_for_loading(self.client)
        return await self.client.zone_name() == dest

    async def _hub_for_objective(self, objective: str, zone: str) -> bool:
        """The hub button whenever the world's hub is nearer the objective
        than this zone (the player's rule): an objective in the hub ('Talk To
        Cyrus Drake in The Basilica' after Pyromancer's Tomb walked out by the
        dungeon's exit), one further on from it, or one in another world (its
        gate is by the hub: the Royal Hall walked through the Altar of Kings
        to the Oasis). Not inside a dungeon the objective is in. True if it
        went."""
        from .dungeon_heal import go_to_hub
        from .travel_data import world_hub

        dest = objective_zone(objective)
        hub = world_hub(zone)
        if not dest or not hub or zone == hub or is_team_up_zone(zone):
            return False
        target = dest if dest.split("/")[0] == zone.split("/")[0] else hub
        walk, via = zone_hops(zone, target), zone_hops(hub, target)
        if not hub_is_closer(walk, via):
            return False
        if await self._in_dungeon(zone) and self._dungeon and (
                dest == zone or dest == self._dungeon[1] or in_same_area(dest, self._dungeon[1])):
            return False  # (the objective is in this dungeon: leaving resets it)
        if not await is_free(self.client) or not self._may_try(objective, zone, "hub_button"):
            return False
        where = "in the hub" if via == 0 and target == dest else (
            "in another world" if target != dest else f"{via} gate(s) from the hub")
        logger.info(f"{objective!r} is {where}: the hub button instead of walking"
                    + (f" {walk} zone(s)" if walk is not None else ""))
        self._teleported = True  # (not a walk-through gate: don't learn it)
        return await go_to_hub(self.client)

    async def _hub_shortcut(self, dest: str) -> bool:
        """The hub button first when the walk from the hub is shorter than
        from here, the jump counted as a hop (it walked to the hub and on,
        when the button would have saved the way there). Not out of a
        dungeon, a team zone or a no-return zone. True if it went."""
        from .dungeon_heal import go_to_hub
        from .dungeons import no_return
        from .travel_data import world_hub

        zone = await self.client.zone_name() or ""
        hub = world_hub(zone)
        if not zone or not hub or zone == hub or dest.split("/")[0] != zone.split("/")[0]:
            return False
        if zone.startswith("WizardCity/"):
            # (No working hub button in Wizard City: from Ravenwood toward
            # Triton Avenue it fell back to the dorm, and the trip ended there.)
            return False
        walk, via = zone_hops(zone, dest), zone_hops(hub, dest)
        if walk is None or not hub_is_closer(walk, via):
            return False
        if is_team_up_zone(zone) or no_return(zone) or await self._in_dungeon(zone):
            return False
        if not await is_free(self.client):
            return False
        logger.info(f"to {dest.split('/')[-1]}: the hub button and {via} gate(s) "
                    f"instead of walking {walk}")
        self._teleported = True  # (not a walk-through gate: don't learn it)
        return await go_to_hub(self.client)

    async def _through_known_door(self, zone: str, dest: str, max_hops: int) -> bool:
        """Toward `dest` by a door learned earlier (state/doors.json): through
        it when we're in its zone, else to its zone first. True if it moved."""
        entry = next((z for z in self.doors.doors if z != zone and self.doors.leading_to(z, dest)), None)
        if (entry and not self.doors.leading_to(zone, dest) and gate_toward(zone, entry, self._bad_gates)
                and max_hops > 1):
            # Gates are surer than a chain of doors (the Cathedral's door to
            # the Library, its approach on the door itself, never took).
            logger.info(f"heading to {dest}: its door is in {entry.split('/')[-1]}; going there first")
            return await self.go_to_zone(entry, max_hops - 1) and True
        hops = self.doors.route(zone, dest)
        if hops:
            _z, door, spot, nxt = hops[0]
            logger.info(f"heading to {dest}: through the door at ({door[0]:.0f}, {door[1]:.0f}) into "
                        f"{nxt.split('/')[-1]}")
            start = XYZ(*spot)
            gap = math.dist((spot[0], spot[1]), (door[0], door[1]))
            if gap > DOOR_SPOT_FAR:
                # A walk learned from far off (Merle Ambrose's door: from 2,800
                # away, and the long walk never got in): land just short of
                # the door instead, on the side the walk came from.
                k = DOOR_SPOT_NEAR / gap
                start = XYZ(door[0] + (spot[0] - door[0]) * k, door[1] + (spot[1] - door[1]) * k, spot[2])
            await self.client.teleport(start)
            await asyncio.sleep(TELEPORT_SETTLE)
            if not await self._zone_changed(zone):
                await self.walk_through(XYZ(door[0], door[1], start.z), zone)
            await wait_for_loading(self.client)
            return await self.client.zone_name() != zone
        return False

    async def _use_x_gate(self, pos: XYZ, zone: str, next_zone: str, ride: bool) -> bool:
        """Use a gate by pressing X at it (WizSprinter's xNoWait/xSkipRide
        types). A ride (the Krokotopia boat) goes through a ride zone first,
        where another X skips the ride. True once in `next_zone`."""
        # The game's own teleporter object there, at its height (Triton
        # Avenue's "WC-TeleporttoCrabAlley" is down at the riverbed, z -1400;
        # the learned gate's z 4 landed the wizard on the willow above it).
        for e in await self._entities_named_like(("teleport",)):
            if math.dist((e.x, e.y), (pos.x, pos.y)) < 400:
                pos = e
                break
        await self.client.teleport(pos)
        await asyncio.sleep(1.5)  # a vendor stands by the boat: let the boat's prompt come up
        if not await ui.is_visible(self.client, ui.NPC_RANGE):
            # No prompt on the gate's own point (Triton Avenue's "To Lower
            # Triton": it landed on the willow beside it): spots around it.
            for radius in (150.0, 300.0):
                for i in range(8):
                    a = i * math.pi / 4
                    near = XYZ(pos.x + radius * math.cos(a), pos.y + radius * math.sin(a), pos.z)
                    await self.client.teleport(near)
                    await asyncio.sleep(0.8)
                    if await ui.is_visible(self.client, ui.NPC_RANGE):
                        logger.info(f"the gate's prompt showed {radius:.0f} beside it")
                        break
                else:
                    continue
                break
        for _leg in range(2 if ride else 1):
            here = await self.client.zone_name()
            pressed = False
            for _ in range(12):
                if await ui.is_visible(self.client, ui.NPC_RANGE):
                    await self.client.send_key(Keycode.X, 0.1)
                    pressed = True
                    await asyncio.sleep(0.6)
                    continue
                if pressed or await self.client.zone_name() != here:
                    break
                await asyncio.sleep(0.5)
            await wait_for_loading(self.client, appear_timeout=8.0)
            self._teleported = True  # not a walk-through gate: don't learn it
            if await self.client.zone_name() == next_zone:
                return True
        return await self.client.zone_name() == next_zone

    async def _sigil_at(self, target: XYZ, within: float = SIGIL_RANGE) -> XYZ | None:
        """Position of a dungeon sigil ("Teleport Semi Circle") at the marker, if any."""
        try:
            entities = await self.client.get_base_entity_list()
        except Exception:
            return None
        for e in entities:
            try:
                template = await e.object_template()
                name = (await template.object_name()).lower() if template else ""
                if "semi circle" not in name and "sigil" not in name:
                    continue
                pos = await e.location()
                if distance(pos, target) < within:
                    return pos
            except Exception:
                continue
        return None

    def _dungeon_at(self, outside: str, sigil: XYZ) -> str | None:
        """The dungeon (its first room's zone) whose sigil this is, if learned."""
        for inside, entry in DungeonMemory.load().dungeons.items():
            if entry.outside == outside and distance(XYZ(*entry.sigil), sigil) < SIGIL_NEAR_RANGE:
                return inside
        return None

    async def _enter_by_sigil(self, sigil: XYZ, zone: str | None) -> bool:
        """Dungeons start with ONE press of X on the sigil, then a ~10s countdown
        that any movement or another X press cancels. After a failed try the
        prompt won't restart until we step off the sigil and back on."""
        # An open menu (e.g. the spellbook) hides the "press X" prompt.
        await close_spellbook(self.client)
        await ui.close_menus(self.client)
        if self._sigil_failed_at is not None and distance(self._sigil_failed_at, sigil) < SIGIL_RANGE:
            # The prompt only comes back after leaving the sigil's (large) area
            # entirely: go somewhere far on the map, then come back.
            away = await self._far_spot(sigil)
            logger.info(f"re-arming the dungeon sigil: leaving to ({away.x:.0f}, {away.y:.0f}), then back")
            await self.client.teleport(away)
            await asyncio.sleep(1.5)
            # Come back like a player would: land short and walk onto it.
            dx, dy = away.x - sigil.x, away.y - sigil.y
            length = math.hypot(dx, dy) or 1.0
            await self.client.teleport(XYZ(sigil.x + dx / length * 300, sigil.y + dy / length * 300, sigil.z))
            await asyncio.sleep(1.0)
            await self.client.goto(sigil.x, sigil.y)
            await asyncio.sleep(1.0)
        elif distance(await self._position(), sigil) > SIGIL_RANGE:
            await self.client.teleport(sigil)
            await asyncio.sleep(TELEPORT_SETTLE)
        await wait_for_loading(self.client)
        if not await is_free(self.client):
            logger.info("pulled into a fight near the sigil; will try again after it")
            self._sigil_failed_at = sigil
            return False
        # Never into a dungeon hurt or low on mana (Mount Olympus twice at low
        # health): heal first, then come back to the sigil.
        if self.upkeep:
            hp, mana = await health_mana(self.client)
            if self.upkeep.needs_recovery(hp, mana):
                logger.info(f"{hp:.0%} health, {mana:.0%} mana: healing before entering the dungeon")
                await recover(self.client, self.upkeep, self.controller, self.go_to_zone)
                hp2, mana2 = await health_mana(self.client)
                if self.upkeep.needs_recovery(hp2, mana2) and (hp2, mana2) != (hp, mana):
                    return False  # healing is under way: come back to the sigil after
                # Nothing to heal with here and "enough to go on" (84%): enter
                # rather than loop sigil <-> heal (it did, at 82-84%).
        dungeon = self._dungeon_at(zone or "", sigil)
        from .teamup import is_team_dungeon

        if is_team_dungeon(dungeon or ""):
            # Too hard alone: only with a team (the Team Up button on the sigil).
            from .teamup import team_up

            await self._mark_here()
            outcome = await team_up(self, dungeon)
            if outcome == "in":
                self._dungeon = (zone or "", await self.client.zone_name() or "")
                self._sigil_failed_at = None
                return True
            # Another realm, or no team yet: back on the sigil and wait again
            # (the user wants this dungeon); never set it aside, never go in alone.
            return False
        self.controller.allow_idle(SIGIL_WAIT + 10)
        try:
            # Mark the entrance: a solo dungeon resets the moment we're defeated,
            # so a mark inside is useless, but Recall to the sigil saves the walk.
            await self._mark_here()
            await asyncio.sleep(0.5)
            logger.info(f"on the dungeon sigil; pressing X once and waiting up to {SIGIL_WAIT:.0f}s")
            await self.client.send_key(Keycode.X, 0.1)
            started = time.monotonic()
            deadline = started + SIGIL_WAIT
            seen_box = ""
            while time.monotonic() < deadline:
                if await self.client.is_loading() or await self.client.zone_name() != zone:
                    await wait_for_loading(self.client)
                    logger.success("entered the dungeon")
                    self._dungeon = (zone or "", await self.client.zone_name() or "")
                    self._sigil_failed_at = None
                    await self._remember_dungeon(zone, sigil)
                    return True
                if await self.client.in_battle():
                    waited = time.monotonic() - started
                    logger.warning(f"a fight started {waited:.0f}s into the sigil countdown")
                    break
                # Don't click anything during the countdown (a blind click on a
                # message box could cancel it); just record what shows up.
                box = await ui.modal_box(self.client)
                text = (await ui.modal_text(box)) if box else ""
                if text and text != seen_box:
                    seen_box = text
                    waited = time.monotonic() - started
                    logger.warning(f"message box {waited:.0f}s into the sigil countdown: {text[:120]!r}")
                if text and "about to enter a dungeon" in text.lower():
                    # The dungeon notice waits for OK before the countdown goes on.
                    await ui.press_modal_button(self.client, box, "centerButton")
                await asyncio.sleep(0.5)
        finally:
            self.controller.end_idle()
        logger.warning("stood on the sigil but the dungeon did not start; will re-arm it")
        self._sigil_failed_at = sigil
        return False

    async def _is_dungeon_zone(self, zone: str) -> bool:
        from .dungeons import INSTANCE_ZONES

        return (zone in DungeonMemory.load().dungeons or zone in INSTANCE_ZONES
                or "/interiors/" in zone.lower() or is_team_up_zone(zone))

    async def _mark_in_dungeon_fight(self, objective: str, zone: str):
        """Inside a dungeon, before its fight: mark here, so a defeat is
        followed by a Recall back into this copy of the dungeon (the sigil
        mark outside meant going in again: a fresh copy, all progress lost,
        after losing to the Death Oni). Once per objective."""
        m = self._mark
        if m and m.kind == "fight" and m.objective == objective and m.zone == zone:
            return  # (one mark per Defeat objective: the player's rule)
        if self._recall_pending:
            return  # a defeat's Recall to the current mark comes first
        from .dungeons import no_return

        if no_return(zone):
            return  # Recall can't come back in here: the entrance mark stays
        logger.info("marking inside the dungeon before its fight (Recall back here after a defeat)")
        await self._mark_here("fight", objective=objective, require_clear=False)

    async def _boss_prisms(self, target: str) -> dict[str, int]:
        """Our school's prisms for a boss of our school (the player: Myth
        Prisms in before a Myth boss, out after): its school from earlier
        fights, else read from the game before engaging."""
        from .deck_keeper import PROGRESS_FILE, _load, boss_prism, school_on_file

        mine = (getattr(self.progression, "school", "") or "") if self.progression else ""
        if not mine:
            return {}
        school = school_on_file(target) or await self._school_in_view(target)
        known = set(_load(PROGRESS_FILE).get("known_spells") or [])
        prisms = boss_prism(school, mine, known)
        said = self.__dict__.setdefault("_prism_said", set())
        if prisms and target not in said:
            said.add(target)
            logger.info(f"{target} is a {school} boss: {', '.join(prisms)} into the deck for the fight")
        return prisms

    async def _school_in_view(self, name: str) -> str:
        """The school of an enemy named `name` in view, read before engaging
        ("" if none is loaded)."""
        from .names import lang_name

        want = _norm_name(name)
        try:
            for m in await self.client.get_mobs():
                t = await m.object_template()
                code = await t.display_name() if t else ""
                if code and _norm_name(await lang_name(self.client, code)) == want:
                    return (await t.primary_school_name() or "").lower()
        except Exception:
            pass
        return ""

    async def _boss_health_bar(self):
        """The player's: into a boss fight at full health (it walked at Ullik
        with 74%, then went toward Jotun at 88%, 'good enough' for regular
        enemies). While the objective is a boss, healing goes on to
        rest_until_health instead of stopping at min_health_to_fight."""
        if not self.upkeep:
            return
        base = self.__dict__.setdefault("_base_min_health", self.upkeep.min_health_to_fight)
        target = defeat_target(await self.objective() or "") or ""
        boss = bool(target) and is_known_boss(target)
        want = max(base, self.upkeep.rest_until_health) if boss else base
        if want != self.upkeep.min_health_to_fight:
            self.upkeep.min_health_to_fight = want
            if boss:
                logger.info(f"{target} is a boss: healing to {want:.0%} before the fight")

    async def _mark_before_boss(self, target: str, objective: str, zone: str):
        """In a dungeon, right before going after the objective's enemy: mark
        this spot, so a defeat's Recall lands beside it (the player, at
        Malistaire: the mark made on arriving was rooms away after his Soul
        Servants, and a heal trip came back to the wrong volcano zone). Once
        per objective and zone."""
        done = self.__dict__.setdefault("_boss_marked", set())
        if (objective, zone) in done or self._recall_pending or is_team_up_zone(zone):
            return
        if not ("/interiors/" in zone.lower() or await self._in_dungeon(zone)):
            return
        from .dungeons import no_return

        if no_return(zone):
            return
        if await self.client.in_battle():
            return
        logger.info(f"marking here before going after {target} (Recall back beside it after a defeat)")
        if await self._mark_here("fight", objective=objective, require_clear=False):
            done.add((objective, zone))

    async def _mark_here(self, kind: str = "dungeon", objective: str | None = None,
                         require_clear: bool = True) -> bool:
        """Mark this spot. A dungeon's sigil: after a defeat and healing, Recall
        brings us straight back instead of walking across the world again. A
        travel mark: a later objective near it is reached by Recall."""
        try:
            objective = await self.objective() if objective is None else objective
            zone = await self.client.zone_name() or ""
            if is_team_up_zone(zone):
                return False  # the mark stays at the sigil outside (Recall can't take us in alone)
            if is_hub(zone) and kind != "dungeon":
                # Never mark a hub: a defeat or the hub button brings us here
                # anyway, and it would overwrite the mark that matters. (A
                # dungeon's sigil in a hub, Mount Olympus's in Aquila's, is
                # marked: Recall is the way back to it from another world.)
                logger.debug(f"not marking in the hub {zone}")
                return False
            # Inside a dungeon or a room the mark goes where we are, enemies near
            # or not: without it the heal trip came back by the sigil mark and
            # the dungeon started over (the Death Oni's tower).
            if require_clear and ("/interiors/" in zone.lower() or await self._in_dungeon(zone)):
                require_clear = False
            if kind != "dungeon" and require_clear:  # a dungeon mark belongs on its sigil
                # Recall lands us here later, when patrols may have wandered in
                # (a mark among Otomo Supply Runners meant a fight on return):
                # only somewhere well clear of every enemy, else no mark.
                await move_to_safety(self.client, MARK_SAFE_RADIUS, "before marking")
                me = await self._position()
                if not clear_of(me, [XYZ(*m) for m in await mob_positions(self.client)], MARK_SAFE_RADIUS):
                    logger.info(f"no spot here {MARK_SAFE_RADIUS:.0f} clear of enemies: not marking")
                    return False
            if not await ui.click_named(self.client, "MarkButton"):
                logger.debug("no Mark button to click")
                return False
            await asyncio.sleep(1.0)
            await ui.confirm_modal(self.client)
            self._mark = Mark(zone, objective, kind)
            save_mark(self._mark)
            if kind == "dungeon":
                logger.info(f"marked the dungeon entrance in {zone} (for a quick return after a defeat)")
            elif kind == "fight":
                logger.info(f"marked this spot in {zone} before the fight (Recall back here after a defeat)")
            elif kind == "room":
                logger.info(f"marked this spot in {zone} (Recall back here after healing)")
            else:
                logger.info(f"marked this spot in {zone} before a long trip (Recall when it's on the way)")
            return True
        except Exception as exc:
            logger.debug(f"marking failed: {exc!r}")
            return False

    async def _mark_for_fight(self, objective: str, zone: str):
        """Reaching a fight objective's zone: mark the spot once, so a defeat
        is followed by a Recall here instead of the long walk back.
        Off: marks are only placed before a dungeon and before a heal trip."""
        if not TRAVEL_AND_FIGHT_MARKS:
            return
        m = self._mark
        if m and m.kind in RETURN_KINDS and m.objective == objective and m.zone == zone:
            return
        if m and m.kind == "dungeon" and self._keep_dungeon_mark(objective):
            return  # a dungeon's sigil mark still matters more
        if self._recall_pending or await self._in_dungeon(zone):
            return  # inside a dungeon a defeat resets it: its sigil is marked instead
        await self._mark_here("fight", objective=objective)

    async def _heal_mark(self) -> bool:
        """Before healing: mark the spot, to Recall back after healing from
        the hub. Not while a dungeon mark waits for its Recall (a defeat)."""
        if not self.healer or self._recall_pending or self.healer.busy:
            return False  # (a heal trip is under way: its mark is placed)
        zone = await self.client.zone_name() or ""
        if time.monotonic() - self._last_defeat < DEFEAT_NO_MARK_SECONDS or is_hub(zone):
            return False  # just respawned in the hub: a mark here is useless
        if self._mark and self._mark.kind in RETURN_KINDS and self._keep_dungeon_mark(await self.objective()):
            return True  # a fight/dungeon mark for this objective waits: heal trips Recall to it
        if self._mark and self._mark.zone != zone and self._mark.zone in DungeonMemory.load().dungeons:
            # The mark is inside a dungeon we left to heal (Katzenstein's Lab):
            # never mark over it out here; Recall back to it.
            return True
        if await self._fight_mark_here():
            return True  # the fight mark does the job: healing Recalls back to it
        return await self._mark_here("room")

    async def _fight_mark_here(self) -> bool:
        """Is the mark a fight mark for the current objective in this zone?"""
        m = self._mark
        if not m or m.kind != "fight":
            return False
        return m.objective == await self.objective() and m.zone == await self.client.zone_name()

    async def _boss_settled(self, objective: str) -> bool:
        """Safe to leave a dungeon to heal? Not during a cutscene, not right
        after a fight, and with a boss to beat next, not until he's in view
        (the player: killing the King's Tomb spider plays the cutscene that
        spawns Zanga Zebu; healing then left him unspawned for good). After
        BOSS_SPAWN_WAIT it goes anyway."""
        since = time.monotonic() - (self.fighter.combat_ended_at if self.fighter else 0.0)
        if not await is_free(self.client):
            logger.debug("heal trip: a cutscene or dialogue first")
            return False
        if since < DUNGEON_SETTLE:
            logger.debug("heal trip: just out of a fight in a dungeon; waiting for what it sets off")
            return False
        target = defeat_target(objective)
        if target and since < BOSS_SPAWN_WAIT:
            from .bossfarm import mobs_named

            names = {_norm_name(n) for n, _p in await mobs_named(self.client)}
            if _norm_name(target) not in names:
                if getattr(self, "_spawn_wait_logged", None) != target:
                    self._spawn_wait_logged = target
                    logger.info(f"waiting for {target} to appear before leaving to heal")
                return False
        return True

    async def _heal_trip(self, force: bool = False, marked: bool = False) -> bool:
        """This zone lacks what recovery needs: heal from the hub and Recall
        back instead of walking out and back through the gates. Goes when the
        spot is marked already, the objective keeps us here, or `force`
        (resting here gave nothing at all)."""
        if not self.healer:
            logger.debug("heal trip: no healer")
            return False
        zone = await self.client.zone_name() or ""
        if is_hub(zone):
            # Already at the hub (a defeat sends us here): a trip there is
            # pointless; recovery goes to a zone with wisps instead. (Not 'just
            # defeated': the fight mark's Recall brought us back into the
            # Emperor's Palace at 2% health, and no trip meant no healing.)
            return False
        objective = await self.objective()
        if await self._in_dungeon(zone) and not await self._boss_settled(objective or ""):
            return False  # (stay: the boss's spawn cutscene; leaving broke the copy)
        dest = objective_zone(objective) if objective else None
        coming_back = dest == zone or (dest is None and is_combat_objective(objective or ""))
        if not zone or not (marked or coming_back or force):
            logger.debug(f"heal trip: not coming back here ({objective!r}, dest {dest}, marked {marked})")
            return False
        # Never over a fight or dungeon mark still wanted for this objective:
        # the trip Recalls to it instead. An old one (Willie Marks's, done) is
        # replaced by a mark here.
        keep = await self._fight_mark_here() or self._keep_dungeon_mark(objective or "")
        if self._mark and self._mark.zone != zone and self._mark.zone in DungeonMemory.load().dungeons:
            keep = True  # a spot inside a dungeon we're out of: keep it, Recall to it
        why = f"not enough wisps here for {objective!r}"
        went = await self.healer.trip(zone, why, mark=not (marked or keep))
        if not went:
            logger.debug(f"heal trip: the healer didn't go (busy {self.healer.busy}, mark {self._mark})")
        return went

    async def _in_dungeon(self, zone: str) -> bool:
        """Still inside the dungeon we entered by its sigil? Leaving it (its
        outside zone, another world) ends that; a heal trip Recalls back first."""
        from .dungeons import INSTANCE_ZONES

        if zone in INSTANCE_ZONES:
            first = INSTANCE_ZONES[zone]
            entry = DungeonMemory.load().dungeons.get(first)
            if entry is not None and (not self._dungeon or self._dungeon[1] != first):
                self._dungeon = (entry.outside, first)
            return True
        if not self._dungeon:
            entry = DungeonMemory.load().dungeons.get(zone)  # e.g. after a restart inside
            if entry is None:
                return False
            self._dungeon = (entry.outside, zone)
        outside, first_room = self._dungeon
        from .dungeons import is_open_zone

        if is_open_zone(zone):
            return False  # (still inside the one entered from here: kept)
        if is_hub(zone) or zone == outside or zone.split("/", 1)[0] != first_room.split("/", 1)[0]:
            self._dungeon = None
            return False
        # Its rooms share the first room's area (Marleybone/MB_BigBen/...);
        # another street of the world is not in it (Hyde Park, visited to heal,
        # counted as the dungeon and its side quest was taken up).
        return zone == first_room or in_same_area(zone, first_room)

    def _keep_dungeon_mark(self, objective: str) -> bool:
        """The dungeon mark is still wanted: a defeat awaits a Recall, we're
        still on the objective it was set for, or the objective is still in
        that dungeon (Katzenstein's Lab: talk to Grunk, collect the crates...).
        Until the dungeon is done, nothing marks over it."""
        m = self._mark
        if not m or m.kind not in RETURN_KINDS:
            return False
        if self._recall_pending or m.objective == objective:
            return True
        target = objective_zone(objective)
        mem = DungeonMemory.load()
        if m.zone in mem.dungeons:  # a mark inside the dungeon (beside its boss)
            return target == m.zone
        entry = mem.dungeons.get(target or "")  # the entrance mark, outside on the sigil
        return entry is not None and entry.outside == m.zone

    def _same_dungeon(self, objective: str, marked_zone: str) -> bool:
        """The objective (after a defeat the game may track another quest:
        'Go To Dean's Cell in The Labyrinth' instead of 'Defeat Andor
        Bristleback') is still in the marked dungeon, or doesn't say where:
        Recall back rather than walk in again (a fresh copy, progress lost)."""
        place = objective_zone(objective) if objective else None
        if place is None or place == marked_zone:
            return True
        if in_same_area(place, marked_zone) or in_same_area(marked_zone, place):
            return True
        entry = DungeonMemory.load().dungeons.get(marked_zone)
        return entry is not None and entry.outside == place

    def _retire_dungeon_mark(self):
        """The dungeon mark has served (or can't any more); the game still holds
        it, so it stays on as a travel mark in the sigil's zone."""
        if self._mark and self._mark.kind in RETURN_KINDS:
            self._mark = Mark(self._mark.zone, self._mark.objective, "travel")
            save_mark(self._mark)

    async def _travel_mark(self, objective: str, zone: str):
        """A new objective several zones away: mark where we are first, so a
        later objective back here is a Recall instead of the same long walk.
        Off: marks are only placed before a dungeon and before a heal trip."""
        if not TRAVEL_AND_FIGHT_MARKS:
            return
        dest = objective_zone(objective)
        dungeons = set(DungeonMemory.load().dungeons)
        keep = self._keep_dungeon_mark(objective)
        if not should_travel_mark(zone, dest, zone_hops, self._mark, keep, dungeons):
            return
        if not await is_free(self.client):
            return
        await self._mark_here("travel", objective=self._last_progress[0] or "")

    async def _recall_if_faster(self, objective: str, zone: str) -> bool:
        """Recall to the mark when that plus the walk from it beats walking to
        the objective's zone from here. True if we recalled."""
        # Travel marks and heal marks (a spot in a dungeon left to heal) both
        # take us back; fight and dungeon marks have their own Recall rules.
        # (A dungeon entrance mark too: back to Counterweight East's sigil in
        # one Recall instead of walking over from the Ironworks.)
        if not self._mark or self._mark.kind not in RECALL_KINDS or self._recalled_for == objective:
            return False
        if time.monotonic() < self._recall_blocked_until:
            return False
        dest = objective_zone(objective)
        if self._grinding and not self._mainline and dest and dest.split("/", 1)[0] != self._main_world:
            return False  # a side world's tracked quest (Grizzleheim): not followed
        if dest == zone or is_team_up_zone(zone) or await self._in_dungeon(zone):
            # Already there, or inside a dungeon (a Recall out resets it: it
            # left Counterweight East right after its last lever).
            return False
        if dest and (self.doors.leading_to(dest, zone) or DungeonMemory.load().bosses.get(
                defeat_target(objective) or "") == zone):
            # In a room entered from the objective's place (the Spirit World
            # off the Burial Grounds, where Tomugawa is): the objective is here.
            return False
        if not recall_is_faster(zone, dest, self._mark, zone_hops, objective):
            return False
        if not await is_free(self.client):
            return False
        self._recalled_for = objective
        walk, via = zone_hops(zone, dest), zone_hops(self._mark.zone, dest)
        logger.info(
            f"recalling to the mark in {self._mark.zone} for {dest}: "
            f"{via} zone(s) from there vs {walk if walk is not None else 'no known route'} from here"
        )
        if await self._recall(self._mark.zone):
            return True
        self._recall_blocked_until = time.monotonic() + RECALL_RETRY_SECONDS
        return False

    async def _learn_arrival_gate(self):
        """Walking from one outdoor zone into another leaves the wizard just in
        front of the gate back: remember it (the data files miss some gates)."""
        zone = await self.client.zone_name() or ""
        prev, deaths = self._zone_before, self.controller.deaths
        seen = getattr(self, "_zone_before_seen", 0.0)
        self._zone_before, self._zone_before_seen = zone, time.monotonic()
        if last_zone_jump() > seen:
            # The hub button, Go Home or a Recall moved us since the last look
            # (a heal trip's hub button "learned" Cathedral -> Plaza of Conquests).
            self._teleported = False
            return
        if not prev or prev == zone or deaths != self._deaths_before or self._teleported:
            self._deaths_before, self._teleported = deaths, False
            return  # first look, same zone, or a defeat/recall moved us
        self._deaths_before = deaths
        if "interiors" in (prev + zone).lower() or prev.split("/")[0] != zone.split("/")[0]:
            return
        if gate_toward(zone, prev, self._bad_gates):
            # Already reachable; and a travel may cross several zones in one step,
            # so `prev` needn't even border this zone.
            return
        pos = await self._position()
        gate = gate_behind(pos, await self.client.body.yaw())
        if learn_gate(zone, prev, gate):
            logger.info(f"learned a gate {zone} -> {prev} at ({gate.x:.0f}, {gate.y:.0f})")

    async def _answer_dungeon_exit(self):
        """Walking into a dungeon's exit asks "If you leave a Dungeon you will lose
        all your progress...": leave when the objective is elsewhere (e.g. hand the
        quest in outside), otherwise stay. Unanswered, it blocks all movement."""
        box = await ui.modal_box(self.client)
        if box is None:
            return
        text = (await ui.modal_text(box)).lower()
        if "leave a dungeon" not in text and "leave this dungeon" not in text:
            return
        zone = await self.client.zone_name() or ""
        objective = await self.objective()
        target = objective_zone(objective)
        brothers_due = pending_pre_bosses(defeat_target(objective or "") or "", load_pre_bosses_beaten())
        if is_team_up_zone(zone):
            leave = False  # never leave a team dungeon
        elif brothers_due and zone in self.__dict__.get("_pre_boss_rooms", load_pre_boss_rooms()).values():
            # In a brother's warren (Ullik's Helgrind Warren) before he's
            # beaten: leaving reset it, and it was walked out of three times.
            leave = False
        elif target is not None:
            leave = target != zone
        else:
            # A place we can't map ("Talk To Sergeant Steeg in Knight's Court",
            # "Defeat Mikey the Brick in Knight's Court"): the marker led us to
            # the exit, so the step is outside; stay only when the enemy it
            # names is in here.
            leave = not await self._named_enemy_here(objective)
        logger.info(f"dungeon exit prompt: {'leaving' if leave else 'staying'} (objective {objective!r})")
        if leave and not is_team_up_zone(zone) and await self._in_dungeon(zone) and self._dungeon:
            # Left by its exit for an objective outside: this dungeon's part
            # is done (back in for a spell quest's boss, its own quest no
            # longer comes first: the Labyrinth was redone for Ranulf Moonclaw).
            from .dungeons import mark_done

            # (Not while quests of this dungeon are still in the book: the bot
            # walked out of the Waterworks after a wrong ranking with 'Stick to
            # the Plan!' open, and its quests stopped coming first in there.)
            open_here = dungeon_quest(self.__dict__.get("_last_quests", []), zone, objective_zone,
                                      skipped=self.setbacks.skipped, entered_with="")
            if open_here is None and mark_done(self._dungeon[1]):
                logger.info(f"dungeon {self._dungeon[1].split('/')[-1]} noted as done")
        await ui.press_modal_button(self.client, box, "centerButton" if leave else "rightButton")
        if leave:
            await wait_for_loading(self.client, appear_timeout=5.0)

    async def _pick_up_loot(self) -> bool:
        """Every few seconds, grab a reagent or chest nearby (clear of enemies):
        a few seconds each, and reagents and chests pay off later."""
        if time.monotonic() - self._last_loot_scan < WANTED_SCAN_SECONDS:
            return False
        self._last_loot_scan = time.monotonic()
        if await self.client.in_battle():
            return False
        try:
            return await self.collector.collect_nearby(self._press_collect)
        except Exception as exc:
            logger.debug(f"loot pickup failed: {exc!r}")
            return False

    def _note_boss_win(self):
        """A boss fight was just won: the farm's boss ends a farm run."""
        if not self.fighter or self.fighter.boss_fights == self._boss_fights_seen:
            return
        won = self.controller.deaths == self._boss_deaths_seen
        self._boss_fights_seen, self._boss_deaths_seen = self.fighter.boss_fights, self.controller.deaths
        farm = Farm.load()
        if won and farm.active and farm.ends_run(self.fighter.last_boss_names):
            self._farm_run_done = True

    async def _dorm_to_wizard_city(self) -> bool:
        """The objective is in Wizard City and we're in another world: the dorm
        button lands in Wizard City at once (the world gate route looped in
        Marleybone's station). True if it went."""
        zone = await self.client.zone_name() or ""
        target = objective_zone(await self.objective())
        if target is None and self._chosen_entry is not None:
            # A place in several worlds ("The Library": Ravenwood's, or the
            # one in Krokotopia): the quest book names the world.
            if self._chosen_entry.zone.strip().lower() == "wizard city":
                target = "WizardCity/" + (self._chosen_entry.world or "?")
        if not target or not target.startswith("WizardCity/") or zone.startswith("WizardCity/"):
            return False
        if not await is_free(self.client) or await self._in_dungeon(zone):
            return False
        from .trainer import go_home, is_house

        if is_house(zone):
            # Home already (Go Home does nothing here: it looped pressing it):
            # the house's world gate to Wizard City.
            return await self._to_world("WizardCity", f"the quest is in Wizard City ({target})")
        logger.info(f"the quest is in Wizard City ({target}); Go Home, then the world gate")
        return await go_home(self.client)

    async def _house_to_world(self) -> bool:
        """At the player's house with the objective in a world: its world gate
        (the quest marker points at the gate, and walking into it as a door
        looped for minutes). True if it went."""
        from .trainer import is_house

        zone = await self.client.zone_name() or ""
        if not is_house(zone):
            return False
        if not self._mainline and self._detour_gap_pending():
            return False  # (the story's next quest first: _detour_ask goes to its world)
        target = objective_zone(await self.objective())
        if target is None and self._chosen_entry is not None and self._chosen_entry.zone:
            target = self._chosen_entry.zone.replace(" ", "")  # the book's world ("Wizard City")
        if not target or is_house(target):
            return False
        world = target.split("/", 1)[0]
        return await self._to_world(world, f"the quest is in {world}")

    async def _leave_spiral_map(self) -> bool:
        """Walking into a world gate (not pressing X at it) opens the Spiral
        Map with the quest's world already ticked: press Go To World. True if
        the map was open."""
        if not await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
            self._map_tries = 0
            return False
        tries = self._map_tries = getattr(self, "_map_tries", 0) + 1
        if tries > MAP_GO_TRIES:
            # The map open step after step and nothing taking us anywhere (at
            # the Zafaria hub for The Spiral Cup's world; with the map up the
            # quest book can't be read, so no main quest was known either):
            # close it and rank the quests again.
            logger.warning("the Spiral Map won't take us anywhere: closing it and choosing quests again")
            self._map_tries = 0
            await ui.click(self.client, ui.SPIRAL_DOOR_EXIT)
            await asyncio.sleep(1.0)
            if await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
                # Clicks don't reach it (the game window 97% off screen): Escape.
                await self.client.send_key(Keycode.ESC, 0.1)
                await asyncio.sleep(1.0)
                if await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
                    logger.warning("the Spiral Map is still open: move the game window onto a monitor "
                                   "(clicks don't reach it)")
            self._ranked_for = None
            self._last_rank = -1e9
            return True
        if not self._mainline and self._detour_gap_pending():
            # On the way to ask for the story's next quest: its world, not the
            # tracked side quest's (the map ticked Wysteria for The Spiral Cup
            # and the bot bounced between the house and Wysteria).
            try:
                gap = json.loads(DETOUR_GAP_FILE.read_text(encoding="utf-8"))
                world = (gap.get("zone") or "").split("/", 1)[0]
            except (OSError, ValueError):
                world = ""
            if world:
                return await self._to_world(world, f"to {world} for the story's next quest")
        if self._grinding and not self._mainline and self._main_world:
            # No main quest: back to its world, not on to the tracked side
            # quest's (the map took the bot from Wizard City to Grizzleheim
            # each time it set out for Dragonspyre).
            why = f"back to {self._main_world} to fight for experience"
            return await self._to_world(self._main_world, why)
        zone = await self.client.zone_name() or ""
        target = objective_zone(await self.objective())
        farm = Farm.load()
        if farm.active and farm.dungeon.startswith("Aquila/"):
            # Aquila isn't on the Spiral Map (it's the Cyclops Lane prompt).
            logger.info("on the Spiral Map while farming Aquila: leaving the map")
            await ui.click(self.client, ui.SPIRAL_DOOR_EXIT)
            await asyncio.sleep(1.0)
            return True
        if target and target.split("/", 1)[0] == zone.split("/", 1)[0]:
            # The quest is in this world (walked in on the way to a side quest
            # elsewhere, then the class quest was picked again): stay.
            logger.info("on the Spiral Map, but the quest is in this world: leaving the map")
            await ui.click(self.client, ui.SPIRAL_DOOR_EXIT)
            await asyncio.sleep(1.0)
            return True
        logger.info("on the Spiral Map: going to the world the quest leads to")
        before = await self.client.zone_name()
        for _ in range(5):
            if not await ui.click(self.client, ui.SPIRAL_DOOR_TELEPORT):
                break
            await asyncio.sleep(0.5)
        await wait_for_loading(self.client)
        if await self.client.zone_name() != before:
            self._map_tries = 0
        return True

    async def _pick_up_wanted(self) -> bool:
        """Every few seconds, grab any wanted "Collect X" item in view (away from
        enemies) for any quest in the book, even one set aside. True if it did."""
        if not self._wanted_items or time.monotonic() - self._last_wanted_scan < WANTED_SCAN_SECONDS:
            return False
        self._last_wanted_scan = time.monotonic()
        for item, quest in self._wanted_items.items():
            if await self.collector.collect_once(item, self._press_collect):
                logger.success(f"picked up {item!r} on the way (for {quest!r})")
                return True
        return False

    async def _grind(self) -> bool:
        """Every quest is set aside (bosses too strong, the rest unreachable):
        gain the level that releases them by fighting enemies here, or where a
        fight was last won. True if it acted this step."""
        if await self.client.in_battle():
            return True
        if await self._detour_ask():
            return True
        back = self.setbacks.release_stuck()
        if back:
            # Nothing else to do: another try at the stuck quests beats grinding.
            self.setbacks.save()
            logger.info(f"nothing else to do: trying {', '.join(map(repr, back))} again")
            self._ranked_for = None
            self._last_rank = -1e9
            return True
        zone = await self.client.zone_name() or ""
        world = self._grind_world()  # (the highest-level world we've reached: Celestia)
        in_main_world = not world or zone.split("/", 1)[0] == world
        if not in_main_world and not self._mainline:
            # No main quest to lead back: an auto-tracked side quest led here
            # (Grizzleheim from Dragonspyre); go back rather than follow it
            # (before re-ranking: a ranking on the way let the step follow it).
            why = f"back to {world} to fight for experience"
            return await self._to_world(world, why)
        if time.monotonic() - self._last_rank > GRIND_RERANK_SECONDS:
            # A quest may have come in (an NPC offered one, the next main
            # quest): read the book again before more grinding.
            self._ranked_for = None
            self._last_rank = -1e9
            return False
        if not in_main_world:
            return False  # the main quest's marker leads there (quest step)
        ask = getattr(self.givers, "main_sweep_zone", "")
        if ask and not self._mainline and zone != ask and ask.split("/", 1)[0] == self._main_world:
            # The next main quest's giver is likely where the last one ended.
            logger.info(f"no main quest: going to {ask.split('/')[-1]} to ask its NPCs for the next one")
            if await self.go_to_zone(ask) or await self.client.zone_name() != zone:
                return True
            self.givers.main_sweep_zone = ""  # no way there known
        place = objective_zone(await self.objective() or "")
        off_world = not self._mainline and place and place.split("/", 1)[0] != world
        # (No main quest and the tracked one is a side world's: its marker
        # leads to the world gate; fight here instead.)
        # Outdoors only: in a dungeon a grinding fight pulled the Runed
        # Devestator (a 5200-health boss) beside a Rubble Reaver in the Hall of
        # Kings, and the wizard went down.
        indoors = "interiors" in zone.lower() or await self._in_any_dungeon(zone)
        if await self.sprinter.get_mobs() and not indoors:
            await self.pull_mob("")
            return True
        if not indoors and await self._to_enemy_spot(zone):
            return True  # (none in view: enemies load only nearby; GH_Wolf stood idle)
        spot = self.__dict__.get("_win_zones", {}).get(world) or GRIND_FALLBACK.get(world, "")
        if spot and spot != zone:
            logger.info(f"grinding: to {spot.split('/')[-1]} for its enemies")
            if await self.go_to_zone(spot):
                return True
        if (self._last_win_zone.split("/", 1)[0] == world and self._last_win_zone != zone
                and not await self._in_any_dungeon(self._last_win_zone)
                and "interiors" not in self._last_win_zone.lower()):
            logger.info(f"no enemies here; going to {self._last_win_zone} to fight for experience")
            if await self.go_to_zone(self._last_win_zone):
                return True
        if off_world:
            await asyncio.sleep(5.0)
            return True
        # Nowhere known yet: the quest step follows the main quest's marker, and
        # the first outdoor zone there with enemies (before its dungeon) is used.
        return False

    def _alert_main_stuck(self, quest: str, why: str, *, hard: bool = False):
        """The main quest can't go on for now: an ALERT line (activity.log) that
        the operator's watcher turns into a phone notification. The bot keeps
        going; once per quest an hour. Truly stuck (`hard`: lost its fight
        MAIN_DEFEATS_TO_DEFER times, or no main quest at all; or stuck
        STUCK_TIMES_TO_FARM times) it farms Aquila instead of side quests."""
        self._stuck_times[quest] = self._stuck_times.get(quest, 0) + 1
        if FARM_WHEN_STUCK and (hard or self._stuck_times[quest] >= STUCK_TIMES_TO_FARM):
            self._farm_when_stuck(quest, why)
        now = time.monotonic()
        if now - self._alerted.get(quest, -1e9) < ALERT_REPEAT_SECONDS:
            return
        self._alerted[quest] = now
        logger.warning(f"ALERT: main quest {quest!r} stuck: {why}; doing side quests meanwhile")

    def _farm_when_stuck(self, quest: str, why: str):
        """The player's rule: when the main quest can't go on after real
        effort, farm Mount Olympus (waiting for players at the sigil) rather
        than do side quests; farming stays on until the player stops it."""
        farm = Farm.load()
        if farm.active or farm.complete:
            return
        farm.active = True
        farm.save()
        logger.warning(f"ALERT: main quest {quest!r} stuck ({why}): farming {farm.name} until stopped")

    async def _set_current_aside(
        self, objective: str, retry_after: float | None = STUCK_RETRY_SECONDS
    ) -> bool:
        """Set the tracked quest aside now (it can't be progressed from here) and
        re-rank, so the next best quest is followed. Just tracking the next quest
        in the book let ranking pick the same one again."""
        quest = self._active_quest
        if not quest:
            return await self.switch_quest()
        level = await self.client.stats.reference_level()
        # Can't be progressed (not a lost fight): aside as stuck, no timed retry
        # (the player: side quests for experience; back when nothing else is left).
        self.setbacks.set_quest_aside(
            quest, objective, level, main=quest in self._mainline, retry_after=retry_after,
            stuck=retry_after == STUCK_RETRY_SECONDS,
        )
        self.setbacks.save()
        if quest in self._mainline:
            self._alert_main_stuck(quest, f"no way to {objective!r} found")
        self._ranked_for = None
        self._last_rank = -1e9
        return True

    def _team_instead(self, quest: str, objective: str, losses: int) -> bool:
        """A main-quest boss in a dungeon with a sigil beat us `losses` times:
        that dungeon goes on the team list (the player's "multiplayer mode":
        wait at its sigil, Team Up, fight it with 2+ players, as at Mount
        Olympus). True if it did; False outside a known dungeon."""
        from .teamup import add_team_dungeon, is_team_dungeon

        if quest not in self._mainline:
            return False
        memory = DungeonMemory.load()
        fought = self.fighter.last_enemy_names if self.fighter else []
        zone = (getattr(self.controller, "last_death", None) or (0, ""))[1]
        dungeon = next((memory.bosses[n] for n in fought if n in memory.bosses), None) or (
            zone if zone in memory.dungeons else None)
        entry = memory.dungeons.get(dungeon or "")
        if not dungeon or entry is None or not entry.sigil or is_team_dungeon(dungeon):
            return False
        add_team_dungeon(dungeon, quest)
        logger.warning(f"lost {objective!r} {losses} times: {dungeon.split('/')[-1]} with a team from now on "
                       f"(Team Up at its sigil) until {quest!r} is done")
        self._alert_main_stuck(quest, f"lost {objective!r} {losses} times; waiting for a team")
        self._recall_pending = False
        self._ranked_for = None
        self._last_rank = -1e9
        return True

    async def _note_defeats(self):
        """After a defeat, count it against the objective; the second one sets the
        quest aside for another questline (until a level-up or an hour passes)."""
        deaths = self.controller.deaths
        if deaths <= self._seen_deaths:
            return
        self._seen_deaths = deaths
        self._recall_pending = bool(self._mark and self._mark.kind in RECALL_KINDS)
        self._last_defeat = time.monotonic()
        # The objective the fight was for: after the respawn the game may track
        # another quest (a Wysteria one), which took the blame for a Labyrinth loss.
        objective = self._last_progress[0] or await self.objective()
        if not objective:
            return
        # Every loss counts, not only on "Defeat X": "Talk To Willie Marks" is
        # a boss fight too. But on "Defeat X" a loss to others (two Scurriers
        # met while healing) isn't a loss to X.
        target = defeat_target(objective)
        fought = self.fighter.last_enemy_names if self.fighter else []
        bosses = self.fighter.last_bosses if self.fighter else set()
        # (A boss counts whatever the objective calls him: 'Defeat Source of
        # Corruption' is Tim-tim Snakeeye, and his losses never added up.)
        if target and fought and not bosses and not any(
                target.lower() in n.lower() or n.lower() in target.lower() for n in fought):
            logger.info(f"defeated by {', '.join(fought)}, not {target}: not counted against {objective!r}")
            return
        level = await self.client.stats.reference_level()
        quest = self._active_quest
        main = quest in self._mainline
        det = self._detour_names()
        # (Main or not: once set aside by the detour fallback the quest reads
        # as a side quest, and its losses went the side path, an hour each.)
        if quest and det is not None and norm(quest) in det[2]:
            # A detour world's fight (Jotun, Ullik and Grettir together in
            # Nidavellir): lost 15 times (the player: two tries is not enough),
            # it waits an hour and the main story
            # goes on meanwhile (the player: Celestia rather than dying all
            # night to it). A level-up brings it back sooner.
            n = self.setbacks.defeats.get(objective, 0) + 1
            self.setbacks.defeats[objective] = n
            limit = DETOUR_DEFEATS
            try:  # (state/detour.json 'defeats_before_wait': the player watching it try again and again)
                limit = int(json.loads(Path("state", "detour.json").read_text(encoding="utf-8"))
                            .get("defeats_before_wait", DETOUR_DEFEATS))
            except (OSError, ValueError, TypeError):
                pass
            if n >= limit and self._team_instead(quest, objective, n):
                self.setbacks.defeats.pop(objective, None)
            elif n >= limit and not self._detour_stays(quest):
                self.setbacks.defeats.pop(objective, None)
                self.setbacks.set_quest_aside(quest, objective, level, main=True,
                                              retry_after=DETOUR_RETRY_SECONDS)
                logger.warning(f"lost {objective!r} {n} times: {quest!r} waits 3 hours (or a level-up); "
                               "the main story meanwhile")
                self._alert_main_stuck(quest, f"lost {objective!r} {n} times", hard=True)
                self._recall_pending = False
                self._retire_dungeon_mark()
                self._ranked_for = None
                self._last_rank = -1e9
            else:
                # (A lost fight isn't a stall: only the loss count sets it aside.)
                self._last_progress_time = time.monotonic()
                logger.info(f"defeat {n}/{limit} on {objective!r}; trying again")
            self.setbacks.save()
            return
        if main:
            # The player's rule: a main-story fight is never set aside for
            # losses; the deck ladder (the other deck, then a simulator search)
            # takes over, and a lost fight isn't a stall either.
            n = self.setbacks.defeats.get(objective, 0) + 1
            self.setbacks.defeats[objective] = n
            self._last_progress_time = time.monotonic()
            ladder = getattr(self, "deck_adapter", None) is not None
            # (Set aside at this level before: back after the hour, Glauco won
            # again; fewer tries until a level-up makes the wizard stronger.)
            again = f"set aside at level {level}: {objective}"
            limit = MAIN_LOSSES_RETRY_SAME_LEVEL if again in self.setbacks.defeats else MAIN_LOSSES_NO_LADDER
            if n >= MAIN_LOSSES_NO_LADDER and quest and self._team_instead(quest, objective, n):
                self.setbacks.defeats.pop(objective, None)
                self.setbacks.save()
                return
            if not ladder and n >= limit and quest:
                self.setbacks.defeats[again] = 1
                # One deck for every fight (combat.adapt_deck: false): nothing
                # changes between tries, so trying again only loses again
                # (Glauco and the Angler Warlord, 5 in a row). The player: really
                # stuck, side quests meanwhile; back at a level-up or in an hour.
                self.setbacks.defeats.pop(objective, None)
                self.setbacks.set_quest_aside(quest, objective, level, main=True,
                                              retry_after=MAIN_LOSSES_RETRY_SECONDS)
                self.setbacks.save()
                logger.warning(f"lost {objective!r} {n} times with the one deck: {quest!r} waits for a "
                               "level-up (or an hour); side quests meanwhile")
                self._alert_main_stuck(quest, f"lost {objective!r} {n} times", hard=True)
                self._recall_pending = False
                self._retire_dungeon_mark()
                self._ranked_for = None
                self._last_rank = -1e9
                return
            self.setbacks.save()
            why = ("the deck ladder decides the deck" if ladder
                   else f"{limit - n} more before it waits")
            logger.info(f"defeat {n} on {objective!r}; trying again ({why})")
            return
        if self.setbacks.record_defeat(objective, quest, level, main=main):
            tries = MAIN_DEFEATS_TO_DEFER if main else DEFEATS_TO_DEFER
            until = f"level {level + 1}" if main else f"level {level + 1} or an hour"
            logger.warning(
                f"lost {objective!r} {tries} times: setting {quest!r} aside until {until}; "
                "doing other quests meanwhile"
            )
            if main:
                self._alert_main_stuck(quest, f"lost {objective!r} {tries} times", hard=True)
            self._recall_pending = False  # no point recalling to it now
            self._retire_dungeon_mark()
            self._ranked_for = None
            self._last_rank = -1e9  # re-rank quests on this step
        else:
            n = self.setbacks.defeats.get(objective, 0)
            # (A lost fight isn't a stall: 'Call of Cablooey' was set aside by
            # the 4-min clock after one loss, the fight itself taking 4 min.)
            self._last_progress_time = time.monotonic()
            logger.info(f"defeat {n} on {objective!r}; trying again")
        self.setbacks.save()

    @property
    def _recall_pending(self) -> bool:
        return self._recall_pending_flag and bool(self._mark and self._mark.kind in RECALL_KINDS)

    @_recall_pending.setter
    def _recall_pending(self, value: bool):
        self._recall_pending_flag = bool(value)
        try:
            if value:
                RECALL_PENDING_FILE.parent.mkdir(exist_ok=True)
                RECALL_PENDING_FILE.write_text(self._mark.zone if self._mark else "", encoding="utf-8")
            else:
                RECALL_PENDING_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    async def _recall_to_mark(self) -> bool:
        """Back at full strength after a defeat, still on the same objective: use
        Recall to jump back to the marked dungeon entrance. True if we recalled."""
        if not self._mark or not self._recall_pending:
            return False  # only after a defeat or a heal trip: otherwise we left on purpose
        marked_zone = self._mark.zone
        zone = await self.client.zone_name()
        if zone == marked_zone:
            self._recall_pending = False  # (back already)
            return False
        if await self._in_dungeon(zone or "") and self._dungeon:
            first = self._dungeon[1]
            if marked_zone == first or in_same_area(marked_zone, first):
                # A defeat in the Labyrinth respawned us in its hall: still in
                # the dungeon, where Recall to its other room is refused.
                logger.info(f"already inside the marked dungeon ({zone.split('/')[-1]}): no Recall needed")
                self._recall_pending = False
                return False
        # Whatever quest the game tracks now: a defeat takes us out of the
        # dungeon, and the quest shown changes with it (the player's rule:
        # heal, then back to the mark, no matter what).
        if not await is_free(self.client):
            return False
        if self.upkeep is not None:
            hp, mana = await health_mana(self.client)
            if self.upkeep.needs_recovery(hp, mana):
                return False  # healed first (no mark), then back to the mark (the player's rule)
        what = {"dungeon": "the dungeon entrance", "fight": "the spot marked before the fight"}.get(
            self._mark.kind, "the marked spot")
        logger.info(f"recalling to {what} in {marked_zone} before anything else")
        kind = self._mark.kind
        if await self._recall(marked_zone, what):
            self._recall_fails = 0
            self._recall_pending = False
            if kind == "dungeon":
                self._retire_dungeon_mark()  # the dungeon resets: its sigil is a fresh start
            return True
        # (A fight started, or the timer: try again next step, but never loop.)
        self._recall_fails = getattr(self, "_recall_fails", 0) + 1
        if self._recall_fails >= RECALL_TRIES:
            logger.warning(f"Recall to {marked_zone} failed {self._recall_fails} times; giving it up "
                           "(and the mark: the game has none there)")
            self._recall_fails = 0
            self._recall_pending = False
            # (Kept, every restart tried it again: Ogun's shack, long after him.)
            self._mark = None
            save_mark(None)
        return True

    async def _resume_instance(self, marked_zone: str) -> bool:
        """The compass's dungeon-return button (a red X beside Recall, shown
        after a defeat throws us out of a dungeon): back into the dungeon we
        left. Recall into the Labyrinth from the Basilica did nothing. True if
        it took us into the marked zone or another room of its dungeon."""
        before = await self.client.zone_name() or ""
        note_zone_jump()
        if not await ui.click_named(self.client, "ResumeInstanceButton"):
            return False
        logger.info("pressed the dungeon-return button (back into the dungeon we left)")
        self.controller.allow_idle(40)
        try:
            await asyncio.sleep(1.0)
            await ui.confirm_modal(self.client)
            await wait_for_loading(self.client, appear_timeout=8.0)
        finally:
            self.controller.end_idle()
        here = await self.client.zone_name() or ""
        if here == before:
            logger.warning("the dungeon-return button didn't move us")
            return False
        inside = here == marked_zone or in_same_area(here, marked_zone) or in_same_area(marked_zone, here)
        if not inside:
            logger.warning(f"the dungeon-return button took us to {here}, not {marked_zone}'s dungeon")
        return inside

    async def _recall(self, marked_zone: str, what: str = "the mark") -> bool:
        """Press Recall and wait to arrive in `marked_zone`. True if we did."""
        if is_team_up_zone(marked_zone) and not is_team_up_zone(await self.client.zone_name() or ""):
            # Recalling into a team-only dungeon means going in alone.
            logger.info(f"not recalling into {marked_zone} alone (team-only dungeon)")
            return False
        if await self._resume_instance(marked_zone):
            return True
        self.controller.allow_idle(40)
        try:
            for attempt in range(2):
                timer = await ui.named_text(self.client, "txtRecallTimer")
                note_zone_jump()
                if not await ui.click_named(self.client, "RecallButton"):
                    logger.warning("no Recall button to click")
                    return False
                logger.debug(f"clicked Recall (try {attempt + 1}; timer text {timer!r})")
                # The teleport plays a short animation before the loading screen.
                deadline = time.monotonic() + RECALL_WAIT
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    box = await ui.modal_box(self.client)
                    if box:
                        text = await ui.modal_text(box)
                        logger.info(f"recall message: {text[:120]!r}")
                        await ui.confirm_modal(self.client)
                        if "cannot teleport" in text.lower():
                            # e.g. the dungeon reset while we were away healing;
                            # otherwise a dungeon Recall can't come back into:
                            # remembered (mark at its entrance, potions there).
                            from .dungeons import learn_no_return, refusal_means_no_return

                            if (refusal_means_no_return(text) and await self._is_dungeon_zone(marked_zone)
                                    and learn_no_return(marked_zone)):
                                logger.warning(f"{marked_zone.split('/')[-1]} can't be Recalled into: "
                                               "a no-return dungeon from now on (mark at its entrance)")
                            logger.warning("the game refused the recall; walking back instead")
                            if "has been reset" in text.lower():
                                # Final: the dungeon is gone (twice more the same
                                # Recall and the same refusal, and every heal
                                # trip after tried it again): forget the mark.
                                self._recall_pending = False
                                self._mark = None
                                save_mark(None)
                            return False
                    if await self.client.is_loading():
                        await wait_for_loading(self.client)
                    if await self.client.zone_name() == marked_zone:
                        self._teleported = True
                        logger.success(f"recalled to {what}")
                        return True
                    if not await is_free(self.client):
                        return False
            timer = await ui.named_text(self.client, "txtRecallTimer")
            logger.warning(f"recall didn't take us to {what} (recall timer text {timer!r})")
            return False
        finally:
            self.controller.end_idle()

    async def _remember_dungeon(self, outside: str | None, sigil: XYZ):
        """Note where this dungeon's sigil is and where/which way we arrive inside
        (the exit is behind the arrival point): boss farming reuses it."""
        try:
            interior = await self.client.zone_name()
            pos = await self._position()
            yaw = await self.client.body.yaw()
            DungeonMemory.load().record_entry(
                interior,
                DungeonEntry(outside or "", (sigil.x, sigil.y, sigil.z), (pos.x, pos.y, pos.z), yaw),
            )
        except Exception as exc:
            logger.debug(f"could not remember the dungeon: {exc!r}")

    async def _far_spot(self, sigil: XYZ) -> XYZ:
        """An on-map spot well outside the sigil's area: a remembered wisp spot or
        a landmark at least SIGIL_LEAVE away (nearest such), else straight back."""
        zone = await self.client.zone_name() or ""
        candidates = list(wisp_memory().spots.get(zone, [])) + await landmarks(self.client)
        # Landing next to a mob starts a fight (and cancels the whole attempt).
        candidates = away_from(candidates, await mob_positions(self.client), SIGIL_LEAVE_MOB_DISTANCE)
        far = [p for p in candidates if math.dist((p[0], p[1]), (sigil.x, sigil.y)) >= SIGIL_LEAVE]
        if far:
            p = min(far, key=lambda p: math.dist((p[0], p[1]), (sigil.x, sigil.y)))
            return XYZ(*p)
        return XYZ(sigil.x + SIGIL_LEAVE, sigil.y, sigil.z)

    async def _clear_spot(self, p: XYZ) -> bool:
        """No enemy within MOB_CLEARANCE of `p` right now (mobs patrol, so this
        is read fresh before each teleport)."""
        mobs = [XYZ(*m) for m in await mob_positions(self.client)]
        return clear_of(p, mobs, MOB_CLEARANCE)

    async def _walk_in_from_around(self, target: XYZ, zone: str | None) -> bool:
        """Standing on a door marker gives walk_through no direction; back off
        to each side in turn and walk through the marker from there."""
        for i in range(4):
            ang = i * math.pi / 2
            spot = XYZ(target.x + 300 * math.cos(ang), target.y + 300 * math.sin(ang), target.z)
            if not await self._clear_spot(spot):
                continue
            await self.client.teleport(spot)
            await asyncio.sleep(TELEPORT_SETTLE)
            if await self._zone_changed(zone):
                return True
            if distance(await self._position(), target) < 50:
                continue  # teleport rejected; still on the marker
            if await self.walk_through(target, zone):
                return True
        # In a small room every spot 300 away is behind a wall (the Post
        # Office's exit): back up a step on foot in each direction and walk
        # through the marker from there.
        from wizwalker.utils import calculate_perfect_yaw

        logger.info("backing off the door marker on foot to walk through it")
        for i in range(8):
            ang = i * math.pi / 4
            away = XYZ(target.x + 100 * math.cos(ang), target.y + 100 * math.sin(ang), target.z)
            await self.client.body.write_yaw(calculate_perfect_yaw(target, away))
            await self.client.send_key(Keycode.W, 0.5)  # a step away from the marker
            if await self._zone_changed(zone):
                return True
            if distance(await self._position(), target) < 40:
                continue  # a wall on that side
            if await self.walk_through(target, zone):
                return True
        logger.warning("could not walk through the door marker from any side")
        return False

    async def travel(self, target: XYZ, avoid_mobs: bool = True, npc: bool = False) -> bool:
        """Get within interact range of `target`. Returns True on success.

        With `avoid_mobs`, a teleport never lands next to an enemy (that starts
        an unplanned fight): it lands at the nearest clear spot and walks in.
        `npc`: the target is someone to talk to, not a door: never walk
        "through" it; inch closer and look for the talk prompt instead."""
        start = await self._position()
        if distance(start, target) <= 5:
            return True
        zone = await self.client.zone_name()
        mobs = [XYZ(*m) for m in await mob_positions(self.client)] if avoid_mobs else []

        if not self.cfg.teleport:
            await self.client.goto(target.x, target.y)
            if await self._zone_changed(zone):
                return True
            return distance(await self._position(), target) < INTERACT_RANGE

        if mobs and not clear_of(target, mobs, MOB_CLEARANCE):
            spot = safe_landing(target, start, mobs, MOB_CLEARANCE)
            if spot is not None:
                logger.info(f"enemies near the destination; landing {distance(spot, target):.0f} away")
                await self.client.teleport(spot)
                await asyncio.sleep(TELEPORT_SETTLE)
                if await self._zone_changed(zone):
                    return True
                await self.client.goto(target.x, target.y)
                if await self._zone_changed(zone) or await self.client.in_battle():
                    return True
                if distance(await self._position(), target) < INTERACT_RANGE:
                    return True
                # Didn't get there on foot (a wall, a door): fall back to the usual way.

        await self.client.teleport(target)
        await asyncio.sleep(TELEPORT_SETTLE)
        if await self._zone_changed(zone):
            return True  # the teleport itself went through a zone transition
        if distance(await self._position(), start) > BOUNCE_DISTANCE:
            await self._clear_of_enemies()
            return True
        if distance(await self._position(), target) < INTERACT_RANGE:
            return True  # hardly moved because we were close already: not a rejection
        if teleport_aborted(self.client):
            # Held back (or jumped back) from enemies at the spot: walking there
            # instead would run through them. Try again next step.
            return False

        objective = self._last_progress[0] or ""
        # The objective's place is this zone: the marker is the target itself
        # (an NPC, an object, a spot), not a door, so a refused teleport means
        # it landed inside something. A ring of small offsets around it first,
        # before any door walking (that took minutes at the brazier, the tablet
        # and 'Go To Village of Sorrow').
        if objective and objective_zone(objective) == zone and await self._land_beside(target, mobs):
            return True
        if (
            npc and distance(await self._position(), target) < NPC_INCH_RANGE
            and self._may_try(objective, zone or "", "inch")
        ):
            return await self._inch_toward(target)
        # Far from the marker with the teleport refused: it's a door (Zan'ne's
        # building in the Oasis), whatever the objective says: walk through it.
        # A door walked through before: straight to where that walk started.
        known_door = self.doors.approach(zone or "", (target.x, target.y, target.z))
        if known_door is not None:
            logger.info("a door walked through before: going to where that walk started")
            await self.client.teleport(XYZ(*known_door))
            await asyncio.sleep(TELEPORT_SETTLE)
            if await self._zone_changed(zone) or await self.walk_through(target, zone):
                return True
        # A spot near it where a teleport worked before: start from there.
        from .tpspots import spots

        good = spots().near(zone or "", (target.x, target.y, target.z), TP_SPOT_NEAR)
        if good and distance(await self._position(), XYZ(*good[0])) > 300:
            logger.info(f"teleport refused; trying a spot a teleport worked at before "
                        f"({good[0][0]:.0f}, {good[0][1]:.0f})")
            await self.client.teleport(XYZ(*good[0]))
            await asyncio.sleep(TELEPORT_SETTLE)
            if await self._zone_changed(zone):
                return True
        # "Go To Village of Sorrow in Village of Sorrow", already in it: the
        # marker is a spot to stand on, not a door (walking 'through' it went
        # on for minutes, through a teleporter and back). Land around it and
        # walk onto it.
        if (objective and _GO_TO.match(objective) and objective_zone(objective) == zone
                and self._may_try(objective, zone or "", "go_to_spot")):
            logger.info("a spot to reach in this zone: landing beside it and walking onto it")
            for radius in (200.0, 450.0, 800.0):
                for i in range(8):
                    a = i * math.pi / 4
                    near = XYZ(target.x + radius * math.cos(a), target.y + radius * math.sin(a), target.z)
                    if avoid_mobs and not await self._clear_spot(near):
                        continue
                    await self.client.teleport(near)
                    await asyncio.sleep(0.5)
                    if distance(await self._position(), near) < 150:
                        await self.client.goto(target.x, target.y)
                        await asyncio.sleep(1.0)
                        return True
            return False
        # "Use Brazier" (Cave of Solitude): the teleport onto the object is
        # refused, but it isn't a door; walking through it from every side took
        # 5 minutes. Walk up to it and press X.
        # (And "Collect Ice Water in Jar": the jar refused the teleport, and the
        # walk path ran under Ravenscar's map, stuck at z 0 for minutes.)
        if (objective and (_USE_OBJECT.match(objective) or objective.lower().startswith("collect "))
                and await self._object_at_marker(objective, target)
                and self._may_try(objective, zone or "", "use_walk")):
            logger.info("an object to use, the teleport onto it refused: from beside it, pressing X")
            if await self._use_object_at(target):
                return True
            # No prompt anywhere around it: inside an instance the room's
            # enemies may have to go first (the Mantra 2 tablet opened once the
            # Kakeda Shadows were beaten). Then the object again.
            # Only when we stood beside it: never reaching it (the hops
            # refused, Crystal Storage in the Labyrinth) isn't a locked object,
            # and the "guard" was a boss across the room.
            reached = getattr(self, "_object_reached", False)
            if reached and "/interiors/" in (zone or "").lower() and await self._fight_guards(
                target, "object", reach=float("inf")
            ):
                logger.info("the object gave no prompt; fought the room's enemies first")
                return True
        # Rejected: usually a door/zone exit, or a spot inside collision.
        logger.info("teleport was rejected (door or blocked spot); teleporting beside it")
        if await self.approach_and_walk(target, zone):
            return True

        logger.debug("approach failed; trying points around the objective")
        for radius in (120, 250, 400):
            for i in range(8):
                ang = i * math.pi / 4
                p = XYZ(target.x + radius * math.cos(ang), target.y + radius * math.sin(ang), target.z)
                # These spots are in this zone's coordinates: stop once a hop has
                # carried us through the door, and check enemies fresh each time
                # (they patrol; a stale list put the wizard on Desert Golems).
                if await self.client.zone_name() != zone:
                    await self._clear_of_enemies()
                    return True
                if avoid_mobs and not await self._clear_spot(p):
                    continue
                await self.client.teleport(p)
                await asyncio.sleep(0.5)
                if distance(await self._position(), start) > BOUNCE_DISTANCE:
                    await self._clear_of_enemies()
                    return True
        # No walk after all that (the player: keep teleporting; walks after a
        # refused teleport ran into fights and fled in a loop).
        return distance(await self._position(), target) < INTERACT_RANGE

    async def _inch_toward(self, target: XYZ, steps: int = 4, step: float = 150.0) -> bool:
        """Short walks toward an NPC (one on a raised platform rejects a
        teleport onto it), checking for the talk prompt after each: no long
        runs past it into enemies."""
        logger.info("teleport onto the NPC was rejected; inching closer")
        for _ in range(steps):
            if await ui.is_visible(self.client, ui.NPC_RANGE):
                return True
            here = await self._position()
            gap = distance(here, target)
            if gap < 60:
                break
            f = min(1.0, step / gap)
            await self.client.goto(here.x + (target.x - here.x) * f, here.y + (target.y - here.y) * f)
            await asyncio.sleep(0.6)
            if not await is_free(self.client):
                return True
        if await ui.is_visible(self.client, ui.NPC_RANGE):
            return True
        return distance(await self._position(), target) < INTERACT_RANGE

    # --- interaction ---------------------------------------------------------

    async def interact(self, objective: str = "") -> bool:
        """Press X on whatever prompt is showing. Returns True if something happened."""
        if not await ui.is_visible(self.client, ui.NPC_RANGE):
            return False
        prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
        if objective.lower().startswith("locate") and "activate" in prompt:
            # A Locate is done by walking to the spot; the pad beside Junho
            # Shan sent us back to the village entrance every time.
            logger.info(f"not pressing X on '{prompt}': a Locate only needs us there")
            return False
        thing = operate_target(objective)
        if "talk" in prompt and thing and not await self._nearest_is(thing):
            # "Use Forge": the prompt is the NPC beside it (Xihong Bi), not the
            # Forge; talking to them again looped for minutes. (Unless the
            # object itself talks: the Water Breathing Device's prompt is
            # 'press x to talk', and refusing it stalled the quest.)
            logger.info(f"not pressing X on '{prompt}': the objective is to use something")
            return False
        logger.info(f"interacting: {prompt or '(no text)'}")

        if "to enter" in prompt:
            # A dungeon sigil: X starts a countdown that any later movement
            # cancels, so let the sigil routine press it and stand still.
            sigil = await self._sigil_at(await self._position(), SIGIL_NEAR_RANGE)
            if sigil is not None:
                return await self._enter_by_sigil(sigil, await self.client.zone_name())

        if self.dialogue and "talk" in objective.lower():
            # This is the NPC the quest helper sent us to: accept what they offer.
            self.dialogue.accept_offers_for(30)
        if objective.lower().startswith("talk") and talk_target(objective):
            self._last_talk = (talk_target(objective), await self.client.zone_name() or "", time.monotonic())
        await self.client.send_key(Keycode.X, 0.1)
        # Move on as soon as something opens (dialogue, a menu, a loading
        # screen), not after a set second.
        for _ in range(10):
            await asyncio.sleep(0.1)
            opened = not await is_free(self.client) or await self.services.is_open()
            if opened or await self.client.is_loading():
                break

        if "to enter" in prompt:
            # Dungeon warning ("you can't leave once you enter...")
            for _ in range(10):
                if await ui.confirm_modal(self.client):
                    break
                if await self.client.is_loading():
                    break
                await asyncio.sleep(0.3)
        elif "to talk" in prompt or await self.services.is_open():
            # The dialogue loop advances the conversation; wait for it to end,
            # including follow-up dialogues that open straight after. NPCs with
            # several quests first show a services menu to pick from.
            quiet_since = time.monotonic()
            picks = 0
            while time.monotonic() - quiet_since < TALK_QUIET_SECONDS:
                await self.controller.checkpoint()
                if picks < 3 and await self.services.is_open():
                    if self.dialogue:
                        self.dialogue.accept_offers_for(30)
                    if await self.services.choose(objective):
                        picks += 1
                        quiet_since = time.monotonic()
                        await asyncio.sleep(1.5)
                        continue
                    break  # nothing left to try; close_menus below shuts it
                if not await is_free(self.client):
                    quiet_since = time.monotonic()
                await asyncio.sleep(0.2)

        await wait_for_loading(self.client)
        await asyncio.sleep(0.2)

        if await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
            # World gate: the quest destination is preselected, just go.
            while await ui.click(self.client, ui.SPIRAL_DOOR_TELEPORT):
                await asyncio.sleep(0.3)
            await wait_for_loading(self.client)

        if self.progression:
            await self.progression.handle_trainer()
        closed = await ui.close_menus(self.client)
        if closed:
            logger.debug(f"closed {closed} menu(s)")
        return True

    def cancel_step(self):
        """Abort the step in progress (used by the stall watchdog)."""
        if self._step_task and not self._step_task.done():
            self._step_task.cancel()

    def reset_objective_memory(self):
        """Forget per-objective assumptions so the next step starts fresh."""
        self.services._tried.clear()
        self._swept_for = None
        self._attempts = 0
        self._fallback_tried_for = None
        self.collector._taken.clear()
        self.collector._cache = None

    async def run_step(self):
        """Run one step as a cancellable task so the watchdog can abort a hung step."""
        async with self.lock:
            self._step_task = asyncio.create_task(self.step())
            try:
                await self._step_task
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # we're being shut down, not just the step
                logger.debug("quest step cancelled by the watchdog")
            finally:
                self._step_task = None

    async def _count_attempt(self):
        """Several tries on the same objective with no change: the tracked quest
        is probably blocked on another active quest, so switch to that one."""
        self._attempts = getattr(self, "_attempts", 0) + 1
        if self._attempts >= SWITCH_QUEST_AFTER and getattr(self, "_active_is_main", False):
            # Not the main story: the next slot in the book may be in another
            # world (Locate Junho Shan -> Emily Chesterfield in Marleybone).
            # Its own fallbacks (and the stall rule) handle it.
            self._attempts = 0
            return
        if self._attempts >= SWITCH_QUEST_AFTER:
            self._attempts = 0
            await asyncio.sleep(2.0)  # let a late objective update land first
            await self.switch_quest()

    async def _open_quest_book(self) -> bool:
        for _ in range(4):
            if await ui.is_visible(self.client, QUEST_BOOK_ALL):
                break
            await self.client.send_key(Keycode.Q, 0.1)
            await asyncio.sleep(0.8)
        else:
            return False
        await ui.click(self.client, QUEST_BOOK_ALL)
        await asyncio.sleep(0.5)
        return True

    async def _close_quest_book(self):
        for _ in range(4):
            if not await ui.is_visible(self.client, QUEST_BOOK_ALL):
                return
            await self.client.send_key(Keycode.Q, 0.1)
            await asyncio.sleep(0.8)

    async def _read_quest_page(self) -> list[QuestEntry]:
        """The quest book page on screen. Read the fast way (each slot's
        fields in one walk); the first page of a run is also read the old way
        (a lookup from the root per field) and a mismatch switches back to it."""
        if self._book_reader == "slow":
            return await self._read_quest_page_slow()
        fast = await self._read_quest_page_fast()
        if self._book_reader == "check":
            slow = await self._read_quest_page_slow()
            if [dataclasses.astuple(q) for q in fast] != [dataclasses.astuple(q) for q in slow]:
                logger.warning(f"fast quest book read differs ({[q.name for q in fast]} vs "
                               f"{[q.name for q in slow]}); reading it the slow way")
                self._book_reader = "slow"
                return slow
            self._book_reader = "fast"
        return fast

    async def _turn_page(self, button: list[str]) -> bool | None:
        """Click a quest book page button and wait until the page shows other
        quests (not a set time). None if the button wasn't there; False if the
        page still looked the same after 1.5 s (the first/last page, or slow)."""
        first = await self._first_book_name()
        if not await ui.click(self.client, button):
            return None
        for _ in range(15):
            await asyncio.sleep(0.1)
            if await self._first_book_name() != first:
                return True
        return False

    async def _first_book_name(self) -> str:
        panel = await ui.window_at(self.client, QUEST_LIST)
        if panel is None:
            return ""
        slot = (await ui.find_named(panel, {"wndQuestInfo0"}, max_depth=2)).get("wndQuestInfo0")
        if slot is None:
            return ""
        return await ui.window_text((await ui.find_named(slot, {"txtName"}, max_depth=4)).get("txtName"))

    async def _read_quest_page_fast(self) -> list[QuestEntry]:
        zone = await self.client.zone_name() or ""
        panel = await ui.window_at(self.client, QUEST_LIST)
        if panel is None:
            return []
        slots = await ui.find_named(panel, {f"wndQuestInfo{i}" for i in range(MAX_QUEST_SLOTS)}, max_depth=2)
        out = []
        for i in range(MAX_QUEST_SLOTS):
            slot = slots.get(f"wndQuestInfo{i}")
            if slot is None:
                continue
            base = slot
            for part in ("questInfoWindow", "wndQuestInfo"):
                base = (await ui.find_named(base, {part}, max_depth=2)).get(part) if base else None
            if base is None:
                continue
            w = await ui.find_named(base, BOOK_FIELDS, max_depth=4)
            name = await ui.window_text(w.get("txtName"))
            if not name:
                continue
            world = await ui.window_text(w.get("txtWorld"))
            reward = await ui.window_text(w.get("txtReward1Amount"))
            out.append(
                QuestEntry(
                    slot=i,
                    name=name,
                    world=world,
                    goal=await ui.window_text(w.get("txtGoal")),
                    zone=await ui.window_text(w.get("txtZone")),
                    target=await ui.window_text(w.get("txtGoalObjective1")),
                    fight=await ui.window_visible(w.get("imgEncounter")),
                    counted=await ui.window_visible(w.get("txtGoalCounter")),
                    hops=hops_to_place(zone, world) if world else None,
                    activity=await ui.window_visible(w.get("imgActivityQuestType")),
                    mainline=await ui.window_visible(w.get("LeftMainline")),
                    active=await ui.window_visible(w.get("imgActiveQuest")),
                    reward=int(reward) if reward.strip().isdigit() else 0,
                )
            )
        return out

    async def _read_quest_page_slow(self) -> list[QuestEntry]:
        zone = await self.client.zone_name() or ""
        out = []
        for i in range(MAX_QUEST_SLOTS):
            base = [*QUEST_LIST, f"wndQuestInfo{i}", "questInfoWindow", "wndQuestInfo"]
            name = (await ui.text_at(self.client, [*base, "txtName"])).strip()
            if not name:
                continue
            reward_path = [*base, "wndReward1", "imgReward1Scroll", "txtReward1Amount"]
            reward = await ui.text_at(self.client, reward_path)
            world = (await ui.text_at(self.client, [*base, "txtWorld"])).strip()
            goal = (await ui.text_at(self.client, [*base, "txtGoal"])).strip()
            target = (await ui.text_at(self.client, [*base, "txtGoalObjective1"])).strip()
            if not self._book_dumped and i == 0:
                # The goal text came back empty for most quests: save the
                # slot's layout once to find where it lives.
                self._book_dumped = True
                win = await ui.window_at(self.client, [*QUEST_LIST, f"wndQuestInfo{i}"])
                if win is not None:
                    lines = await ui.dump_tree(win, max_depth=10, only_visible=False, with_types=True)
                    Path("state", "quest_book_window.txt").write_text(
                        "\n".join(lines), encoding="utf-8", errors="replace"
                    )
            out.append(
                QuestEntry(
                    slot=i,
                    name=name,
                    world=world,
                    goal=goal,
                    zone=(await ui.text_at(self.client, [*base, "txtZone"])).strip(),
                    target=target,
                    fight=await ui.is_visible(self.client, [*base, "imgEncounter"]),
                    counted=await ui.is_visible(self.client, [*base, "txtGoalCounter"]),
                    hops=hops_to_place(zone, world) if world else None,
                    activity=await ui.is_visible(self.client, [*base, "imgActivityQuestType"]),
                    mainline=await ui.is_visible(self.client, [*base, "LeftMainline"]),
                    active=await ui.is_visible(self.client, [*base, "imgActiveQuest"]),
                    reward=int(reward) if reward.strip().isdigit() else 0,
                )
            )
        return out

    async def prioritize_quests(self) -> bool:
        """Track the quest `choose_quest` picks (keep the current questline unless a
        spell quest is waiting). True if it switched."""
        if not await self._open_quest_book():
            logger.warning("could not open the quest book to rank quests")
            return False
        page_button = [*QUEST_LIST, "btnNextPage"]
        back_button = [*QUEST_LIST, "btnPrevPage"]
        all_quests: list[tuple[int, QuestEntry]] = []
        pages = 0
        complete = False  # read through to the last page
        try:
            # Every page: with 5 pages at most, 'Fetch Bones!' (main) sat past
            # the end, and quests pushed off the read counted as completed.
            for page in range(MAX_BOOK_PAGES):
                known = {q.name for _, q in all_quests}
                raw = await self._read_quest_page()
                entries = [e for e in raw if e.name not in known]
                if not entries and raw and all_quests:
                    # The same page again: the last page, or a slow turn (one
                    # read page 1 twice, took it for the end, and 'Armed to the
                    # Gills' on page 2 "left" the book: a false "no main quest"
                    # alert). Another look a second later catches a slow turn;
                    # still the same, it's the end. (Calling it incomplete made
                    # every read incomplete: the last page never turns.)
                    await asyncio.sleep(1.0)
                    raw = await self._read_quest_page()
                    entries = [e for e in raw if e.name not in known]
                    if not entries:
                        complete = True
                        break
                if not entries:
                    # An empty book while there's an objective is a failed read
                    # (after a public fight in the Plaza of Conquests it read []
                    # and raised a false "no main quest" alert).
                    complete = bool(all_quests) or not await self.objective()
                    break
                pages = page + 1
                all_quests += [(page, e) for e in entries]
                if await self._turn_page(page_button) is None:
                    complete = True
                    break
                # (Unchanged after the wait: the next read finds nothing new
                # and ends it; a slow page still gets read.)
            if complete and await self._in_dungeon(await self.client.zone_name() or ""):
                # Inside a dungeon the book can leave quests out (the Haunted
                # Cave's Halloween dungeon showed 4, Wizard Tours not among
                # them): twice a false "no main quest" alert, and the pin
                # dropped. Such a read decides nothing is done.
                complete = False
            if complete:
                from .teamup import drop_team_dungeons

                for d in drop_team_dungeons({q.name for _, q in all_quests}):
                    logger.info(f"{d.split('/')[-1]}'s quest is done: alone there again")
            det = self._detour_names()
            side_quest_world = self.cfg.side_quest_world.strip()
            side_quest_names: set[str] = set()
            if side_quest_world:
                from .givers import load_guide

                side_quest_names = {
                    norm(q.name) for q in (load_guide(side_quest_world) or []) if not q.main
                }
                for _, q in all_quests:
                    if norm(q.name) in side_quest_names:
                        q.mainline = False
            game_main = {id(q): q.mainline for _, q in all_quests}
            # None of the world's own quests in the book yet: its lead-in (the
            # quests before its list starts) is whatever the game calls main.
            # Only before the world has begun: once one of its listed quests is
            # done, a gap in its story isn't a lead-in ('The Spiral Cup', main
            # to the game, was taken for Zafaria's story after 'Meddling Wizards').
            lead_in = det is not None and not any(norm(q.name) in det[2] for _, q in all_quests)
            if lead_in:
                from .questlist import load_completed

                start = norm((det[0].get("start") or {}).get("quest", ""))
                begun = {norm(n) for n in load_completed()} & (det[2] - {start})
                lead_in = not begun
            for _, q in all_quests:
                if det is not None and lead_in:
                    pass  # (the game's main-story flag stands)
                elif det is not None:
                    # A detour (state/detour.json: Grizzleheim, then Wintertusk,
                    # before Celestia): its world's story is the main story now,
                    # everything else waits.
                    # The game's main-story flag, for quests on that world's list
                    # (a 'SIDE ×N' tag on the list isn't 'side quest': the
                    # player's Celestia guide has Explorer101 as main story).
                    q.mainline = game_main[id(q)] and norm(q.name) in det[2]
                elif q.mainline and in_side_world(q):
                    q.mainline = False  # (a side world's story: a side quest here)
            if det is not None and complete:
                has = any(norm(q.name) in det[2] or (lead_in and game_main[id(q)]) for _, q in all_quests)
                self._detour_start(det[0], has)
                await self._note_detour_gap(has)
            activities = {q.name for _, q in all_quests if q.activity}
            self._wanted_items = {  # main-story/spell quests only: side quests are ignored
                collect_item_name(q.goal): q.name
                for _, q in all_quests
                if collect_item_name(q.goal) and (q.mainline or q.activity)
            }
            # A quest missing from a partial read isn't done: only a full read counts.
            done = self.completions.update({q.name for _, q in all_quests}) if complete else set()
            if not complete:
                logger.warning(f"read {len(all_quests)} quests over {pages} pages without reaching the end")
            for name in done:
                listed = self.quest_order.get(norm(name))
                where = f" (#{listed.index} on the quest list)" if listed else ""
                logger.success(f"quest completed: {name!r}{where}")
            self.completions.log(done)
            active = next((q for _, q in all_quests if q.active), None)
            self._active_quest = active.name if active else self._active_quest
            level = await self.client.stats.reference_level()
            set_aside = self.setbacks.set_aside(level)
            detour_mains = [q for _, q in all_quests if q.mainline] if det is not None else []
            if (detour_mains and all(q.name in set_aside for q in detour_mains)
                    and not any(self._detour_stays(q.name) for q in detour_mains)):
                # The detour's quest is stuck (set aside after trying every
                # way): the main story (Celestia) meanwhile, not grinding (the
                # player); the detour comes back when its quest is retried.
                logger.info(f"detour quest {detour_mains[0].name!r} set aside as stuck: "
                            "the main story meanwhile")
                for _, q in all_quests:
                    q.mainline = game_main[id(q)] and not in_side_world(q)
            elif det is not None and complete and not detour_mains and not self._detour_gap_pending():
                # No detour quest at all, its NPCs asked already: the main story
                # (Celestia) meanwhile, never grinding (the player).
                logger.info("no detour quest to follow: the main story meanwhile")
                self._detour_fallback = True
                for _, q in all_quests:
                    q.mainline = game_main[id(q)] and not in_side_world(q)
            if side_quest_world:
                for _, q in all_quests:
                    if norm(q.name) in side_quest_names:
                        q.mainline = False
            if any(q.active and q.mainline and not in_side_world(q) for _, q in all_quests):
                here_now = await self.client.zone_name() or ""
                if here_now and not here_now.startswith(("Grizzleheim", "WizardCity/Interiors")):
                    MAIN_STORY_ZONE_FILE.write_text(json.dumps({"zone": here_now, "asked": 0}),
                                                    encoding="utf-8")
            logger.debug(
                f"quest book: {[q.name for _, q in all_quests]}; set aside: {sorted(set_aside)}"
            )
            for _, q in all_quests:
                flags = "main" if q.mainline else "spell" if q.activity else "side"
                tracked = ", tracked" if q.active else ""
                step = f"{q.goal or q.target!r}{' (fight)' if q.fight else ''}"
                step += " (counted)" if q.counted else ""
                logger.debug(f"  {q.name!r} [{flags}{tracked}] {q.zone}/{q.world!r} {q.hops} hops: {step}")
            self._mainline = ({q.name for _, q in all_quests if q.mainline}
                              if not side_quest_world else set())
            if self._mainline:
                self._last_main = sorted(self._mainline)[0]
                self._last_main_zone = await self.client.zone_name() or ""
                _save_last_main(self._last_main, self._last_main_zone)
            elif complete and not side_quest_world and self._last_main_zone != "swept":
                # (After a restart: where we are now.)
                self._last_main_zone = self._last_main_zone or await self.client.zone_name() or ""
                # The main quest was just handed in and no next one came: its
                # giver is usually near (asked an hour ago, before it had it).
                logger.info(f"no main quest after {self._last_main!r}: asking the NPCs of "
                            f"{self._last_main_zone.split('/')[-1]} again")
                self.givers.sweep_now(self._last_main_zone)
                self._last_main_zone = "swept"
                _save_last_main(self._last_main, self._last_main_zone)
            alert_due = time.monotonic() - self._no_main_alerted > NO_MAIN_ALERT_SECONDS
            if complete and not side_quest_world and not self._mainline and alert_due:
                # The next main quest isn't in the book (after 'Weights and
                # Measures', 'The Last Meow' was never offered): side quests
                # meanwhile, but the player should know.
                self._no_main_alerted = time.monotonic()
                after = f" after {self._last_main!r}" if self._last_main else ""
                logger.warning(f"ALERT: main quest stuck: no main-story quest in the book{after}; "
                               "its giver wasn't found")
                self._no_main_reads += 1
                if self._no_main_reads >= 2:  # two full reads 30 min apart: really none
                    self._farm_when_stuck(self._last_main or "(none)", "no main-story quest in the book")
            names = {q.name for _, q in all_quests}
            if self._pin_new_from is not None and complete:
                # Class/spell quests first, then main story ('Mything Persons'
                # over an old side quest a short read had missed).
                new = [q for _, q in all_quests if q.name not in self._pin_new_from]
                new.sort(key=lambda q: (not q.activity, not q.mainline))
                if new:
                    self._pin = new[0].name
                    save_pin(self._pin)
                    others = ", ".join(repr(q.name) for q in new[1:])
                    logger.success(f"new quest from the visit: {new[0].name!r}; following it"
                                   + (f" (also new: {others})" if others else ""))
                self._pin_new_from = None
            prev_names = self._book_names
            if complete:
                self._book_names = names  # only a full read says what's in the book
                self._talk_again_after_turn_in(prev_names - names, names - prev_names)
            here = await self.client.zone_name() or ""
            # Side quests fill in only in the main quest's world (where it will be
            # picked up again at the next level), never a trip to another world.
            main_quests = [q for _, q in all_quests if q.mainline]
            main_world = next((w for w in map(quest_world, main_quests) if w), None)
            if main_world is None and main_quests:
                # The book's area name may be unknown; its objective can place it.
                zones = [objective_zone(q.goal) for q in main_quests if q.goal]
                main_world = next((z.split("/", 1)[0] for z in zones if z), None)
            world = main_world or self._main_world or (here.split("/", 1)[0] if here else None)
            if main_world is None and self._detour_names() is None and _side_world(world):
                # A side-world detour over (Wintertusk): the story goes on in
                # its own world (Celestia), side quests there rather than
                # grinding in Grizzleheim for want of a quest.
                from .questlist import story_world

                story = story_world()
                if story:
                    if self.__dict__.get("_story_logged") != story:
                        self._story_logged = story
                        logger.info(f"the detour is over: the main story's world is {story} again")
                    world = story
            if side_quest_world:
                world = side_quest_world
            self._main_world = side_quest_world or zone_world(world)
            hunted_out = time.monotonic() - getattr(self, "_hunt_exhausted", -1e9) < HUNT_EXHAUSTED_SECONDS
            if side_quest_world:
                chosen = choose_side_quest_in_world(
                    [q for _, q in all_quests], side_quest_world, side_quest_names, set_aside
                )
            else:
                chosen = choose_quest([q for _, q in all_quests], set_aside, self.quest_order, world,
                                      FALLBACK_SIDE_PLACES.get(zone_world(world) or "", ()),
                                      anywhere=hunted_out)
            grinding = chosen is None and (bool(all_quests) or bool(side_quest_world))
            if grinding and not self._grinding:
                logger.warning(f"nothing to do in {world}: fighting there for experience until a level-up")
                for q in main_quests:
                    self._alert_main_stuck(q.name, f"nothing left to do in {world}; grinding for a level")
            self._grinding = grinding
            if grinding and main_quests and not side_quest_world:
                # Track the main quest so its marker leads back into its world;
                # _grind fights outdoors there instead of taking on the boss.
                chosen = main_quests[0]
            chosen = self._apply_pin([q for _, q in all_quests], chosen, set_aside, prev_names, complete)
            if not side_quest_world:
                chosen = self._later_story_first(chosen, [q for _, q in all_quests], set_aside)
            # Mid-way through a quest (its objective moved on minutes ago): keep
            # it. 'Left Behind' was at Nomoonaga's Tower when a ranking outside
            # the dungeon switched to the pinned 'Oni No Death'.
            if (not side_quest_world and self._momentum
                    and time.monotonic() - self._momentum[1] < MOMENTUM_SECONDS):
                busy = next((q for _, q in all_quests if q.name == self._momentum[0]), None)
                # (Never over a class quest: 'Bone to be Wild' waited while
                # Wizard Tours was kept for being mid-way.)
                spell_first = bool(chosen and chosen.activity) and not (busy and busy.activity)
                # (Nor a side quest over the main story: the player's main
                # quest first; 'Bad Vacation' kept 'Ship of Tears' waiting.)
                busy_counts = bool(busy and (busy.mainline or busy.activity))
                main_first = bool(chosen and chosen.mainline) and not busy_counts
                if (busy is not None and busy is not chosen and busy.name not in set_aside
                        and not in_side_world(busy) and not spell_first and not main_first):
                    logger.info(f"keeping {busy.name!r}: mid-way through it "
                                f"({time.monotonic() - self._momentum[1]:.0f}s since its last step)")
                    chosen, self._grinding = busy, False
            # (A room entered by a door counts too: Nomoonaga's tower, after a
            # heal trip's Recall, went back to the main quest instead.)
            inside = is_team_up_zone(here) or await self._in_dungeon(here) or "/interiors/" in here.lower()
            if not inside:
                self._ranked_outside = True
            from .dungeons import load_done

            first_room = self._dungeon[1] if self._dungeon else here
            if inside and first_room in load_done():
                # A dungeon finished before (the Labyrinth, back in for a spell
                # quest's boss): its own quest doesn't come first again.
                if self._done_dungeon_logged != first_room:
                    self._done_dungeon_logged = first_room
                    logger.info(f"in {first_room.split('/')[-1]}, done before: "
                                "its own quest doesn't come first")
            elif inside:
                # After the pin: the dungeon's own quest ('The Right Combination')
                # opens the way to the pinned one ('Weird Science') in there.
                first = self._dungeon[1] if self._dungeon else here
                if self._entry_quest[0] != first:
                    # The quest tracked on entering this dungeon: none known
                    # after a restart inside it (the game then tracked what
                    # the bot had picked before, not what it entered with).
                    entered = self._active_quest if self._ranked_outside else ""
                    self._entry_quest = (first, entered or "")
                # In a building that isn't a dungeon (the Myth school) the
                # quest the game tracks isn't the room's own ('Give a Dog a
                # Bone', just accepted from Cyrus Drake, won over the class
                # quest picked).
                real = is_team_up_zone(here) or await self._in_dungeon(here)
                instances = story_instance_quests(here) if real else set()
                dungeon_candidates = ([q for _, q in all_quests if norm(q.name) in side_quest_names]
                                      if side_quest_world else [q for _, q in all_quests])
                local = dungeon_quest(dungeon_candidates, here, objective_zone, set_aside,
                                      self.setbacks.skipped,
                                      entered_with=self._entry_quest[1] if real else "",
                                      story_instances=instances)
                # A side quest of the dungeon's doesn't beat the main story (the
                # player: the main quest only; 'Tomb of the Zebra Kings' kept the
                # bot at Zanga Zebu over 'Into the Zebra Tomb'), except farming.
                # (A story INSTANCE quest is the main story's own step.)
                side_over_main = (local is not None and not local.mainline and chosen is not None
                                  and chosen.mainline
                                  and norm(local.name) not in instances
                                  and not Farm.load().active)
                if local and local is not chosen and not side_over_main:
                    logger.info(f"in the dungeon: {local.name!r} comes first (this dungeon's own quest)")
                    chosen, self._grinding = local, False
            # No errand detours while a quest is pinned: the player picked it
            # (quick Marleybone errands chained ahead of the Myth class quest).
            if (ERRAND_DETOURS and not side_quest_world and not self._grinding and not self._pin
                    and not await self._in_dungeon(here)):
                errand = errand_detour([q for _, q in all_quests], chosen, set_aside, world)
                if errand:
                    if not errand.active:
                        logger.info(f"quick errand first: {errand.name!r} ({errand.goal or errand.target}), "
                                    f"then back to {chosen.name!r}")
                    chosen = errand
            if (not side_quest_world and not self._mainline and chosen is not None
                    and not chosen.mainline and self._detour_gap_pending()):
                # On the way to ask for the story's next quest: a quest of the
                # story's world tracked meanwhile, so the house's world gate
                # opens the Spiral Map on that world (it opened on The Spiral
                # Cup's Wysteria and took the bot there again and again).
                try:
                    gap_world = json.loads(DETOUR_GAP_FILE.read_text(encoding="utf-8")).get("zone", "")
                except (OSError, ValueError):
                    gap_world = ""
                gap_world = SPIRAL_WORLD_NAMES.get(gap_world.split("/", 1)[0], gap_world.split("/", 1)[0])
                there = [q for _, q in all_quests if q.zone and norm(q.zone) == norm(gap_world)
                         and q.name not in set_aside]
                if there and chosen not in there:
                    logger.info(f"tracking {there[0].name!r} meanwhile: the Spiral Map then opens on "
                                f"{there[0].zone} (the story's next quest is asked for there)")
                    chosen = there[0]
            self._last_quests = [q for _, q in all_quests]
            _write_quest_book([q for _, q in all_quests], chosen, world)
            self._chosen_entry = chosen
            best = next(((p, q) for p, q in all_quests if q is chosen), None)
            # A spell quest that left the book was completed: it usually taught a spell.
            finished = self._activity_quests - activities
            if finished and self.progression:
                self.progression.request_check(f"finished {', '.join(sorted(finished))}")
            self._activity_quests = activities
            if best is None:
                return False
            page, entry = best
            # The book's encounter icon: this step is a fight even when its
            # words aren't ("Open Locked Door": beat the Kettleheads guarding it).
            self._step_is_fight = entry.fight
            if entry.active:
                logger.info(f"quest priority: continuing {entry.name!r}")
                return False
            for _ in range(pages):
                if not await self._turn_page(back_button):
                    break  # the first page already
            for _ in range(page):
                await self._turn_page(page_button)
            info = [*QUEST_LIST, f"wndQuestInfo{entry.slot}", "questInfoWindow", "wndQuestInfo"]
            slot = [*info, "btnActivate"]
            for attempt in range(TRACK_TRIES):
                await ui.click(self.client, slot)
                await asyncio.sleep(0.6)
                if await ui.is_visible(self.client, [*info, "imgActiveQuest"]):
                    break
                if await ui.modal_box(self.client) is not None:
                    break  # a message about it: handled below
                logger.debug(f"tracking {entry.name!r} didn't take (try {attempt + 1}); clicking again")
            else:
                logger.warning(f"could not track {entry.name!r}; will try again at the next ranking")
                self._ranked_for = None
                self._last_rank = -1e9
                return False
            box = await ui.modal_box(self.client)
            if box and "quest helper is not allowed" in (await ui.modal_text(box)).lower():
                # e.g. a Duel Arena (PvP) quest: nothing the bot can follow.
                logger.warning(f"quest helper not allowed for {entry.name!r}; skipping that quest")
                self.setbacks.skipped.add(entry.name)
                self.setbacks.save()
                await ui.dismiss_notice(self.client)
                self._ranked_for = None
                self._last_rank = -1e9
                return False
            kind = "spell/activity" if entry.activity else "main story" if entry.mainline else "side"
            if entry.goal and not is_combat_objective(entry.goal):
                kind += ", no fight"
            where = f"{entry.world}, {entry.hops} hops" if entry.hops is not None else entry.world
            logger.success(f"quest priority: tracking {entry.name!r} ({kind} quest in {where})")
            self._active_quest = entry.name
            self._active_is_main = entry.mainline
            return True
        finally:
            await self._close_quest_book()

    def _talk_again_after_turn_in(self, gone: set[str], new: set[str] = frozenset()):
        """A quest just left the book right after talking to someone: that was
        the turn-in. Talk to them again at once and accept what they offer
        (the player: after Turning Tiles, Thornton Lewis gave 'Archivist,
        Revisited' only when talked to a second time; the bot had walked off
        and the story stopped)."""
        last = self.__dict__.get("_last_talk")
        if not gone or not last or time.monotonic() - last[2] > TURN_IN_TALK_SECONDS:
            return
        npc, zone, _t = last
        self._last_talk = None
        if VISIT_FILE.exists() or not zone:
            return
        if new:
            # (They gave the next one with the turn-in: Pierce Stanson handed
            # out 'Spirit of the Sea' with 'On the Waterfront'.)
            logger.debug(f"turn-in to {npc} came with {', '.join(sorted(new))}: no second talk")
            return
        logger.info(f"handed in {', '.join(sorted(gone))} to {npc}: talking to them again for the next quest")
        VISIT_FILE.write_text(json.dumps({"npc": npc, "zone": zone}), encoding="utf-8")

    def _later_story_first(self, chosen, quests: list, set_aside: set[str]):
        """A story quest that stays open over later ones (Wintertusk's 'Bones
        of the Earth' until #50 is done; docs/guides): while a later quest of
        its guide is in the book, that one first (the pin stays)."""
        from .main_guide import all_guides, later_in_book

        if chosen is None:
            return chosen
        names = [q.name for q in quests if q.name not in set_aside]
        for _world, guide in all_guides():
            later = later_in_book(guide, chosen.name, names)
            if later:
                if self.__dict__.get("_later_logged") != (chosen.name, later):
                    self._later_logged = (chosen.name, later)
                    logger.info(f"{later!r} first: {chosen.name!r} is handed in after it (the guide)")
                return next(q for q in quests if q.name == later)
        return chosen

    def _fetch_next_story_quest(self):
        """The guide's next story quest isn't in the book while an earlier one
        waits on it, or (a main-story world: Celestia) none of its quests is in
        the book: visit who gives it (Dulin Helmsplitter for 'Hammer Don't
        Hurt 'Em'), then the people it names, where the entity map last saw
        them. Each one once per 10 min."""
        from .main_guide import all_guides, next_to_pick_up, who_to_ask
        from .questlist import SIDE_WORLDS, load_completed

        if self.cfg.side_quest_world or VISIT_FILE.exists() or not self._book_names:
            return
        done = set(load_completed())
        for world, guide in all_guides():
            # (Alone only with no story quest in the book at all: 'An Old Sea
            # Chantry' was in it, spelled 'A Old...' in the guide, and the bot
            # set off to fetch the quest it had just handed in.)
            alone = world not in SIDE_WORLDS and self._detour_names() is None and not self._mainline
            nxt = next_to_pick_up(guide, done, self._book_names, alone=alone)
            if nxt is None:
                continue
            asked = self.__dict__.setdefault("_story_asked", {})

            def seen_in(npc: str) -> list[str]:
                want = _norm_name(npc)

                def same(n: str) -> bool:
                    m = _norm_name(n)
                    return bool(want) and len(m) > 3 and (want in m or m in want)

                return [z for z, names in self.entity_map.zones.items() if any(same(n) for n in names)]

            people = who_to_ask(guide, nxt)
            near = next((zs[0] for p in people if (zs := seen_in(p))), None)
            for npc in people:
                if time.monotonic() - asked.get(npc, -1e9) < 600:
                    continue
                # (Not on the map, e.g. The Archivist: where the giver stands.)
                zones = seen_in(npc) or ([near] if near else [])
                asked[npc] = time.monotonic()
                if not zones:
                    logger.warning(f"next story quest {nxt.name!r} (#{nxt.index}): {npc} hasn't been "
                                   "seen anywhere yet")
                    continue
                logger.info(f"next story quest {nxt.name!r} (#{nxt.index}) isn't in the book: "
                            f"visiting {npc} ({zones[0].split('/')[-1]}) for it")
                VISIT_FILE.write_text(json.dumps({"npc": npc, "zone": zones[0]}), encoding="utf-8")
                return

    def pin_new_quest_after(self, before: set[str]):
        """After fetching quests from an NPC: at the next full read of the book,
        pin the quest that wasn't there before."""
        self._pin_new_from = set(before)
        self._ranked_for = None
        self._last_rank = -1e9

    def _apply_pin(
        self, quests: list[QuestEntry], chosen, set_aside: set[str],
        before: set[str] = frozenset(), complete: bool = True,
    ):
        """The player's pick wins: a pinned quest (state/quest_pin.json, or the
        main-story quest tracked when the bot starts) is followed while it's in
        the book and not set aside (a boss won 5 times, no progress for 5 min)."""
        if self.cfg.side_quest_world:
            return chosen
        if self._pin is None:  # first ranking this session: the player's current pick
            active = next((q for q in quests if q.active), None)
            self._pin = load_pin() or (active.name if active and active.mainline else "")
            if self._pin:
                logger.info(f"following the quest you picked: {self._pin!r}")
                save_pin(self._pin)
        if not self._pin:
            return chosen
        pinned = next((q for q in quests if q.name == self._pin), None)
        if pinned is None and not complete:
            return chosen  # a read cut short: it may just be further in the book
        if pinned is None:
            # Missing from one "complete" reading isn't enough ('Through This
            # Door...' was dropped by a read that also lost two other quests):
            # done only when the next reading misses it too.
            self._pin_missing = getattr(self, "_pin_missing", 0) + 1
            if self._pin_missing < 2:
                logger.info(f"your pick {self._pin!r} isn't in this reading of the book; checking again")
                return chosen
        else:
            self._pin_missing = 0
        if pinned is None and before:
            # Done: follow the quest line to the quest it handed us ('Quest For
            # Glory' finished at Romulus and the next one began).
            new = [q for q in quests if q.name not in before and q.name not in set_aside]
            new.sort(key=lambda q: (not q.activity, not q.mainline))
            if new:
                logger.success(f"your pick {self._pin!r} is done; its quest line goes on: {new[0].name!r}")
                self._pin = new[0].name
                save_pin(self._pin)
                # A new pick starts with no misses (the old count, 2, made one
                # misread of 'Signs and Portents' count as done, and the bot
                # went off visiting Wizard City for side quests).
                self._pin_missing = 0
                pinned = new[0]  # (a class quest still comes first: below)
        if pinned is None or pinned.name in set_aside:
            why = "done" if pinned is None else "set aside"
            logger.info(f"your pick {self._pin!r} is {why}; choosing quests again")
            self._pin = ""
            save_pin("")
            return chosen
        # A class/spell quest comes before the pin (the player's rule: the new
        # spell first; 'Two Heads Are Better...' waited behind Wizard Tours'
        # trip around the Spiral). The pin stays for afterwards.
        spell = next((q for q in quests if q.activity and q.name not in set_aside and q is not pinned), None)
        if spell is not None and not pinned.activity:
            if self._spell_first_logged != spell.name:
                self._spell_first_logged = spell.name
                logger.info(f"the class quest {spell.name!r} first (a new spell), "
                            f"then back to {pinned.name!r}")
            return spell
        return pinned

    async def switch_quest(self) -> bool:
        """Track the next quest in the quest book. True if the objective changed.
        Never away from the main story (the player: the main quest only; it
        switched from 'Talk to Juma Fasttrack' to The Spiral Cup's step)."""
        if self._mainline:
            logger.info("not switching quests: the main story stays tracked")
            self._ranked_for = None
            self._last_rank = -1e9  # (re-rank: put the main quest back if the game moved off it)
            return False
        before = await self.objective()
        if not await self._open_quest_book():
            logger.warning("could not open the quest book to switch quests")
            return False

        self._quest_index = getattr(self, "_quest_index", 0)
        clicked = False
        for _ in range(MAX_QUEST_SLOTS):
            self._quest_index = (self._quest_index + 1) % MAX_QUEST_SLOTS
            entry = f"wndQuestInfo{self._quest_index}"
            slot = [*QUEST_LIST, entry, "questInfoWindow", "wndQuestInfo", "txtGoal"]
            if await ui.click(self.client, slot):
                clicked = True
                await asyncio.sleep(0.6)
                break

        await self._close_quest_book()

        after = await self.objective()
        if clicked and after != before:
            logger.info(f"switched tracked quest: {before!r} -> {after!r}")
            return True
        logger.warning(f"tried to switch quests but the objective is still {after!r}")
        return False

    async def _press_collect(self):
        if not await ui.is_visible(self.client, ui.NPC_RANGE):
            # Like NPCs and sigils, the prompt appears on walking into range, not teleporting.
            await self.client.send_key(Keycode.S, 0.3)
            await self.client.send_key(Keycode.W, 0.3)
            await asyncio.sleep(0.5)
        if not await ui.is_visible(self.client, ui.NPC_RANGE):
            logger.debug("no collect prompt at the item")
            return
        for _ in range(3):
            await self.client.send_key(Keycode.X, 0.1)
            await asyncio.sleep(0.2)
        await wait_until_free(self.client, timeout=15)

    async def _leave_minigame(self, zone: str | None) -> bool:
        """In a minigame's zone (ThePhantomZoneWorld/Shockalock, opened by X on
        a locked chest): no quest needs one. Its close button if there is one,
        else a relog (it's the reliable way out). True if we were in one."""
        if not (zone or "").startswith(MINIGAME_WORLD):
            return False
        from .relog import _dump, _find_button, relog

        logger.warning(f"in a minigame ({zone.split('/')[-1]}); leaving it")
        await _dump(self.client, "minigame")
        words = ("close", "exit", "quit", "leave", "cancel", "x")
        button = await _find_button(self.client.root_window, words)
        if button is not None:
            logger.info(f"minigame: clicking {await button.name() or 'close'!r}")
            await ui.click_center(self.client, button)
            for _ in range(10):
                await asyncio.sleep(0.5)
                if not (await self.client.zone_name() or "").startswith(MINIGAME_WORLD):
                    logger.success("left the minigame")
                    return True
        logger.info("minigame: no way out on screen (windows in state/relog_minigame.txt); relogging")
        await relog(self.client)
        return True

    async def _search_hint(self, item: str, objective: str) -> bool:
        """The player told us where `item` is (FIND_HINTS): once per objective,
        stand at points in rings around that landmark (a Locate is done by
        coming close; a prompt there gets an X). True if it searched."""
        hint = FIND_HINTS.get("".join(ch for ch in item.lower() if ch.isalnum()))
        zone = await self.client.zone_name() or ""
        if not hint or hint[0] != zone or getattr(self, "_hinted_for", None) == objective:
            return False
        self._hinted_for = objective
        _zone, (cx, cy, cz), radius = hint
        logger.info(f"looking for {item!r} where the player said: around ({cx:.0f}, {cy:.0f})")
        for r in (radius * 0.45, radius):
            for k in range(8):
                a = k * math.pi / 4
                spot = XYZ(cx + r * math.cos(a), cy + r * math.sin(a), cz)
                if not await is_free(self.client):
                    return True
                if not await self._clear_spot(spot):
                    continue
                await self.client.teleport(spot)
                await asyncio.sleep(0.8)
                if await self.objective() != objective:
                    logger.success(f"found {item}")
                    return True
                if await self.collector.collect_once(item, self._press_collect):
                    return True
                if await ui.is_visible(self.client, ui.NPC_RANGE):
                    await self.client.send_key(Keycode.X, 0.1)
                    await asyncio.sleep(1.5)
                    if await self.objective() != objective:
                        logger.success(f"found {item} (pressed X at it)")
                        return True
        logger.info(f"{item!r} wasn't around the spot the player gave; searching the zone")
        return True

    async def _fetch_from_known_spots(self, item: str, objective: str) -> bool:
        """Go to spots where `item` was seen (nearest first), each at most
        KNOWN_SPOT_TRIES times for this objective; never into its guards (no
        fights the quest doesn't ask for). True if it went somewhere."""
        start = await self.client.body.position()
        zone = await self.client.zone_name() or ""
        tries = self.__dict__.setdefault("_known_spot_tries", {})
        # Not an enemy's name ("Supplies" also matched Otomo Supply Runner):
        # every enemy fought is in the simulator's stats.
        from .combat.sim import load_stats

        enemies = set(load_stats().get("enemies", {}))

        def is_item(name: str) -> bool:
            return name not in enemies and matches_item(item, name)

        known = self.entity_map.spots(zone, is_item, (start.x, start.y, start.z))
        known = spread_points(known, (start.x, start.y, start.z), 800.0)[:KNOWN_SPOTS_FIRST]
        known = [p for p in known if tries.get((objective, tuple(round(v) for v in p)), 0) < KNOWN_SPOT_TRIES]
        if not known:
            return False
        logger.info(f"looking for {item!r} where it was seen before ({len(known)} spot(s))")
        went = False
        for p in known:
            if not await is_free(self.client):
                return True
            key = (objective, tuple(round(v) for v in p))
            tries[key] = tries.get(key, 0) + 1
            if not await self._clear_spot(XYZ(*p)):
                # Guards on it (the Stolen Weapons among Sanzoku bandits): no
                # fight (the player's rule); this spot waits for another try.
                tries[key] -= 1
                continue
            await self.client.teleport(XYZ(p[0] + 200, p[1], p[2]))
            await asyncio.sleep(1.0)
            went = True
            await scan_entities(self.client, zone, self.entity_map)
            if not await is_free(self.client):
                return True
            if await self.collector.collect_once(item, self._press_collect):
                return True
        return went

    async def _scout_for(self, item: str) -> bool:
        """Hop under the map across the zone until `item` loads, then fetch
        it. True if it found it (or a fight interrupted)."""
        from . import walkmap

        zone = await self.client.zone_name() or ""
        start = await self.client.body.position()
        centers = walkmap.chunk_centers(await walkmap.nav_points(zone), (start.x, start.y))[:SCOUT_MAX]
        if len(centers) < 2:
            return False
        raw = getattr(self.client, "_teleport_raw", self.client.teleport)
        logger.info(f"scouting {zone.split('/')[-1]} for {item!r} from under the map ({len(centers)} spots)")
        for c in centers:
            if not await is_free(self.client):
                return True
            self.controller.allow_idle(10)
            try:
                await raw(XYZ(c[0], c[1], c[2] - walkmap.UNDER_MAP))
            except Exception:
                continue
            await asyncio.sleep(SCOUT_SETTLE)
            await scan_entities(self.client, zone, self.entity_map)
            if await self.collector._candidates(item):
                logger.info(f"{item!r} is near ({c[0]:.0f}, {c[1]:.0f})")
                if await self.collector.collect_once(item, self._press_collect):
                    return True
        await raw(start)
        logger.info(f"no {item!r} anywhere in {zone.split('/')[-1]} right now")
        return False

    async def _collect_through_door(self, item: str, objective: str, marker: XYZ, zone: str) -> bool:
        """Go to the quest marker; with no `item` near it, take it for a door
        and walk through. True if it did something (the zone changed: the
        building is remembered as where this objective's items are)."""
        if not self._may_try(objective, zone, "collect_door"):
            return False
        near = [e for e in await self.collector._candidates(item)
                if distance(await e.location(), marker) < COLLECT_DOOR_NEAR]
        if near:
            return False  # the marker is on the item itself
        if distance(await self._position(), marker) > INTERACT_RANGE:
            await self.travel(marker)
            if not await wait_until_free(self.client, timeout=5) or await self._zone_changed(zone):
                return True
            self.collector._cache = None
            if any(distance(await e.location(), marker) < COLLECT_DOOR_NEAR
                   for e in await self.collector._candidates(item)):
                return True  # there after all: collected on the next step
        logger.info(f"no {item!r} by the quest marker: it must be a door; walking through it")
        if await self.walk_through(marker, zone) or await self._walk_in_from_around(marker, zone):
            inside = await self.client.zone_name() or ""
            if inside and inside != zone:
                self._door_rooms[objective] = inside
                logger.info(f"through the marker's door into {inside.split('/')[-1]}: "
                            f"looking for {item!r} here")
            return True
        return False

    def _room_with(self, item: str, area: str) -> str | None:
        """A building of `area` where an entity named exactly `item` was seen,
        when none by that name was seen in `area` itself."""
        want = item.strip().lower()

        def has(z: str) -> bool:
            return any(n.strip().lower() == want for n in self.entity_map.zones.get(z, {}))

        if not want or has(area):
            return None
        return next((z for z in self.entity_map.zones if room_of(z, area) and has(z)), None)

    async def collect(self, item: str, objective: str) -> bool:
        """Handle a collect objective. Returns True if it did something this step."""
        names = loose_names(item)
        item = names[min(self._loose_level.get(objective, 0), len(names) - 1)]
        if not self._loose_level.get(objective):
            item = await self._resolve_name(item, await self.client.zone_name() or "", "collect")
        if await self.collector.collect_once(item, self._press_collect):
            await asyncio.sleep(0.5)
            if await self.objective() != objective:
                logger.success(f"collected {item}")
            return True
        # The guide says where it comes from ("Collect Cogitator (from
        # Deactivated Golem)"): there's nothing by the item's name to pick up.
        from .main_guide import collect_source

        source = collect_source(collect_item_name(objective) or item)
        if source:
            if self.__dict__.get("_source_said") != (objective, source):
                self._source_said = (objective, source)
                logger.info(f"the guide: {item!r} comes from the {source}; going to it")
            if await self._use_named_object(f"Use {source}"):
                return True
        if await self._search_hint(item, objective):
            return True
        # The quest marker points at the next one: far from it, go there first.
        # (Diseased Mushrooms in Kishibe Village lay 34000 east; the old
        # sightings and landmarks were all near the entrance, among the guards,
        # and 20 minutes went to fights there before the quest was set aside.)
        marker = await self.client.quest_position.position()
        zone = await self.client.zone_name() or ""
        # The marker on a dungeon sigil: the items are in that dungeon.
        if (distance(marker, XYZ(0, 0, 0)) > 1 and distance(await self._position(), marker) < 3000
                and self._may_try(objective, zone, "collect_sigil")):
            sigil = await self._sigil_at(marker)
            if sigil is not None:
                logger.info(f"no {item!r} in view; the quest marker is a dungeon sigil: going in")
                await self._enter_by_sigil(sigil, zone)
                return True
        # The marker on a building's door (Light Candles in Baobab
        # Crossroads: the candles are inside, the marker points at the door;
        # the bot swept the zone and the next ones for minutes): nothing by
        # that name near it, so walk through it.
        if (distance(marker, XYZ(0, 0, 0)) > 1 and "/interiors/" not in zone.lower()
                and distance(await self._position(), marker) <= COLLECT_MARKER_RANGE
                and await self._collect_through_door(item, objective, marker, zone)):
            return True
        if (distance(marker, XYZ(0, 0, 0)) > 1
                and distance(await self._position(), marker) > COLLECT_MARKER_RANGE
                and self._may_try(objective, zone, "collect_marker")):
            logger.info(f"no {item!r} in view: going to the quest marker "
                        f"({distance(await self._position(), marker):.0f} away)")
            await self.travel(marker)
            return True
        # Where it was seen before, each spot up to KNOWN_SPOT_TRIES times per
        # objective (a guard fight there cut the first look short: after
        # winning, the bot went sweeping instead of back to the item).
        if await self._fetch_from_known_spots(item, objective):
            return True
        # Nothing matching in view: look around the zone once per objective.
        if getattr(self, "_swept_for", None) != objective:
            self._swept_for = objective
            start = await self.client.body.position()
            points = sweep_points((start.x, start.y, start.z), [], 0)
            logger.info(f"searching the zone for {item!r} ({len(points)} spots)")
            for p in points:
                if not await is_free(self.client):
                    return True
                if not await self._clear_spot(XYZ(*p)):
                    continue  # an enemy is there: landing on it starts a fight
                await self.client.teleport(XYZ(*p))
                await asyncio.sleep(TELEPORT_SETTLE)
                if await self.collector.collect_once(item, self._press_collect):
                    return True
            await self.client.teleport(start)
            return True
        # Nothing nearby: from under the map, every part of the zone loads in
        # turn without an enemy seeing us (the zone's nav graph, in squares
        # of the load range: Deimos's auto-collect); go only where it is.
        if getattr(self, "_scouted_for", None) != objective:
            self._scouted_for = objective
            if await self._scout_for(item):
                return True
        # Pickups only load close to the wizard, so hop across the
        # zone's landmarks and walkways (e.g. Triton's cogs are ~20k units from
        # the entrance).
        if getattr(self, "_far_swept_for", None) != objective:
            self._far_swept_for = objective
            start = await self.client.body.position()
            points = await self._landmarks() + floor_points(await path_points(self.client), start.z)
            spots = spread_points(points, (start.x, start.y, start.z), FAR_SWEEP_SPACING)
            logger.info(f"nothing near here; searching {len(spots)} landmarks across the zone for {item!r}")
            for p in spots[:FAR_SWEEP_MAX]:
                if not await is_free(self.client):
                    return True
                self.controller.allow_idle(10)
                if not await self._clear_spot(XYZ(*p)):
                    continue
                await self.client.teleport(XYZ(*p))
                await asyncio.sleep(1.5)  # let nearby objects stream in
                if await self.collector.collect_once(item, self._press_collect):
                    logger.info(f"found {item!r} near ({p[0]:.0f}, {p[1]:.0f})")
                    return True
            return True
        # Searched the whole zone: the items may lie in a neighbouring zone (the
        # Hall of Champions' gemstones are out on the Krokosphinx streets).
        # Search the zones around the one the objective names (or this one)
        # before giving up.
        # The exact name found nothing in the whole zone: look again for
        # anything like it ('Red Crystal Sample' -> 'Crystal Sample' ->
        # 'Crystal'; the player's: the samples are all just "Crystal Sample").
        level = self._loose_level.get(objective, 0)
        if level + 1 < len(names):
            self._loose_level[objective] = level + 1
            self._scouted_for = self._far_swept_for = None
            logger.info(f"no {item!r} anywhere here: looking for anything like {names[level + 1]!r}")
            return True
        if await self._search_next_zone(item, objective):
            return True
        # Inside a dungeon an item that's nowhere often comes from its boss
        # (the Medallion of Fire in Pyromancer's Tomb spawns once the boss is
        # beaten): fight the room's boss, else its enemies, then search again.
        if await self._fight_for_item(item, objective):
            return True
        # Nothing lying around anywhere near right now (they spawn over time):
        # follow the next best quest, and pick these up whenever they come into
        # view (see _pick_up_wanted).
        quest = self._active_quest
        if quest and self._stall_switched_for != objective:
            self._stall_switched_for = objective
            level = await self.client.stats.reference_level()
            self.setbacks.set_quest_aside(quest, objective, level, main=quest in self._mainline)
            self.setbacks.save()
            self._zones_searched.pop(objective, None)  # a fresh search next time
            logger.info(
                f"no {item!r} in this zone or the ones around it right now: setting {quest!r} aside; "
                "will pick them up if they show up"
            )
            self._ranked_for = None
            self._last_rank = -1e9
            return True
        # Nothing else to do: let the quest marker (if any) guide us, else wait for respawns.
        if distance(await self.client.quest_position.position(), XYZ(0, 0, 0)) < 1:
            logger.debug(f"no {item!r} found; waiting for respawns")
            self.controller.allow_idle(15)
            await asyncio.sleep(10)
            self._swept_for = None
            return True
        return False

    async def _search_next_zone(self, item: str, objective: str) -> bool:
        """Go to the next zone around the objective's place not searched yet
        for `objective`, to sweep it for `item`. True if it went."""
        zone = await self.client.zone_name() or ""
        searched = self._zones_searched.setdefault(objective, set())
        searched.add(zone)
        home = objective_zone(objective) or zone
        for candidate in zones_near(home, COLLECT_SEARCH_DEPTH):
            if candidate in searched:
                continue
            searched.add(candidate)  # one try each, even if the trip fails
            logger.info(f"no {item!r} in {zone}; searching {candidate} next")
            if await self.go_to_zone(candidate):
                self._swept_for = self._far_swept_for = None  # sweep the new zone
                return True
        return False

    def _may_try(self, objective: str, zone: str, approach: str) -> bool:
        """Count a try at `approach`; False once it has had its APPROACH_LIMITS
        tries for this objective here (so the next approach gets its turn)."""
        key = (objective, zone, approach)
        self._attempts_at[key] = self._attempts_at.get(key, 0) + 1
        return self._attempts_at[key] <= APPROACH_LIMITS.get(approach, 3)

    async def _all_approaches_used(self, objective: str, what: str) -> bool:
        """Every approach failed: set the quest aside now instead of looping.
        True if it did."""
        logger.warning(f"tried every way to {what} for {objective!r}; setting this quest aside for now")
        self._attempts_at = {k: v for k, v in self._attempts_at.items() if k[0] != objective}
        return await self._set_current_aside(objective)

    async def _walk_toward(self, marker: XYZ, target: str) -> bool:
        """Teleport toward a far marker in hops of WALK_LEG (enemies load only
        near the wizard: King Shemet was 26000 away), looking for `target`
        after each. Every landing is checked for enemies (safe_teleport); a hop
        the game refuses is tried 30 degrees to either side. Walking there ran
        into fights. True if it came into view (or a fight started)."""
        from .bossfarm import find_entity_named

        logger.info(f"teleporting toward the quest marker to find {target}")
        circles = await self._duel_circles(await self.client.zone_name() or "")
        for _ in range(WALK_LEGS):
            here = await self._position()
            gap = distance(here, marker)
            if gap < INTERACT_RANGE:
                break
            base = math.atan2(marker.y - here.y, marker.x - here.x)
            leg = min(WALK_LEG, gap)
            moved = False
            for turn in (0.0, math.pi / 6, -math.pi / 6):
                a = base + turn
                z = marker.z if leg == gap else here.z
                hop = XYZ(here.x + math.cos(a) * leg, here.y + math.sin(a) * leg, z)
                if any(math.dist((hop.x, hop.y), c[:2]) < CIRCLE_KEEP_AWAY for c in circles):
                    logger.info("the marker is by a duel circle: not hopping any closer")
                    return False
                await self.client.teleport(hop)
                await asyncio.sleep(0.6)
                if not await is_free(self.client):
                    return True
                if distance(await self._position(), here) > leg / 3:
                    moved = True
                    break
            if not moved:
                logger.info("no way on toward the marker from here")
                return False
            await asyncio.sleep(0.6)  # let what's around load
            if await find_entity_named(self.client, target) is not None:
                logger.info(f"{target} is in view")
                return True
        return False

    async def _fetch_door_key(self, objective: str, target: str) -> bool:
        """A target behind a door that wants an item first (DOOR_KEYS, from
        the player): go to each place it can be and scout it out (collected
        when found). True if it acted this step."""
        hint = DOOR_KEYS.get((target or "").lower())
        if not hint or objective in self._door_keys_found:
            return False
        item, places = hint
        zone = await self.client.zone_name() or ""
        # Only in front of the door: past it (in Malistaire's Lair, the player
        # had opened it) the caves aren't the way on, and going there looped.
        before_door = places[0].rsplit("/", 1)[0] + "/"  # (the area the key's places are in)
        if not zone.startswith(before_door):
            return False
        # Something here named for it first (the Crystal Stand in the volcano:
        # using it was all Malistaire's door needed): walk up and press X.
        for name, spots in (self.entity_map.zones.get(zone, {}) or {}).items():
            named = item.lower() in name.lower() and spots
            if named and self._may_try(objective, f"{zone}|{name}", "door_key_use"):
                logger.info(f"{target}'s door is locked: using the {name} here first")
                await self._use_object_at(XYZ(*spots[0]))
                return True
        for place in places:
            if not self._may_try(objective, place, "door_key"):
                continue
            if zone != place:
                logger.info(f"{target}'s door is locked: the {item} is at the end of {place.split('/')[-1]}")
                await self.go_to_zone(place)
                return True
            logger.info(f"looking for the {item} through {place.split('/')[-1]}")
            if await self._scout_for(item) and await is_free(self.client):
                # Picked up (not a fight cutting the scouting short): back to the door.
                self._door_keys_found.add(objective)
                logger.info(f"got the {item}: back to {target}'s door")
            return True  # (else this cave's tries count down; the next one after)
        return False

    async def _fight_zone_boss(self, objective: str, zone: str) -> bool:
        """A locked door on the way to the objective: beat the boss on this
        zone's duel circle first (Gurtok Firebender guards the crystal that
        opens Malistaire's door). Goes to the circle remembered in the
        entity map, then onto the enemy standing on it. True if it acted."""
        circles = (self.entity_map.zones.get(zone, {}) or {}).get("Duel Circle") or []
        if not circles or not self._may_try(objective, zone, "zone_boss"):
            return False
        c = XYZ(*circles[0])
        near = []
        for mob in await self.client.get_mobs():
            try:
                pos = await mob.location()
            except Exception:
                continue
            if math.dist((pos.x, pos.y), (c.x, c.y)) < ZONE_BOSS_RANGE:
                near.append(pos)
        if not near:
            logger.info("a locked way on: to this zone's duel circle, where its boss stands")
            await self.client.teleport(XYZ(c.x + 900, c.y, c.z))  # (lands clear of enemies)
            await asyncio.sleep(1.5)
            return True
        # The zone's boss by name when one was fought here (its guards stand
        # nearer the circle: a Magma Fury was picked, and fled, over Gurtok).
        from .bossfarm import find_entity_named
        from .dungeons import zone_bosses

        target = None
        for name in zone_bosses(zone):
            target = await find_entity_named(self.client, name)
            if target is not None:
                logger.info(f"a locked way on: beating {name}, this zone's boss, first")
                break
        if target is None:
            target = min(near, key=lambda p: math.dist((p.x, p.y), (c.x, c.y)))
            logger.info("a locked way on: beating the boss on this zone's duel circle first")
        self._wanted_fight_until = time.monotonic() + WANTED_FIGHT_SECONDS
        allow_engage(self.client)  # this teleport is meant to start the fight
        await self.client.teleport(target)
        await asyncio.sleep(3.0)
        return True

    async def _fight_for_item(self, item: str, objective: str) -> bool:
        """`item` is nowhere in this dungeon room: start a fight with the enemy
        on the room's duel circle (its boss), else the nearest enemy, so the
        item can drop or appear; the search starts over after it. True if it
        went into a fight."""
        zone = await self.client.zone_name() or ""
        if is_team_up_zone(zone) or not (await self._in_dungeon(zone) or "/interiors/" in zone.lower()):
            return False
        mobs = []
        for mob in await self.client.get_mobs():
            try:
                mobs.append(await mob.location())
            except Exception:
                continue
        if not mobs or not self._may_try(objective, zone, "fight_for_item"):
            return False
        here = await self._position()
        circles = await self._duel_circles(zone)
        on_circle = [m for m in mobs if any(math.dist((m.x, m.y), c[:2]) < BOSS_ON_CIRCLE for c in circles)]
        target = min(on_circle or mobs, key=lambda m: distance(m, here))
        who = "the boss on its duel circle" if on_circle else "the nearest enemy"
        logger.info(f"no {item!r} anywhere here: fighting {who} (it may drop or appear after)")
        # A fresh search after the fight (the item shows up once it's won).
        self._scouted_for = self._far_swept_for = None
        self._zones_searched.pop(objective, None)
        allow_engage(self.client)  # this teleport is meant to start the fight
        await self.client.teleport(target)
        await asyncio.sleep(3.0)
        return True

    async def _use_zone_teleporter(self, objective: str) -> bool:
        """Take the next untried in-zone teleporter (an object labelled "To
        ...", like the Djeserit tomb's "To the Sarcophagus") toward a far quest
        marker. True if it used one."""
        from .names import lang_name

        zone = await self.client.zone_name() or ""
        tried = self._teleporters_tried.setdefault((objective, zone), set())
        here = await self._position()
        options = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                code = await t.display_name() if t else None
                label = await lang_name(self.client, code) if code else ""
                # "To the Sarcophagus"; Katzenstein's Lab's floors: plain "Teleporter"
                is_tp = label.lower().startswith("to ") or label.lower() == "teleporter"
                pos = await e.location()
                key = f"{label}@{pos.x:.0f},{pos.y:.0f}"  # several plain "Teleporter"s
                if not is_tp or key in tried:
                    continue
                options.append((distance(pos, here), key, label, pos))
            except Exception:
                continue
        if not options:
            return False
        _d, key, label, pos = min(options, key=lambda o: o[0])
        tried.add(key)
        logger.info(f"the quest marker is out of reach: taking the {label!r} teleporter")
        dx, dy = here.x - pos.x, here.y - pos.y
        length = math.hypot(dx, dy) or 1.0
        await self.client.teleport(XYZ(pos.x + dx / length * 200, pos.y + dy / length * 200, pos.z))
        await asyncio.sleep(TELEPORT_SETTLE)
        await self.client.goto(pos.x, pos.y)
        await self._press_x_here(zone, adjust=True)
        return True

    async def _try_switch_puzzle(self, objective: str, zone: str) -> bool:
        """'Use X' where X should be but isn't (a chest that appears when the
        room's switches are right): try every switch combination, once per
        objective and room. True if it tried."""
        from .bossfarm import find_entity_named
        from .puzzles import solve_by_trying, use_target

        target = use_target(objective)
        if not target or (objective, zone) in self._puzzles_tried:
            return False
        marker = await self.client.quest_position.position()
        if distance(marker, XYZ(0, 0, 0)) < 1 or distance(await self._position(), marker) > PUZZLE_NEAR:
            return False  # not there yet
        if await find_entity_named(self.client, target) is not None:
            return False
        self._puzzles_tried.add((objective, zone))
        await solve_by_trying(self, objective)
        return True

    async def _landmarks(self) -> list[tuple[float, float, float]]:
        return await landmarks(self.client)

    async def _in_any_dungeon(self, zone: str) -> bool:
        from .dungeons import is_open_zone

        if is_open_zone(zone):
            return False
        known = DungeonMemory.load().dungeons
        return (
            is_team_up_zone(zone) or "/interiors/" in zone.lower() or "/gauntlets/" in zone.lower()
            or zone in known or in_known_dungeon_folder(zone, known)
            or await self._in_dungeon(zone)
        )

    async def may_flee(self) -> bool:
        """Never in a dungeon: fleeing throws us out and its progress is lost
        (a mark there didn't help: it fled Mount Olympus and lost the team).
        Only the user's state/flee.request still flees there."""
        return not await self._in_any_dungeon(await self.client.zone_name() or "")

    async def unneeded_fight(self, battle) -> bool:
        """True if the fight that just started isn't needed for the tracked quest."""
        try:
            # The quest goal text can read blank mid-battle: use the last one seen.
            objective = (await self.objective()).strip() or self._last_progress[0] or ""
            zone = await self.client.zone_name() or ""
        except Exception:
            return False
        if await self._in_any_dungeon(zone):
            return False  # every fight in a dungeon is fought (fleeing loses it)
        if time.monotonic() < getattr(self, "_wanted_fight_until", 0.0):
            return False  # a fight started on purpose (a locked door's guard): fought
        if self._grinding:
            return False  # grinding: every fight is what we came for (one was fled)
        names = [e.name for e in battle.enemies]
        has_boss = any(e.is_boss for e in battle.enemies)
        if fight_needed(objective, names, zone, has_boss):
            return False
        # Not the objective's fight: flee (the player's rule), then the usual
        # recovery refills the mana fleeing cost before going on.
        # Fleeing the same enemies again and again on one objective means they
        # stand in the way (Desert Golems on the road to Akori's Chamber): fight.
        key = (objective, frozenset(names))
        self._fled[key] = self._fled.get(key, 0) + 1
        if self._fled[key] > FLEES_BEFORE_FIGHTING:
            who = ', '.join(sorted(set(names)))
            hp, mana = await health_mana(self.client)
            if self.upkeep and self.upkeep.needs_recovery(hp, mana):
                # Fighting through at 0 mana (the O'Leary Nappers) only loses.
                logger.info(f"fled {who} {self._fled[key] - 1} times here, but at {hp:.0%} health, "
                            f"{mana:.0%} mana: fleeing again")
                return True
            logger.info(f"fled {who} {self._fled[key] - 1} times here; fighting through")
            return False
        return True

    async def _talk_target_enemy(self, objective: str):
        """The enemy a "Talk To X" objective names, if X is a mob here (Willie
        Marks before he's beaten), else None."""
        name = talk_target(objective)
        if not name:
            return None
        from .names import lang_name

        want = _norm_name(name)
        try:
            for mob in await self.client.get_mobs():
                t = await mob.object_template()
                code = await t.display_name() if t else ""
                label = await lang_name(self.client, code) if code else ""
                if want and want == _norm_name(label):
                    return mob
        except Exception as exc:
            logger.debug(f"talk-target enemy check failed: {exc!r}")
        return None

    async def _talk_means_fight(self, objective: str) -> bool:
        """"Talk To Willie Marks" where Willie Marks is an enemy (the talk comes
        after beating him, in the middle of his clocktower): go start the fight.
        True if it went for one."""
        mob = await self._talk_target_enemy(objective)
        if mob is None:
            return False
        logger.info(f"{talk_target(objective)} is an enemy here: fighting him to get on with {objective!r}")
        allow_engage(self.client)
        await self.client.teleport(await mob.location())
        await asyncio.sleep(3.0)
        return True

    async def _alone_at(self, pos: XYZ) -> bool:
        """No other enemy within COMPANY_RANGE of the one at `pos`."""
        try:
            for mob in await self.client.get_mobs():
                d = distance(await mob.location(), pos)
                if NOT_SAME_MOB < d < COMPANY_RANGE:
                    return False
        except Exception:
            pass
        return True

    async def _boss_alone(self, name: str) -> bool | None:
        """Is the enemy `name` alone (no other enemy within COMPANY_RANGE of
        it)? None when it isn't in view (the deck choice waits)."""
        from .bossfarm import find_entity_named

        try:
            pos = await find_entity_named(self.client, name)
            if pos is None:
                return None
            for mob in await self.client.get_mobs():
                p = await mob.location()
                d = distance(p, pos)
                if NOT_SAME_MOB < d < COMPANY_RANGE:
                    return False
        except Exception:
            return None
        return True

    async def _fight_guards(self, spot: XYZ, what: str, reach: float = 0.0) -> bool:
        """Enemies within GUARD_RANGE of `spot` (a lever): fight the nearest
        first. Skipping a floor's fights can keep a dungeon's boss from
        spawning (Sprockets). True if it went into a fight."""
        circles = await self._duel_circles(await self.client.zone_name() or "")
        for mob in await self.client.get_mobs():
            try:
                pos = await mob.location()
            except Exception:
                continue
            if distance(pos, spot) > (reach or GUARD_RANGE):
                continue
            if any(math.dist((pos.x, pos.y), c[:2]) < CIRCLE_KEEP_AWAY for c in circles):
                continue  # the boss's own circle: not now
            logger.info(f"enemies guard the {what}: fighting them first")
            allow_engage(self.client)
            await self.client.teleport(pos)
            await asyncio.sleep(3.0)
            return True
        return False

    async def _clear_dungeon(self, objective: str, zone: str) -> bool:
        """Before a boss that won't spawn: fight every enemy group left in the
        dungeon (off the boss's circle), floor by floor. True if it acted;
        False once a full pass found nobody."""
        circles = await self._duel_circles(zone)

        def off_circle(p) -> bool:
            return all(math.dist(p[:2], c[:2]) >= CIRCLE_KEEP_AWAY for c in circles)

        for mob in await self.client.get_mobs():
            try:
                pos = await mob.location()
            except Exception:
                continue
            if off_circle((pos.x, pos.y, pos.z)):
                logger.info("clearing the room before its boss: fighting the enemies here")
                self._wanted_fight_until = time.monotonic() + WANTED_FIGHT_SECONDS  # (not fled)
                allow_engage(self.client)
                await self.client.teleport(pos)
                await asyncio.sleep(3.0)
                return True
        # Enemies load only nearby: visit each floor seen in this dungeon once.
        seen = [p for p in self.entity_map.spots(zone, lambda _n: True, (0.0, 0.0, 0.0)) if off_circle(p)]
        floors: dict[int, tuple] = {}
        for p in seen:
            floors.setdefault(round(p[2] / FLOOR_HEIGHT_STEP), p)
        visited = self._floors_cleared.setdefault((objective, zone), set())
        for level in sorted(floors):
            if level in visited:
                continue
            visited.add(level)
            spot = floors[level]
            logger.info(f"clearing the dungeon: checking the floor at height {spot[2]:.0f} for enemies")
            await self.client.teleport(XYZ(*spot))
            await asyncio.sleep(1.5)
            return True
        return False

    async def _after_pull(self, objective: str):
        """After pulling a lever in a dungeon, stand still while what it moves
        gets there (Counterweight East: the counterweight must reach the top
        before Sprockets spawns)."""
        if not objective.strip().lower().startswith("pull"):
            return
        wait = LEVER_WAITS.get(await self.client.zone_name() or "")
        if not wait:
            return
        logger.info(f"pulled it; waiting {wait:.0f}s for it to take effect")
        self.controller.allow_idle(wait + 10)
        try:
            await asyncio.sleep(wait)
        finally:
            self.controller.end_idle()

    async def _use_named_object(self, objective: str) -> bool:
        """'Use X': teleport beside the object named X (its own height), nudge
        until the X prompt shows and press it. True if it tried."""
        name = operate_target(objective)
        if not name:
            return False
        name = await self._resolve_name(name, await self.client.zone_name() or "", "use")
        # Several with that name (a Counterweight Lever on every floor): the
        # one at the quest marker is this step's.
        marker = await self.client.quest_position.position()
        near = marker if distance(marker, XYZ(0, 0, 0)) > 1 else await self._position()
        zone = await self.client.zone_name() or ""
        # 'Use Inactive Protector (0 of 3)': the ones used already are skipped
        # (each used one stays, same name), the nearest other one next.
        used = self.__dict__.setdefault("_used_objects", {}).setdefault((name, zone), [])
        pos = await self._npc_named(name, near=near, skip=used)  # exact name, not an enemy
        if pos is None:
            if near is marker and distance(await self._position(), marker) > MARKER_REACHED:
                # A marker not reached yet: it leads there (the Heart of Winter
                # is in the room past a door; sweeping Northguard and
                # Savarstaad Pass for it went on for minutes, fleeing fights).
                return False
            # Not loaded here (no marker; the fallback spot was across the
            # zone): sweep the zone for it, where it was seen first.
            return await self._seek_object(name, zone, used)
        me = await self._position()
        if distance(me, pos) > USE_OBJECT_RANGE:
            logger.info(f"going right up to the {name}")
        # The object's own spot first, then a few feet off, then rings around
        # it (the player's order). A 'talk' prompt there is the NPC beside it
        # (Xihong Bi at the Forge): pressing X talked to them again, in a loop.
        for dx, dy in use_spots():
            if not await is_free(self.client):
                return True  # (a fight started at the lever: the step takes it from there)
            await self.client.teleport(XYZ(pos.x + dx, pos.y + dy, pos.z))
            await asyncio.sleep(0.6)
            for nudge in (None, (Keycode.W, 0.15), (Keycode.S, 0.15)):
                if nudge:
                    await self.client.send_key(*nudge)
                    await asyncio.sleep(0.2)
                if not await ui.is_visible(self.client, ui.NPC_RANGE):
                    continue
                prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
                if "talk" in prompt and "talk" not in objective.lower() and not await self._nearest_is(name):
                    break  # someone else's prompt: another spot
                await self.interact(objective)
                await self._after_pull(objective)
                used.append(pos)
                # (Used: not the answer for a later step's other name here.)
                self.__dict__.setdefault("_used_names", {}).setdefault(zone, set()).add(name)
                return True
        logger.info(f"no prompt at the {name}")
        used.append(pos)  # (nothing to use there: the next one)
        # Another one not tried yet (Sun Stands, 6 in the Chancel): straight to
        # it next step. Returning False fell through to walking the marker as
        # a door for 2 minutes, and the story quest was set aside as stuck.
        if await self._npc_named(name, near=near, skip=used) is not None:
            logger.info(f"another {name} not tried yet: that one next")
            self._last_progress_time = time.monotonic()  # (ruling one out is progress)
            return True
        return False

    async def _seek_object(self, name: str, zone: str, used: list) -> bool:
        """Teleport across the zone (spots it was seen first) until an
        object named `name` not used yet is in view. True if it moved."""
        me = await self._position()
        here = (me.x, me.y, me.z)
        known = self.entity_map.spots(zone, lambda n: same_object_name(n, name), here)
        known = [k for k in known if all(math.dist(k[:2], (u.x, u.y)) > 150 for u in used)]
        sweep = spread_points(await self._landmarks() + floor_points(await path_points(self.client), me.z),
                              here, ENEMY_SWEEP_SPACING)
        visited = self._swept_spots.setdefault((f"use:{name}", zone), [])
        spots = [k for k in known if k not in visited] + [
            p for p in sweep if all(math.dist(p[:2], v[:2]) > ENEMY_SWEEP_SPACING / 2 for v in visited)]
        if not spots:
            visited.clear()
            return False
        logger.info(f"no {name} in view: looking around the zone ({len(known)} spot(s) it was seen at first)")
        for p in spots[:FAR_SWEEP_MAX]:
            if not await is_free(self.client):
                return True
            visited.append(p)
            if not await self._clear_spot(XYZ(*p)):
                continue
            await self.client.teleport(XYZ(*p))
            await asyncio.sleep(1.5)  # let nearby entities stream in
            await scan_entities(self.client, zone, self.entity_map)
            if await self._npc_named(name, near=await self._position(), skip=used) is not None:
                logger.info(f"found a {name} near ({p[0]:.0f}, {p[1]:.0f})")
                return True
        return True

    async def _nearest_is(self, name: str) -> bool:
        """Is the nearest named entity to the wizard the one called `name`
        (its 'talk' prompt is the object's own: the Water Breathing Device)?"""
        from .names import lang_name

        want = "".join(c for c in name.lower() if c.isalnum())
        me = await self._position()
        best: tuple[float, str] | None = None
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                code = await t.display_name() if t else None
                if not code:
                    continue
                display = await lang_name(self.client, code)
                key = "".join(c for c in (display or "").lower() if c.isalnum())
                # The object itself, or a person (NPCBehavior) who could own
                # the prompt; not our pet hovering beside us.
                if not key or (key != want and "NPCBehavior" not in await e.list_behavior_names()):
                    continue
                d = distance(await e.location(), me)
                if best is None or d < best[0]:
                    best = (d, display)
            except Exception:
                continue
        return best is not None and "".join(c for c in best[1].lower() if c.isalnum()) == want

    async def _learn_dungeon_exit(self, old_zone: str, zone: str):
        """Out of a dungeon room into the dungeon's outside zone right after a
        teleport: that landing was its exit. Remembered, so teleports there
        keep clear of it (state/dungeon_exits.json)."""
        from .dungeons import add_exit

        entry = DungeonMemory.load().dungeons.get(old_zone)
        if entry is None or entry.outside != zone or self._recall_pending:
            return
        land = getattr(self.client, "_last_landing", None)
        if not land or time.monotonic() - land[0] > EXIT_LEARN_SECONDS:
            return
        if add_exit(old_zone, (land[1], land[2], land[3])):
            logger.info(f"a landing at ({land[1]:.0f}, {land[2]:.0f}) took us out of "
                        f"{old_zone.split('/')[-1]}: remembered as its exit, kept clear of from now on")

    async def _learn_door_walk(self):
        """The zone changed since the last step while heading for a quest
        marker: the last teleport before it is a way through that marker's
        door. Saved (state/doors.json) and used first next time (it got stuck
        at the Sun Chamber door, then got in some other way and forgot how)."""
        try:
            zone = await self.client.zone_name() or ""
            marker = await self.client.quest_position.position()
        except Exception:
            return
        prev = self._prev_step
        self._prev_step = (zone, marker if distance(marker, XYZ(0, 0, 0)) > 1 else None)
        if prev and prev[0] and zone and zone != prev[0] and prev[1] is None:
            await self._learn_dungeon_exit(prev[0], zone)
        if not prev or not prev[0] or not zone or zone == prev[0] or prev[1] is None:
            return
        old_zone, old_marker = prev
        await self._learn_dungeon_exit(old_zone, zone)
        if is_hub(zone) or zone.split("/", 1)[0] != old_zone.split("/", 1)[0] or self._recall_pending:
            return  # the hub button, a Recall or a relog, not a door (Throne Room -> hub after Zeus)
        land = getattr(self.client, "_last_landing", None)
        if not land or time.monotonic() - land[0] > 60:
            return
        spot = XYZ(land[1], land[2], land[3])
        if distance(spot, old_marker) > DOOR_LEARN_RANGE:
            return
        key = (old_marker.x, old_marker.y, old_marker.z)
        if self.doors.approach(old_zone, key) is None:
            self.doors.record(old_zone, key, (spot.x, spot.y, spot.z), zone)
            logger.info(f"remembered the way from {old_zone} into {zone}: from ({spot.x:.0f}, {spot.y:.0f})")

    async def _to_world(self, world: str, why: str) -> bool:
        """Toward another world: by the dorm to Wizard City, into the World
        Tree (Ravenwood, Bartleby's mouth), its world gate, the Spiral Map,
        stage after stage without going back to the step in between (each
        stage waited a whole step's checks: the trip took half a minute). True
        if it acted; False once in `world` (off the map)."""
        acted = False
        deadline = time.monotonic() + SPIRAL_TRIP_SECONDS
        while time.monotonic() < deadline:
            before = await self.client.zone_name() or ""
            map_open = await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT)
            if not await self._to_world_stage(world, why):
                return acted
            acted = True
            on_map = await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT)
            if not on_map and not await is_free(self.client):
                return True  # (a fight or a dialogue: the step takes it from here)
            after = await self.client.zone_name() or ""
            if after == before and (map_open or not on_map):
                return True  # this stage got nowhere (or the map didn't take us): the next step tries again
        return acted

    async def _to_world_stage(self, world: str, why: str) -> bool:
        """One stage of _to_world. True if it acted."""
        zone = await self.client.zone_name() or ""
        if await ui.is_visible(self.client, ui.SPIRAL_DOOR_TELEPORT):
            from .relog import _find_button

            label = SPIRAL_WORLD_NAMES.get(world, world.lower())
            button = await _find_button(self.client.root_window, (label,))
            # Not on this page (4 worlds a page): page on through the list.
            for _ in range(SPIRAL_PAGES):
                if button is not None:
                    break
                if not await ui.click(self.client, ui.SPIRAL_DOOR_NEXT):
                    break
                await asyncio.sleep(0.4)
                button = await _find_button(self.client.root_window, (label,))
            if button is None:
                logger.warning(f"{why}: {label.title()} isn't on the Spiral Map's pages")
            if button is not None:
                logger.info(f"{why}: choosing {label.title()} on the Spiral Map")
                await ui.click_center(self.client, button)
                await asyncio.sleep(0.3)
            await ui.click(self.client, ui.SPIRAL_DOOR_TELEPORT)
            await wait_for_loading(self.client, appear_timeout=3.0)
            landed = await self.client.zone_name() or ""
            if landed.split("/")[0] == world:
                from .route import note_arrival

                note_arrival(world, landed)  # (the stream page's route lands there)
            return True
        if zone.split("/", 1)[0] != world:
            # A Spiral Map at the player's house (its world gate) and in the
            # World Tree (Ravenwood, Bartleby's mouth). The home button reaches
            # the house from anywhere: no walk to the World Tree (the player).
            from .trainer import go_home, is_house

            if not is_house(zone) and zone not in (WORLD_TREE, RAVENWOOD):
                logger.info(f"{why}: Go Home to the house's world gate, then {world}")
                if await go_home(self.client):
                    return True
            from .trainer import DORM, DORM_DOOR

            if zone == DORM:
                return await self.approach_and_walk(DORM_DOOR, DORM)
            if zone == RAVENWOOD:
                door = await self._entity_named_like(("bartlebymouth",)) or XYZ(*BARTLEBY_MOUTH)
                logger.info(f"{why}: into the World Tree for the Spiral Map")
                await self.approach_and_walk(door, zone)
                return True
            if zone == WORLD_TREE or is_house(zone):
                # Not loaded from here: where it was seen before (the (0, 0)
                # guess stood by the house's own teleporter).
                seen = [p for n, ps in (self.entity_map.zones.get(zone) or {}).items()
                        if n.lower() == "universeteleport" for p in ps]
                gate = await self._entity_named_like(("universeteleport",)) or (
                    XYZ(*seen[0]) if seen else XYZ(0, 0, 89))
                # A prompt counts only beside the gate: Go Home lands by the
                # house's teleporter, whose prompt was taken for the gate's and
                # its X went to Wysteria again and again.
                near_gate = distance(await self._position(), gate) < GATE_STAND + 250
                if not (near_gate and await ui.is_visible(self.client, ui.NPC_RANGE)):
                    # Land beside the gate (walking into it doesn't open the
                    # map: its "Press X" prompt does) and wait for the prompt.
                    here = await self._position()
                    dx, dy = here.x - gate.x, here.y - gate.y
                    back = GATE_STAND / (math.hypot(dx, dy) or 1.0)
                    await self.client.teleport(XYZ(gate.x + dx * back, gate.y + dy * back, gate.z))
                    if not await self._wait_visible(ui.NPC_RANGE, 2.0):
                        logger.info(f"{why}: walking up to the world gate")
                        await self.client.goto(gate.x, gate.y)
                        if not await self._wait_visible(ui.NPC_RANGE, 2.0):
                            return True
                # "World Gate: Press X to Interact" opens the Spiral Map.
                logger.info(f"{why}: opening the Spiral Map at the world gate")
                await self.client.send_key(Keycode.X, 0.1)
                if await self._wait_visible(ui.SPIRAL_DOOR_TELEPORT, 6.0):  # (3 s: the map came up after)
                    # Choose the world now: left open, the step's map handler
                    # went to the tracked quest's world instead.
                    return await self._to_world_stage(world, why)
                return True
            logger.info(f"{why}: walking to Ravenwood")
            return await self.go_to_zone(RAVENWOOD)
        return False

    def _write_route(self, objective: str, zone: str, place: str | None, world: str | None):
        """The zones the bot means to cross to the objective, for the stream
        page's navigation graph (state/route.json); cleared once there."""
        from .route import arrival_for, plan_route, write_route
        from .travel_data import _data, world_hub

        try:
            gates, display, _spots = _data()
            dest = place or (world_hub(f"{world}/x") if world else None)
            final = None
            target = talk_target(objective) or defeat_target(objective)
            if dest and target:
                # The room off the place where the one to see was last seen
                # (Cyrus Drake in the Myth School, off Ravenwood).
                rooms = [z for z, names in self.entity_map.zones.items() if target in names and z != dest]
                final = next((z for z in rooms if self.doors.leading_to(dest, z)
                              or any(t == z for _p, t in gates.get(dest, []))), None)
            route = plan_route(zone, dest, gates, hub=world_hub(zone),
                               arrival=arrival_for(dest.split("/")[0]), final=final) if dest else []
            old = self._route_written or []
            if route and old and old[-1] == route[-1] and zone in old:
                route, at = old, old.index(zone)  # on the way: the planned route, further along
            else:
                at = 0
            if (route, at) != (self._route_written, self._route_at):
                self._route_written, self._route_at = route, at
                write_route(route, objective, display, at)
        except Exception as exc:
            logger.debug(f"route not written: {exc!r}")

    def _book_world(self, objective: str) -> str | None:
        """The world the quest book gives for the tracked quest ("Krokotopia"
        -> "Krokotopia", "Dragonspyre" -> "DragonSpire"), when its entry is
        for this objective."""
        entry = self._chosen_entry
        if entry is None or not entry.zone:
            return None
        if entry.goal and not objective.lower().startswith(entry.goal.lower()):
            return None  # (the entry is from before the objective changed)
        by_name = {v: k for k, v in SPIRAL_WORLD_NAMES.items()}
        return by_name.get(entry.zone.strip().lower())

    async def _wait_visible(self, path, seconds: float) -> bool:
        """Poll for a window to show (every 0.2 s), up to `seconds`."""
        deadline = time.monotonic() + seconds
        while True:
            if await ui.is_visible(self.client, path):
                return True
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.2)

    def _detour_stays(self, quest: str | None) -> bool:
        """detour.json 'stay': true (the player: no Celestia until Jotun is
        beaten) and `quest` is the detour world's: it's never set aside and
        the main story doesn't take over."""
        det = self._detour_names()
        if det is None or not quest or norm(quest) not in det[2]:
            return False
        try:
            return bool(json.loads(Path("state", "detour.json").read_text(encoding="utf-8")).get("stay"))
        except (OSError, ValueError):
            return False

    def _detour_names(self) -> tuple[dict, set[str], set[str]] | None:
        """The active detour world (detour.py): (its entry, its main-story
        quest names, all its quest names), normalized; None with no detour."""
        from . import detour
        from .questlist import load_completed, load_world_lists

        entry = detour.active(detour.load(), set(load_completed()))
        if entry is None:
            return None
        if getattr(self, "_world_lists", None) is None:
            self._world_lists = load_world_lists()
        listed = self._world_lists.get(entry["world"], [])
        main = {norm(q.name) for q in listed if not any("SIDE" in t for t in q.tags)}
        # The quest that starts the world counts as its story, listed or not
        # (Merle Ambrose's 'Dire News From Abroad' leads into Zafaria from
        # Ravenwood, and was taken for a side quest).
        start = norm((entry.get("start") or {}).get("quest", ""))
        lead = {start} if start else set()
        return entry, main | lead, {norm(q.name) for q in listed} | lead

    async def _note_detour_gap(self, has: bool):
        """The detour world's quest just left the book with none after it
        ('News of the North' ended in Hrundle Fjord; 'Cold Day in Hrundle'
        waits with someone there): remember where, to ask its NPCs."""
        had = getattr(self, "_detour_had", None)
        self._detour_had = has
        if has or not had:
            return
        zone = await self.client.zone_name() or ""
        if zone:
            DETOUR_GAP_FILE.write_text(json.dumps({"zone": zone, "asked": 0}), encoding="utf-8")
            logger.info(f"the detour's quest ended in {zone.split('/')[-1]}: its NPCs have the next one")

    def _detour_gap_pending(self) -> bool:
        """The detour's NPCs not asked yet for its next quest (state/detour_gap.json)."""
        try:
            gap = json.loads(DETOUR_GAP_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(gap.get("zone")) and time.time() - gap.get("asked", 0) >= DETOUR_ASK_SECONDS

    async def _detour_ask(self) -> bool:
        """No detour quest in the book: to the zone where the last one ended
        and ask its NPCs, before any grinding (it went to grind in Celestia).
        Once an hour. Then, with no main quest either (the fallback to the
        main story, its next quest not in the book): the NPCs where the main
        story was last worked on. True if it acted."""
        if self._mainline or self._detour_names() is None:
            return False
        gap_file = DETOUR_GAP_FILE if self._detour_gap_pending() else MAIN_STORY_ZONE_FILE
        try:
            gap = json.loads(gap_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        dest = gap.get("zone") or ""
        if not dest or time.time() - gap.get("asked", 0) < DETOUR_ASK_SECONDS:
            return False
        zone = await self.client.zone_name() or ""
        if zone != dest:
            world = dest.split("/", 1)[0]
            if zone.split("/", 1)[0] != world:
                # A quest of that world tracked first (the ranking does it while
                # this trip is pending): the Spiral Map opens on the tracked
                # quest's world, and The Spiral Cup's took it to Wysteria.
                target = objective_zone(await self.objective() or "") or ""
                if target.split("/", 1)[0] != world and not getattr(self, "_gap_reranked", False):
                    self._gap_reranked = True
                    self._ranked_for = None
                    self.controller.allow_idle(30)
                    try:
                        await self.prioritize_quests()
                    finally:
                        self.controller.end_idle()
                return await self._to_world(world, f"to {dest.split('/')[-1]} for the detour's next quest")
            self._gap_reranked = False
            logger.info(f"no detour quest: to {dest.split('/')[-1]} to ask its NPCs for the next one")
            if await self.go_to_zone(dest) or await self.client.zone_name() != zone:
                return True
        gap["asked"] = time.time()
        gap_file.write_text(json.dumps(gap), encoding="utf-8")
        if zone == dest:
            what = "the detour's" if gap_file == DETOUR_GAP_FILE else "the main story's"
            logger.info(f"asking the NPCs of {dest.split('/')[-1]} for {what} next quest")
            self.givers.sweep_now(dest)
            self._ranked_for = None
            return True
        return False

    def _detour_start(self, entry: dict, started: bool):
        """No quest of the detour world in the book yet: visit the NPC who
        starts it (Merle Ambrose for 'Cold News')."""
        from . import detour
        from .questlist import load_completed

        want = detour.needs_start(entry, started, set(load_completed()))
        if want is None or VISIT_FILE.exists():
            return
        logger.info(f"detour to {entry['world']}: visiting {want['npc']} for its first quest")
        VISIT_FILE.write_text(json.dumps(want), encoding="utf-8")

    async def _visit_npc(self) -> bool:
        """state/visit_npc.json {"npc": ..., "zone": ...}: go and talk to that
        NPC (accepting what they offer), e.g. the giver of the next main quest
        ('The Last Meow' from Sherlock Bones in the Royal Museum). Crosses
        worlds by the Spiral Map in the World Tree. True if it acted."""
        try:
            want = json.loads(VISIT_FILE.read_text(encoding="utf-8"))
            npc, dest = want["npc"], want["zone"]
        except (OSError, ValueError, KeyError):
            VISIT_FILE.unlink(missing_ok=True)
            return False
        zone = await self.client.zone_name() or ""
        world = dest.split("/", 1)[0]
        if await self._to_world(world, f"visit {npc}"):
            return True
        if zone != dest:
            from .trainer import DORM, home_to_ravenwood

            if zone == DORM:
                # (No gate route leads out of the dorm: the visit to Blad
                # Raveneye in Triton Avenue was dropped as "no route".)
                logger.info(f"visit {npc}: out of the dorm to Ravenwood first")
                await home_to_ravenwood(self)
                return True
            logger.info(f"visit {npc}: going to {dest}")
            if not await self.go_to_zone(dest):
                # Not reachable yet (Village of Sorrow before the story opens
                # it): drop the visit (the giver waits an hour) rather than
                # asking for a route every second.
                logger.warning(f"visit {npc}: no route to {dest} from {zone}; dropping the visit")
                VISIT_FILE.unlink(missing_ok=True)
                self._visit_tries = 0
            return True
        logger.info(f"visit: talking to {npc} in {dest}")
        if self.dialogue:
            self.dialogue.accept_offers_for(60)
        objective = f"Talk To {npc}"
        self._attempts = 0
        if await self._find_npc(npc) is None:
            # Nowhere in the zone: not a talk failure (those get a few tries).
            VISIT_FILE.unlink(missing_ok=True)
            self._visit_tries = 0
            logger.warning(f"visit: {npc} isn't anywhere in {dest.split('/')[-1]}; giving up")
            return True
        before = set(self._book_names)
        talked = await self._talk_to_named(objective)
        if talked and want.get("pin") and before:  # (an empty "before": every quest looked new)
            # The quest they gave is followed, wherever it is (the Waterworks
            # unlock chain in Wizard City while the story is in Celestia).
            self.pin_new_quest_after(before)
        if talked or self._visit_tries >= 3:
            VISIT_FILE.unlink(missing_ok=True)
            self._visit_tries = 0
            self._last_rank = -1e9  # a new quest: rank again now
            self._ranked_for = None
            logger.success(f"visit: talked to {npc}" if talked else f"visit: couldn't reach {npc}; giving up")
        else:
            self._visit_tries += 1
        return True

    async def _world_tree_to_aquila(self, zone: str) -> bool:
        """Aquila from another world: the dorm button to Wizard City, walk to
        Ravenwood, into the World Tree (Bartleby's mouth door), and on from
        inside it to Aquila. The tree's inside isn't mapped: its entities are
        saved to state/world_tree_entities.txt and the way on is found by name
        (aquila / portal / teleport / door). True if it acted."""
        if zone.startswith("Aquila/"):
            return False
        if not zone.startswith("WizardCity/"):
            from .trainer import go_home, is_house

            if is_house(zone):  # (the house's world gate to Wizard City)
                return await self._to_world("WizardCity", "to Aquila")
            logger.info("to Aquila: Go Home, then the house's world gate to Wizard City")
            return await go_home(self.client)
        if zone == CYCLOPS_LANE:
            # The way to Aquila: a "press X" prompt at the far end of Cyclops
            # Lane (how the wizard first went, following Silenus's marker).
            portal = XYZ(*AQUILA_PORTAL)
            logger.info(f"to Aquila: to the prompt in Cyclops Lane at ({portal.x:.0f}, {portal.y:.0f})")
            await self.approach_and_walk(portal, zone)
            if await self.client.zone_name() == zone:
                await self._press_x_here(zone)
                await asyncio.sleep(2.0)
                await wait_for_loading(self.client)
            now = await self.client.zone_name() or ""
            if now.startswith("Aquila/"):
                logger.success(f"to Aquila: arrived ({now})")
            return True
        if zone == self._world_tree_zone:
            await self._dump_entities("state/world_tree_entities.txt")
            me = await self._position()
            ways = await self._entities_named_like(("aquila", "portal", "teleport", "spiral", "gate", "door"))
            ways = [w for w in ways if (zone, round(w.x / 100), round(w.y / 100)) not in self._tree_tried]
            if not ways:
                logger.warning("to Aquila: no way on found in the World Tree; "
                               "entities saved to state/world_tree_entities.txt")
                return False
            way = min(ways, key=lambda w: distance(w, me))
            self._tree_tried.add((zone, round(way.x / 100), round(way.y / 100)))
            logger.info(f"to Aquila: trying the way at ({way.x:.0f}, {way.y:.0f}) in the World Tree")
            await self.approach_and_walk(way, zone)
            await self._press_x_here(zone)
            await asyncio.sleep(2.0)
            await wait_for_loading(self.client)
            return True
        from .trainer import DORM, DORM_DOOR

        if zone == DORM:
            logger.info("to Aquila: out of the dorm onto Ravenwood")
            return await self.approach_and_walk(DORM_DOOR, DORM)
        logger.info("to Aquila: walking to Cyclops Lane")
        return await self.go_to_zone(CYCLOPS_LANE)

    async def _entities_named_like(self, words: tuple[str, ...]) -> list[XYZ]:
        out = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                if not t:
                    continue
                name = f"{await t.object_name() or ''} {await t.display_name() or ''}".lower()
                if any(w in name for w in words) and "collision" not in name:
                    out.append(await e.location())
            except Exception:
                continue
        return out

    async def _entity_named_like(self, words: tuple[str, ...]) -> XYZ | None:
        found = await self._entities_named_like(words)
        return found[0] if found else None

    async def _dump_entities(self, path: str):
        lines = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                pos = await e.location()
                name = f"{await t.object_name() if t else '?'} | {await t.display_name() if t else ''}"
                lines.append(f"{name} | ({pos.x:.0f}, {pos.y:.0f}, {pos.z:.0f})")
            except Exception:
                continue
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8", errors="replace")
        except Exception:
            pass

    async def _farm_trip(self, zone: str) -> bool:
        """Farming a dungeon: go to its sigil and in with a team. True if it acted."""
        farm = Farm.load()
        if not farm.active:
            return False
        entry = DungeonMemory.load().dungeons.get(farm.dungeon)
        if entry is None:
            logger.warning(f"farming {farm.name}: its sigil isn't learned yet; questing instead")
            return False
        self._last_progress_time = time.monotonic()  # farming isn't a stalled quest
        if self.gear and not await self.client.in_battle():
            # The loot of the last run (Zeus' chest, fight drops): note and try
            # it before going back in (farming skipped the gear check entirely).
            self.controller.allow_idle(600)
            try:
                await self.gear.tick()
            except Exception as exc:
                logger.opt(exception=exc).warning("gear check failed")
            finally:
                self.controller.end_idle()
            if not await is_free(self.client):
                return True
        from .farm import load_looted

        if farm.targets_done(load_looted()):
            # The whole set is in (the player's goal): stop farming for good,
            # and level on Marleybone side quests; the stuck main quest (The
            # Last Meow: Meowiarty) stays skipped until the player says.
            farm.active, farm.complete = False, True
            farm.save()
            # (Mount Olympus: the stuck story quest was skipped then, the
            # player's call; no other farm skips anything.)
            skip = (self._last_main or "The Last Meow") if farm.name == "Mount Olympus" else ""
            if skip:
                self.setbacks.skipped.add(skip)
                self.setbacks.save()
            self._last_rank = -1e9
            logger.success(f"ALERT: {farm.name}: the whole set is in after {farm.runs} runs; farming done. "
                           + (f"Levelling on side quests ({skip!r} skipped)" if skip else "Back to quests"))
            return False
        if zone != entry.outside:
            # The Mark stays at the sigil (made before each Team Up): Recall is
            # the way back from another world (no gate route crosses worlds).
            other_world = zone.split("/")[0] != entry.outside.split("/")[0]
            # (From anywhere, not only another world: walking Triton Avenue to
            # Crab Alley's door ran into Haunted Minions, fled, lost the mana,
            # healed and ran into them again.)
            if self._mark and self._mark.zone == entry.outside:
                logger.info(f"farming {farm.name}: recalling to the mark at its sigil")
                if await self._recall(entry.outside, "the sigil mark"):
                    return True
            if not other_world:
                logger.info(f"farming {farm.name}: going to {entry.outside}")
                if await self.go_to_zone(entry.outside):
                    return True
            if entry.outside.startswith("Aquila/") and await self._world_tree_to_aquila(zone):
                return True
            if time.monotonic() - self._farm_alerted > 600:
                self._farm_alerted = time.monotonic()
                logger.warning(f"ALERT: farming {farm.name}: can't get to {entry.outside} from {zone} "
                               "(no mark there); trying again")
            # Farming means no questing (it went back to Meowiarty): wait and retry.
            self.controller.allow_idle(20)
            await asyncio.sleep(5.0)
            return True
        logger.info(f"farming {farm.name} (run {farm.runs + 1}): to the sigil")
        await self._enter_by_sigil(XYZ(*entry.sigil), zone)
        return True

    async def _team_step(self) -> bool:
        """With a team (a Team Up dungeon): never start a fight. Join the ones
        teammates start (walk into their circle); on a fight step, stay with
        the nearest teammate meanwhile. Other steps (talks, pick-ups) go on
        as usual. True if it acted."""
        from .collect import duel_circles
        from .teamup import team_fight_at, teammates

        me = await self._position()
        mates = await teammates(self.client, me)
        now = time.monotonic()
        self._track_room(await self.client.zone_name() or "")
        await self._final_boss_potion()
        if mates:
            self._team_with_us = True  # seen one in here: we have a team
            self._mate_last_seen = now
            _save_team_state(await self.client.zone_name() or "", now)
        elif not self._team_with_us and _team_state_recent(await self.client.zone_name() or ""):
            # A restart inside the dungeon forgot the team (it then left after
            # 90 s as "alone"): a teammate was seen here minutes ago.
            self._team_with_us = True
            self._mate_last_seen = now
        gone = self._team_with_us and now - self._mate_last_seen > TEAM_GONE_AFTER
        if self._team_alone_since is None:
            self._team_alone_since = now
        from .teamup import team_list

        # (A quest dungeon on the team list: alone in there means back in by
        # Recall after a loss, not a team elsewhere: out at once to Team Up.)
        in_quest_team = (await self.client.zone_name() or "") in team_list()
        alone_after = TEAM_ALONE_QUEST if in_quest_team else TEAM_ALONE_AFTER
        elif_alone = not self._team_with_us and now - self._team_alone_since > alone_after
        if (elif_alone or gone) and now - self._team_alone_since > 0:
            # The team left (none seen for TEAM_GONE_AFTER: they went after
            # Apollo, and it waited alone in the Moon Chamber for 20 minutes).
            # Only when no teammate has been seen in here at all (and we didn't
            # come in with one): out of sight in another room isn't alone.
            # Nobody with us: leave by the world hub button and queue for a new team.
            from .dungeon_heal import go_to_hub

            if gone:
                why = f"no teammate seen for {TEAM_GONE_AFTER / 60:.0f} min"
            else:
                why = f"alone for {TEAM_ALONE_AFTER:.0f}s"
            logger.warning(f"{why} in the team dungeon; leaving to wait for a new team")
            self._team_with_us = False
            self._team_alone_since = None
            self._mate_seen = None
            await go_to_hub(self.client)
            return True
        if not self._team_with_us:
            # No teammate seen in here yet: no quest steps on our own (players
            # on the sigil who didn't come in left it alone; it then walked into
            # Apollo's fight following the dungeon quest), but look for them in
            # the other rooms rather than wait in this one.
            here_zone = await self.client.zone_name() or ""
            if self._mate_seen is None or self._mate_seen[0] != here_zone:
                self._mate_seen = (here_zone, me, now, False)
            if await self._after_team_through_door():
                return True
            logger.debug("no teammate seen in this dungeon yet; waiting")
            self._last_progress_time = now
            self.controller.allow_idle(TEAM_WAIT_TICK + 10)
            try:
                await asyncio.sleep(TEAM_WAIT_TICK)
            finally:
                self.controller.end_idle()
            return True
        circles = sorted((XYZ(*c) for c in await duel_circles(self.client)), key=lambda c: distance(c, me))
        # Only teammates standing still count as fighting (locked in a battle):
        # teammates walking by Apollo's circle were taken for a fight, and it
        # walked onto the circle with none started.
        prev_time, prev = self._prev_mates
        still = []
        if now - prev_time < STILL_WINDOW:
            still = [m for m in mates if any(distance(m, q) < STILL_RANGE for q in prev)]
        self._prev_mates = (now, list(mates))
        fight = team_fight_at(circles, still)
        mates_for_mobs = still
        if fight is None and mates_for_mobs:
            # No duel circle listed there: a teammate beside an enemy is fighting it.
            mobs = []
            for mob in await self.sprinter.get_mobs():
                try:
                    mobs.append(await mob.location())
                except Exception:
                    continue
            fight = team_fight_at(sorted(mobs, key=lambda m: distance(m, me)), mates_for_mobs)
        logger.debug(f"team: {len(mates)} teammate(s), {len(circles)} circle(s), fight at {fight}")
        if fight is not None:
            return await self._join_team_fight(fight, me)
        objective = await self.objective()
        # Only this dungeon's own non-fight steps are done as usual (talks,
        # pick-ups); a quest from elsewhere would walk away from the team.
        here = any(n in (objective or "").lower() for n in TEAM_UP_NAMES)
        # The step's own words decide (the book's fight icon is the whole quest's:
        # 'Talk To Athena' was taken for a fight and routed to "her room").
        fight_step = is_combat_objective(objective or "")
        # Straight on to the next fight's room after each fight (known doors),
        # to wait by its circle rather than arrive after it started.
        if here and fight_step and await self._walk_to_boss_room():
            self._last_progress_time = time.monotonic()
            return True
        farming = Farm.load().active and not here  # the dungeon's own quest (given on entering) is followed
        if not farming and not fight_step and talk_target(objective or ""):
            # A talk counts for each player: do it, wherever in the dungeon
            # ("Talk To Silenus in Garden Of Hesperides" after Zeus). But a talk
            # that doesn't move on waits on the team (Hephaestus until the
            # Bronze Eagles are found: it talked to him for minutes while the
            # team was elsewhere): after a few, go after the team instead.
            tries = self._team_talks[objective] = self._team_talks.get(objective, 0) + 1
            if tries <= TEAM_TALK_TRIES:
                return False
            if tries == TEAM_TALK_TRIES + 1:
                logger.info(f"{objective!r} isn't moving on (waits on the team?): going after the team")
        # Anything else not a fight (collect tokens, use things) counts for the
        # whole team: leave it to the others and stay with them.
        self._last_progress_time = time.monotonic()  # waiting on the team isn't a stall
        # This dungeon's fight step: head for the boss along the quest marker,
        # stopping short of any duel circle there (the team starts the fight;
        # we walk in once a player is in it). A marker with no circle near it
        # is a door or passage: normal travel goes through it.
        marker = await self.client.quest_position.position()
        if here and fight_step and not farming and distance(marker, XYZ(0, 0, 0)) > 1:
            d = distance(me, marker)
            at_fight = [c for c in circles if distance(c, marker) < TEAM_CIRCLE_NEAR]
            if not at_fight:
                # No duel circle listed (Apollo's room): enemies by the marker are the fight.
                for mob in await self.sprinter.get_mobs():
                    try:
                        pos = await mob.location()
                    except Exception:
                        continue
                    if distance(pos, marker) < TEAM_CIRCLE_NEAR:
                        at_fight.append(pos)
            if d > TEAM_APPROACH or (at_fight and d > TEAM_STANDOFF + 400):
                dx, dy = me.x - marker.x, me.y - marker.y
                length = math.hypot(dx, dy) or 1.0
                stop = XYZ(marker.x + dx / length * TEAM_STANDOFF, marker.y + dy / length * TEAM_STANDOFF,
                           marker.z)
                key = (await self.client.zone_name() or "", round(stop.x / 300), round(stop.y / 300))
                self._standoff_tries[key] = self._standoff_tries.get(key, 0) + 1
                if self._standoff_tries[key] > 2 and not at_fight:
                    # Teleporting there gets no closer (off the map, or the way
                    # on is a gateway): travel walks the doors.
                    return await self._team_travel(marker)
                logger.info(f"heading for the boss; stopping {TEAM_STANDOFF:.0f} short at "
                            f"({stop.x:.0f}, {stop.y:.0f}) (a teammate starts the fight)")
                await self.client.teleport(stop)
                await asyncio.sleep(TEAM_WAIT_TICK)
                return True
            if not at_fight:
                return await self._team_travel(marker)  # a door or passage on the way
            logger.debug("near the boss's circle; watching for a teammate to start the fight")
            # Every second, not once a step (a step takes ~5 s; Sylster's
            # fight started without us and the player moved the bot in).
            near = [c for c in circles if distance(c, marker) < TEAM_CIRCLE_NEAR] or at_fight
            prev = mates
            end = time.monotonic() + BOSS_WATCH
            self.controller.allow_idle(BOSS_WATCH + 10)
            try:
                while time.monotonic() < end:
                    await asyncio.sleep(1.0)
                    if not await is_free(self.client):
                        return True  # pulled in already
                    now_mates = await teammates(self.client, await self._position())
                    still = [m for m in now_mates if any(distance(m, q) < STILL_RANGE for q in prev)]
                    prev = now_mates
                    fight = team_fight_at(near, still)
                    if fight is not None:
                        return await self._join_team_fight(fight, await self._position())
            finally:
                self.controller.end_idle()
            return True
        elif here and not fight_step and await self._to_objective(marker):
            return True
        elif mates:
            here_zone = await self.client.zone_name() or ""
            walking = walk_zone(self.client, here_zone)
            # The teammate furthest along (the player: follow whoever leads
            # through the dungeon): furthest from where we came into this
            # room; on foot (walk-only zones) closer behind, to be pulled
            # into their fight.
            entry = self.__dict__.setdefault("_room_entry", {})
            if here_zone not in entry:
                entry.clear()
                entry[here_zone] = me
            start = entry[here_zone]
            mate = max(mates, key=lambda m: distance(m, start)) if walking else min(
                mates, key=lambda m: distance(m, me))
            follow = TEAM_FOLLOW_WALKING if walking else TEAM_FOLLOW
            self._mate_seen = (here_zone, mate, time.monotonic(), True)
            self._mate_trail = [*self._mate_trail[-4:], (here_zone, mate)]
            if distance(mate, me) > follow:
                logger.info(f"following the team (teammate at ({mate.x:.0f}, {mate.y:.0f})); "
                            "not starting fights")
                dx, dy = me.x - mate.x, me.y - mate.y
                length = math.hypot(dx, dy) or 1.0
                await self.client.teleport(XYZ(mate.x + dx / length * TEAM_BEHIND,
                                               mate.y + dy / length * TEAM_BEHIND, mate.z))
        elif await self._after_team_through_door():
            return True
        self.controller.allow_idle(TEAM_WAIT_TICK + 10)
        try:
            await asyncio.sleep(TEAM_WAIT_TICK)
        finally:
            self.controller.end_idle()
        return True

    async def _team_travel(self, marker: XYZ) -> bool:
        """On the way to the boss's room (the Sun Chamber's door): through the
        known doors to its room if the boss's room is known (boss -> room from
        dungeons.json, doors -> rooms from doors.json), else the quest's travel
        (doors, remembered door walks). The normal step's travel for a fight is
        off in a team dungeon (it goes onto enemies), so handing over to it
        left the bot standing at the door."""
        if await self._walk_to_boss_room():
            return True
        zone = await self.client.zone_name() or ""
        key = (zone, round(marker.x / 100), round(marker.y / 100))
        tries = self._door_tries[key] = self._door_tries.get(key, 0) + 1
        if tries > DOOR_TRIES:
            # Going nowhere (it tried the Watchful Eye's door 77 times): look
            # for the team elsewhere instead.
            if tries == DOOR_TRIES + 1:
                logger.info(f"the door at ({marker.x:.0f}, {marker.y:.0f}) goes nowhere; "
                            "looking for the team")
            if await self._after_team_through_door():
                return True
            if tries > DOOR_TRIES * 4:
                self._door_tries[key] = 0  # try it again after a while
            return False
        logger.info(f"heading for the boss's room: to the door at ({marker.x:.0f}, {marker.y:.0f})")
        self.controller.allow_idle(30)
        try:
            if distance(await self._position(), marker) < DOOR_NEAR:
                # Already at it: travel says "arrived" and never goes through.
                await self.walk_through(marker, zone)
            else:
                await self.travel(marker)
        finally:
            self.controller.end_idle()
        return True

    async def _join_team_fight(self, fight: XYZ, me: XYZ) -> bool:
        """Walk into the fight a teammate is in (teleported near it first)."""
        from .safe_teleport import allow_engage

        logger.info(f"a teammate is fighting at ({fight.x:.0f}, {fight.y:.0f}): joining")
        if distance(me, fight) > TEAM_JOIN_FROM * 1.5:
            dx, dy = me.x - fight.x, me.y - fight.y
            length = math.hypot(dx, dy) or 1.0
            await self.client.teleport(XYZ(fight.x + dx / length * TEAM_JOIN_FROM,
                                           fight.y + dy / length * TEAM_JOIN_FROM, fight.z))
            await asyncio.sleep(0.8)
        allow_engage(self.client)
        await self.client.goto(fight.x, fight.y)
        await self._hold_for_fight()
        self._last_progress_time = time.monotonic()
        return True

    async def _to_objective(self, marker: XYZ) -> bool:
        """A dungeon with a known room order (the Waterworks): straight for
        the quest's objective rather than trailing the team (the player: it
        lagged far behind). Enemies still standing near us: hold for the team
        to start that fight (we join it), never one of our own. True if it
        acted (went, or held)."""
        from .teamup import room_order

        zone = await self.client.zone_name() or ""
        if not room_order(zone) or distance(marker, XYZ(0, 0, 0)) <= 1:
            return False
        me = await self._position()
        if distance(me, marker) < TEAM_MARKER_NEAR:
            return False  # there: the usual wait (and joining fights)
        mobs = [XYZ(*m) for m in await mob_positions(self.client)]
        if any(distance(m, me) < ROOM_MOB_CLEAR for m in mobs):
            if self.__dict__.get("_held_for_mobs") != zone:
                self._held_for_mobs = zone
                logger.info("enemies near: holding here for the team to start that fight")
            return False
        self._held_for_mobs = None
        logger.info(f"to the objective at ({marker.x:.0f}, {marker.y:.0f}), "
                    f"{distance(me, marker):.0f} away (not trailing the team)")
        self.controller.allow_idle(40)
        try:
            if not await self._known_door_walk(zone, marker):
                await self._team_travel(marker)
                if await self.client.zone_name() == zone and distance(await self._position(), me) < 300:
                    # The walk got nowhere ("no way on foot"): beside the
                    # marker by teleport (clear of enemies), and walk in now
                    # rather than at the next step.
                    await self._teleport_walk_in(zone, marker)
        finally:
            self.controller.end_idle()
        await self._answer_dungeon_exit()
        self._last_progress_time = time.monotonic()
        return True

    async def _known_door_walk(self, zone: str, marker: XYZ) -> bool:
        """A door walk remembered in this zone (state/doors.json, the
        player's own walks included) at the marker: teleport to where it
        started and walk it. True if it went through."""
        from .safe_teleport import allow_teleport

        near = [e for e in self.doors.doors.get(zone, [])
                if len(e) > 2 and e[2] and is_team_up_zone(e[2])
                and math.dist(e[0], (marker.x, marker.y)) < KNOWN_DOOR_NEAR]
        if not near:
            return False
        door, start, dest = min(near, key=lambda e: math.dist(e[0], (marker.x, marker.y)))
        landing = XYZ(*start)
        mobs = [XYZ(*m) for m in await mob_positions(self.client)]
        if any(distance(m, landing) < ROOM_MOB_CLEAR for m in mobs):
            return False
        logger.info(f"through the door into {dest.split('/')[-1]} the way it was walked before")
        for _ in range(2):
            allow_teleport(self.client)
            await self.client.teleport(landing)
            await asyncio.sleep(TELEPORT_SETTLE)
            if not await self._zone_changed(zone):
                await self.walk_through(XYZ(door[0], door[1], landing.z), zone)
            await self._answer_dungeon_exit()
            if await self._zone_changed(zone):
                return True
        return False

    async def _teleport_walk_in(self, zone: str, marker: XYZ) -> bool:
        from .safe_teleport import allow_teleport

        me = await self._position()
        dx, dy = me.x - marker.x, me.y - marker.y
        k = GATE_CATCH_UP / (math.hypot(dx, dy) or 1.0)
        landing = XYZ(marker.x + dx * k, marker.y + dy * k, marker.z)
        mobs = [XYZ(*m) for m in await mob_positions(self.client)]
        if any(distance(m, landing) < ROOM_MOB_CLEAR for m in mobs):
            return False
        logger.info("the walk there got nowhere: teleporting beside the marker and walking in")
        allow_teleport(self.client)
        await self.client.teleport(landing)
        await asyncio.sleep(TELEPORT_SETTLE)
        if not await self._zone_changed(zone):
            await self.walk_through(marker, zone)
        await self._answer_dungeon_exit()
        return await self._zone_changed(zone)

    def _track_room(self, zone: str):
        """Keep the run's place in the dungeon's room order (a new run when
        outside the dungeon)."""
        from .teamup import advance_room, room_order

        order = room_order(zone)
        if not order:
            self._room_pos = -1
            self._final_potion = None  # a new run: its own kept potion
            return
        before = self._room_pos
        deaths = self.controller.deaths
        if deaths != self.__dict__.get("_room_deaths", deaths):
            # A defeat sent us somewhere (the entrance): not the run's next room.
            self._room_deaths = deaths
            return
        self._room_deaths = deaths
        self._room_pos = advance_room(order, before, zone)
        if self._room_pos != before:
            logger.debug(f"room order: {zone.split('/')[-1]} (room {self._room_pos + 1} of {len(order)})")

    async def _final_boss_potion(self):
        """Back in the last room of the run (the Waterworks' entrance, where
        the Drain Valve brings Sylster): the potion kept for him, once, if
        health is short."""
        from .teamup import room_order
        from .upkeep import team_potion

        order = room_order(await self.client.zone_name() or "")
        if not order or self._room_pos != len(order) - 1 or self.__dict__.get("_final_potion") == id(order):
            return
        self._final_potion = id(order)
        if not (self.upkeep and self.upkeep.use_potions):
            return
        hp, _mana = await health_mana(self.client)
        charges = await self.client.stats.potion_charge()
        if team_potion(hp, charges, before_final=True):
            logger.info(f"drinking the potion kept for the final boss (hp {hp:.0%}, {charges:.0f} left)")
            await ui.click(self.client, ui.POTION_BUTTON)
            await asyncio.sleep(1.5)

    async def _to_next_room(self, zone: str, seen: tuple) -> bool:
        """No teammate in this room of a dungeon with a known room order (the
        Waterworks): the team is ahead; teleport beside the gate to the next
        room and walk in (the player: keep up with the group by teleporting to
        the next gate). Come into an empty room: ROOM_ALONE_ADVANCE first, in
        case they're still on the way. True if it went."""
        from .safe_teleport import allow_teleport
        from .teamup import next_room, room_order

        order = room_order(zone)
        if not order:
            return False
        self._track_room(zone)
        if not seen[3] and time.monotonic() - seen[2] < ROOM_ALONE_ADVANCE:
            return False
        nxt = next_room(order, self._room_pos)
        if nxt is None or nxt == zone:
            return False
        # A door walk that worked before (state/doors.json: where we stood,
        # where we walked) beats a learned gate point: that one is only where
        # we stood on coming in the other way (room 03's "gate" to Luska's
        # room was in the wrong corner; it walked about there for 20 s).
        hops = self.doors.route(zone, nxt)
        walked = hops[0] if hops else None
        hop = gate_toward(zone, nxt, self._bad_gates)
        if walked is not None:
            _z, door, spot, _to = walked
            gate = XYZ(door[0], door[1], spot[2])
        elif hop is not None:
            gate = hop[0]
        else:
            return False
        me = await self._position()
        dx, dy = me.x - gate.x, me.y - gate.y
        k = GATE_CATCH_UP / (math.hypot(dx, dy) or 1.0)
        landing = XYZ(gate.x + dx * k, gate.y + dy * k, gate.z)
        if walked is not None:
            landing = XYZ(*walked[2])  # its own start spot (checked for enemies below too)
        # Never into a fight of our own (the player: enemies stand where the
        # bot can walk into them; join only fights a teammate is in): enemies
        # still up near us, the landing or the gate mean the team isn't
        # through here; wait for them instead.
        mobs = [XYZ(*m) for m in await mob_positions(self.client)]
        if any(distance(m, p) < ROOM_MOB_CLEAR for m in mobs for p in (me, landing, gate)):
            if self.__dict__.get("_room_mobs_said") != zone:
                self._room_mobs_said = zone
                logger.info(f"no teammate in {zone.split('/')[-1]}, but enemies by the way to "
                            f"{nxt.split('/')[-1]}: waiting for the team")
            return True  # (and no other way on: the marker's would walk into them)
        logger.info(f"no teammate in {zone.split('/')[-1]}: on to {nxt.split('/')[-1]} "
                    f"by the gate at ({gate.x:.0f}, {gate.y:.0f})")
        self.controller.allow_idle(30)
        try:
            allow_teleport(self.client)
            await self.client.teleport(landing)
            await asyncio.sleep(TELEPORT_SETTLE)
            if walked is not None and not await self._zone_changed(zone):
                await self.walk_through(gate, zone)
            if not await self._zone_changed(zone):
                await self.approach_and_walk(gate, zone)
            await self._answer_dungeon_exit()
            await wait_for_loading(self.client)
        finally:
            self.controller.end_idle()
        now = await self.client.zone_name() or ""
        if now != zone:
            logger.success(f"on to {now.split('/')[-1]} after the team")
            self._mate_seen = (now, await self._position(), time.monotonic(), False)
        return True

    async def _walk_to_boss_room(self) -> bool:
        """The fight step's boss has a known room and a known chain of doors
        leads there: walk the first door. True if it went (or tried)."""
        target = defeat_target(await self.objective() or "")
        room = DungeonMemory.load().bosses.get(target) if target else None
        zone = await self.client.zone_name() or ""
        if not room or room == zone:
            return False
        hops = self.doors.route(zone, room)
        if not hops:
            return False
        _z, door, spot, nxt = hops[0]
        logger.info(f"to {target}'s room: through the door at ({door[0]:.0f}, {door[1]:.0f}) into "
                    f"{nxt.split('/')[-1]}")
        await self.client.teleport(XYZ(*spot))
        await asyncio.sleep(TELEPORT_SETTLE)
        if await self._zone_changed(zone):
            return True
        await self.walk_through(XYZ(door[0], door[1], spot[2]), zone)
        return True

    async def _after_team_through_door(self) -> bool:
        """No teammate in sight for TEAM_LOST_AFTER, one last seen here: they
        went on to another room. First follow their tracks: land where the
        last one was seen and walk on the way they were going (they vanished
        through a door the bot hadn't used, heading north in Mount Olympus).
        Then the known doors of this zone, nearest to that spot first, each
        once. A door walk that works is remembered (state/doors.json). True
        if it went somewhere."""
        zone = await self.client.zone_name() or ""
        seen = self._mate_seen
        walking = walk_zone(self.client, zone)
        lost_after = TEAM_LOST_WALKING if walking else TEAM_LOST_AFTER
        if walking and (not seen or seen[0] != zone):
            # Just came into this room and nobody's here (they'd moved on
            # before it loaded): the clock starts now, from where we arrived.
            self._mate_seen = (zone, await self._position(), time.monotonic(), False)
            return False
        if not seen or seen[0] != zone or time.monotonic() - seen[2] < lost_after:
            return False
        if walking and await self._to_next_room(zone, seen):
            return True
        from .teamup import room_order as _room_order

        if _room_order(zone):
            # A dungeon with a known room order (the Waterworks): only that
            # moves us on. The door search walked into the exit at the start
            # of a run and sat on "leave the dungeon?" (the player).
            return False
        # seen[3] False: nobody seen in this room, seen[1] is where we arrived:
        # search its doors from there (it waited in one room while the team
        # was elsewhere).
        last = seen[1]
        key = (zone, round(last.x / 300), round(last.y / 300))
        tries = self._track_tries.get(key, 0)
        from .teamup import room_order

        if (walking and self._team_with_us and not (seen[3] and tries < TEAM_TRACK_TRIES)
                and not self.__dict__.get("_to_marker") and not room_order(zone)):
            # On foot and nobody to follow here (the team left this room before
            # we loaded in, or their tracks led nowhere): on toward the dungeon
            # quest's objective, which is where they're headed (the player).
            marker = await self.client.quest_position.position()
            me = await self._position()
            if distance(marker, XYZ(0, 0, 0)) > 1 and distance(me, marker) > TEAM_FOLLOW_WALKING:
                logger.info(f"no teammate in sight: on toward the dungeon's objective at "
                            f"({marker.x:.0f}, {marker.y:.0f})")
                self._to_marker = True
                try:
                    return await self._team_travel(marker)
                finally:
                    self._to_marker = False
        if seen[3] and tries < TEAM_TRACK_TRIES:
            self._track_tries[key] = tries + 1
            trail = [p for z, p in self._mate_trail if z == zone]
            prev = trail[-2] if len(trail) >= 2 else await self._position()
            dx, dy = last.x - prev.x, last.y - prev.y
            length = math.hypot(dx, dy) or 1.0
            step = TEAM_TRACK_AHEAD / length
            ahead = XYZ(last.x + dx * step, last.y + dy * step, last.z)
            if walking:
                # On foot: a straight walk on from where they vanished missed
                # room 01's gate to 08 (it's off that line). A known gate of
                # this room near their tracks is where they went: walk to it.
                gates = await self._doors_here(zone)
                gates += [XYZ(e[0][0], e[0][1], last.z) for e in self.doors.doors.get(zone, [])
                          if len(e) <= 2 or not e[2] or is_team_up_zone(e[2])]
                near = [g for g in gates if distance(g, last) < TEAM_GATE_NEAR]
                if near:
                    gate = min(near, key=lambda g: distance(g, ahead))
                    logger.info(f"the team went on; to the gate by their tracks at "
                                f"({gate.x:.0f}, {gate.y:.0f})")
                    # Teleported beside it (the player: walking after the
                    # team snagged on the scenery), then the short walk in.
                    from .safe_teleport import allow_teleport

                    me = await self._position()
                    dx, dy = me.x - gate.x, me.y - gate.y
                    k = GATE_CATCH_UP / (math.hypot(dx, dy) or 1.0)
                    allow_teleport(self.client)
                    await self.client.teleport(XYZ(gate.x + dx * k, gate.y + dy * k, gate.z))
                    await asyncio.sleep(TELEPORT_SETTLE)
                    await self.approach_and_walk(gate, zone)
                    await wait_for_loading(self.client)
                    now = await self.client.zone_name() or ""
                    if now != zone:
                        logger.success(f"followed the team into {now}")
                    return True
            logger.info(f"the team went on; following their tracks from ({last.x:.0f}, {last.y:.0f}) "
                        f"toward ({ahead.x:.0f}, {ahead.y:.0f})")
            await self.client.teleport(last)
            await asyncio.sleep(TELEPORT_SETTLE)
            start = await self._position()
            await self.client.goto(ahead.x, ahead.y)
            await asyncio.sleep(1.5)
            await wait_for_loading(self.client)
            now = await self.client.zone_name() or ""
            if now != zone:
                logger.success(f"followed the team into {now}")
                self.doors.record(zone, (ahead.x, ahead.y, ahead.z), (start.x, start.y, start.z), now)
            return True
        doors = await self._doors_here(zone)
        doors += [XYZ(e[0][0], e[0][1], last.z) for e in self.doors.doors.get(zone, [])]
        side_room = "/interiors/" in zone.lower()
        # Never a way out of the dungeon (the Throne Room's teleporter took it
        # out mid-search): doors known to lead elsewhere, or by a teleporter.
        exits = await self._entities_named_like(("teleporter",))
        from .travel_data import _data as _gates

        exits += [pos for pos, to in _gates()[0].get(zone, []) if not is_team_up_zone(to)]
        for e in self.doors.doors.get(zone, []):
            if len(e) > 2 and e[2] and not is_team_up_zone(e[2]):
                exits.append(XYZ(e[0][0], e[0][1], 0))
        for door in sorted(doors, key=lambda d: distance(d, last)):
            dkey = (zone, round(door.x / 100), round(door.y / 100))
            if dkey in self._mate_doors or any(math.dist((door.x, door.y), (x.x, x.y)) < 400 for x in exits):
                continue
            if not side_room:
                self._mate_doors.add(dkey)  # the main area's doors: each once (a room's way out: always)
            logger.info(f"looking for the team: through the door at ({door.x:.0f}, {door.y:.0f})")
            await self.approach_and_walk(door, zone)
            await self._answer_dungeon_exit()  # (an exit after all: stay, at once)
            return True
        return False

    async def _walk_into_circle(self, marker: XYZ) -> bool:
        """Land CIRCLE_WALK_FROM away from the duel circle nearest the marker
        and walk into it (the way a player starts a fight). True if it went."""
        from .safe_teleport import allow_engage

        # Loaded now or seen before here: far from the marker (Plague Oni's in
        # Shirataki Temple) none is loaded, and this did nothing.
        circles = [XYZ(*c) for c in await self._duel_circles(await self.client.zone_name() or "")]
        near = [c for c in circles if distance(c, marker) < CIRCLE_NEAR_MARKER]
        if not near:
            return False
        zone_now = await self.client.zone_name() or ""
        if zone_now in HAND_OVER_BOSS_ZONES:
            # Sprockets and Bellows: walking up to their circle froze the wizard
            # in a 0-opponent battle every time. The levers are done: the user
            # takes it from here.
            where = zone_now.split("/")[-1]
            self.controller.stop(f"levers done in {where}; handing the boss over to the player")
            return True
        circle = min(near, key=lambda c: distance(c, marker))
        zone = await self.client.zone_name() or ""
        below = self._floor_below(zone, circle)
        if below is not None:
            # The boss spawns when the wizard walks up the last staircase
            # (Sprockets, Counterweight East): start on the floor below and walk.
            start = below
            logger.info(f"the fight is on a duel circle up a floor: starting below it at "
                        f"({start.x:.0f}, {start.y:.0f}, {start.z:.0f}) and walking up")
        else:
            here = await self._position()
            dx, dy = here.x - circle.x, here.y - circle.y
            length = math.hypot(dx, dy) or 1.0
            back = CIRCLE_WALK_FROM / length
            start = XYZ(circle.x + dx * back, circle.y + dy * back, circle.z)
            logger.info(f"the fight is on a duel circle: landing {CIRCLE_WALK_FROM:.0f} away and walking in")
        gate = await self._gate_below(circle) if below is not None else None
        key = (self._last_progress[0] or "", zone)
        if key not in self._boss_waited:
            # Right after the last lever: give the boss time to spawn, standing
            # where we are, before going anywhere near its circle.
            self._boss_waited.add(key)
            logger.info(f"waiting {BOSS_SPAWN_WAIT:.0f}s for the boss to spawn before walking to it")
            self.controller.allow_idle(BOSS_SPAWN_WAIT + 10)
            try:
                await asyncio.sleep(BOSS_SPAWN_WAIT)
            finally:
                self.controller.end_idle()
            from .bossfarm import find_entity_named

            target = defeat_target(self._last_progress[0] or "")
            if target and await find_entity_named(self.client, target) is not None:
                return True  # it's there now: the next step goes after it
        if gate is not None:
            # The gate the last lever opened (Counterweight East's top floor):
            # land in front of it, walk through, and climb the stairs beyond
            # on foot; never teleport near the boss's circle.
            here = await self._position()
            dx, dy = here.x - gate.x, here.y - gate.y
            length = math.hypot(dx, dy) or 1.0
            front = XYZ(gate.x + dx / length * GATE_FRONT, gate.y + dy / length * GATE_FRONT, gate.z)
            beyond = XYZ(gate.x - dx / length * GATE_BEYOND, gate.y - dy / length * GATE_BEYOND, gate.z)
            logger.info(f"walking through the gate at ({gate.x:.0f}, {gate.y:.0f}, {gate.z:.0f}) "
                        "and up the stairs")
            await self.client.teleport(front)
            await asyncio.sleep(TELEPORT_SETTLE)
            await self.client.goto(beyond.x, beyond.y)
            await asyncio.sleep(0.5)
            await self._walk_route_to(circle, zone)
        else:
            if below is None or distance(await self._position(), start) > NEAR_START:
                await self.client.teleport(start)
                await asyncio.sleep(TELEPORT_SETTLE)
            if below is not None:
                await self._walk_route_to(circle, zone)
        allow_engage(self.client)
        await self.client.goto(circle.x, circle.y)
        await self._hold_for_fight()
        return True

    async def _walk_route_to(self, goal: XYZ, zone: str):
        """Walk (never teleport) to `goal` along the zone's walkway points, so
        stairs are climbed the way a player does (Sprockets spawns partway up
        the last staircase). Falls back to nothing when no route links up."""
        from .collect import walk_route

        here = await self._position()
        points = await path_points(self.client) + await self._landmarks()
        points += self.entity_map.spots(zone, lambda _n: True, (here.x, here.y, here.z))
        route = walk_route(points, (here.x, here.y, here.z), (goal.x, goal.y, goal.z))
        if not route:
            logger.info("no walkway route up to the fight; walking straight at it")
            return
        logger.info(f"walking up to the fight along {len(route)} walkway point(s)")
        for x, y, z in route[:-1]:  # the last one is the circle itself
            if await self.client.in_battle():
                return
            await self.client.goto(x, y)
            await asyncio.sleep(0.2)
            pos = await self._position()
            if math.dist((pos.x, pos.y), (x, y)) > 250:
                logger.debug(f"walkway point ({x:.0f}, {y:.0f}, {z:.0f}) not reached "
                             f"(at {pos.x:.0f}, {pos.y:.0f})")

    async def _hold_for_fight(self) -> bool:
        """Just walked into a fight's circle: stand still while it starts. A
        boss's entrance (Sprockets) plays out before the game says 'in battle';
        teleporting away meanwhile left a 'battle' with 0 opponents that froze
        the wizard. True once the fight is on."""
        self.controller.allow_idle(FIGHT_START_WAIT + 10)
        try:
            deadline = time.monotonic() + FIGHT_START_WAIT
            while time.monotonic() < deadline:
                if await self.client.in_battle():
                    return True
                await asyncio.sleep(1.0)  # the dialogue loop advances any cutscene talk
            logger.info(f"no fight started within {FIGHT_START_WAIT:.0f}s of walking in")
            return False
        finally:
            self.controller.end_idle()

    async def _gate_below(self, circle: XYZ) -> XYZ | None:
        """The gate object on the floor just under a raised fight (the one the
        last lever opens: DynaTrigger_MB_BigBen_Gate on Counterweight East's
        3300 floor), read from the zone's entities; None if there's none."""
        gates = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                name = (await t.object_name() or "").lower() if t else ""
                if "gate" not in name:
                    continue
                pos = await e.location()
                if 300 < circle.z - pos.z < FLOOR_BELOW_MAX:
                    gates.append(pos)
            except Exception:
                continue
        if not gates:
            return None
        top = max(g.z for g in gates)
        return min((g for g in gates if abs(g.z - top) < 150), key=lambda g: distance(g, circle))

    def _floor_below(self, zone: str, spot: XYZ) -> XYZ | None:
        """The nearest known spot on the floor under `spot` (between 300 and
        FLOOR_BELOW_MAX lower), to walk up from."""
        found = self.entity_map.spots(zone, lambda _n: True, (spot.x, spot.y, spot.z))
        lower = [p for p in found if 300 < spot.z - p[2] < FLOOR_BELOW_MAX]
        if not lower:
            return None
        top = max(p[2] for p in lower)  # the floor right under it
        same = [p for p in lower if abs(p[2] - top) < 150]
        best = min(same, key=lambda p: math.dist(p[:2], (spot.x, spot.y)))
        return XYZ(*best)

    async def _doors_here(self, zone: str) -> list[XYZ]:
        """Ways out of this zone: gates from the travel data and learned ones,
        and door/gate objects in the zone's entity list."""
        from .travel_data import _data

        found = [pos for pos, _to in _data()[0].get(zone, [])]
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                name = (await t.object_name() or "").lower() if t else ""
                if any(w in name for w in ("door", "gate", "portal", "entrance")) and "collision" not in name:
                    found.append(await e.location())
            except Exception:
                continue
        out: list[XYZ] = []
        for p in found:  # one per doorway
            if all(distance(p, q) > 300 for q in out):
                out.append(p)
        return out

    async def _search_doors_for(self, name: str, objective: str) -> bool:
        """X isn't where the quest said: walk through the doors around there,
        looking for X behind each; from the zones they lead to, their doors
        too (one layer deeper), then back to try the next. True if it acted."""
        zone = await self.client.zone_name() or ""
        st = self._npc_search.setdefault(objective, {"home": zone, "visited": set(), "depth": {zone: 0}})
        depth = st["depth"].setdefault(zone, NPC_SEARCH_DEPTH)
        marker = await self.client.quest_position.position()
        anchor = marker if distance(marker, XYZ(0, 0, 0)) > 1 else await self._position()
        if depth < NPC_SEARCH_DEPTH:
            for door in sorted(await self._doors_here(zone), key=lambda d: distance(d, anchor)):
                key = (zone, round(door.x / 100), round(door.y / 100))
                if key in st["visited"]:
                    continue
                st["visited"].add(key)
                logger.info(f"{name} isn't in {zone}: looking behind the door at "
                            f"({door.x:.0f}, {door.y:.0f})")
                if await self.approach_and_walk(door, zone):
                    new = await self.client.zone_name() or ""
                    st["depth"].setdefault(new, depth + 1)
                    return True
        if zone != st["home"]:
            back = st["home"] if depth <= 1 else None
            logger.info(f"no {name} behind these doors; going back")
            # The learned door that brought us here was the wrong way (Mavra
            # Flamewing: travel took it again and again for minutes).
            if self.doors.forget(st["home"], zone):
                logger.info(f"forgot the door from {st['home'].split('/')[-1]} into {zone.split('/')[-1]}")
            if back and await self.go_to_zone(back):
                return True
            # One layer in: back out the way we came (the arrival gate is learned).
            prev = next((z for z, d in st["depth"].items() if d == depth - 1), st["home"])
            return await self.go_to_zone(prev)
        return False

    async def _talk_to_named(self, objective: str) -> bool:
        """Walk up to the NPC a Talk To objective names (exact name, not an
        enemy) and talk; a few tries per objective. True if it went."""
        name = talk_target(objective)
        if not name or not self._may_try(objective, await self.client.zone_name() or "", "talk_named"):
            return False
        pos = await self._npc_named(name, near=await self._position())
        if pos is None:
            return False
        logger.info(f"{name} is here: walking up to talk")
        await self.travel(pos, npc=True)
        if not await wait_until_free(self.client, timeout=5):
            return True
        if await self.interact(objective):
            return True
        await self.client.send_key(Keycode.S, 0.3)
        await self.client.send_key(Keycode.W, 0.3)
        await asyncio.sleep(0.5)
        await self.interact(objective)
        return True

    async def _talk_to_npc_near(self, spot: XYZ, objective: str) -> bool:
        """Walk up to a named NPC (not an enemy) within NPC_NEAR_MARKER of
        `spot` and talk, once per objective. True if it talked."""
        from .givers import is_named_npc
        from .names import lang_name

        key = (objective, "npc-near")
        if key in self._puzzles_tried:
            return False
        try:
            mobs = {await m.global_id_full() for m in await self.client.get_mobs()}
        except Exception:
            mobs = set()
        best = None
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                code = await t.display_name() if t else ""
                display = await lang_name(self.client, code) if code else ""
                if not display or await e.global_id_full() in mobs:
                    continue
                if not is_named_npc(await t.object_name() or "", display, await e.list_behavior_names()):
                    continue
                pos = await e.location()
                d = distance(pos, spot)
                if d < NPC_NEAR_MARKER and (best is None or d < best[0]):
                    best = (d, display, pos)
            except Exception:
                continue
        if best is None:
            return False
        self._puzzles_tried.add(key)
        _d, name, pos = best
        logger.info(f"nothing to use at the marker; talking to {name} beside it")
        await self.travel(pos, npc=True)
        if not await wait_until_free(self.client, timeout=5):
            return True
        if not await self.interact(objective):
            await self.client.send_key(Keycode.S, 0.3)
            await self.client.send_key(Keycode.W, 0.3)
            await asyncio.sleep(0.5)
            await self.interact(objective)
        return True

    async def _walk_in_from_entrance(self, objective: str, target: XYZ, zone: str) -> bool:
        """In a dungeon, the person to talk to isn't at the marker (the Jade
        Champion in the Emperor's Throne Room: the palace was empty after a
        teleport straight to it). Scenes like that start on walking in, so
        once per objective: back to the entrance, walk to the marker in legs,
        looking for them after each. True if it found and talked to them."""
        name = talk_target(objective) or defeat_target(objective)
        fight = talk_target(objective) is None  # a Defeat: the boss shows (and attacks) on the way in
        entry = DungeonMemory.load().dungeons.get(zone)
        if not name or entry is None or not entry.spawn or not self._may_try(objective, zone, "walk_in"):
            return False
        spawn = XYZ(*entry.spawn)
        logger.info(f"{name} isn't at the marker: walking in from the entrance, as a player would")
        await self.client.teleport(spawn)
        await asyncio.sleep(1.0)
        for i in range(1, WALK_IN_LEGS + 1):
            leg = XYZ(spawn.x + (target.x - spawn.x) * i / WALK_IN_LEGS,
                      spawn.y + (target.y - spawn.y) * i / WALK_IN_LEGS, spawn.z)
            await self.client.goto(leg.x, leg.y)
            await asyncio.sleep(0.5)
            if not await is_free(self.client) or await self.client.zone_name() != zone:
                return True  # a scene, dialogue or fight started, or a door took us on
            if fight:
                continue
            pos = await self._npc_named(name, near=await self._position())
            if pos is not None:
                logger.success(f"{name} appeared on the way in")
                return await self._talk_to_named(objective)
        logger.info(f"walked in from the entrance; still no {name}")
        return False

    async def _reenter_for_npc(self, objective: str, zone: str) -> bool:
        """The person still isn't in this dungeon (its copy came up without
        the Jade Champion): leave (Recall to the mark at its entrance, else
        walk out) and go back in for a fresh copy. Once per objective."""
        entry = DungeonMemory.load().dungeons.get(zone)
        if entry is None or not entry.sigil or not self._may_try(objective, zone, "reenter"):
            return False
        warrens = self.__dict__.get("_pre_boss_rooms", load_pre_boss_rooms()).values()
        if getattr(self, "_in_pre_boss_warren", False) or zone in warrens:
            return False  # (Ullik comes after the warren's rooms: leaving reset it)
        who = talk_target(objective) or defeat_target(objective)
        if norm(who or "") in LATE_BOSSES:
            return False  # (appears only later in this copy: leaving reset the levers)
        logger.info(f"{who} isn't in this copy of {zone.split('/')[-1]}: leaving and going back in")
        out = False
        if self._mark and self._mark.zone == entry.outside:
            out = await self._recall(entry.outside, "the mark at the dungeon's entrance")
        if not out:
            await self.go_to_zone(entry.outside)
        if await self.client.zone_name() != entry.outside:
            logger.warning("couldn't get out of the dungeon to go back in")
            return False
        await self._enter_by_sigil(XYZ(*entry.sigil), entry.outside)
        return True

    async def _find_npc(self, name: str) -> XYZ | None:
        """An NPC who isn't loaded here yet (no quest marker points at them:
        Ken Shui, visited for his quests): where they were seen before, then
        landmarks across the zone, checking at each stop. Ends near them."""
        pos = await self._npc_named(name, near=await self._position())
        if pos is not None:
            return pos
        zone = await self.client.zone_name() or ""
        start = await self._position()
        want = _norm_name(name)
        seen = self.entity_map.spots(zone, lambda n: _norm_name(n) == want, (start.x, start.y, start.z))
        spots = seen[:3] + spread_points(
            await self._landmarks() + floor_points(await path_points(self.client), start.z),
            (start.x, start.y, start.z), FAR_SWEEP_SPACING,
        )[:FAR_SWEEP_MAX]
        logger.info(f"looking for {name} around {zone.split('/')[-1]} ({len(spots)} spots)")
        for p in spots:
            if not await is_free(self.client):
                return None
            spot = XYZ(*p)
            if not await self._clear_spot(spot):
                continue
            await self.client.teleport(spot)
            await asyncio.sleep(0.6)
            await scan_entities(self.client, zone, self.entity_map)
            pos = await self._npc_named(name, near=await self._position())
            if pos is not None:
                logger.success(f"found {name} at ({pos.x:.0f}, {pos.y:.0f})")
                return pos
        return None

    async def _names_here(self, zone: str) -> list[str]:
        """Names of the things in this zone that aren't enemies: on the map
        and in view now (for closest_name)."""
        from .combat.sim import load_stats
        from .names import lang_name

        enemies = set(load_stats().get("enemies", {}))
        names = list(self.entity_map.zones.get(zone, {}))
        try:
            mobs = {await m.global_id_full() for m in await self.client.get_mobs()}
            for e in await self.client.get_base_entity_list():
                try:
                    if await e.global_id_full() in mobs:
                        continue
                    t = await e.object_template()
                    code = await t.display_name() if t else ""
                    if code:
                        names.append(await lang_name(self.client, code))
                except Exception:
                    continue
        except Exception:
            pass
        return [n for n in dict.fromkeys(names) if n and n not in enemies]

    async def _resolve_name(self, want: str, zone: str, kind: str) -> str:
        """`want` as the zone actually names it (closest_name), looked up
        once per name and zone, at once rather than after sweeps."""
        aliases = self.__dict__.setdefault("_name_aliases", {})
        key = (want, zone)
        if key in aliases:
            return aliases[key]
        # (Nothing like it yet: looked up again after a while, as it may load.)
        checked = self.__dict__.setdefault("_name_checked", {})
        if time.monotonic() - checked.get(key, -1e9) < NAME_RECHECK_SECONDS:
            return want
        checked[key] = time.monotonic()
        names = await self._names_here(zone)
        if any(same_object_name(n, want) for n in names):
            aliases[key] = want
            return want
        used = self.__dict__.setdefault("_used_names", {}).get(zone, set()) if kind == "use" else ()
        found = closest_name(want, names, used)
        if found:
            aliases[key] = found
            logger.info(f"{kind} {want!r}: nothing here by that name; going by {found!r}, "
                        "the closest name in the zone")
        return found or want

    async def _npc_named(self, name: str, near: XYZ | None = None, skip: list | None = None):
        """Position of an entity named exactly `name` that isn't an enemy
        ('Clockwork', not the 'Clockwork Warrior' mobs), or None; with `near`,
        the one closest to it."""
        from .names import lang_name

        try:
            mobs = {await m.global_id_full() for m in await self.client.get_mobs()}
        except Exception:
            mobs = set()
        found: list[XYZ] = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                code = await t.display_name() if t else ""
                if not code or not same_object_name(await lang_name(self.client, code), name):
                    continue
                if await e.global_id_full() in mobs:
                    continue
                pos = await e.location()
                if skip and any(distance(pos, u) < 150 for u in skip):
                    continue  # (used already)
                if near is None:
                    return pos
                found.append(pos)
            except Exception:
                continue
        return min(found, key=lambda q: distance(q, near)) if found else None

    async def _named_enemy_here(self, objective: str) -> bool:
        """Is the enemy the objective names (Defeat X, or Talk To an enemy) in this zone?"""
        if await self._talk_target_enemy(objective or "") is not None:
            return True
        from .bossfarm import find_entity_named

        for name in defeat_names(objective or ""):
            if await find_entity_named(self.client, name) is not None:
                return True
        return False

    async def _fight_ahead(self, objective: str) -> bool:
        """Is the next thing to do a fight (a Defeat objective, a step the book
        marks as a fight, or a Talk To someone who is still an enemy here)?"""
        if is_combat_objective(objective or "") or self._step_is_fight:
            return True
        return await self._talk_target_enemy(objective or "") is not None

    async def _seek_target(self, objective: str, zone: str) -> bool:
        """'Defeat X' with no X in view: rather than the quest marker (for the
        Supply Runners it sat among Ronin Keyholders, whose patrols walked into
        us there), go to the nearest spot X was seen that has no other kind of
        enemy near it now, then after a lone X from there. True if it acted."""
        from .bossfarm import find_entity_named, mobs_named
        from .safe_teleport import is_target

        names = defeat_names(objective)
        if not names or any([await find_entity_named(self.client, n) for n in names]):
            return False  # in view: pull_mob goes after it
        # The boss standing as itself before its fight ("Malistaire"): pull_mob
        # walks up to it. Teleporting between the spots Malistaire Drake was
        # seen looped for minutes and never let it.
        first = names[0].split()[0]
        stand_in = len(first) >= 5 and first.lower() != names[0].lower()
        if stand_in and await find_entity_named(self.client, first):
            return False
        if not self._may_try(objective, zone, "seek_target"):
            return False
        me = await self._position()
        seen = self.entity_map.spots(zone, lambda n: is_target(n, names), (me.x, me.y, me.z))
        if not seen:
            return False
        strangers = [p for n, p in await mobs_named(self.client) if not is_target(n, names)]
        for spot in seen[:SEEK_SPOTS]:
            s = XYZ(*spot)
            if distance(s, me) < SEEK_NEAR or any(distance(s, o) < SEEK_CLEAR for o in strangers):
                continue
            logger.info(f"looking for {names[0]} where it was seen, clear of other enemies "
                        f"({s.x:.0f}, {s.y:.0f})")
            await self.client.teleport(s)
            await asyncio.sleep(1.0)
            if await self.client.in_battle():
                return True
            target = await self._lone_target(names[0])
            if target is not None:
                allow_engage(self.client)  # a lone one in view: go
                await self.client.teleport(target)
                await asyncio.sleep(3.0)
            return True
        return False

    async def _engage(self, target: str, pos: XYZ, objective: str, zone: str, walk: bool = False) -> bool:
        """Start the fight with `target` at `pos`: land on it; after a landing
        that started nothing (Malistaire: his fight comes with a cutscene that
        walking up to him triggers), or with `walk`, back off, further each
        time, and walk in. True if a fight started."""
        misses = self.__dict__.setdefault("_engage_misses", {})
        key = (objective, zone, target)
        miss = misses.get(key, 0)
        if miss >= ENGAGE_RESET_MISSES and await self._reenter_for_npc(objective, zone):
            # A boss that never starts its fight: this copy of the dungeon is
            # broken (Zanga Zebu in the King's Tomb, a known game bug after
            # leaving and coming back in): a fresh copy (the player's notes).
            misses.pop(key, None)
            return False
        # Walking in tried ENGAGE_WALK_TRIES times for nothing: every other try
        # a landing on him again (Zanga Zebu: 25 walk-ins from 2,185 away, no
        # fight; the teleport is the player's way outside the Waterworks).
        land_again = not walk and miss >= ENGAGE_WALK_TRIES and miss % 2 == 1
        if (miss or walk) and not land_again:
            self._walked_in_at = time.monotonic()
            # The player: from where we fought the room's last enemies, walk in
            # (teleported near him, Malistaire showed but never fully loaded,
            # and his cutscene's trigger is further out); further back each try.
            origin = self._walk_in_origin(target, pos, zone)
            if origin is not None:
                # Always from the fight's own spot (floor we stood on: starts
                # pushed further back ended inside the lair's rock), along a
                # path around the walls.
                logger.info(f"walking up to {target} from where we fought, "
                            f"{distance(origin, pos):.0f} away (try {miss + 1})")
                await self.client.teleport(origin)
                await asyncio.sleep(2.0)
                from .walkmap import walk_path

                path = None if await self.client.in_battle() else await walk_path(zone, origin, pos)
                self._walked_in_at = time.monotonic()
                if path:
                    logger.info(f"following a {len(path)}-waypoint path around the walls")
                    if await self._follow_path(path, zone) is False:
                        return False  # (off a ledge: no straight walk at him after it either)
            else:
                back = min(ENGAGE_BACKOFF * (miss + 1), ENGAGE_BACKOFF_MAX)
                me = await self._position()
                dx, dy = me.x - pos.x, me.y - pos.y
                norm = math.hypot(dx, dy)
                if norm < 50.0:  # (standing on it: any direction)
                    dx, dy, norm = 1.0, 0.0, 1.0
                start = XYZ(pos.x + dx / norm * back, pos.y + dy / norm * back, pos.z)
                logger.info(f"walking up to {target} from {back:.0f} away (try {miss + 1})")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.client.goto(start.x, start.y), 20)
                await asyncio.sleep(1.0)
            if not await self.client.in_battle():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.client.goto(pos.x, pos.y), 45)
                await asyncio.sleep(4.0)
        else:
            logger.info(f"going after {target} for {objective!r}" + (f" (landing on him, try {miss + 1})"
                                                                   if land_again else ""))
            allow_engage(self.client)  # this teleport is meant to start the fight
            await self.client.teleport(pos)
            await asyncio.sleep(3.0)
        if await self.client.in_battle() or not await is_free(self.client):
            misses.pop(key, None)  # (a cutscene or dialogue: the dialogue loop has it)
            return True
        misses[key] = miss + 1
        return False

    def _walk_in_origin(self, target: str, pos: XYZ, zone: str) -> XYZ | None:
        """Where a walk up to `target` starts: our last fight in this zone, else
        the nearest spot another enemy was seen here (not one of the zone's
        objects, 'DS_DragonEye'), at least WALK_IN_MIN from it."""
        from .dungeons import last_fight

        p = last_fight(zone)
        if p is not None and distance(XYZ(*p), pos) >= WALK_IN_MIN:
            return XYZ(*p)
        key = target.lower()

        def enemy_like(n: str) -> bool:
            return " " in n and "_" not in n and key not in n.lower() and n.lower() not in key

        spots = [XYZ(*s) for s in self.entity_map.spots(zone, enemy_like, (pos.x, pos.y, pos.z))]
        return next((s for s in spots if distance(s, pos) >= WALK_IN_MIN), None)

    async def _investigate(self, objective: str, zone: str) -> bool:
        """'Investigate Plunkett House' with no marker (the player: try the
        objects that can be used until the right one): walk up to each
        selectable object here (MB_KT-PreCel_Relic_01..03), nearest first,
        and press X at its prompt, one per step, until the objective moves
        on. Any other objective with no marker: only a Questlight (the
        player: 'Complete Magic Wheel Training in Selenopolis' is through the
        door in the Blended Grove, KT_Questlight). True if it tried one."""
        investigate = objective.strip().lower().startswith("investigate")
        done = self.__dict__.setdefault("_investigated", {}).setdefault((objective, zone), set())
        me = await self._position()
        found = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                if not t:
                    continue
                obj = await t.object_name() or ""
                pos = await e.location()
                if investigable(obj, await e.list_behavior_names()) and (
                        investigate or "questlight" in obj.lower()):
                    found.append((distance(pos, me), (obj, round(pos.x), round(pos.y)), obj, pos))
            except Exception:
                continue
        left = sorted(f for f in found if f[1] not in done)
        if not left:
            if found and done:
                logger.info(f"tried every object here for {objective!r}; going round again")
                done.clear()
            return False
        _d, key, obj, pos = left[0]
        done.add(key)
        logger.info(f"investigating: {obj} ({len(left) - 1} more to try)")
        if not await self.bring_out._use(pos):
            logger.info(f"no prompt at {obj}")
        return True

    async def _pre_bosses(self, objective: str, zone: str) -> bool:
        """'Defeat Jotun': his brothers first, each in its side dungeon (by
        its sigil), so he fights alone (PRE_BOSSES). True if it acted."""
        if self._grinding:
            return False  # (only for the quest being followed, not the game's tracked one while grinding)
        target = defeat_target(objective) or ""
        todo = pending_pre_bosses(target, load_pre_bosses_beaten())
        if not todo:
            return False
        boss, sigil_zone, sigil = todo[0]
        rooms = self.__dict__.setdefault("_pre_boss_rooms", load_pre_boss_rooms())
        inside = rooms.get(boss)
        in_warren = inside and (zone == inside or (
            zone != sigil_zone and "HallofKings" not in zone and await self._in_any_dungeon(zone)
            and zone.split("/")[:2] == inside.split("/")[:2]))
        if in_warren and await self._scout_for_brother(boss, zone, objective):
            return True
        if in_warren:
            # In its warren (any of its rooms): the usual ways to a boss that
            # isn't in view (its duel circle, clearing the rooms) with it as
            # the target.
            self._in_pre_boss_warren = True
            try:
                await self.pull_mob(f"Defeat {boss} in {zone}")
            finally:
                self._in_pre_boss_warren = False
            return True
        if zone != sigil_zone:
            logger.info(f"{target} fights alone once {boss} is beaten: to {boss}'s dungeon first")
            world = sigil_zone.split("/", 1)[0]
            if zone.split("/", 1)[0] != world:
                # (From Celestia: go_to_zone has no gates to another world.)
                await self._to_world(world, f"to {boss}'s dungeon")
                return True
            if not await self.go_to_zone(sigil_zone):
                from .dungeon_heal import go_to_hub

                await go_to_hub(self.client)  # (out of a warren with no gate known back)
            return True
        logger.info(f"{target} fights alone once {boss} is beaten: into {boss}'s dungeon")
        spot = XYZ(*sigil)
        await self.travel(spot)
        if await self._enter_by_sigil(spot, zone):
            self._pre_boss_rooms[boss] = await self.client.zone_name() or ""
            with contextlib.suppress(OSError):
                PRE_BOSS_ROOMS_FILE.write_text(json.dumps(self._pre_boss_rooms), encoding="utf-8")
            logger.info(f"in {boss}'s dungeon: {self._pre_boss_rooms[boss]}")
        return True

    async def _scout_for_brother(self, boss: str, zone: str, objective: str) -> bool:
        """The player's: in the warren, no fights but his. In view, or seen
        here before: go and walk into his fight. Else teleport over the
        zone's walkable ground (landing clear of enemies) looking for him.
        False once the whole warren was scouted without him (then the rooms
        are cleared the usual way). True if it acted."""
        from .bossfarm import find_entity_named
        from .walkmap import nav_points

        if await self._warren_gate_boss(boss, zone, objective):
            return True
        blocked = self.__dict__.setdefault("_brother_blocked", {})
        wins = len(self.__dict__.get("_won_names", ()))
        if blocked.get(zone, (0, -1))[0] >= WARREN_BLOCKED_TRIES:
            if blocked[zone][1] == wins:
                # Behind a gate that a boss of the warren opens (the player:
                # one whose name we don't know): fight through the rooms until
                # a fight is won, then try his gate again.
                return False
            blocked[zone] = (0, wins)
        pos = await find_entity_named(self.client, boss)
        me = await self._position()
        if pos is None:
            want = _norm_name(boss)
            seen = self.entity_map.spots(zone, lambda n: _norm_name(n) == want, (me.x, me.y, me.z))
            if seen and distance(XYZ(*seen[0]), me) > 600:
                logger.info(f"{boss} was seen at ({seen[0][0]:.0f}, {seen[0][1]:.0f}): going straight there")
                await self.client.teleport(XYZ(*seen[0]))
                await asyncio.sleep(1.5)
                pos = await find_entity_named(self.client, boss)
        if pos is not None:
            logger.info(f"{boss} found: straight into his fight, skipping the rest of the warren")
            self._wanted_fight_until = time.monotonic() + WANTED_FIGHT_SECONDS
            await self._mark_before_boss(boss, objective, zone)
            if not await self._engage(boss, pos, objective, zone, walk=True):
                tries = blocked.get(zone, (0, wins))[0] + 1
                blocked[zone] = (tries, wins)
                if tries >= WARREN_BLOCKED_TRIES:
                    logger.info(f"{boss} can't be reached ({tries} walks started nothing): "
                                "fighting the warren's rooms for the boss that opens his gate")
            return True
        # Something to use in view (the Storm room's Yardbird, a lever, a
        # chest): use it, clear of enemies (the player: interact, don't fight).
        if await self._use_nearby_object(zone):
            return True
        done = self.__dict__.setdefault("_warren_scouted", {}).setdefault(zone, [])
        points = spread_points(await nav_points(zone), (me.x, me.y), WARREN_SCOUT_SPACING)
        left = [p for p in points if all(math.dist(p[:2], d[:2]) > WARREN_SCOUT_SPACING / 2 for d in done)]
        if not left:
            rounds = self.__dict__.setdefault("_warren_rounds", {})
            rounds[zone] = rounds.get(zone, 0) + 1
            if rounds[zone] < WARREN_SCOUT_ROUNDS:
                done.clear()  # (once more: what was used may have opened a way)
                return True
            return False
        logger.info(f"scouting {zone.split('/')[-1]} for {boss}, clear of fights ({len(left)} spots left)")
        for p in left[:WARREN_SCOUT_BATCH]:
            if await self.client.in_battle() or not await is_free(self.client):
                return True
            done.append(p)
            if not await self._clear_spot(XYZ(*p)):
                continue
            await self.client.teleport(XYZ(*p))
            await asyncio.sleep(1.0)
            await scan_entities(self.client, zone, self.entity_map)
            if await find_entity_named(self.client, boss) is not None:
                return True  # (the next step goes for him)
            if await self._use_nearby_object(zone):
                return True
        return True

    async def _warren_gate_boss(self, boss: str, zone: str, objective: str) -> bool:
        """A boss that opens the way to `boss` (WARREN_GATE_BOSSES), not beaten
        yet: in view, or where it was seen in this zone, go and fight it.
        Seen there before and not there now: beaten in this run. True if it acted."""
        from .bossfarm import find_entity_named

        won = self.__dict__.get("_won_names", set())
        gone = self.__dict__.setdefault("_gate_gone", set())
        for gate in WARREN_GATE_BOSSES.get(boss, ()):
            if norm(gate) in won or (zone, gate) in gone:
                continue
            pos = await find_entity_named(self.client, gate)
            if pos is None:
                me = await self._position()
                want = _norm_name(gate)
                seen = self.entity_map.spots(zone, lambda n, w=want: _norm_name(n) == w, (me.x, me.y, me.z))
                if not seen:
                    continue  # (not found yet: scouting looks for it)
                logger.info(f"{gate} opens the way to {boss}: to where it was seen")
                await self.client.teleport(XYZ(*seen[0]))
                await asyncio.sleep(1.5)
                if await self.client.in_battle():
                    return True
                pos = await find_entity_named(self.client, gate)
                if pos is None:
                    logger.info(f"no {gate} where it was: beaten already")
                    gone.add((zone, gate))
                    continue
            logger.info(f"{gate} opens the way to {boss}: fighting it")
            self._wanted_fight_until = time.monotonic() + WANTED_FIGHT_SECONDS
            await self._engage(gate, pos, objective, zone)
            return True
        return False

    async def _use_nearby_object(self, zone: str) -> bool:
        """A selectable object in view (not a person, not a door used
        already), clear of enemies: walk up to it and press X. Each once.
        True if it tried one."""
        used = self.__dict__.setdefault("_warren_used", set())
        me = await self._position()
        found = []
        for e in await self.client.get_base_entity_list():
            try:
                t = await e.object_template()
                if not t:
                    continue
                obj = await t.object_name() or ""
                if not investigable(obj, await e.list_behavior_names()):
                    continue
                pos = await e.location()
                key = (zone, obj, round(pos.x / 100), round(pos.y / 100))
                if key in used or distance(pos, me) > WARREN_USE_RANGE:
                    continue
                found.append((distance(pos, me), key, obj, pos))
            except Exception:
                continue
        for _d, key, obj, pos in sorted(found):
            used.add(key)
            if not await self._clear_spot(pos):
                continue  # (enemies by it: not walking into a fight)
            logger.info(f"using {obj} on the way (no fights)")
            if not await self.bring_out._use(pos):
                logger.info(f"no prompt at {obj}")
            return True
        return False

    def _grind_world(self) -> str | None:
        """Where to fight for experience: the highest-level world we've been
        to (the player: Celestia's enemies give far more than Grizzleheim's,
        where 50 fights moved the bar 8%), else the main world."""
        known = set(self.__dict__.get("_win_zones", {}))
        for w in GRIND_WORLDS:
            if w in known or w in GRIND_FALLBACK:
                return w
        return self._main_world

    async def _grind_beside_set_aside(self, objective: str | None) -> bool:
        """Grinding while the game still tracks a set-aside quest's objective:
        not walked into (Jotun's trio); fight outdoors here, else where a
        fight was last won outdoors in the main world. True if it acted."""
        if self._grinding and await self._detour_ask():
            return True  # (the detour's or the main story's next quest first: never grinding for it)
        waiting = {d.get("objective") for d in self.setbacks.deferred.values()}
        place = objective_zone(objective or "")
        world = self._grind_world()
        elsewhere = bool(place and world and place.split("/", 1)[0] != world)
        # (Or another world's: Wysteria's 'Go To Spiral Cup' tracked by the game
        # led the grinding wizard to the Spiral Map again and again.)
        if not self._grinding or (objective not in waiting and not elsewhere):
            return False
        here = await self.client.zone_name() or ""
        indoors = "interiors" in here.lower() or await self._in_any_dungeon(here)
        if not indoors and await self.sprinter.get_mobs():
            await self.pull_mob("")
        elif not indoors and await self._to_enemy_spot(here):
            pass  # (enemies load only nearby: went where some were seen)
        elif world and here.split("/", 1)[0] != world:
            await self._to_world(world, f"grinding in {world}")
        elif (spot := self.__dict__.get("_win_zones", {}).get(world or "", GRIND_FALLBACK.get(world or ""))
              ) and spot != here:
            logger.info(f"grinding: to {spot.split('/')[-1]} for its enemies")
            if not await self.go_to_zone(spot):
                await asyncio.sleep(5.0)
        else:
            await asyncio.sleep(2.0)
        self._ground_at = time.monotonic()
        return True

    async def _to_enemy_spot(self, zone: str) -> bool:
        """Grinding with no enemy in view (they load only nearby): teleport to
        the next spot here where an enemy we've fought was seen. True if it
        went."""
        import json as _json

        try:
            fought = set(_json.loads(Path("state", "enemy_stats.json").read_text(encoding="utf-8"))
                         .get("enemies", {}))
        except (OSError, ValueError):
            fought = set()
        if not fought:
            return False
        me = await self._position()
        spots = self.entity_map.spots(zone, lambda n: n in fought, (me.x, me.y, me.z))
        spots = [s for s in spots if distance(XYZ(*s), me) > 800]
        if not spots:
            return False
        i = self.__dict__.get("_enemy_spot_i", 0) % len(spots)
        self._enemy_spot_i = i + 1
        x, y = spots[i][0], spots[i][1]
        logger.info(f"grinding: no enemies in view; to where some were seen ({x:.0f}, {y:.0f})")
        await self.client.teleport(XYZ(*spots[i]))
        await asyncio.sleep(1.5)
        return True

    async def _lone_target(self, name: str) -> XYZ | None:
        """The nearest enemy called `name` with no other kind of enemy within
        LONE_TARGET_CLEARANCE (those would join, or start the fight instead)."""
        from .bossfarm import mobs_named

        want = "".join(c for c in name.lower() if c.isalpha())

        def is_target(n: str) -> bool:
            key = "".join(c for c in n.lower() if c.isalpha())
            return bool(key) and (want in key or key in want)

        mobs = await mobs_named(self.client)
        targets = [p for n, p in mobs if is_target(n)]
        others = [p for n, p in mobs if not is_target(n)]
        me = await self._position()
        clean = [t for t in targets if all(distance(t, o) > LONE_TARGET_CLEARANCE for o in others)]
        return min(clean, key=lambda t: distance(t, me)) if clean else None

    async def pull_mob(self, objective: str = ""):
        """For defeat objectives: teleport onto the enemy the objective names
        ("Defeat Gobbler Gorger ..."), else the closest mob, to start a fight."""
        if is_team_up_zone(await self.client.zone_name() or ""):
            return  # with a team, the team starts fights (see _team_step)
        target = defeat_target(objective)
        if target:
            from .bossfarm import find_entity_named

            pos = None
            for name in defeat_names(objective):  # "Any Sphinx Sokkwi": any Sokkwi
                pos = await find_entity_named(self.client, name)
                if pos is None and name.endswith("s"):
                    pos = await find_entity_named(self.client, name[:-1])  # "Lost Souls"
                if pos is not None:
                    target = name
                    break
            if pos is not None:
                # Of the ones in view, one with no other enemies by it: landing on
                # the first Otomo Supply Runner put us among Ronin Keyholders, and
                # they (not the runner) started six fights in a row.
                clean = await self._lone_target(target)
                zone_now = await self.client.zone_name() or ""
                if clean is None and self._may_try(objective, zone_now, "lone_wait"):
                    logger.info(f"every {target} in view has other enemies by it; waiting for a clear one")
                    await asyncio.sleep(3.0)
                    return
                pos = clean or pos  # (always in company, like a boss's guards: go anyway)
                # The deck for this encounter, now that the enemy is in view
                # (Vasek Ashweaver loaded only on the way in: the step's check
                # before saw nobody, and the fight went in on the AoE deck).
                adapter = getattr(self, "deck_adapter", None)
                if adapter is not None:
                    adapter.prepare_for(target, await self._alone_at(pos))
                    if adapter._pending is not None:
                        logger.info(f"{target} in view: the right deck goes in before the fight")
                        return  # (the next step's deck tick switches it, clear of enemies)
                await self._mark_before_boss(target, objective, zone_now)
                if await self._engage(target, pos, objective, zone_now):
                    return
            else:
                where = objective_zone(objective)
                here_zone = await self.client.zone_name() or ""
                # Inside a dungeon, or a room entered from the objective's place
                # (Usunoki in the Town Dojo, 'in Village of Sorrow'), it's here:
                # returning silently looped every 2 s for minutes.
                inside = await self._in_dungeon(here_zone) or bool(
                    where and self.doors.leading_to(where, here_zone))
                if where and where != here_zone and not inside:
                    return  # "... in Hall of Champions": not here; the quest marker leads there
                # A locked door whose key the player told us about (Malistaire's:
                # the Crystal at the end of the Dragon's Maw or the Howling Cave).
                if await self._fetch_door_key(objective, target):
                    return
                # The enemy nowhere in view and a duel circle in this zone: its
                # fight first (the player: there's nearly always one to win before
                # going on; Gurtok Firebender before Malistaire's door).
                if await self._fight_zone_boss(objective, here_zone):
                    return
                # A boss that hasn't come out yet (Malistaire: his Soul Servants
                # first): in a dungeon or a room, beat the enemies around it.
                # (Not for a boss who needs the dungeon's own tasks instead:
                # Sylster after the Waterworks' levers. The player: skip as
                # many fights as possible there.)
                if ((inside or "/interiors/" in here_zone.lower()) and norm(target or "") not in LATE_BOSSES
                        and await self._clear_dungeon(objective, here_zone)):
                    return
                # The boss standing as itself before its cutscene ("Malistaire"
                # for "Malistaire Drake"): walking up to it starts the fight.
                # (After the room is clear: walk-ins with his Soul Servants
                # still up, the dungeon reset, started nothing.)
                first = target.split()[0]
                if len(first) >= 5 and first.lower() != target.lower():
                    from .bossfarm import mobs_named

                    stand_in = await find_entity_named(self.client, first)
                    mobs = [p for _n, p in await mobs_named(self.client)]
                    if stand_in is not None and all(distance(stand_in, p) > 50 for p in mobs):
                        await self._mark_before_boss(first, objective, here_zone)
                        await self._engage(first, stand_in, objective, here_zone, walk=True)
                        return
                # At the marker with the enemy nowhere in the zone: the marker is
                # the way to it (a teleporter like the Djeserit tomb's "To the
                # Sarcophagus", a door): use its X prompt first.
                marker = await self.client.quest_position.position()
                zone_now = await self.client.zone_name() or ""
                at_marker = distance(await self._position(), marker) < MARKER_WAY_RANGE
                if _real_marker(marker) and at_marker and self._may_try(
                    objective, zone_now, "marker_x"
                ):
                    if await self._press_x_here(zone_now, adjust=True):
                        return
                    if not await is_free(self.client):
                        return
                # A boss remembered in another room (War Oni in a Crimson Fields
                # battlefield): the marker here is that room's door, so go
                # through it rather than sweep this zone for it.
                room = DungeonMemory.load().bosses.get(target)
                # (Not for a brother fought in his own dungeon first: Ullik was
                # remembered from Jotun's hall, and each step walked out of
                # Helgrind Warren toward it and back in.)
                if any(norm(target) == norm(b) for plan in PRE_BOSSES.values() for b, _z, _s in plan):
                    room = None
                if (room and room != zone_now and _real_marker(marker)
                        and self._may_try(objective, zone_now, "boss_room_door")):
                    logger.info(f"{target} is in {room.split('/')[-1]}: through the quest marker's door")
                    if distance(await self._position(), marker) < DOOR_NEAR:
                        await self.walk_through(marker, zone_now)
                    else:
                        await self.travel(marker)
                    return
                # A door walked through before, by the marker (Nomoonaga's tower
                # gate in the Tree of Life, into MS_Death3_T4): through it.
                if _real_marker(marker) and self._may_try(objective, zone_now, "known_door"):
                    near_doors = [e for e in self.doors.doors.get(zone_now, [])
                                  if math.dist(e[0][:2], (marker.x, marker.y)) < KNOWN_DOOR_NEAR_MARKER]
                    if near_doors:
                        door, spot = near_doors[0][0], near_doors[0][1]
                        logger.info(f"{target} not in view: through the door by the marker "
                                    f"({door[0]:.0f}, {door[1]:.0f}), walked through before")
                        await self.client.teleport(XYZ(*spot))
                        await asyncio.sleep(TELEPORT_SETTLE)
                        if not await self._zone_changed(zone_now):
                            await self.walk_through(XYZ(door[0], door[1], spot[2]), zone_now)
                        return
                # A Spirit World portal by the marker (Tomugawa the Evil, Ancient
                # Burial Grounds): light every ritual candle around it, then X
                # at the portal brings the fight (the player's directions).
                if self._may_try(objective, zone_now, "spirit_portal") and await self._light_and_enter(
                    zone_now, marker
                ):
                    return
                # Not in view and not remembered anywhere: the marker may still
                # be a door or sigil into its room (Tomugawa the Evil: the hops
                # toward it were refused and the main quest was set aside in a
                # minute). Travel there first: it goes through doors.
                # (Not in a brother's warren: the marker is Jotun's, and led to
                # the warren's exit.)
                if (not at_marker and _real_marker(marker) and not getattr(self, "_in_pre_boss_warren", False)
                        and self._may_try(objective, zone_now, "marker_travel")):
                    logger.info(f"no {target} in view: to the quest marker first (it may be a way in)")
                    await self.travel(marker)
                    return
                # The marker's door refused us twice ("The door is locked. Find
                # the crystal." at Malistaire's): the zone's chests, crystals,
                # stands, its boss, its NPCs and switches before anything else
                # (the sweep and the zones around ran out first, and the quest
                # was set aside).
                # (Counted over every zone: around Malistaire's door the bot
                # went between three zones and none reached two tries.)
                tries = sum(n for (o, _z, a), n in self._attempts_at.items()
                            if o == objective and a == "marker_travel")
                locked = _real_marker(marker) and tries >= APPROACH_LIMITS["marker_travel"]
                if locked and await self._fight_zone_boss(objective, zone_now):
                    return
                if (locked and self._may_try(objective, zone_now, "locked_door_early")
                        and await self.bring_out.step(objective, zone_now, target, fight=True)):
                    return
                # Far from the marker: walk toward it (enemies only load nearby;
                # King Shemet was 26000 away, easy to reach on foot).
                if not at_marker and self._may_try(objective, zone_now, "walk"):
                    if await self._walk_toward(marker, target):
                        return
                # Far from an unreachable marker: an in-zone teleporter ("To the
                # Sarcophagus") is the way over to it.
                if not at_marker and self._may_try(objective, zone_now, "teleporter"):
                    if await self._use_zone_teleporter(objective):
                        return
                # A boss fought on a duel circle (Sprockets in Counterweight
                # East): teleporting near it half-joins the circle and freezes
                # the wizard; land well clear and walk into it instead.
                # Inside a dungeon the boss may only come out once the place is
                # worked through (Counterweight East: talk to Gus, use the
                # Counterweight Levers): talk, pick up, try the switches first.
                # Outside dungeons too, once the marker's door keeps refusing us
                # ("The door is locked. Find the crystal." at Malistaire's).
                worked = (await self._in_dungeon(zone_now)
                          or not self._may_try(objective, zone_now, "locked_door"))
                if worked and await self.bring_out.step(objective, zone_now, target, fight=True):
                    return
                if self._may_try(objective, zone_now, "walk_circle") and await self._walk_into_circle(marker):
                    return
                # A boss that isn't there yet usually appears when the wizard
                # walks into its spot (the marker); a teleport doesn't set that off.
                if await self._walk_onto_marker():
                    return
                # Fighting whatever is closest (Gobbler Scavengers instead of
                # Munchers) costs time and risk for nothing: look around the zone
                # for the named enemy, twice at most; then move on.
                # In a dungeon the boss may come out only when the wizard walks
                # in from the entrance (Usunoki in the Town Dojo: teleporting to
                # Ting Yin skipped it): that first, before any sweep.
                in_dungeon = await self._in_dungeon(zone_now)
                if in_dungeon and "/interiors/" in zone_now.lower() and await self._walk_in_from_entrance(
                    objective, marker, zone_now
                ):
                    return
                if self._may_try(objective, zone_now, "sweep"):
                    await self._look_for(target)
                elif in_dungeon and await self._reenter_for_npc(objective, zone_now):
                    pass  # a fresh copy of the dungeon
                elif await self._fight_zone_boss(objective, zone_now):
                    pass  # (a locked way on: the zone's boss first; Gurtok before Malistaire's door)
                elif (self._may_try(objective, zone_now, "locked_door_late")
                      and await self.bring_out.step(objective, zone_now, target, fight=True)):
                    pass  # its chests, crystals, stands, NPCs, switches
                else:
                    await self._all_approaches_used(objective, f"find {target}")
                return
        for _ in range(3):
            if await self.client.in_battle():
                return
            try:
                allow_engage(self.client)  # "any enemy will do": going onto one on purpose
                await self.sprinter.tp_to_closest_mob()
            except Exception as exc:
                logger.debug(f"no mob to pull: {exc}")
                return
            await asyncio.sleep(3.0)

    async def _press_x_at_thing(self, prompt: str) -> bool:
        """A Press X prompt at the thing to locate (a document, a crate: the
        Bill of Lading on the barge took walking onto it from every side,
        forever): press it. Never a teleporter's or a door's. True if pressed."""
        if not prompt or not await is_free(self.client):
            return False
        if any(w in prompt for w in ("teleport", "activate", "enter", "exit", "door")):
            return False
        logger.info(f"pressing X at the prompt '{prompt}'")
        await self.client.send_key(Keycode.X, 0.1)
        return True

    async def _locate_by_walking(self, objective: str) -> bool:
        """'Locate X': the game counts the spot when we walk into it, not when
        a teleport puts us there. Land a little off the marker and walk onto
        it, from each side in turn, until the objective moves on."""
        # A person to locate (Junho Shan, a few steps from the marker, which
        # sat on the teleporter beside him): go to them and talk.
        name = locate_target(objective)
        if name:
            pos = await self._npc_named(name, near=await self._position())
            if pos is not None:
                logger.info(f"{name} is here: walking up to them")
                await self.travel(pos, npc=True)
                await asyncio.sleep(1.0)
                prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
                if await self.objective() == objective and await is_free(self.client) and "talk" in prompt:
                    await self.interact(f"Talk To {name}")  # (never the teleporter's 'activate')
                    await asyncio.sleep(1.5)
                elif await self.objective() == objective and await self._press_x_at_thing(prompt):
                    await asyncio.sleep(1.5)
                if await self.objective() != objective or not await is_free(self.client):
                    logger.success(f"located {name}")
                    return True
        marker = await self.client.quest_position.position()
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            start = XYZ(marker.x + dx * MARKER_WALK_BACK, marker.y + dy * MARKER_WALK_BACK, marker.z)
            await self.client.teleport(start)
            await asyncio.sleep(TELEPORT_SETTLE)
            await self.client.goto(marker.x, marker.y)
            await asyncio.sleep(1.5)
            if await self.objective() == objective and await self._press_x_at_thing(
                    (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()):
                await asyncio.sleep(1.5)
            if await self.objective() != objective or not await is_free(self.client):
                logger.success(f"located by walking in: {objective!r}")
                return True
        logger.info(f"walked onto the marker from every side; {objective!r} still open")
        return False

    async def _walk_onto_marker(self) -> bool:
        """Near the quest marker: back off in each direction in turn and walk
        onto it, which triggers boss spawns and cutscenes. True if a fight or
        dialogue started."""
        marker = await self.client.quest_position.position()
        if distance(marker, XYZ(0, 0, 0)) < 1 or distance(await self._position(), marker) > MARKER_WALK_RANGE:
            return False
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            start = XYZ(marker.x + dx * MARKER_WALK_BACK, marker.y + dy * MARKER_WALK_BACK, marker.z)
            await self.client.teleport(start)
            await asyncio.sleep(TELEPORT_SETTLE)
            allow_engage(self.client)  # walking onto it is meant to bring the boss out
            await self.client.goto(marker.x, marker.y)
            await asyncio.sleep(2.0)
            if not await is_free(self.client):
                logger.info("walking onto the quest marker started something")
                return True
        return False

    async def _duel_circles(self, zone: str) -> list[tuple[float, float, float]]:
        """Duel circles here: loaded now, or seen before in this zone."""
        from .collect import duel_circles

        found = list(await duel_circles(self.client))
        found += self.entity_map.spots(zone, lambda n: n.lower() == "duel circle", (0.0, 0.0, 0.0))
        return found

    async def _look_for(self, target: str):
        """Hop across the zone's landmarks (clear of enemies) until an enemy
        named `target` is in view, then go after it."""
        from .bossfarm import find_entity_named

        start = await self._position()
        zone = await self.client.zone_name() or ""
        want = _norm_name(target).removesuffix("s")
        # Where it was seen before comes first (nearest first); then the sweep
        # over ground those visits haven't covered.
        here = (start.x, start.y, start.z)
        known = self.entity_map.spots(zone, lambda n: bool(want) and want in _norm_name(n), here)
        known = spread_points(known, (start.x, start.y, start.z), ENEMY_SWEEP_SPACING / 2)[:KNOWN_SPOTS_FIRST]
        # Wanderers patrol the walkways: search along the path markers as well
        # as the named landmarks, so the whole zone gets covered.
        points = await self._landmarks() + floor_points(await path_points(self.client), start.z)
        # Every floor seen in this zone too (a boss up a tower, Sprockets), in
        # random order; never near a duel circle: landing by one froze the
        # wizard in a 'battle' with 0 opponents.
        seen = self.entity_map.spots(zone, lambda _n: True, here)
        random.shuffle(seen)
        points += seen
        circles = await self._duel_circles(zone)
        sweep = [
            p for p in spread_points(points, (start.x, start.y, start.z), ENEMY_SWEEP_SPACING)
            if all(math.dist(p[:2], k[:2]) > ENEMY_SWEEP_SPACING / 2 for k in known)
            and all(math.dist(p[:2], c[:2]) > CIRCLE_KEEP_AWAY for c in circles)
        ]
        visited = self._swept_spots.setdefault((self._last_progress[0] or "", zone), [])
        fresh = [p for p in sweep if all(math.dist(p[:2], v[:2]) > ENEMY_SWEEP_SPACING / 2 for v in visited)]
        spots = [k for k in known if k not in visited] + fresh[:FAR_SWEEP_MAX]
        where = f"{len(known)} spot(s) it was seen at, then " if known else ""
        n_new = len(fresh[:FAR_SWEEP_MAX])
        logger.info(f"no {target} in view; looking at {where}{n_new} new spots around the zone")
        for p in spots:
            if not await is_free(self.client):
                return
            visited.append(p)
            if not await self._clear_spot(XYZ(*p)):
                continue
            await self.client.teleport(XYZ(*p))
            await asyncio.sleep(1.5)  # let nearby entities stream in
            await scan_entities(self.client, zone, self.entity_map)
            pos = await find_entity_named(self.client, target)
            if pos is not None:
                logger.info(f"found {target} near ({p[0]:.0f}, {p[1]:.0f}); going after it")
                if any(math.dist((pos.x, pos.y), c[:2]) < CIRCLE_KEEP_AWAY for c in circles):
                    # On its duel circle: walk in from outside, don't land on it.
                    me = await self._position()
                    dx, dy = me.x - pos.x, me.y - pos.y
                    back = CIRCLE_WALK_FROM / (math.hypot(dx, dy) or 1.0)
                    await self.client.teleport(XYZ(pos.x + dx * back, pos.y + dy * back, pos.z))
                    await asyncio.sleep(TELEPORT_SETTLE)
                    allow_engage(self.client)
                    await self.client.goto(pos.x, pos.y)
                    await self._hold_for_fight()
                    return
                else:
                    allow_engage(self.client)
                    await self.client.teleport(pos)
                await asyncio.sleep(3.0)
                return

    async def _no_marker_fallback(self, objective: str, zone: str) -> bool:
        """Walk through a known gate toward the place the objective names, else
        try the known hidden quest-target spots in this zone. True if it acted."""
        for _ in range(3):
            gate = find_zone_gate(objective, zone, self._bad_gates)
            if not gate:
                break
            pos, dest_zone = gate
            logger.info(f"no quest marker for {objective!r}; walking to {dest_zone} via a known gate")
            await self.controller.checkpoint()
            await self.travel(pos)
            if await self.client.zone_name() != zone:
                return True
            # Landing on the gate point doesn't always cross the trigger; walk into it.
            if await self.approach_and_walk(pos, zone):
                return True
            # Some gate entries in the data are wrong; route around this one from now on.
            logger.warning(f"gate {zone} -> {dest_zone} did not work; avoiding it")
            self._bad_gates.add((zone, dest_zone))
        target_zone = objective_zone(objective)
        entry = DungeonMemory.load().dungeons.get(target_zone or "")
        if entry is not None and target_zone != zone:
            # A dungeon learned before (Katzenstein's Lab, off Scotland Yard
            # Roof): its outside zone, then in by the sigil.
            if zone != entry.outside:
                logger.info(f"no quest marker for {objective!r}; heading to {entry.outside} for its dungeon")
                if await self.go_to_zone(entry.outside) or await self.client.zone_name() != zone:
                    return True
            else:
                sigil = XYZ(*entry.sigil)
                logger.info(f"no quest marker for {objective!r}; entering its dungeon by the sigil")
                await self.travel(sigil)
                await self._enter_by_sigil(sigil, zone)
                return True
        if target_zone not in (None, zone) and gate_toward(zone, target_zone, self._bad_gates):
            # Not next door: go there through the known gates, several hops if needed.
            logger.info(f"no quest marker for {objective!r}; heading to {target_zone}")
            if await self.go_to_zone(target_zone) or await self.client.zone_name() != zone:
                return True
        if target_zone not in (None, zone):
            other_world = target_zone.split("/")[0] != (zone or "").split("/")[0]
            # No gate route: switch, unless we're just inside a building of the
            # same world (walking out may find one). Another world: no gates lead there.
            routable = gate_toward(zone, target_zone, self._bad_gates)
            if not routable and other_world and target_zone.split("/")[0] in SPIRAL_WORLD_NAMES:
                # Another world (Marleybone from Aquila): by the Spiral Map.
                return await self._to_world(target_zone.split("/")[0], f"to {target_zone}")
            if not routable and (other_world or "interiors" not in zone.lower()):
                logger.warning(f"no route from {zone} to {target_zone}; setting this quest aside")
                return await self._set_current_aside(objective)
            if any(to == target_zone for _frm, to in self._bad_gates) and not find_zone_gate(
                objective, zone, self._bad_gates
            ):
                # Every way in refused us: the zone is still locked by the story.
                logger.warning(f"{target_zone} looks locked (every gate refused); setting this quest aside")
                return await self._set_current_aside(objective)
            return False  # the target is in another zone; its spots here are someone else's
        # "Talk to Clockwork in Katzenstein's Lab" with no marker: look for
        # Clockwork around here and walk up to him.
        name = talk_target(objective)
        if name:
            pos = await self._npc_named(name)
            if pos is not None:
                logger.info(f"no quest marker for {objective!r}; {name} is here: going to talk")
                await self.controller.checkpoint()
                await self.travel(pos, npc=True)
                if not await wait_until_free(self.client, timeout=5):
                    return True
                if await self.interact(objective):
                    return True
                await self.client.send_key(Keycode.S, 0.3)
                await self.client.send_key(Keycode.W, 0.3)
                await asyncio.sleep(0.5)
                await self.interact(objective)
                return True
            # Nowhere to be seen (Clockwork in Katzenstein's Lab: beat the
            # doctor, bring Grunk the crates, pull his levers): work the room,
            # but only there (it talked to NPCs in the Marleybone hub).
            here = target_zone == zone or (target_zone is None and await self._in_dungeon(zone))
            if here and await self.bring_out.step(objective, zone, name):
                return True
        for pos in quest_spots(zone):
            logger.info(f"no quest marker for {objective!r}; trying quest spot ({pos.x:.0f}, {pos.y:.0f})")
            await self.controller.checkpoint()
            await self.travel(pos)
            if not await wait_until_free(self.client, timeout=5):
                return True
            if distance(await self._position(), pos) < INTERACT_RANGE and await self.interact(objective):
                return True
        return False

    # --- main step -----------------------------------------------------------

    async def step(self):
        try:
            zone_known = bool(await self.client.zone_name())
        except Exception:
            zone_known = False
        if not zone_known:
            from .relog import at_character_select, play_from_character_select
            from .upkeep import reconnect_if_asked

            if await reconnect_if_asked(self.client):
                return

            if await at_character_select(self.client):
                await play_from_character_select(self.client)
                return
        if not await is_free(self.client):
            logger.debug("step: not free")
            return
        await self._learn_door_walk()
        await clear_popups(self.client)
        logger.debug("step: popups cleared")
        from .petdance import in_pet_game

        zone_here = await self.client.zone_name() or ""
        if self.pet is not None and in_pet_game(zone_here):
            # In the dance game (a restart mid-trip): the pet trip plays on,
            # never the quest's spellbook or a relog from there.
            await self.pet.trip(0)
            return
        from .petdance import PET_PARK

        if (self.pet is not None and self.pet.cfg.auto and zone_here == PET_PARK
                and time.monotonic() - getattr(self, "_pet_resumed_at", -1e9) > PET_RESUME_SECONDS):
            # In the Pet Pavilion with the trip cut short (a restart): train on
            # until the energy runs out, then back to the mark (the player).
            self._pet_resumed_at = time.monotonic()
            await self.pet.trip(0)
            return
        if self._grinding and not VISIT_FILE.exists() and self._main_world:
            # Nothing left to do here: a giver from the player's list with a
            # quest for us (MooShu: Ken Shui in the Village of Sorrow) beats
            # grinding, or walking back into the stuck main quest.
            target = self.givers.visit_target(self._main_world)
            source = "from the quest list"
            if not target:
                # No list for this world (Celestia): look for side quests among
                # the people seen there, zone by zone, rather than grind (the
                # player: side quests give far more experience).
                from .combat.sim import load_stats

                enemies = set(load_stats().get("enemies", {}))
                for place in (self._main_world, *FALLBACK_SIDE_PLACES.get(self._main_world, ())):
                    target = self.givers.hunt_target(place, self.entity_map.zones, enemies)
                    if target:
                        break
                source = "for side quests instead of grinding"
            if not target and not getattr(self, "_hunt_exhausted_logged", False):
                # Nobody left to ask here: the other worlds' side quests in the
                # book before grinding (choose_quest's `anywhere`).
                self._hunt_exhausted = time.monotonic()
                self._hunt_exhausted_logged = True
                logger.info("everyone here asked for quests: side quests in other worlds next")
                self._ranked_for = None
                self._last_rank = -1e9
            elif target:
                self._hunt_exhausted_logged = False
            if target:
                npc, where = target
                self.givers._remember(where, npc)  # one try an hour, whatever happens
                logger.info(f"nothing left to do: visiting {npc} ({where.split('/')[-1]}) {source}")
                VISIT_FILE.write_text(json.dumps({"npc": npc, "zone": where}), encoding="utf-8")
        self._fetch_next_story_quest()
        if VISIT_FILE.exists() and await self._visit_npc():
            logger.debug("step: visited an NPC")
            return
        if await self._leave_spiral_map():
            logger.debug("step: Spiral Map")
            return
        if await self._dorm_to_wizard_city():
            logger.debug("step: dorm to Wizard City")
            return
        if await self._house_to_world():
            logger.debug("step: house's world gate")
            return
        if not self._mainline and self._detour_gap_pending() and await self._detour_ask():
            # The story's quest ended with none after it: its NPCs before any
            # side quest (the player's order; it went off to 'The Spiral Cup'
            # after 'Meddling Wizards' with Zafaria's next quest at the hub).
            logger.debug("step: asked for the story's next quest")
            return
        logger.debug("step: past the trips")
        # Noting what's around and where wisps spawn (~3 s each) runs beside
        # the step, not in its way.
        if time.monotonic() - self._last_entity_scan > ENTITY_SCAN_SECONDS:
            self._last_entity_scan = time.monotonic()
            zone_scan = await self.client.zone_name() or ""
            self._in_background("entities", lambda: scan_entities(self.client, zone_scan, self.entity_map))
        if time.monotonic() - self._last_wisp_scan > WISP_SCAN_SECONDS:
            self._last_wisp_scan = time.monotonic()
            self._in_background("wisps", lambda: scan_wisps(self.client))  # learn wisp spawn points
        await self._note_defeats()
        logger.debug("step: defeats noted")
        await self._boss_health_bar()
        # A patrol walked up while we stood still: step aside (outdoors, and not
        # when the objective is a fight, which means going onto enemies).
        zone_now = await self.client.zone_name() or ""
        # (Not while grinding: enemies are what it's there for; stepping clear of
        # them every step left GH_Wolf's grind with no fights.)
        if ("interiors" not in zone_now.lower() and not self._grinding
                and not is_combat_objective(await self.objective())):
            await self._clear_of_enemies()
        # After healing from a win or a loss: back to the mark first, before any
        # pick-up or quest ranking (the player's rule; walking back in by the Labyrinth's
        # sigil reset it). _recall_to_mark waits until we're healed.
        if self._recall_pending and await self._recall_to_mark():
            logger.debug("step: recalled to the mark")
            return
        self._note_boss_win()
        # With a team, no other detours for pick-ups (it teleported all over
        # Mount Olympus): the team's shared objectives are left to the others.
        if not is_team_up_zone(zone_now):
            if await self._pick_up_wanted():
                logger.debug("step: picked up a wanted item")
                return
            if await self._pick_up_loot():
                logger.debug("step: picked up loot")
                return
        await self._answer_dungeon_exit()
        await self._learn_arrival_gate()
        if self._farm_run_done and is_team_up_zone(zone_now):
            from .dungeon_heal import go_to_hub

            self._farm_run_done = False
            Farm.load().record_run()
            logger.info("farm run done; leaving for the next one")
            self._team_alone_since = None
            await go_to_hub(self.client)
            return
        if is_team_up_zone(zone_now) and zone_now.startswith(FARM_ONLY_DUNGEONS):
            farm = Farm.load()
            if not (farm.active and zone_now.startswith(FARM_ONLY_DUNGEONS)
                    and farm.dungeon.startswith(FARM_ONLY_DUNGEONS)):
                # Farming stopped (the player: back to the main story): the
                # Waterworks is only ever for the farm; out to the hub.
                from .dungeon_heal import go_to_hub

                logger.info("not farming any more: leaving the dungeon for the story")
                self._team_alone_since = None
                await go_to_hub(self.client)
                return
        team_mode = is_team_up_zone(zone_now)
        if team_mode:
            from .teamup import team_list

            obj_now = await self.objective() or ""
            who = talk_target(obj_now) or defeat_target(obj_now) or ""  # ("Talk to Belloq" starts his fight)
            boss_here = DungeonMemory.load().bosses.get(who) == zone_now
            if zone_now in team_list() and not is_combat_objective(obj_now) and not boss_here:
                # A quest dungeon taken with a team (the team list): the fight
                # is over, our quest goes on alone (the player: it followed an
                # AFK teammate round Belloq's tent after the win).
                if not getattr(self, "_team_done_logged", False):
                    self._team_done_logged = True
                    logger.info("the team fight is done: going on alone (out of the dungeon)")
                team_mode = False
            else:
                self._team_done_logged = False
        if team_mode:
            if time.monotonic() - self._team_ranked > TEAM_RERANK_SECONDS:
                # The dungeon hands out its quest on entering: track it (its
                # marker leads room to room; the team step ends the step before
                # the usual ranking further down).
                self._team_ranked = time.monotonic()
                self.controller.allow_idle(30)
                try:
                    await self.prioritize_quests()
                finally:
                    self.controller.end_idle()
                self._ranked_for = await self.objective()
            if await self._team_step():
                return
        else:
            self._team_ranked = -1e9
            self._team_alone_since = None
            self._team_with_us = False
        # After a defeat by a boss we marked beside: go back first and heal
        # there (Katzenstein's Lab), not slowly out in the hub.
        in_dungeon = await self._in_dungeon(zone_now)
        # In a dungeon, leaving to heal resets it: first do everything that
        # needs no fight (the talk after beating Willie Marks), and heal only
        # when the next step is a fight.
        heal_now = not in_dungeon or await self._fight_ahead(await self.objective())
        if heal_now and in_dungeon and self.upkeep:
            hp, mana = await health_mana(self.client)
            if hp >= self.upkeep.min_health_to_fight and mana >= DUNGEON_MANA_TRIP:
                heal_now = False  # only mana a little low: not worth leaving the dungeon
        if heal_now and not in_dungeon and self.upkeep:
            # No fight in this step (a talk, a place to go): not worth a heal
            # trip unless really low (the player: it went zone to zone for
            # empty wisp spots at 45% before a talk). Healed before the fight.
            objective_now = await self.objective() or ""
            if not is_combat_objective(objective_now) and not self._step_is_fight:
                hp, mana = await health_mana(self.client)
                if hp >= NO_FIGHT_HEAL_BELOW and mana >= NO_FIGHT_MANA_BELOW:
                    heal_now = False
        # Never leave a zone Recall can't bring us back into (the Death Realm:
        # "You cannot teleport to that location", a fresh dungeon every time):
        # the next fight comes straight after the last; this room's wisps,
        # else a potion when low (as with a team).
        from .dungeons import no_return

        if heal_now and (is_team_up_zone(zone_now) or no_return(zone_now)):
            heal_now = False
            if self.upkeep and await heal_in_room(self.client, self.upkeep):
                return
            if self.upkeep and not is_team_up_zone(zone_now):
                hp, mana = await health_mana(self.client)
                low = hp < self.upkeep.potion_health_ratio or mana < self.upkeep.potion_mana_ratio
                if self.upkeep.use_potions and low and await self.client.stats.potion_charge() >= 1.0:
                    logger.info(f"drinking a potion (hp {hp:.0%}, mana {mana:.0%}); staying in the dungeon")
                    await ui.click(self.client, ui.POTION_BUTTON)
                    await asyncio.sleep(1.5)
        if not heal_now:
            logger.debug("in the dungeon with no fight ahead: finishing the objective before healing")
        if heal_now and in_dungeon and not await self._boss_settled(await self.objective() or ""):
            return  # (no trip out yet: the boss's spawn cutscene)
        if heal_now and self.healer and in_dungeon and await self.healer.between_fights(zone_now):
            return
        if heal_now and self.upkeep and not await recover(
            self.client, self.upkeep, self.controller, self.go_to_zone,
            trip=self._heal_trip, mark=self._heal_mark,
        ):
            logger.debug("step: recovering")
            return
        # With a team, nothing that could leave the dungeon or hold us back
        # (Recall, NPC visits, grinding, gear checks, training trips); the quest
        # step itself still runs (returning here stood still while the team left).
        team = is_team_up_zone(zone_now)
        if not team and await self._farm_trip(zone_now):
            logger.debug("step: farm trip")
            return
        if not team and await self._recall_to_mark():
            logger.debug("step: recall to mark")
            return
        # Quests beat grinding for experience: ask the NPCs around first (the
        # next main quest may be waiting with one of them).
        if not team and await self.givers.ask_nearby():
            logger.debug("step: asked an NPC nearby")
            return
        # Grinding comes after healing: right after a defeat it went looking for
        # fights at 0 mana and a third of its health.
        if not team and self._grinding and await self._grind():
            self._ground_at = time.monotonic()  # (the status says grinding only while it really is)
            return
        if getattr(self, "_ranked_for", None) is not None and await self._grind_beside_set_aside(
                await self.objective()):
            return
        if self.gear and not team:
            self.controller.allow_idle(600)  # a full check tries ~40 items (~5 min): not a stall
            try:
                await self.gear.tick()
            except Exception as exc:
                logger.opt(exception=exc).warning("gear check failed")
            finally:
                self.controller.end_idle()
            if not await is_free(self.client):
                return
        pet = getattr(self, "pet", None)
        if pet and not team and not await self._in_dungeon(zone_now):
            self.controller.allow_idle(1800)  # the trip and up to dozens of games
            try:
                acted = await pet.tick()
            except Exception as exc:
                logger.opt(exception=exc).warning("pet dance trip failed")
                acted = True
            finally:
                self.controller.end_idle()
            if acted:
                return
        shop = getattr(self, "potions", None)
        if shop and not team and not await self._in_dungeon(zone_now) and not no_return(zone_now):
            self.controller.allow_idle(300)  # the trip to the Commons and back
            try:
                acted = await shop.tick()
            except Exception as exc:
                logger.opt(exception=exc).warning("potion trip failed")
                acted = True
            finally:
                self.controller.end_idle()
            if acted:
                return
        if self.trainer and not team:
            self.controller.allow_idle(240)  # the trip crosses zones and a training window
            try:
                acted = await self.trainer.tick()
            except Exception as exc:
                logger.opt(exception=exc).warning("spell training trip failed")
                acted = True
            finally:
                self.controller.end_idle()
            if acted:
                return
        adapter = getattr(self, "deck_adapter", None)
        if adapter is not None and not team and await is_free(self.client):
            self.controller.allow_idle(120)  # deck clicks look like "nothing happening"
            try:
                if await adapter.tick(self.client):
                    return
            except Exception as exc:
                logger.opt(exception=exc).warning("deck switch failed")
            finally:
                self.controller.end_idle()
        keeper = getattr(self, "deck_keeper", None)
        if keeper is not None and not team and await is_free(self.client):
            self.controller.allow_idle(120)  # deck clicks look like "nothing happening"
            try:
                here = await self.client.zone_name() or ""
                # The boss deck (deck_general.json 'boss_deck', the player's
                # heavier deck) in a dungeon or room, or with a boss fight next.
                # Only a fight decides (talking in the Northguard throne room,
                # an interior, swapped the deck items every few seconds):
                # otherwise the deck worn stays.
                bosses = DungeonMemory.load().bosses
                obj = await self.objective() or ""
                extra: dict[str, int] = {}
                if is_combat_objective(obj):
                    target = defeat_target(obj) or ""
                    # A boss by name (known bosses, the fights logged); a place
                    # alone isn't enough (the boss deck and Feint necklace went
                    # on for the Shadow-Web Haunts in an interior): only an
                    # enemy never fought yet, inside, counts as a likely boss,
                    # and never a counted one ("(0 of 3)": regular enemies).
                    known = bool(target and is_known_enemy(target))
                    counted = bool(re.search(r"\(\d+ of \d+\)", obj))
                    boss = (any(bosses.get(n) for n in defeat_names(obj))
                            or bool(target and is_known_boss(target))
                            or (not known and not counted
                                and (await self._in_any_dungeon(here) or "/interiors/" in here.lower())))
                    if target and boss:
                        extra = await self._boss_prisms(target)
                else:
                    boss = getattr(keeper, "last_boss", False)
                # (No prisms asked for once the boss fight is over: they come out.)
                if await keeper.tick(self.client, boss=boss, extra=extra):
                    return
            except Exception as exc:
                logger.opt(exception=exc).warning("deck keeping failed")
            finally:
                self.controller.end_idle()
        if self.progression:
            self.controller.allow_idle(90)  # spellbook work looks like "nothing happening"
            try:
                await self.progression.tick()
            finally:
                self.controller.end_idle()
            if not await is_free(self.client):
                return

        objective = await self.objective()
        accepted = self.dialogue.accepted if self.dialogue else 0
        if accepted != self._accepted_seen:
            # A newly accepted quest gets tracked by the game (Harold's side
            # quest took over from the main story): rank again now.
            self._accepted_seen = accepted
            self._ranked_for = None
            self._last_rank = -1e9
        # The game may auto-track a quest we set aside (e.g. after handing one in):
        # re-rank at once rather than walking back to the fight we keep losing.
        set_aside_objectives = {d.get("objective") for d in self.setbacks.deferred.values()}
        on_set_aside = objective in set_aside_objectives and bool(
            self.setbacks.set_aside(await self.client.stats.reference_level())
        )
        # The game tracked another quest by itself (after 'Foe of Foes' was
        # handed in it tracked the side quest 'Grizzleheim', and the bot went
        # for the Spiral Map 80 s after the last ranking): rank again at once.
        try:
            quest_id = await self.client.quest_id()
        except Exception:
            quest_id = None
        switched = quest_id is not None and quest_id != getattr(self, "_ranked_quest", quest_id)
        if objective != getattr(self, "_ranked_for", None) and (
            on_set_aside or switched
            or time.monotonic() - getattr(self, "_last_rank", -1e9) > RANK_QUESTS_EVERY
        ):
            self._last_rank = time.monotonic()
            if switched:
                logger.info("the game is tracking another quest: ranking the quest book again")
            self.controller.allow_idle(30)
            tracked = False
            try:
                # Reading the quest book stands still: not beside enemies.
                await move_to_safety(self.client, EXPOSED_RADIUS, "before reading the quest book")
                tracked = await self.prioritize_quests()
                if tracked:
                    await asyncio.sleep(1.0)
                    objective = await self.objective()
            finally:
                self.controller.end_idle()
            self._ranked_for = objective
            try:
                # Kept as it was before the reading when the ranking only
                # continued: a switch made by the game while the book was read
                # (re-entering the King's Tomb tracked The Spiral Cup) is then
                # seen at the next step, not taken for our quest's next step.
                self._ranked_quest = await self.client.quest_id() if tracked or quest_id is None else quest_id
            except Exception:
                pass
            # (After the ranking too: Jotun's quest set aside after two losses,
            # the same step walked into his fight a third time.)
            if await self._grind_beside_set_aside(objective):
                return
        zone = await self.client.zone_name()
        # A new objective: Recall first if the mark gets us there sooner (the
        # game keeps one mark: marking here first would lose it); else mark
        # here before a long trip.
        if objective and await self._recall_if_faster(objective, zone or ""):
            await self._note_progress(objective, zone)
            return
        if objective and self._last_progress[0] and objective != self._last_progress[0]:
            await self._travel_mark(objective, zone or "")
        if await self._leave_minigame(zone):
            return
        await self._note_progress(objective, zone)
        # Stalled on one objective: make sure we aren't wedged inside a building
        # or wall from a teleport (walking then does nothing).
        now = time.monotonic()
        stalled = now - self._last_progress_time > STUCK_CHECK_AFTER
        # Teleports refused even after a long wait, twice running: check now
        # (a frozen wizard took 40 s of tries before the relog).
        frozen = getattr(self.client, "_refused_in_row", 0) >= FROZEN_REFUSALS
        # Not after walking up to a boss (its cutscene holds the wizard still:
        # twice the relog that followed reset Malistaire's Lair), and in a
        # dungeon only when teleports fail too (a relog resets it).
        cutscene = now < getattr(self, "_walked_in_at", -1e9) + WALK_IN_QUIET
        if (not frozen and not cutscene and stalled
                and await self._in_any_dungeon(zone or "")):
            stalled = False
        if cutscene:
            frozen = stalled = False
        if frozen or (stalled and now - self._last_stuck_check > STUCK_CHECK_EVERY):
            self._last_stuck_check = now
            self.client._refused_in_row = 0
            if await unstick(self.client, frozen=frozen):
                return
        # Walks meant to start a fight only go toward the objective's enemies
        # (safe_teleport reads this): "Defeat Otomo Supply Runners" -> them.
        self.client._target_names = defeat_names(objective) if is_combat_objective(objective or "") else None
        if time.monotonic() - getattr(self, "_last_status", 0.0) > STATUS_EVERY_SECONDS:
            self._last_status = time.monotonic()
            waited = time.monotonic() - self._last_progress_time
            logger.info(f"working on: {objective or '(no objective shown)'} [{zone}] for {waited:.0f}s")
        if (zone and self._active_quest and self._active_quest in self._mainline
                and zone != self._last_main_zone and self._last_main_zone != "swept"):
            self._last_main, self._last_main_zone = self._active_quest, zone
            _save_last_main(self._last_main, zone)

        if objective and is_combat_objective(objective) and await self._pre_bosses(objective, zone or ""):
            return

        if await self.services.is_open():
            # A services menu left open (e.g. after an error) blocks the X prompt.
            if self.dialogue:
                self.dialogue.accept_offers_for(30)
            if not await self.services.choose(objective):
                await self.services.close()
            await asyncio.sleep(2.0)
            return

        # A boss fight next with a deck found for it before: that deck first.
        adapter = getattr(self, "deck_adapter", None)
        if adapter is not None and objective and is_combat_objective(objective):
            for name in defeat_names(objective):
                adapter.prepare_for(name, await self._boss_alone(name))
                if adapter.searching_for(name) and await is_free(self.client):
                    if time.monotonic() - getattr(self, "_search_wait_logged", 0.0) > 60:
                        self._search_wait_logged = time.monotonic()
                        logger.info(f"waiting for the deck search against {name} before fighting it again")
                    # (Not a stall: the main quest 'It's an Honor' was set aside
                    # for 30 min while this wait ran, and the bot went grinding.)
                    self._last_progress_time = time.monotonic()
                    self.controller.allow_idle(30)
                    await asyncio.sleep(10.0)
                    return

        # The objective names a place in another world ('Talk To Rila Samoosuke
        # in Jade Palace' from Wizard City): the dorm, Ravenwood, the World Tree's
        # gate and the Spiral Map. Following the marker there walked at the
        # world gate as if it were a door for five minutes.
        place = objective_zone(objective) if objective else None
        world = self._book_world(objective) if place is None else None
        self._write_route(objective, zone or "", place, world)
        if world and zone.startswith("WizardCity/") and world != "WizardCity":
            # A place in several worlds ('Talk To Zan'ne in The Library',
            # Krokotopia's): the quest book names the world. (It followed the
            # marker onto the World Tree's gate for three minutes instead.)
            if await self._to_world(world, f"{objective!r} is in {world} (the quest book)"):
                return
        # (From Wizard City only, where the World Tree is: a place name shared by
        # two worlds, a Throne Room, mustn't send the bot across the Spiral.)
        if place and zone.startswith("WizardCity/") and place.split("/", 1)[0] != "WizardCity":
            if await self._to_world(place.split("/", 1)[0], f"{objective!r} is in {place.split('/', 1)[0]}"):
                return

        # The objective's place is another zone the gates lead to: there by its
        # gates first ('Talk To Mavra Flamewing in Plaza of Conquests': the
        # Cathedral's marker pointed at a door that wouldn't open, minutes of
        # walking at it). Not from inside a dungeon or a room (its quest is
        # often 'in' the zone outside: Usunoki in the Town Dojo).
        place = objective_zone(objective) if objective else None
        if (place and zone and place != zone and "/interiors/" not in zone.lower()
                and not await self._in_dungeon(zone)
                and (gate_toward(zone, place, self._bad_gates) or self.doors.route(zone, place))
                and self._may_try(objective, zone, "zone_first")):
            logger.info(f"{objective!r} is in {place.split('/')[-1]}: there by its gates first")
            await self.go_to_zone(place)
            return

        if await self._try_switch_puzzle(objective, zone or ""):
            return

        # "Find Ting Yin in Village of Sorrow": Ting Yin is a person (in the
        # town dojo), not an item; the collect search swept the zone for him.
        # A person named so in view: talk to them; else the quest marker leads
        # there (through doors) before any item search.
        m = _FIND.match(objective or "")
        if m:
            who = m.group(1).strip()
            if await self._npc_named(who, near=await self._position()) is not None:
                if await self._talk_to_named(f"Talk To {who}"):
                    return
            marker = await self.client.quest_position.position()
            if distance(marker, XYZ(0, 0, 0)) > 1 and self._may_try(objective, zone or "", "find_marker"):
                # The marker on a dungeon sigil: they're in that dungeon (Ting
                # Yin in the Town Dojo, its sigil in Yoshihito Temple). The
                # objective's 'in Village of Sorrow' sent the item search back
                # there, away from the marker.
                near = distance(await self._position(), marker) < 3000
                sigil = await self._sigil_at(marker) if near else None
                if sigil is not None:
                    logger.info(f"{who} not in view; the quest marker is a dungeon sigil: going in")
                    await self._enter_by_sigil(sigil, zone)
                    return
                # Already on the marker with a use/activate prompt showing (the
                # Hall of Time's portal to the past: 40 s of re-travelling to it
                # before the watchdog's nudge pressed X): use it.
                prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
                if (distance(await self._position(), marker) < FIND_AT_MARKER and prompt
                        and "talk" not in prompt):
                    logger.info(f"{who} not in view; at the quest marker, using its prompt ({prompt!r})")
                    await self.interact(objective)
                    return
                logger.info(f"{who} not in view: following the quest marker")
                await self.travel(marker)
                return

        item = collect_item_name(objective)
        if item:
            # "Collect Cog in Triton Avenue": searching any other zone is pointless.
            where = objective_zone(objective)
            # Not while searching the zones around it (see _search_next_zone).
            searching_here = zone in self._zones_searched.get(objective, ())
            if where and where != zone and (room_of(zone or "", where)
                                            or self._door_rooms.get(objective) == zone):
                # A building of that area (the Drum House in Elephant Graveyard:
                # "Collect Drum in Elephant Graveyard" has its drums inside, no
                # marker, and the bot waited 3 min and set the main quest aside).
                if await self.collect(item, objective):
                    return
                if gate_toward(zone, where, self._bad_gates):
                    logger.info(f"{objective!r}: none in this building; going out to {where}")
                    await self.go_to_zone(where)
                    return
            elif where and where == zone and (room := self._room_with(item, zone)):
                # Seen by that very name in a building of this area (Drum in the
                # Drum House): in there, not a sweep outside that took the
                # Drum House's webs for drums.
                logger.info(f"{objective!r}: {item!r} was seen in {room}; going in")
                # Its way in a dungeon sigil (the Drum House): stand on it for
                # the countdown, no door walk (moving cancels it).
                for door, _approach in self.doors.leading_to(zone, room):
                    sigil = await self._sigil_at(XYZ(*door))
                    if sigil is not None:
                        await self._enter_by_sigil(sigil, zone)
                        return
                await self.go_to_zone(room)
                return
            elif where and where != zone and not searching_here:
                if gate_toward(zone, where, self._bad_gates):
                    logger.info(f"{objective!r} is in {where}; going there first")
                    await self.go_to_zone(where)
                    return
                # No known route (e.g. inside a building): let the quest marker lead out.
            elif await self.collect(item, objective):
                return

        target = await self.client.quest_position.position()
        if distance(target, XYZ(0, 0, 0)) < 1:
            # No marker. Usually a zone change is in progress, or the objective
            # is a photomancy / collect task handled below.
            await asyncio.sleep(2.0)
            target = await self.client.quest_position.position()

        if self.cfg.photomancy and "photomance" in objective.lower():
            await self.client.send_key(Keycode.Z, 0.1)
            await asyncio.sleep(0.3)
            await self.client.send_key(Keycode.Z, 0.1)

        if distance(target, XYZ(0, 0, 0)) < 1:
            if await self._investigate(objective, zone or ""):
                return
            if getattr(self, "_fallback_tried_for", None) != (objective, zone):
                self._fallback_tried_for = (objective, zone)
                if await self._no_marker_fallback(objective, zone):
                    return
            if is_combat_objective(objective) and objective_zone(objective) in (None, zone):
                # "Summon Myth Minion in Unicorn Way": any fight here will do
                # (the brain summons/casts what the objective asks for).
                if not await self.client.in_battle():
                    await self.pull_mob()
                return
            # "Use Inactive Protector in District of the Stars (0 of 3)" has no
            # marker: the object by its name (seen spots first, else a sweep
            # of the zone), the used ones skipped. It waited here for minutes.
            if (operate_target(objective) and objective_zone(objective) in (None, zone)
                    and await self._use_named_object(objective)):
                return
            logger.debug(f"no quest marker for {objective!r}; waiting")
            await asyncio.sleep(2.0)
            return

        logger.info(f"[{zone}] {objective}")
        await self.controller.checkpoint()
        if await self._hub_for_objective(objective, zone or ""):
            return
        near = distance(await self._position(), target) < 3000
        sigil = await self._sigil_at(target) if near else None
        if sigil is not None:
            await self._enter_by_sigil(sigil, zone)
            return
        # Always land clear of enemies: the marker of a "Defeat X" objective can
        # sit beside other mobs (a Desert Golem by the Nirini Warriors), and
        # pull_mob goes after the named enemy on purpose afterwards.
        if is_combat_objective(objective) and objective_zone(objective) in (None, zone):
            if objective_zone(objective) == zone:  # an unknown place: no mark (it went in the Oasis)
                await self._mark_for_fight(objective, zone or "")
        if (is_combat_objective(objective) and not is_team_up_zone(zone or "")
                and ("/interiors/" in (zone or "").lower() or await self._in_dungeon(zone or ""))):
            await self._mark_in_dungeon_fight(objective, zone or "")
            allow_close_landing(self.client)  # enemies there are what we came for
        # "Use Charging Lever": go right up to the object itself, at its own
        # height (on a raised ledge the marker's approach never got the prompt).
        if await self._use_named_object(objective):
            return
        # An NPC here: inch toward it. Elsewhere the marker is a door on the way.
        if is_combat_objective(objective) and await self._seek_target(objective, zone or ""):
            return
        npc_here = "talk" in objective.lower() and objective_zone(objective) in (None, zone)
        if npc_here and await self._talk_means_fight(objective):
            return
        # The person may be standing somewhere else than the marker (the Jade
        # Champion by the palace entrance, the marker at the back of the room:
        # teleporting there lost him). Seen here already: straight to them.
        name = talk_target(objective) if npc_here else None
        if name:
            seen = await self._npc_named(name, near=await self._position())
            if seen is not None and distance(seen, target) > INTERACT_RANGE:
                logger.info(f"{name} is here, away from the marker: going to them")
                if await self._talk_to_named(objective):
                    return
        await self.travel(target, npc=npc_here)
        if not await wait_until_free(self.client, timeout=5):
            return  # a fight or dialogue started on arrival

        dist = distance(await self.client.body.position(), target)
        in_interior = "interiors" in (zone or "").lower()
        if (name and in_interior and dist < INTERACT_RANGE
                and await self._npc_named(name) is None
                and await self._walk_in_from_entrance(objective, target, zone or "")):
            return  # not at the marker in a dungeon: walk in from the entrance first
        # A Talk To whose person isn't out here (Dworgyn, inside a building in
        # Nightside): after two talks at the marker (someone else answered),
        # the marker is a door; stop talking and go through it.
        talk_marker = "talk" in objective.lower()
        wrong_talker = (
            talk_marker and dist < INTERACT_RANGE and not self._may_try(objective, zone or "", "talk_marker")
            and talk_target(objective) and await self._npc_named(talk_target(objective)) is None
        )
        if wrong_talker and (
            await self._walk_in_from_entrance(objective, target, zone or "")
            or await self._reenter_for_npc(objective, zone or "")
        ):
            return
        if wrong_talker:
            logger.info(f"{talk_target(objective)} isn't out here; the marker must be a door")
        if dist < INTERACT_RANGE and objective.lower().startswith("locate"):
            await self._locate_by_walking(objective)
            return
        if dist < INTERACT_RANGE and not wrong_talker and await self.interact(objective):
            await self._count_attempt()
            return
        if dist < INTERACT_RANGE and talk_marker and not wrong_talker:
            # NPC prompts appear on walking into range, not on teleporting there.
            await self.client.send_key(Keycode.S, 0.3)
            await self.client.send_key(Keycode.W, 0.3)
            await asyncio.sleep(0.5)
            if await self.interact(objective):
                await self._count_attempt()
                return

        if is_combat_objective(objective) or self._step_is_fight:
            if not await self.client.in_battle():
                await self.pull_mob(objective)
            return

        in_dungeon = "interiors" in (zone or "").lower() or await self._in_dungeon(zone or "")
        if dist >= INTERACT_RANGE and in_dungeon:
            # A dungeon on several floors (Katzenstein's Lab): its teleporter
            # pads lead to the marker's floor.
            if self._may_try(objective, zone or "", "teleporter") and await self._use_zone_teleporter(
                objective
            ):
                return
            # A marker we can't reach is usually behind a gate that opens once
            # the enemies in front of it are beaten: go fight them.
            key = (objective, zone)
            self._unreached[key] = self._unreached.get(key, 0) + 1
            if (
                self._unreached[key] >= UNREACHED_BEFORE_FIGHT
                and not is_team_up_zone(zone or "")  # with a team, the team starts fights
                and await self.sprinter.get_mobs()
            ):
                logger.info("can't reach the quest marker (a locked gate?); fighting nearby enemies")
                self._unreached[key] = 0
                await self.pull_mob()
                return

        # "Talk To Baxter" and Baxter is in this zone: go to him by name, not
        # through the marker as if it were a door.
        searching = objective in self._npc_search
        if (npc_here or searching) and await self._talk_to_named(objective):
            return
        who = talk_target(objective)
        if searching and who and await self._search_doors_for(who, objective):
            return
        # At the marker with no prompt, and someone standing right there
        # ("Return to Platform Assemble Parts": Grunk by the platform): talk.
        if dist < INTERACT_RANGE and not wrong_talker and await self._talk_to_npc_near(target, objective):
            return
        doorish = dist < DOOR_RANGE or (
            ("talk" not in objective.lower() or wrong_talker) and dist < INTERACT_RANGE
        )
        if doorish and await self.client.zone_name() == zone:
            # At (or near) the marker with nothing to interact with: it's most
            # likely a door or zone exit, which needs walking into. (The Post
            # Office's exit sat 590 away, beyond DOOR_RANGE, and a teleport to
            # it was rejected: nothing handled it.)
            if await self.walk_through(target, zone) or await self._walk_in_from_around(target, zone):
                logger.info("walked through a door")
            elif wrong_talker and talk_target(objective):
                # The marker's door didn't open to X: search the doors around.
                await self._search_doors_for(talk_target(objective), objective)
