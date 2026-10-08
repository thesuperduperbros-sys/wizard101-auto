# wiz101-auto

An autonomous Wizard101 bot. It reads the game's memory through
[WizWalker](https://github.com/StarrFox/wizwalker), follows the quest arrow,
talks to NPCs, goes through doors and dungeons, and plays battles with its own
card-evaluation logic.



## What it does

| Area | Behaviour |
|---|---|
| **Questing** | Reads the objective text and the quest marker position, teleports there (walks if the server bounces the teleport), presses X on NPCs, doors, sigils and objects, confirms dungeon entry, uses world gates, closes shops and training menus it opens, and pulls the nearest mob for "Defeat…" objectives. |
| **Combat** | Reads every card in hand (damage, target, pips, accuracy, enchants), every combatant (health, blades, traps, shields, boss flag) and your pips. Each step it heals when low, enchants its attack, stacks blades or traps against bosses, picks the spell and target that removes the most enemy health (kills weighted heavily), sets up while waiting for pips, and discards dead cards. |
| **Progression** | Notices level-ups and new spells and rebuilds your deck from your spellbook: the best attacks (always keeping a cheap one for round one), a heal, a blade, a trap and a shield, all within copy limits. When a spell trainer window is open, it tries to train (experimental). |
| **Upkeep** | Advances dialogue, declines side quests (configurable), drinks potions, picks up health and mana wisps after fights, and retries areas that haven't downloaded. |
| **Safety** | **Ctrl+Shift+Q** stops the bot and **Ctrl+Shift+P** pauses or resumes it. It also stops after a maximum run time or no quest progress for N minutes (deaths never stop it). |

Modes: `quest` (default), `fight` (you walk, it fights) and `farm` (fights
the nearest mob over and over).

## Setup and running (Windows)

1. Install **Python 3.13+** from python.org and tick "Add python.exe to PATH".
   Also install **Git for Windows**. (The launcher can install Python 3.13
   itself if you have the Python Install Manager.)
2. Extract the bot's zip to its own folder.
3. Log into Wizard101 and stand in the world with your wizard.
4. Double-click **`wiz101.bat`**. It will:
   - stop any other copy of the bot that's still running (older versions too),
   - check Python and Git,
   - install the bot the first time, and again only when its dependencies change
     (log in `state\setup.txt`),
   - create `config.yaml` from the Myth preset if you don't have one,
   - show a menu and start the bot after 8 seconds unless you pick something else.

**Updates are automatic.** Every time `wiz101.bat` starts, it pulls the latest
version from GitHub (`szatcg/wiz101-auto`, branch `main`) before doing anything
else. Your `config.yaml`, `state` folder and installed packages are never
touched. The first update may open a GitHub sign-in window, because the repo is
private. If GitHub can't be reached, the launcher carries on with the files it
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

**Let Claude Code run it for you.** Open this folder in VS Code with the
Claude Code extension (or run `claude` in its terminal). `CLAUDE.md` tells it
how to start the bot, watch `status` and the logs, stop it cleanly, patch
problems, run the tests, restart and push fixes. Ask it something like
*"run the bot and keep it going; fix anything that gets stuck"*.

## Controlling the bot

**Commands** (run them in the activated `.venv`):

| Command | What it does |
|---|---|
| `wiz101-auto run -c config.yaml` | Start the bot (menu option 1 in `wiz101.bat`). Add `-m fight` or `-m farm` to change mode, and `-v` for detailed output. |
| `wiz101-auto inspect` | Print what the bot sees right now: zone, health, quest, and in battle your cards and the move it would make. |
| `wiz101-auto inspect --windows` | Also print the game's UI window tree. |
| `wiz101-auto deck` | Show the known spells and the deck the bot would build. Changes nothing. |
| `wiz101-auto deck --apply` | Rebuild the in-game deck from that plan. |
| `wiz101-auto explore` | Save every nearby NPC, door and mob with its position to `state/explore_*.txt`. |

**Launcher menu:** besides running the bot, `wiz101.bat` offers a health
check (`state\doctor.txt`), the deck plan (`state\deck.txt`), a deck rebuild,
what the bot sees (`state\inspect.txt`) and a zone recording, all saved to the
`state` folder ready to send.

**Keys while running:** **Ctrl+Shift+Q** stops and **Ctrl+Shift+P** pauses or resumes (Ctrl+C in the bot window also stops it). The keys
can be changed under `safety`.

**Settings:** everything is in `config.yaml`, which starts as a copy of
`configs/myth.yaml`. See `config.example.yaml` for every option. The ones you
will actually touch:

- `mode`: `quest`, `fight` or `farm`
- `safety.max_hours`: session time limit
- `quest.teleport`: `false` to walk instead of teleporting
- `progression.deck.include` / `exclude`: force a card in or keep one out,
  e.g. `include: {"Pixie": 2}`
- `progression.auto_train`: turn the experimental trainer clicks off

**Files the bot writes:**

- `wiz101-auto.log`: full debug log
- `state/progress.json`: level, known spells and the deck it last built
- `state/trainer_window_*.txt`: the layout of the spell trainer's window,
  saved the first time it opens
- `state/explore_*.txt`: output of `explore`

## Starting a brand-new wizard

The bot starts once your wizard is standing in the world, so do these by hand:

1. Create the character (the school quiz and appearance).
2. Optional, but a good idea: play the short opening tutorial. It's scripted
   and occasionally asks for specific clicks. If the bot gets stuck there,
   finish that part yourself and restart it.
3. From Wizard City onward, run `wiz101-auto run`.

**Spells and deck:** the bot rebuilds your deck on startup, on every level
up, whenever a trainer window closes, and every 30 minutes. Training new
spells still needs a visit to your professor (Cyrus Drake, the Myth
professor, in Ravenwood). The quest line takes you there sometimes. Auto
training clicks inside the trainer window are experimental until its
layout has been mapped. Walking to the professor automatically is on the
roadmap and needs `explore` output from Ravenwood and the Myth school.

**Progress limits:** Wizard City is free to play. Areas after it need a
membership or crown-purchased zones. Without either, the quest line
eventually hits a locked area and the bot stops with "no quest progress".

## Configuration

All options are documented in `config.example.yaml`. The most useful ones:

- `quest.teleport: false`: walk instead of teleporting. It's slower but looks
  less bot-like.
- `combat.strategy.heal_threshold`: heal below this share of your health.
- `combat.flee_below`: flee when health drops below this share.
- `safety.max_hours`: hard time limit on a session.

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
- Buying potions when out, and selling or clearing a full backpack
- Pet training

## Credits and license

- [WizWalker](https://github.com/StarrFox/wizwalker) by StarrFox: memory hooks
  and game object model.
- [Deimos](https://github.com/Deimos-Wizard101/Deimos-Wizard101): maintained
  WizWalker and WizSprinter forks (used as dependencies) and much of the UI
  window-path mapping.

Both are GPL-3.0, so this project is licensed **GPL-3.0-or-later**.
Not affiliated with or endorsed by KingsIsle Entertainment.
