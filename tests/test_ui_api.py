def test_ui_helpers_used_elsewhere_exist():
    """Names other modules call on `ui` must exist (one was lost in an edit once)."""
    import re
    from pathlib import Path

    from wiz101_auto import ui

    src = Path(__file__).resolve().parents[1] / "src" / "wiz101_auto"
    used = set()
    for f in src.rglob("*.py"):
        used |= set(re.findall(r"\bui\.([a-z_]+)\(", f.read_text(encoding="utf-8")))
    missing = sorted(n for n in used if not hasattr(ui, n))
    assert not missing, f"ui is missing: {missing}"


def test_endorsement_popup_selects_friendly_and_verifies_dismissal(monkeypatch):
    import asyncio

    from wiz101_auto import ui, upkeep

    visible = iter((True, False, False))
    clicked = []

    async def is_visible(_client, path):
        assert path == ui.ENDORSEMENT
        return next(visible)

    async def click(_client, path):
        clicked.append(path)
        return True

    async def sleep(_seconds):
        return None

    monkeypatch.setattr(ui, "is_visible", is_visible)
    monkeypatch.setattr(ui, "click", click)
    monkeypatch.setattr(upkeep.asyncio, "sleep", sleep)

    assert asyncio.run(upkeep.dismiss_endorsement(object()))
    assert clicked == [ui.ENDORSE_FRIENDLY]


def test_endorsement_popup_falls_back_to_close_button(monkeypatch):
    import asyncio

    from wiz101_auto import ui, upkeep

    visible = iter((True, True, False))
    clicked = []

    async def is_visible(_client, _path):
        return next(visible)

    async def click(_client, path):
        clicked.append(path)
        return path == ui.ENDORSE_CLOSE

    async def sleep(_seconds):
        return None

    monkeypatch.setattr(ui, "is_visible", is_visible)
    monkeypatch.setattr(ui, "click", click)
    monkeypatch.setattr(upkeep.asyncio, "sleep", sleep)

    assert asyncio.run(upkeep.dismiss_endorsement(object()))
    assert clicked == [ui.ENDORSE_FRIENDLY, ui.ENDORSE_CLOSE]
