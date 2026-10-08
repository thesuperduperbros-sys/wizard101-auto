# wiz101-auto

A focused Wizard101 setup for farming Couch Potato seeds from Troubled
Warriors in Grizzleheim's Savarstaad Pass. The default `farm` mode targets
only that enemy in `Grizzleheim/GH_Hero` (Savarstaad Pass); the existing combat
engine handles the fights and waits in other areas. The repository retains
upstream features, but the default farm preset does not use them.

The default launcher configuration is the Couch Potato farm: start with your
wizard at the Troubled Warrior spawn. The bot stays in the zone where it starts,
targets only nearby Troubled Warriors, and waits there when none are visible;
it does not travel to `farm_zone`, roam, or use the quest watchdog's movement
recovery. After an automatic recovery detour, it returns to the position where
farming started. When health or mana needs recovery, it goes to Northguard
(`Grizzleheim/GH_MainHub`) to look for recovery resources instead of searching
Savarstaad Pass, where Grendel Darters can start unwanted fights. Leave your
wizard logged into the game. There is no time limit by default. When pet energy
reaches its maximum, it pauses farming, travels to the Pet Pavilion, plays the
dance game cycling through the available tracks until the pet has no energy for
another game, then returns and continues. Dance move inputs are separated by a
brief 30 ms pause. If Northguard cannot restore health or mana to the fight threshold,
the bot waits there rather than returning to farm while under threshold. Use
**Ctrl+Shift+Q** to stop the bot. When the backpack reaches capacity, it pauses
farming, talks to the Bazaar NPC, opens **BackPack Buddy**, selects **Find Items**,
confirms the listed sale, waits for the inventory count to decrease, and returns
to the saved farm camp. The trigger uses the Backpack screen's displayed
used/allowed slot count (not the raw inventory-object list). If no items match
the BackPack Buddy options, it resumes farming and retries the sale after a delay.

## Setup and running (Windows)

1. Install **Python 3.13+** from python.org and tick "Add python.exe to PATH".
   Also install **Git for Windows**. (The launcher can install Python 3.13
   itself if you have the Python Install Manager.)
2. Extract the bot's zip to its own folder.
3. Log into Wizard101 with your wizard in the world.
4. Double-click **`wiz101.bat`**. It will:
   - stop any other copy of the bot that's still running (older versions too),
   - check Python and Git,
   - install the bot the first time, and again only when its dependencies change
     (log in `state\setup.txt`),
   - create `config.yaml` from the Couch Potato farm preset if you don't have one,
   - show a menu and start the bot after 8 seconds unless you pick something else.

**Updates are automatic.** Every time `wiz101.bat` starts, it pulls the latest
version from GitHub (`szatcg/wiz101-auto`, branch `main`) before doing anything
else. Your `config.yaml`, `state` folder and installed packages are never
touched. If GitHub can't be reached, the launcher carries on with the files it
already has.

If hooks fail to activate, fully restart Wizard101 and try again. Moving your
wizard one step while it starts can also help.

## Running from a terminal (VS Code)

From the repo folder:

```powershell
.\wiz101.bat start --supervise   # background; restarts itself after crashes
.\wiz101.bat status              # what it's doing right now
.\wiz101.bat logs -n 100         # recent log (add -f to follow)
.\wiz101.bat stop                # clean stop (unhooks the game)
.\wiz101.bat restart --supervise
```

With arguments the launcher skips the menu and the automatic git update, so
local changes are safe.

## Controlling the bot

**Commands** (run them in the activated `.venv`):

| Command | What it does |
|---|---|
| `wiz101-auto run -c config.yaml` | Start the Couch Potato farm. |
| `wiz101-auto inspect` | Print the current zone, health, and battle state. |
| `wiz101-auto sell-backpack` | Force one BackPack Buddy sale trip and return to camp (for a supervised live test). |
| `wiz101-auto watch` | Follow the farm activity log. |
| `wiz101.bat stop` | Stop the background bot and unhook from the game. |

**Launcher menu:** besides running the bot, `wiz101.bat` offers a health
check (`state\doctor.txt`), the deck plan (`state\deck.txt`), a deck rebuild,
what the bot sees (`state\inspect.txt`) and a zone recording, all saved to the
`state` folder ready to send.

**Keys while running:** **Ctrl+Shift+Q** stops and **Ctrl+Shift+P** pauses or resumes (Ctrl+C in the bot window also stops it). The keys
can be changed under `safety`.

**Settings:** everything is in `config.yaml`, which starts as a copy of
`configs/couch_potato.yaml`. See `config.example.yaml` for every option. The
farm target settings are:

- `farm_zone`: expected spawn zone (default: `Grizzleheim/GH_Hero`, Savarstaad Pass); informational only—the bot stays where it starts
- `farm_mob`: exact enemy name to target (default: `Troubled Warrior`)
- `safety.max_hours`: session time limit (`0` means no time limit)

## Reporting problems

Logs go to `wiz101-auto.log` (at DEBUG level). When something goes wrong, the
most useful things to send are:

- the last ~100 lines of the log,
- the output of `wiz101-auto inspect` taken at the moment the bot is stuck,
- for UI problems, `wiz101-auto inspect --windows` (the live window tree).

Game patches can move memory offsets or rename UI windows. Offsets are handled
by updating WizWalker (`pip install -e . --upgrade --force-reinstall`). UI
window paths live in `src/wiz101_auto/ui.py`.

## Layout

```
src/wiz101_auto/
  cli.py            wiz101-auto run | inspect
  bot.py            connects to the client and runs the concurrent loops
  quest.py          quest-arrow following, travel, interaction
  upkeep.py         dialogue, potions, wisps, popups
  safety.py         stop/pause hotkeys, run limits, death counter
  ui.py             UI window paths and helpers
  progression.py    level-up / new-spell detection, trainer handling
  deck.py           spellbook reading and deck rebuilding (DeckBuilder)
  deck_plan.py      which spells go in the deck (unit tested)
  explore.py        zone entity dump for route building
  combat/
    model.py        pure data model (cards, combatants, actions)
    brain.py        turn decision logic (unit tested)
    reader.py       WizWalker memory -> model
    fighter.py      executes decisions each round
tests/              run with `pytest` on any OS
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The combat brain and effect mapping are pure Python, so they can be tested on
Linux or macOS. Everything under `bot`, `quest`, `upkeep` and `fighter` needs
the Windows game client.

## Roadmap

- Map the trainer window exactly (from `state/trainer_window_*.txt`) and
  walk to the professor on level-up
- Minion support in the combat brain (Myth has several minion spells)
- Smarter collision-aware teleporting (see Deimos' `collision_tp`)
- School-aware pip accounting, and handling of shadow magic and
  multi-target spells
- Buying potions when out
- Pet training

## Credits and license

- [WizWalker](https://github.com/StarrFox/wizwalker) by StarrFox: memory hooks
  and game object model.
- [Deimos](https://github.com/Deimos-Wizard101/Deimos-Wizard101): maintained
  WizWalker and WizSprinter forks (used as dependencies) and much of the UI
  window-path mapping.

Both are GPL-3.0, so this project is licensed **GPL-3.0-or-later**.
Not affiliated with or endorsed by KingsIsle Entertainment.
