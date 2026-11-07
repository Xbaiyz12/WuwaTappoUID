"""真实开箱结果和分类背包到 Pillow 卡片的数据适配。"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from gsuid_core.logger import logger
from gsuid_core.models import Event
from pydantic import BaseModel

from .api import WuwaAPI
from .atlas_card import RARITIES as ATLAS_RARITIES
from .atlas_card import AtlasGroup, AtlasItem, render_atlas_card
from .backpack import BagFilter
from .chest_card import Card, render_chest_card
from .database import Account, ChestRecord, ChestTask, Store
from .formatting import mask_cuid
from .render_cache import LocalImage, render_in_thread
from .render_common import Header, Progress
from .reply_card import TaskSnapshot, render_account, render_clicks, render_task
from .resources import Resources
from .schema import ApiError, Item, NetworkError, OpenResult, SynthesisResult, UserInfo, WuwaError
from .statistics import CHINA, ChestStatistics, StatisticsPeriod
from .statistics_card import render_statistics

KINDS = {"character": "角色", "accessory": "配件", "emote": "表情"}
RESULT_PAGE_SIZE = 40
CATALOG_PATH = Path(__file__).parent.parent / "assets" / "tappo" / "catalog.json"


class CatalogEntry(BaseModel):
    asset: int | str
    name: str
    type: str
    rarity: str
    role: int | str = 0


class BundledCatalog(BaseModel):
    items: list[CatalogEntry]
    roles: dict[str, str]


@dataclass
class ResultContext:
    header: Header
    progress: list[Progress] | None


class Cards:
    def __init__(self, api: WuwaAPI, resources: Resources) -> None:
        self.api = api
        self.resources = resources
        catalog = BundledCatalog.model_validate_json(CATALOG_PATH.read_bytes())
        self._bundled_catalog = {str(item.asset): item for item in catalog.items}
        self._bundled_roles = catalog.roles
        self._catalog_source: tuple[int, int] | None = None
        self._catalog_map: dict[str, CatalogEntry] = {}
        self._catalog_types: dict[str, list[CatalogEntry]] = {}
        self._catalog_rarities: dict[tuple[str, str], list[CatalogEntry]] = {}
        self._avatars: dict[str, tuple[float, bytes]] = {}

    def _refresh_catalog(self) -> None:
        live = self.resources.catalog
        source = (id(live), id(self.resources.dictionary))
        if source == self._catalog_source:
            return
        if live is None:
            self._catalog_map = self._bundled_catalog
        else:
            self._catalog_map = {
                str(item.asset_id): CatalogEntry(
                    asset=item.asset_id,
                    name=self.resources.name(item.asset_name, item.asset_id),
                    type="character" if item.asset_type == "character_skin" else item.asset_type,
                    rarity=str(item.rarity).lower(),
                    role=item.role_id,
                )
                for item in live.items
            }
        self._catalog_types = {}
        self._catalog_rarities = {}
        for entry in self._catalog_map.values():
            self._catalog_types.setdefault(entry.type, []).append(entry)
            self._catalog_rarities.setdefault((entry.type, entry.rarity), []).append(entry)
        self._catalog_source = source

    @property
    def catalog(self) -> dict[str, CatalogEntry]:
        self._refresh_catalog()
        return self._catalog_map

    def catalog_group(self, kind: str, rarity: str | None = None) -> list[CatalogEntry]:
        self._refresh_catalog()
        if rarity is None:
            return self._catalog_types[kind] if kind in self._catalog_types else []
        key = (kind, rarity)
        return self._catalog_rarities[key] if key in self._catalog_rarities else []

    def kind(self, asset_id: str | int, raw: str = "") -> str:
        key = str(asset_id)
        manifest = self.resources.manifest
        if manifest is not None:
            for group, kind in (
                (manifest.character_skin, "character"),
                (manifest.accessory, "accessory"),
                (manifest.emote, "emote"),
            ):
                if key in group:
                    return kind
        catalog = self.catalog
        if key in catalog:
            return catalog[key].type
        return "character" if raw == "character_skin" else raw

    @staticmethod
    def _read_images(paths: dict[str, Path]) -> dict[str, bytes]:
        return {key: path.read_bytes() for key, path in paths.items() if path.exists()}

    def item_name(self, asset_id: str | int, raw: str | None = None) -> str:
        entry = self.catalog.get(str(asset_id))
        return entry.name if entry is not None and entry.name else self.resources.name(raw, asset_id)

    async def item_cards(self, items: list[Item], *, lazy: bool = False) -> list[Card]:
        catalog = self.catalog
        manifest = self.resources.manifest
        paths: dict[str, Path] = {}
        cards = []
        for item in items:
            key = str(item.asset_id)
            entry = catalog[key] if key in catalog else None
            name = self.item_name(item.asset_id, item.asset_name)
            rarity = str(item.rarity or (entry.rarity if entry is not None else "common")).lower()
            cards.append(Card(name=name, rarity=rarity, kind=self.kind(key, item.asset_type)))
            if manifest is not None:
                relative = manifest.lookup(key)
                if relative:
                    paths[key] = self.resources._raw_path(self.resources._image_url(relative))
        if lazy:
            for item, card in zip(items, cards, strict=True):
                path = paths.get(str(item.asset_id))
                card.image = LocalImage(path, self.resources.revision) if path is not None else None
            return cards
        # 一批图片只进入一次线程池；同批重复物品共用一次文件读取。
        images = await asyncio.to_thread(self._read_images, paths) if paths else {}
        for item, card in zip(items, cards, strict=True):
            key = str(item.asset_id)
            card.image = images[key] if key in images else None
        return cards

    async def header(self, account: Account, available: int | None, event: Event) -> Header:
        # 与原项目一致：昵称和头像属于消息发送者；UID 属于绑定的鸣潮角色。
        cached_name, cached_avatar = await Store.sender_profile(event)
        sender = event.sender or {}
        nickname = sender.get("nickname") or cached_name or "漂泊者"
        avatar_url = sender.get("avatar") or cached_avatar
        if not avatar_url.startswith(("http://", "https://")):
            if event.bot_id == "onebot":
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={event.user_id}&s=640"
            elif event.bot_id == "qqgroup":
                avatar_url = f"https://q.qlogo.cn/qqapp/{event.bot_self_id}/{event.user_id}/100"
            else:
                avatar_url = ""
        face = None
        if avatar_url:
            cached = self._avatars.get(avatar_url)
            if cached is not None and cached[0] > time.time():
                face = cached[1]
            else:
                try:
                    async with asyncio.timeout(12):
                        path = (
                            self.resources.root / "avatars" / (hashlib.sha256(avatar_url.encode()).hexdigest() + ".png")
                        )
                        if path.exists() and path.stat().st_mtime > time.time() - 3600:
                            face = await asyncio.to_thread(path.read_bytes)
                        else:
                            face = await self.api.fetch_bytes(avatar_url)
                            await asyncio.to_thread(self.resources._write_file, path, face)
                        self._avatars[avatar_url] = (time.time() + 3600, face)
                except (WuwaError, TimeoutError, OSError) as error:
                    logger.warning(f"[WuwaTappoUID] 聊天头像暂不可用：{type(error).__name__}")
        info = UserInfo.model_validate_json(account.info) if account.info else None
        uid = str(info.player_id) if info and info.player_id else ""
        return Header(nickname=nickname, uid=uid if uid != "0" else "", available=available, avatar=face)

    def progress(self, items: list[Item]) -> list[Progress]:
        owned: dict[str, set[str]] = {kind: set() for kind in KINDS}
        for item in items:
            if item.quantity > 0:
                kind = self.kind(item.asset_id, item.asset_type)
                if kind in owned:
                    owned[kind].add(str(item.asset_id))
        return [Progress(label, len(owned[kind]), len(self.catalog_group(kind))) for kind, label in KINDS.items()]

    async def draw(
        self, header: Header, title: str, cards: list[Card], note: str, progress: list[Progress] | None = None
    ) -> bytes:
        return await render_in_thread(render_chest_card, header, title, cards, note, progress)

    @staticmethod
    def task_snapshot(task: ChestTask, subscribed: bool) -> TaskSnapshot:
        return TaskSnapshot(
            task.status,
            task.pause_reason,
            task.started_at,
            task.expires_at,
            task.paused_at,
            task.successes,
            subscribed,
        )

    async def desktop_avatar(self, account: Account, info: UserInfo) -> bytes | None:
        if info.character is None:
            return None
        try:
            raw = await self.resources.character_head(info.character)
        except (WuwaError, OSError) as error:
            if isinstance(error, (ApiError, NetworkError)):
                detail = error.details
            elif isinstance(error, WuwaError):
                detail = str(error)
            else:
                detail = type(error).__name__
            logger.warning(f"[WuwaTappoUID] 桌宠头像暂不可用，使用占位：{detail}。")
            return None
        return raw

    async def account_summary(
        self,
        account: Account,
        task: ChestTask | None,
        event: Event,
    ) -> bytes:
        info = UserInfo.model_validate_json(account.info)
        snapshot = self.task_snapshot(task, account.subscribed) if task is not None else None
        profile = await self.header(account, None, event)
        desktop_avatar = await self.desktop_avatar(account, info)
        return await render_in_thread(
            render_account,
            profile,
            info,
            mask_cuid(account.cuid),
            account.domestic,
            snapshot,
            account.subscribed,
            desktop_avatar,
        )

    async def click_summary(
        self,
        account: Account,
        event: Event,
        requested: int | None = None,
        delta: int = 0,
    ) -> bytes:
        info = UserInfo.model_validate_json(account.info)
        profile = await self.header(account, None, event)
        return await render_in_thread(
            render_clicks,
            profile,
            info,
            requested,
            delta,
        )

    async def task_summary(self, account: Account, task: ChestTask, event: Event) -> bytes:
        snapshot = self.task_snapshot(task, account.subscribed)
        profile = await self.header(account, None, event)
        return await render_in_thread(render_task, profile, snapshot)

    async def result_context(self, account: Account, event: Event) -> ResultContext:
        progress = None
        if account.token_state == "expired":
            return ResultContext(await self.header(account, None, event), progress)
        try:
            async with asyncio.timeout(12):
                progress = self.progress(await self.api.bags(account.token))
        except (WuwaError, TimeoutError) as error:
            logger.warning(f"[WuwaTappoUID] 本次汇总图无法刷新收集进度：{type(error).__name__}")
        return ResultContext(await self.header(account, None, event), progress)

    async def results(
        self, records: list[ChestRecord], available: int | None, note: str, context: ResultContext
    ) -> tuple[bytes, ...]:
        pages = (len(records) + RESULT_PAGE_SIZE - 1) // RESULT_PAGE_SIZE
        images = []
        for offset in range(0, len(records), RESULT_PAGE_SIZE):
            page_note = f"{note} · 第 {offset // RESULT_PAGE_SIZE + 1}/{pages} 页" if pages > 1 else note
            images.append(
                await self.result_page(records[offset : offset + RESULT_PAGE_SIZE], available, page_note, context)
            )
        return tuple(images)

    async def result_page(
        self, records: list[ChestRecord], available: int | None, note: str, context: ResultContext
    ) -> bytes:
        results = [OpenResult.model_validate_json(record.result) for record in records if record.result]
        loaded = iter(
            await self.item_cards(
                [
                    Item(asset_id=result.asset_id, asset_name=result.asset_name, rarity=result.rarity_type)
                    for result in results
                ]
            )
        )
        cards = [
            next(loaded) if record.result else Card(name="物品信息未收到", rarity="common", kind="未知")
            for record in records
        ]
        header = replace(context.header, available=available)
        return await self.draw(header, "开箱结果", cards, note, context.progress)

    def item_role(self, item: Item) -> str:
        role = str(item.role_id).strip()
        if role and role != "0":
            return role
        match = re.fullmatch(r"(?:role|emote)_(\d+)_[a-z0-9]+(?:\.png)?", item.asset_name or "", re.I)
        if match:
            return match[1]
        entry = self.catalog.get(str(item.asset_id))
        return str(entry.role) if entry is not None else "0"

    def filter_bag(self, items: list[Item], filters: BagFilter) -> list[Item]:
        role_ids: set[str] | None = None
        if filters.role:
            roles = dict(self._bundled_roles)
            if self.resources.dictionary is not None:
                roles.update(self.resources.dictionary.character)
            role_ids = {key for key, name in roles.items() if name == filters.role}
            if not role_ids:
                raise WuwaError(f"未找到角色“{filters.role}”，请使用完整角色名，例如 角色=今汐。")
        selected = []
        for item in items:
            entry = self.catalog.get(str(item.asset_id))
            rarity = str(item.rarity or (entry.rarity if entry is not None else "")).strip().lower()
            if item.quantity <= 0:
                continue
            if filters.kind and self.kind(item.asset_id, item.asset_type) != filters.kind:
                continue
            if filters.rarity and rarity != filters.rarity:
                continue
            if filters.duplicates and item.quantity <= 1:
                continue
            if role_ids is not None and self.item_role(item) not in role_ids:
                continue
            selected.append(item)
        return selected

    async def backpack(
        self, account: Account, items: list[Item], event: Event, filters: BagFilter | None = None
    ) -> list[bytes]:
        filters = filters or BagFilter()
        selected = self.filter_bag(items, filters)
        if filters.description and not selected:
            raise WuwaError(f"没有符合条件的背包物品：{filters.description}。")
        info = UserInfo.model_validate_json(account.info)
        header = await self.header(account, info.remaining, event)
        progress = self.progress(items)
        header.available = None
        header.collected = (sum(row.owned for row in progress), sum(row.total for row in progress))
        groups: dict[str, list[Item]] = {name: [] for name in KINDS} if filters.kind is None else {filters.kind: []}
        for item in selected:
            groups.setdefault(self.kind(item.asset_id, item.asset_type), []).append(item)
        if filters.description:
            groups = {kind: entries for kind, entries in groups.items() if entries}
        images = []
        for kind, entries in groups.items():
            cards = [
                AtlasItem(card.name, card.rarity, card.image, item.quantity)
                for item, card in zip(entries, await self.item_cards(entries, lazy=True), strict=True)
            ]
            cards.sort(
                key=lambda card: (ATLAS_RARITIES.index(card.rarity) if card.rarity in ATLAS_RARITIES else 4, card.name)
            )
            rarity_groups = [
                AtlasGroup(
                    rarity,
                    selection,
                    len(selection),
                    len(self.catalog_group(kind, rarity)),
                )
                for rarity in ATLAS_RARITIES
                if (selection := [card for card in cards if card.rarity == rarity])
            ]
            duplicates = sum(card.quantity - 1 for card in cards)
            note = f"共 {len(cards)} 项    重复 {duplicates} 项"
            if filters.description:
                note = f"筛选：{filters.description}    {note}"
            images.append(
                await self.draw_atlas(
                    header,
                    f"背包 · {KINDS.get(kind, kind or '其他')}",
                    rarity_groups,
                    progress,
                    note,
                )
            )
        return images

    async def redeemed(self, account: Account, items: list[Item], event: Event) -> list[bytes]:
        info = UserInfo.model_validate_json(account.info)
        info_header = await self.header(account, info.remaining, event)
        cards = await self.item_cards(items)
        for card, item in zip(cards, items, strict=True):
            card.quantity = item.quantity
        return [
            await self.draw(
                info_header, "兑换获得", cards[offset : offset + RESULT_PAGE_SIZE], "兑换前后背包增加的物品"
            )
            for offset in range(0, len(cards), RESULT_PAGE_SIZE)
        ]

    async def chest_statistics(
        self, account: Account, event: Event, period: StatisticsPeriod, stats: ChestStatistics, first_at: float | None
    ) -> bytes:
        info_header = await self.header(account, None, event)
        top = [
            (self.item_name(item.asset_id, item.asset_name), str(item.rarity_type).strip().lower(), count)
            for item, count in stats.top
        ]
        coverage = "仅统计本插件记录"
        if first_at is not None:
            coverage += f" · 记录始于 {datetime.fromtimestamp(first_at, CHINA):%Y.%m.%d}"
        return await render_in_thread(render_statistics, info_header, period.caption(first_at), stats, top, coverage)

    async def draw_atlas(
        self, header: Header, title: str, groups: list[AtlasGroup], progress: list[Progress], note: str = ""
    ) -> bytes:
        return await render_in_thread(render_atlas_card, header, title, groups, progress, note)

    async def atlas(self, account: Account, items: list[Item], event: Event, kind: str | None = None) -> list[bytes]:
        quantities = {str(item.asset_id): item.quantity for item in items}
        progress = self.progress(items)
        info = UserInfo.model_validate_json(account.info)
        header = await self.header(account, info.remaining, event)
        header.available = None
        header.collected = (sum(row.owned for row in progress), sum(row.total for row in progress))
        kinds = list(KINDS) if kind is None else [kind]
        return [await self.atlas_kind(header, quantities, progress, name) for name in kinds]

    async def atlas_kind(
        self, header: Header, quantities: dict[str, int], progress: list[Progress], kind: str
    ) -> bytes:
        entries = self.catalog_group(kind)
        loaded = await self.item_cards(
            [Item(asset_id=entry.asset, asset_type=kind, rarity=entry.rarity) for entry in entries], lazy=True
        )
        groups = []
        for rarity in ATLAS_RARITIES:
            cards = [
                AtlasItem(card.name, rarity, card.image, quantities.get(str(entry.asset), 0))
                for entry, card in zip(entries, loaded, strict=True)
                if card.rarity == rarity
            ]
            if cards:
                groups.append(AtlasGroup(rarity, cards, sum(card.quantity > 0 for card in cards), len(cards)))
        return await self.draw_atlas(header, f"图鉴 · {KINDS[kind]}", groups, progress)

    async def synthesis(
        self, account: Account, results: list[SynthesisResult], items: list[Item] | None, event: Event, note: str
    ) -> list[bytes]:
        info = UserInfo.model_validate_json(account.info)
        header = await self.header(account, info.remaining, event)
        cards = await self.item_cards(
            [
                Item(asset_id=r.asset_id, asset_name=r.asset_name, asset_type=r.asset_type, rarity=r.rarity_type)
                for r in results
            ]
        )
        pages = (len(cards) + 39) // 40
        progress = self.progress(items) if items is not None else None
        return [
            await self.draw(
                header,
                "合成结果",
                cards[i * 40 : (i + 1) * 40],
                f"{note} · 第 {i + 1}/{pages} 页",
                progress,
            )
            for i in range(pages)
        ]
