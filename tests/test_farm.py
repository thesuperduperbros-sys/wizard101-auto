from wizwalker import XYZ

from wiz101_auto.farm import Farm


def test_target_mob_name_matches_case_and_spacing_only():
    from wiz101_auto.farm import is_farm_zone, is_target_mob

    assert is_target_mob("Troubled Warrior", "troubled warrior")
    assert not is_target_mob("Warrior", "Troubled Warrior")
    assert is_farm_zone("Grizzleheim/GH_Hero", "Grizzleheim/GH_Hero")
    assert is_farm_zone("Grizzleheim/GH_Hero/Interior", "Grizzleheim/GH_Hero")
    assert not is_farm_zone("Grizzleheim/GH_HeroExtra", "Grizzleheim/GH_Hero")


def test_northguard_is_a_healing_hub():
    from wiz101_auto.farm import FARM_HEAL_ZONE
    from wiz101_auto.upkeep import is_hub_zone

    assert FARM_HEAL_ZONE == "Grizzleheim/GH_MainHub"
    assert is_hub_zone(FARM_HEAL_ZONE)


def test_backpack_full_uses_capacity_not_a_hard_coded_count():
    from wiz101_auto.backpack import (
        is_backpack_full,
        is_no_items_notice,
        is_sale_label,
        label_matches,
        parse_backpack_capacity,
    )

    assert parse_backpack_capacity("82/150") == (82, 150)
    assert parse_backpack_capacity("<center>82/150") == (82, 150)
    assert parse_backpack_capacity(" 150 / 150 ") == (150, 150)
    assert not is_backpack_full(82, 150)
    assert is_backpack_full(150, 150)
    assert is_backpack_full(151, 150)
    assert not is_backpack_full(149, 150)
    assert not is_backpack_full(150, 0)
    assert is_sale_label("Sell Items")
    assert not is_sale_label("Sell Pets")
    assert label_matches("Find Items", "Find Items")
    assert label_matches("BackPack Buddy", "Backpack Buddy")
    assert not label_matches("Sell Items", "Find Items")
    assert is_no_items_notice("You have no items in your backpack that match the current options.")
    assert not is_no_items_notice("Are you sure that you want to sell 7 items?")


def test_failed_backpack_sale_does_not_pause_farming_during_retry_cooldown(monkeypatch):
    import asyncio

    from wiz101_auto.backpack import BackpackSeller

    class Quester:
        client = object()

    seller = BackpackSeller(Quester())
    monkeypatch.setattr(seller, "full", lambda: _async_true())
    attempts = 0

    async def fail_sale(_zone, _position):
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(seller, "_sell", fail_sale)

    async def check():
        assert not await seller.tick("Grizzleheim/GH_Hero", XYZ(0, 0, 0))
        assert not await seller.tick("Grizzleheim/GH_Hero", XYZ(0, 0, 0))

    async def _async_true():
        return True

    asyncio.run(check())
    assert attempts == 1


def test_backpack_buddy_service_must_match_find_items():
    from wiz101_auto.npc import named_service_matches

    assert named_service_matches("Find Items", "Find Items")
    assert named_service_matches("Find Items in your backpack", "Find Items")
    assert not named_service_matches("Sell Items", "Find Items")


def test_farm_camp_return_travels_back_to_zone_and_saved_position():
    import asyncio
    from types import SimpleNamespace

    from wiz101_auto.bot import return_to_farm_camp

    class Client:
        def __init__(self):
            self.zone = "WizardCity/WC_Commons"
            self.position = XYZ(500, 500, 0)
            self.body = SimpleNamespace(position=self.get_position)

        async def get_position(self):
            return self.position

        async def zone_name(self):
            return self.zone

        async def teleport(self, position):
            self.position = position

    class Quester:
        def __init__(self, client):
            self.client = client

        async def go_to_zone(self, zone):
            self.client.zone = zone
            return True

    client = Client()
    camp = XYZ(10, 20, 0)
    assert asyncio.run(return_to_farm_camp(client, Quester(client), "Grizzleheim/GH_Hero", camp))
    assert client.zone == "Grizzleheim/GH_Hero"
    assert client.position == camp


def test_farm_camp_return_stops_if_travel_fails():
    import asyncio
    from types import SimpleNamespace

    from wiz101_auto.bot import return_to_farm_camp

    class Client:
        body = SimpleNamespace(position=lambda: XYZ(500, 500, 0))

        async def zone_name(self):
            return "WizardCity/WC_Commons"

    class Quester:
        async def go_to_zone(self, _zone):
            return False

    assert not asyncio.run(
        return_to_farm_camp(Client(), Quester(), "Grizzleheim/GH_Hero", XYZ(10, 20, 0))
    )


def test_farm_recovery_travels_to_northguard():
    import asyncio

    from wiz101_auto.bot import go_to_farm_recovery_zone
    from wiz101_auto.farm import FARM_HEAL_ZONE

    class Client:
        zone = "Grizzleheim/GH_Hero"

        async def zone_name(self):
            return self.zone

    class Quester:
        def __init__(self, client):
            self.client = client
            self.destinations = []

        async def go_to_zone(self, zone):
            self.destinations.append(zone)
            self.client.zone = zone
            return True

    client = Client()
    quester = Quester(client)
    assert asyncio.run(go_to_farm_recovery_zone(client, quester))
    assert quester.destinations == [FARM_HEAL_ZONE]


def test_farm_recovery_does_not_claim_failed_northguard_travel():
    import asyncio

    from wiz101_auto.bot import go_to_farm_recovery_zone
    from wiz101_auto.farm import FARM_HEAL_ZONE

    class Client:
        async def zone_name(self):
            return "Grizzleheim/GH_Hero"

    class Quester:
        async def go_to_zone(self, zone):
            assert zone == FARM_HEAL_ZONE
            return False

    assert not asyncio.run(go_to_farm_recovery_zone(Client(), Quester()))


def test_farm_runs_end_on_the_final_boss_and_count(tmp_path):
    path = tmp_path / "farm.json"
    farm = Farm(active=True)
    assert farm.ends_run(["Zeus Sky Father"])
    assert not farm.ends_run(["Ares Savage Spear", "Apollo Bright One"])
    farm.record_run(path)
    farm.record_run(path)
    again = Farm.load(path)
    assert again.runs == 2 and again.active and again.final_boss == "Zeus Sky Father"


def test_targets_fill_in_as_looted():
    from wiz101_auto.farm import Farm, target_status

    looted = {"Helmet of Zeus' Will": {"slot": "Hat"}, "Zeus' Armor of Supremacy": {"slot": "Robe"}}
    status = {t["name"]: t["have"] for t in target_status("Mount Olympus", looted)}
    assert status["Helmet of Zeus' Will"] and status["Zeus' Armor of Supremacy"]
    assert not status["Boots of Zeus' Lore"]
    assert not Farm().targets_done(looted)
    assert Farm().targets_done({**looted, "Boots of Zeus' Lore": {"slot": "Shoes"}})


def test_raiment_is_a_robe_and_hasta_a_wand():
    from wiz101_auto.gear import is_wand, item_slot

    assert item_slot(["Zeus' Conjurer Raiment"]) == "Tab_Robe"
    assert is_wand(["Sky Iron Hasta"]) and item_slot(["Sky Iron Hasta"]) is None
