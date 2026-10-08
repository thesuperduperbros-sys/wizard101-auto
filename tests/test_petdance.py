from wiz101_auto import petdance


def test_moves_decode_to_wasd():
    assert petdance.decode_moves(b"acbd\0\0\0\0") == "WSDA"
    assert petdance.decode_moves(b"\0" * 8) == ""


def test_post_keys_adds_a_small_pause_between_move_inputs(monkeypatch):
    events = []
    pauses = []

    class User32:
        @staticmethod
        def PostMessageW(hwnd, message, key, _):
            events.append((hwnd, message, key))

    class Ctypes:
        windll = type("WinDLL", (), {"user32": User32})()

    monkeypatch.setattr(petdance, "ctypes", Ctypes)
    monkeypatch.setattr(petdance.time, "sleep", pauses.append)

    petdance.post_keys(42, "WD")

    assert events == [
        (42, petdance.WM_KEYDOWN, ord("W")),
        (42, petdance.WM_KEYUP, ord("W")),
        (42, petdance.WM_KEYDOWN, ord("D")),
        (42, petdance.WM_KEYUP, ord("D")),
    ]
    assert pauses == [petdance.DANCE_MOVE_DELAY, petdance.DANCE_MOVE_DELAY]


def test_available_tracks_returns_visible_track_indices():
    import asyncio

    class Window:
        def __init__(self, name, children=(), visible=True):
            self._name = name
            self._children = list(children)
            self._visible = visible

        async def name(self):
            return self._name

        async def children(self):
            return self._children

        async def is_visible(self):
            return self._visible

    panel = Window(
        "PetGameTracks",
        [
            Window("btnTrack0"),
            Window("layout", [Window("btnTrack2"), Window("btnTrack1", visible=False)]),
            Window("btnNext"),
        ],
    )

    assert asyncio.run(petdance.available_tracks(panel)) == [0, 2]


def test_energy_numbers():
    assert petdance.first_number("Energy: 12/45") == 12
    assert petdance.first_number("Cost: 5") == 5
    assert petdance.first_number("") is None


def test_request(tmp_path, monkeypatch):
    monkeypatch.setattr(petdance, "PET_REQUEST", tmp_path / "pet.request")
    assert petdance.games_requested() is None
    (tmp_path / "pet.request").write_text("0")
    assert petdance.games_requested() == 0
    (tmp_path / "pet.request").write_text("3")
    assert petdance.games_requested() == 3


def test_stage_and_goal():
    assert petdance.stage_in(["Your pet is now an Adult!", "Teen"]) == "adult"
    assert petdance.stage_in(["nothing here"]) is None
    assert petdance.kind_in(["Rudy the Bloodbat"], ["bloodbat"]) == "bloodbat"
    goals = {"bloodbat": "adult"}
    assert not petdance.goal_reached("bloodbat", "teen", goals, "mega")
    assert petdance.goal_reached("bloodbat", "adult", goals, "mega")
    assert not petdance.goal_reached("wolf", "adult", goals, "mega")
    assert petdance.goal_reached("wolf", "mega", goals, "mega")


def test_pet_goal_can_be_ignored_until_energy_runs_out(tmp_path, monkeypatch):
    from wiz101_auto.config import PetConfig

    monkeypatch.setattr(petdance, "PET_STATE", tmp_path / "pet.json")
    petdance.save_pet({"kind": "bloodbat", "stage": "adult"})
    dancer = object.__new__(petdance.PetDancer)
    dancer.cfg = PetConfig(stop_at_goal=False)
    assert not dancer.done()

    dancer.cfg.stop_at_goal = True
    assert dancer.done()
