from wiz101_auto.givers import parse_guide, pending_givers, same_quest

GUIDE = """REGENT'S SQUARE
(MAIN QUEST)

Sergeant Major Talbot
Missing Souls(150 XP, 1 extra potion)
-Talk to Private Kinchley in Wolfminster Abbey

Sherlock Bones
The Last Meow(88 Gold, 1640 XP, Ring)
-Defeat Meowiarty in Big Ben

Sherlock Bones
Bad News...(176 Gold, 265 XP, Telescope) (after finishing “The Last Meow”)
- Talk to Merle Ambrose

(SIDE QUEST)

Mayor Pimsbury
Under the weather(83 gold, 245 XP)
-Talk to Houghe Warner

Houghe Warner
Down in the park(83 gold, 500 XP)(after finishing “Under the weather”)
-Talk back to Houghe Warner

Officer Darby
Crazy Cats(55 Gold, 850 XP)
-Defeat 8 O’Leary Scurrier

Officer Darby
More Crazy Cats(55 Gold, 865 XP) (after finishing “Crazy Cats”)
-Defeat 8 O’Leary Burglars

THE IRONWORKS

Baxter
Gate Crushers(63 Gold, 1360 XP)
-Talk to Baxter
"""


def test_parse_guide_reads_givers_and_what_comes_first():
    g = parse_guide(GUIDE)
    assert [(q.giver, q.name) for q in g][:2] == [("Sergeant Major Talbot", "Missing Souls"),
                                                  ("Sherlock Bones", "The Last Meow")]
    assert g[2].after == "The Last Meow" and g[2].main
    assert not g[3].main and g[4].after == "Under the weather"


def test_same_quest_forgives_typos_not_sequels():
    assert same_quest("Gate Crushers", "Gate Crashers")
    assert same_quest("Mail Calll", "Mail Call")
    assert not same_quest("More Crazy Cats", "Crazy Cats")


def test_pending_givers():
    g = parse_guide(GUIDE)
    # The Last Meow held: the story before it is done; Bad News waits on it.
    # Gate Crashers skipped (held); Under the Weather logged done.
    out = pending_givers(g, {"The Last Meow", "Gate Crashers"}, {"Under the Weather"})
    assert out == {"houghewarner": ["Down in the park"], "officerdarby": ["Crazy Cats"]}
    # More Crazy Cats held: Crazy Cats (what it came after) is done too.
    out = pending_givers(g, {"The Last Meow", "Gate Crashers", "More Crazy Cats"}, {"Under the Weather"})
    assert "officerdarby" not in out


def test_side_quest_focus_only_visits_side_quest_givers(monkeypatch):
    from types import SimpleNamespace

    import wiz101_auto.givers as givers

    guide = parse_guide("""DRAGONSPYRE
(MAIN QUEST)

Story Giver
Main Quest (100 XP)
- Talk to someone

(SIDE QUEST)

Side Giver
Optional Quest (200 XP)
- Talk to someone
""")
    q = SimpleNamespace(
        cfg=SimpleNamespace(side_quest_world="Dragonspyre"),
        setbacks=SimpleNamespace(skipped=set()),
    )
    visitor = givers.QuestGivers.__new__(givers.QuestGivers)
    visitor.q = q
    visitor._guides = {"Dragonspyre": guide}
    monkeypatch.setattr(givers, "_book_and_done", lambda: (set(), set()))

    assert visitor.wanted_givers("DragonSpire/DS_Hub_Cathedral") == {"sidegiver": ["Optional Quest"]}


def test_prospector_zeke_is_never_a_giver_to_visit():
    g = parse_guide("Prospector Zeke\nStray Cat Strut(176 gold, 1640 XP)\n-Locate Regent's Square Cat\n")
    assert pending_givers(g, set(), set()) == {}


def test_a_new_worlds_story_starts_at_its_first_quest():
    g = parse_guide("""(MAIN QUEST)

Ken Shui
Be Very, Very Quiet (100 gold, 2030 XP)
- Defeat 10 Cursed Ronins

Ken Shui
Or Call A Locksmith (100 gold, 2030 XP) (after finishing “Be Very, Very Quiet”)
- Collect Spectral Key

Yishin Chen
Tree of Life (206 gold, 2840 XP) (after finishing “Or Call A Locksmith”)
- Defeat Kagemoosha
""")
    assert pending_givers(g, set(), set()) == {"kenshui": ["Be Very, Very Quiet"]}


def test_quests_know_the_area_heading_they_are_under():
    g = parse_guide("""VILLAGE OF SORROW
(MAIN QUEST)

Ken Shui
Be Very, Very Quiet (100 gold, 2030 XP)
- Defeat 10 Cursed Ronins

NEWGATE PRISON (MAIN QUEST)
Officer Ness
Stop that cat!(400 XP, Buried Bone)
- Locate Meowiarty’s cell
""")
    assert [q.area for q in g] == ["VILLAGE OF SORROW", "NEWGATE PRISON"]
