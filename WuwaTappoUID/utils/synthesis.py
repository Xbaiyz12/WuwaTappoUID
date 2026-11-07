"""官方客户端的合成材料规则：每种留一件，升阶十件、同阶五件。"""

from dataclasses import dataclass

from .schema import DataError, Item

KINDS = ("character_skin", "accessory", "emote")
RARITIES = ("common", "uncommon", "rare", "epic")


@dataclass
class Recipe:
    same_level: bool
    asset_ids: list[int]


def category(item: Item) -> tuple[str, str]:
    kind = item.asset_type.removesuffix("_atlas")
    if kind == "character":
        kind = "character_skin"
    rarity = str(item.rarity or "").strip().lower()
    if kind not in KINDS or rarity not in RARITIES:
        raise DataError("背包或图鉴包含未识别的品类/稀有度，已停止合成。")
    return kind, rarity


def highest_tiers(atlas: list[Item]) -> dict[str, str]:
    highest: dict[str, str] = {}
    for item in atlas:
        kind, rarity = category(item)
        if kind not in highest or RARITIES.index(rarity) > RARITIES.index(highest[kind]):
            highest[kind] = rarity
    if any(kind not in highest for kind in KINDS):
        raise DataError("图鉴缺少品类，无法确定升阶上限。")
    return highest


def next_recipe(items: list[Item], highest: dict[str, str], *, same_level: bool = False) -> Recipe | None:
    groups: dict[tuple[str, str], list[Item]] = {}
    for item in items:
        if item.quantity > 1 and not item.is_collect:
            groups.setdefault(category(item), []).append(item)
    cost = 5 if same_level else 10
    for kind in KINDS:
        for rarity in RARITIES:
            if not same_level and RARITIES.index(rarity) >= RARITIES.index(highest[kind]):
                continue
            group = groups.get((kind, rarity), [])
            if sum(item.quantity - 1 for item in group) < cost:
                continue
            material_ids: list[int] = []
            for item in sorted(group, key=lambda entry: int(entry.asset_id)):
                count = min(item.quantity - 1, cost - len(material_ids))
                material_ids.extend([int(item.asset_id)] * count)
                if len(material_ids) == cost:
                    return Recipe(same_level, material_ids)
    return None
