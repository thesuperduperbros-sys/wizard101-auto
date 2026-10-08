"""Sell items selected by BackPack Buddy when the farm backpack is full."""

from __future__ import annotations

import asyncio
import math
import re
import time

from loguru import logger
from wizwalker import Keycode

from . import ui
from .upkeep import is_free, mob_positions, wait_for_loading

BAZAAR = "WizardCity/WC_Streets/Interiors/WC_OldeTown_AuctionHouse"
SALE_TIMEOUT = 120.0
RETRY_SECONDS = 300.0
CAPACITY_CHECK_SECONDS = 15.0


async def backpack_capacity(client) -> tuple[int, int]:
    """Read the game's displayed backpack slots used and allowed."""
    from .gear import PAGE, GearManager

    was_open = await ui.is_visible(client, PAGE)
    gear = GearManager(client, "")
    if not was_open and not await gear._open():
        raise RuntimeError("could not open the backpack to read its displayed capacity")
    try:
        text = await ui.named_text(client, "inventorySpace")
        return parse_backpack_capacity(text)
    finally:
        if not was_open:
            await gear._close()


async def inventory_item_count(client) -> int:
    """Return the raw inventory-object count for verifying that a sale changed it."""
    behavior = await client.client_object.try_get_inventory_behavior()
    if behavior is None:
        raise RuntimeError("inventory behavior is unavailable")
    return len(await behavior.item_list())


def parse_backpack_capacity(text: str) -> tuple[int, int]:
    text = ui._TAGS.sub("", text)
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text)
    if match is None:
        raise ValueError(f"invalid backpack capacity display: {text!r}")
    return int(match.group(1)), int(match.group(2))


def is_backpack_full(count: int, capacity: int) -> bool:
    return capacity > 0 and count >= capacity


def is_sale_label(text: str) -> bool:
    return " ".join(text.lower().split()) in {
        "sell", "sell items", "sell found items", "sell all"
    }


def is_no_items_notice(text: str) -> bool:
    return "no items in your backpack" in " ".join(text.casefold().split())


def label_matches(text: str, expected: str) -> bool:
    normalized = " ".join(text.casefold().split())
    wanted = " ".join(expected.casefold().split())
    return normalized == wanted or wanted in normalized


async def _visible_button_text(window, depth: int = 0) -> str:
    if depth > 5:
        return ""
    parts = []
    try:
        text = ui._TAGS.sub("", await window.maybe_text() or "").strip()
        if text:
            parts.append(text)
    except Exception:
        pass
    try:
        children = await window.children()
    except Exception:
        children = []
    for child in children:
        try:
            if await child.is_visible():
                parts.append(await _visible_button_text(child, depth + 1))
        except Exception:
            continue
    return " ".join(parts)


async def click_labeled_button(client, label: str) -> bool:
    """Click a visible button whose own or child text matches `label`."""
    try:
        windows = await client.root_window.get_windows_with_predicate(_is_button)
    except Exception as exc:
        logger.warning(f"backpack: could not inspect buttons for {label!r}: {exc!r}")
        return False
    for window in windows:
        try:
            if not await window.is_visible():
                continue
            text = await _visible_button_text(window)
            if not label_matches(text, label):
                continue
            await client.mouse_handler.click_window(window)
            logger.info(f"backpack: clicked {label!r} button ({text.strip()!r})")
            return True
        except Exception as exc:
            logger.debug(f"backpack: click {label!r} failed: {exc!r}")
    return False


async def click_sale_button(client) -> bool:
    """Click a visible button explicitly labeled for selling items."""
    try:
        windows = await client.root_window.get_windows_with_predicate(_is_sale_button)
    except Exception as exc:
        logger.warning(f"backpack: could not inspect sale buttons: {exc!r}")
        return False
    for window in windows:
        try:
            if not await window.is_visible():
                continue
            text = await _visible_button_text(window)
            if not is_sale_label(text):
                continue
            await client.mouse_handler.click_window(window)
            logger.info(f"backpack: clicked {text.strip()!r}")
            return True
        except Exception as exc:
            logger.debug(f"backpack: sale-button click failed: {exc!r}")
    return False


async def _is_sale_button(window) -> bool:
    return await _is_button(window)


async def _is_button(window) -> bool:
    try:
        kind = (await window.maybe_read_type_name() or "").lower()
        return "button" in kind
    except Exception:
        return False


async def confirm_sale_modal(client) -> bool:
    box = await ui.modal_box(client)
    if box is None:
        return False
    text = (await ui.modal_text(box)).lower()
    if "sell" not in text and "sale" not in text:
        return False
    return await ui.confirm_modal(client, buttons=("centerButton",))


class BackpackSeller:
    def __init__(self, quester):
        self.q = quester
        self.client = quester.client
        self._retry_at = 0.0
        self._next_capacity_check = 0.0
        self.pending_return = False
        self.last_sale_succeeded = False
        self.last_return_succeeded = False

    async def full(self) -> bool:
        count, capacity = await backpack_capacity(self.client)
        return is_backpack_full(count, capacity)

    async def tick(self, camp_zone: str, camp_position) -> bool:
        """Sell BackPack Buddy's found items and return to the saved farm camp."""
        now = time.monotonic()
        if now < self._next_capacity_check:
            return False
        self._next_capacity_check = now + CAPACITY_CHECK_SECONDS
        try:
            full = await self.full()
        except Exception as exc:
            logger.opt(exception=exc).warning("backpack: could not read inventory capacity")
            self._retry_at = time.monotonic() + RETRY_SECONDS
            return False
        if not full:
            return False
        if time.monotonic() < self._retry_at:
            return False
        self._retry_at = time.monotonic() + RETRY_SECONDS
        return await self._sell(camp_zone, camp_position)

    async def run_once(self, camp_zone: str, camp_position) -> bool:
        """Run the sale trip once without requiring the backpack to be full."""
        self._retry_at = 0.0
        return await self._sell(camp_zone, camp_position)

    async def _sell(self, camp_zone: str, camp_position) -> bool:
        self.last_sale_succeeded = False
        self.last_return_succeeded = False
        marked = await self.q._mark_here("travel", require_clear=False)
        if not marked:
            logger.warning("backpack: could not mark the farm camp; not leaving to sell items")
            return False
        logger.info("backpack full: going to the Wizard City Bazaar to sell found items")
        did_sale = False
        returned = False
        try:
            from .trainer import home_to_ravenwood

            zone = await self.client.zone_name() or ""
            if not zone.startswith("WizardCity/") or "/interiors/" in zone.lower():
                if not await home_to_ravenwood(self.q):
                    logger.warning("backpack: could not travel to Ravenwood")
                else:
                    did_sale = await self._travel_and_sell()
            else:
                did_sale = await self._travel_and_sell()
        except Exception as exc:
            logger.opt(exception=exc).warning("backpack sale trip failed")
        finally:
            try:
                returned = await self.return_to_camp(camp_zone, camp_position)
                self.pending_return = not returned
                if not returned:
                    logger.warning("backpack: could not return to the saved farm camp")
                elif did_sale:
                    logger.success("backpack: sale finished; returned to the farm camp")
                else:
                    logger.info("backpack: sale attempt finished; returned to the farm camp")
                if returned:
                    try:
                        if await self.full():
                            logger.warning("backpack: inventory is still full after the sale attempt")
                    except Exception as exc:
                        logger.opt(exception=exc).warning("backpack: could not verify inventory after sale")
            except Exception as exc:
                logger.opt(exception=exc).warning("backpack: returning from the Bazaar failed")
                self.pending_return = True
        self.last_sale_succeeded = did_sale
        self.last_return_succeeded = returned
        return did_sale and returned

    async def return_to_camp(self, camp_zone: str, camp_position) -> bool:
        for window, button in (
            ("BackpackBuddyWindow", "CloseBackpackBuddyButton"),
            ("BazaarMainWindow", "CloseBazaarMainButton"),
        ):
            path = ["WorldView", window]
            if await ui.is_visible(self.client, path) and not await ui.click_named(
                self.client, button
            ):
                logger.warning(f"backpack: could not close the Bazaar {window} before returning")
                return False
        if await self.client.zone_name() != camp_zone:
            if not await self.q._recall(camp_zone, "the farm camp"):
                from .trainer import home_to_ravenwood

                if not await home_to_ravenwood(self.q):
                    return False
                world = camp_zone.split("/", 1)[0]
                if world != "WizardCity" and not await self.q._to_world(
                    world, "return to farm camp"
                ):
                    return False
                if await self.client.zone_name() != camp_zone and not await self.q.go_to_zone(camp_zone):
                    return False
            if await self.client.zone_name() != camp_zone:
                return False
        from .bot import return_to_farm_camp

        return await return_to_farm_camp(self.client, self.q, camp_zone, camp_position)

    async def _travel_and_sell(self) -> bool:
        if not await self.q.go_to_zone(BAZAAR):
            logger.warning("backpack: could not reach the Wizard City Bazaar")
            return False
        await wait_for_loading(self.client)
        if await self.client.zone_name() != BAZAAR:
            logger.warning("backpack: did not arrive in the Bazaar")
            return False
        return await self._find_and_sell()

    async def _find_and_sell(self) -> bool:
        if not await self._find_items_from_bazaar_npc():
            return False
        before = await inventory_item_count(self.client)
        end = time.monotonic() + SALE_TIMEOUT
        clicked_sale = False
        while time.monotonic() < end:
            if not clicked_sale and await click_sale_button(self.client):
                clicked_sale = True
                continue
            box = await ui.modal_box(self.client)
            if box is not None and is_no_items_notice(await ui.modal_text(box)):
                if await ui.confirm_modal(self.client, buttons=("rightButton",)):
                    logger.warning("backpack: no items match the current BackPack Buddy options")
                else:
                    logger.warning("backpack: could not dismiss the BackPack Buddy no-items notice")
                return False
            if not await is_free(self.client):
                await confirm_sale_modal(self.client)
                await asyncio.sleep(0.3)
                continue
            count = await inventory_item_count(self.client)
            if count < before:
                await ui.close_menus(self.client)
                for _ in range(40):
                    if await is_free(self.client):
                        break
                    await confirm_sale_modal(self.client)
                    await asyncio.sleep(0.25)
                logger.success(f"backpack: inventory reduced from {before} to {count} items")
                return True
            if clicked_sale and await confirm_sale_modal(self.client):
                continue
            await asyncio.sleep(0.5)
        logger.warning("backpack: timed out waiting for the sale to finish or inventory to decrease")
        return False

    async def _find_items_from_bazaar_npc(self) -> bool:
        from .givers import is_named_npc
        from .names import lang_name

        if await ui.is_visible(self.client, ["WorldView", "BackpackBuddyWindow"]):
            return await self._wait_and_click_find_items()
        if await ui.is_visible(self.client, ["WorldView", "BazaarMainWindow"]):
            if not await click_labeled_button(self.client, "Backpack Buddy"):
                logger.warning("backpack: Bazaar menu is open, but BackPack Buddy is not visible")
                return False
            return await self._wait_and_click_find_items()

        if await self.q.services.is_open():
            labels = await self.q.services.visible_labels()
            logger.info(f"backpack: current Bazaar NPC offers {labels!r}")
            if await self.q.services.choose_named("Backpack Buddy"):
                return await self._wait_and_click_find_items()
            if await self.q.services.choose_named("Find Items"):
                return True
            logger.warning("backpack: current Bazaar NPC does not offer BackPack Buddy or Find Items")
            return False

        me = await self.client.body.position()
        hazards = await mob_positions(self.client)
        candidates = []
        for entity in await self.client.get_base_entity_list():
            try:
                template = await entity.object_template()
                if template is None:
                    continue
                object_name = await template.object_name() or ""
                code = await template.display_name()
                display = (await lang_name(self.client, code) if code else "") or object_name
                if not is_named_npc(object_name, display, await entity.list_behavior_names()):
                    continue
                position = await entity.location()
                if any(math.dist((position.x, position.y), hazard[:2]) < 700 for hazard in hazards):
                    continue
                distance = math.dist((position.x, position.y), (me.x, me.y))
                candidates.append((distance, display, position))
            except Exception as exc:
                logger.debug(f"backpack: couldn't inspect a Bazaar NPC: {exc!r}")
        candidates.sort(key=lambda candidate: candidate[0])
        if not candidates:
            logger.warning("backpack: no named NPCs are currently loaded in the Bazaar")
            return False

        for distance, name, position in candidates:
            logger.info(f"backpack: checking Bazaar NPC {name!r} ({distance:.0f} away) for Find Items")
            if not await self.q.travel(position, npc=True):
                continue
            for _ in range(8):
                if await ui.is_visible(self.client, ui.NPC_RANGE):
                    break
                await asyncio.sleep(0.3)
            else:
                continue
            prompt = (await ui.text_at(self.client, ui.NPC_RANGE_TEXT)).lower()
            if "talk" not in prompt:
                continue
            await self.client.send_key(Keycode.X, 0.1)
            for _ in range(20):
                if (
                    await self.q.services.is_open()
                    or await ui.is_visible(self.client, ["WorldView", "BazaarMainWindow"])
                    or await ui.is_visible(self.client, ["WorldView", "BackpackBuddyWindow"])
                ):
                    break
                if await ui.is_visible(self.client, ui.ADVANCE_DIALOG):
                    await self.client.send_key(Keycode.ESC, 0.1)
                    await asyncio.sleep(0.5)
                    break
                await asyncio.sleep(0.25)
            if await ui.is_visible(self.client, ["WorldView", "BackpackBuddyWindow"]):
                return await self._wait_and_click_find_items()
            if await ui.is_visible(self.client, ["WorldView", "BazaarMainWindow"]):
                if await click_labeled_button(self.client, "Backpack Buddy"):
                    if await self._wait_and_click_find_items():
                        logger.info(f"backpack: opened BackPack Buddy from {name!r}'s Bazaar menu")
                        return True
                continue
            if not await self.q.services.is_open():
                continue
            labels = await self.q.services.visible_labels()
            logger.info(f"backpack: {name!r} offers {labels!r}")
            if await self.q.services.choose_named("Backpack Buddy"):
                if await self._wait_and_click_find_items():
                    logger.info(f"backpack: selected Find Items from {name!r}'s BackPack Buddy")
                    return True
            elif await self.q.services.choose_named("Find Items"):
                logger.info(f"backpack: selected Find Items directly from {name!r}")
                return True
            await self.q.services.close()
        logger.warning("backpack: no Bazaar NPC offered the exact 'Find Items' service")
        return False

    async def _wait_and_click_find_items(self) -> bool:
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if await click_labeled_button(self.client, "Find Items"):
                return True
            if await self.q.services.is_open() and await self.q.services.choose_named("Find Items"):
                return True
            await asyncio.sleep(0.25)
        logger.warning("backpack: BackPack Buddy opened, but its Find Items button did not appear")
        return False
