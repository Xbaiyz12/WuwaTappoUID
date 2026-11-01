"""插件专属表和 GS 数据库会话封装。"""

import time
from uuid import uuid4

from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.database import base_models
from gsuid_core.utils.database.base_models import with_read_session, with_session
from gsuid_core.utils.database.models import CoreUser, Subscribe
from sqlalchemy import Column, Connection, Index, Text, func, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel, col, select


def new_id() -> str:
    return uuid4().hex


class Account(SQLModel, table=True):
    __tablename__ = "wt_account"
    __table_args__ = {"extend_existing": True}
    id: str = Field(primary_key=True, max_length=64)
    cuid: str = Field(max_length=32)
    domestic: int
    token: str = Field(default="", sa_column=Column(Text, nullable=False), repr=False)
    info: str = Field(default="", sa_column=Column(Text, nullable=False))
    verified_at: float = 0
    subscribed: bool = False


class Binding(SQLModel, table=True):
    __tablename__ = "wt_binding"
    __table_args__ = {"extend_existing": True}
    id: str = Field(primary_key=True, max_length=64)
    platform: str = Field(max_length=64)
    user_id: str = Field(max_length=128)
    account_id: str = Field(index=True, max_length=64)


class AccountLink(SQLModel, table=True):
    __tablename__ = "wt_account_link"
    __table_args__ = {"extend_existing": True}
    owner_id: str = Field(primary_key=True, max_length=64)
    account_id: str = Field(primary_key=True, max_length=64)


class ChestTask(SQLModel, table=True):
    __tablename__ = "wt_chest_task"
    __table_args__ = {"extend_existing": True}
    account_id: str = Field(primary_key=True, max_length=64)
    run_id: str = Field(max_length=32)
    owner_id: str = Field(max_length=64)
    subscription_id: int
    status: str = Field(default="active", max_length=16)
    pause_reason: str = Field(default="", max_length=32)
    started_at: float
    expires_at: float
    next_check: float
    reminded: bool = False
    successes: int = 0
    failures: int = 0
    paused_at: float | None = None

    @property
    def timer_paused(self) -> bool:
        return self.status == "paused" and self.paused_at is not None

    def remaining(self, now: float | None = None) -> float:
        clock = self.paused_at if self.timer_paused else time.time() if now is None else now
        return self.expires_at - clock


class ChestRecord(SQLModel, table=True):
    __tablename__ = "wt_chest_record"
    __table_args__ = (
        Index("ix_wt_chest_record_pending", "state", "notice_id", "account_id"),
        Index("ix_wt_chest_record_recent", "account_id", "run_id", "side", "created_at"),
        {"extend_existing": True},
    )
    id: str = Field(default_factory=new_id, primary_key=True, max_length=32)
    account_id: str = Field(index=True, max_length=64)
    run_id: str = Field(index=True, max_length=32)
    subscription_id: int
    side: str = Field(max_length=8)
    created_at: float = Field(default_factory=time.time)
    state: str = Field(default="sending", index=True, max_length=16)
    before_info: str = Field(sa_column=Column(Text, nullable=False))
    result: str = Field(default="", sa_column=Column(Text, nullable=False))
    supplement: str = Field(default="", sa_column=Column(Text, nullable=False))
    notice_id: str = Field(default="", max_length=32)


class Notice(SQLModel, table=True):
    __tablename__ = "wt_notice"
    __table_args__ = {"extend_existing": True}
    id: str = Field(default_factory=new_id, primary_key=True, max_length=32)
    subscription_id: int
    account_id: str = Field(index=True, max_length=64)
    created_at: float = Field(default_factory=time.time)
    text: str = Field(sa_column=Column(Text, nullable=False))
    # 发送时按这两项取物品图；为空就是纯文字通知。
    asset_id: str = Field(default="", max_length=32)
    rarity: str = Field(default="", max_length=32)
    submitted: bool = Field(default=False, index=True)
    submitted_at: float | None = None
    attempts: int = 0
    last_error: str = Field(default="", max_length=160)
    record_ids: str = Field(default="", sa_column=Column(Text, nullable=False))
    available_clicks: int | None = None


class SynthesisRecord(SQLModel, table=True):
    __tablename__ = "wt_synthesis_record"
    __table_args__ = {"extend_existing": True}
    id: str = Field(default_factory=new_id, primary_key=True, max_length=32)
    run_id: str = Field(index=True, max_length=32)
    account_id: str = Field(index=True, max_length=64)
    created_at: float = Field(default_factory=time.time)
    mode: str = Field(max_length=16)
    material_ids: str = Field(sa_column=Column(Text, nullable=False))
    before_quantity: int
    after_quantity: int | None = None
    state: str = Field(default="sending", max_length=16)
    result: str = Field(default="", sa_column=Column(Text, nullable=False))
    error: str = Field(default="", max_length=160)


# create_all 不给旧表补列；只在首次新增 subscribed 时开启现有账号的订阅。
ADDED_COLUMNS = {
    "wt_account": {"subscribed": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "wt_chest_task": {"paused_at": "FLOAT"},
    "wt_chest_record": {"notice_id": "VARCHAR(32) NOT NULL DEFAULT ''"},
    "wt_notice": {
        "asset_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        "rarity": "VARCHAR(32) NOT NULL DEFAULT ''",
        "record_ids": "TEXT NOT NULL DEFAULT ''",
        "available_clicks": "BIGINT",
    },
}


def _add_missing_columns(connection: Connection) -> list[str]:
    added = []
    for table, columns in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"))
                added.append(f"{table}.{name}")
    if "wt_account.subscribed" in added:
        connection.execute(text("UPDATE wt_account SET subscribed = TRUE"))
    if "wt_chest_record.notice_id" in added:
        # 旧成功结果已有逐箱通知，不能在升级后再次汇总推送。
        connection.execute(text("UPDATE wt_chest_record SET notice_id = 'legacy' WHERE state = 'success'"))
    if "wt_chest_task.paused_at" in added:
        now = time.time()
        connection.execute(
            text(
                "UPDATE wt_chest_task SET paused_at=:now WHERE status='paused' "
                + "AND pause_reason='manual' AND expires_at>:now"
            ),
            {"now": now},
        )
    if "wt_account.subscribed" in added:
        # 只在旧版首次迁移时恢复关联，后续重启不能重新添加用户已删除的账号。
        links = set(connection.execute(text("SELECT owner_id, account_id FROM wt_account_link")).all())
        bindings = connection.execute(text("SELECT id, account_id FROM wt_binding")).all()
        owners = {row[0] for row in bindings}
        previous = connection.execute(text("SELECT owner_id, account_id FROM wt_chest_task")).all()
        for owner_id, account_id in set(bindings + previous) - links:
            if owner_id in owners:
                connection.execute(
                    text("INSERT INTO wt_account_link (owner_id, account_id) VALUES (:owner, :account)"),
                    {"owner": owner_id, "account": account_id},
                )
    # create_all 跳过已有表时也跳过新增索引，升级时单独创建。
    for index in SQLModel.metadata.tables["wt_chest_record"].indexes:
        index.create(connection, checkfirst=True)
    return added


async def migrate_notice_columns() -> None:
    async with base_models.engine.begin() as connection:
        added = await connection.run_sync(_add_missing_columns)
    if added:
        logger.info(f"[WuwaTappoUID] 数据库迁移完成，新增字段：{'、'.join(added)}")


class Store:
    @classmethod
    @with_read_session
    async def statistics(cls, session: AsyncSession) -> tuple[int, int, int]:
        bound_people = await session.scalar(select(func.count()).select_from(Binding))
        task_people = await session.scalar(
            select(func.count(col(ChestTask.owner_id).distinct())).where(
                col(ChestTask.status).in_(["active", "paused"]),
                col(ChestTask.expires_at) > func.coalesce(col(ChestTask.paused_at), time.time()),
            )
        )
        total_chests = await session.scalar(
            select(func.count()).select_from(ChestRecord).where(col(ChestRecord.state) == "success")
        )
        return bound_people, task_people, total_chests

    @classmethod
    @with_session
    async def delete_link(cls, session: AsyncSession, owner_id: str, account_id: str, replacement: str | None) -> None:
        link = await session.get(AccountLink, (owner_id, account_id))
        if link is not None:
            await session.delete(link)
        binding = await session.get(Binding, owner_id)
        if binding is not None and binding.account_id == account_id:
            if replacement is None:
                await session.delete(binding)
            else:
                binding.account_id = replacement

    @classmethod
    @with_read_session
    async def sender_profile(cls, session: AsyncSession, event: Event) -> tuple[str, str]:
        result = await session.execute(
            select(CoreUser)
            .where(col(CoreUser.user_id) == event.user_id, col(CoreUser.bot_id) == event.bot_id)
            .order_by((col(CoreUser.group_id) == event.group_id).desc(), col(CoreUser.id).desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return "", ""
        name = row.user_name if row.user_name not in (None, "1") else ""
        avatar = row.user_icon if row.user_icon not in (None, "1") else ""
        return name, avatar

    @classmethod
    @with_read_session
    async def result_accounts(cls, session: AsyncSession) -> list[str]:
        result = await session.execute(
            select(col(ChestRecord.account_id))
            .where(col(ChestRecord.state) == "success", col(ChestRecord.notice_id) == "")
            .distinct()
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def linked_accounts(cls, session: AsyncSession, owner_id: str) -> list[Account]:
        result = await session.execute(
            select(Account)
            .join(AccountLink, col(AccountLink.account_id) == col(Account.id))
            .where(col(AccountLink.owner_id) == owner_id)
            .order_by(col(Account.id))
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def pending_results(cls, session: AsyncSession, account_id: str) -> list[ChestRecord]:
        result = await session.execute(
            select(ChestRecord)
            .where(
                col(ChestRecord.account_id) == account_id,
                col(ChestRecord.state) == "success",
                col(ChestRecord.notice_id) == "",
            )
            .order_by(col(ChestRecord.created_at), col(ChestRecord.id))
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def records(cls, session: AsyncSession, ids: list[str]) -> list[ChestRecord]:
        result = await session.execute(select(ChestRecord).where(col(ChestRecord.id).in_(ids)))
        rows = {row.id: row for row in result.scalars().all()}
        return [rows[key] for key in ids]

    @classmethod
    @with_read_session
    async def binding(cls, session: AsyncSession, identity: str) -> Binding | None:
        return await session.get(Binding, identity)

    @classmethod
    @with_read_session
    async def account(cls, session: AsyncSession, account_id: str) -> Account | None:
        return await session.get(Account, account_id)

    @classmethod
    @with_read_session
    async def task(cls, session: AsyncSession, account_id: str) -> ChestTask | None:
        return await session.get(ChestTask, account_id)

    @classmethod
    @with_read_session
    async def active_tasks(cls, session: AsyncSession) -> list[ChestTask]:
        result = await session.execute(select(ChestTask).where(col(ChestTask.status).in_(["active", "paused"])))
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def unresolved_accounts(cls, session: AsyncSession) -> list[str]:
        result = await session.execute(
            select(col(ChestRecord.account_id)).where(col(ChestRecord.state).in_(["sending", "uncertain"])).distinct()
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def unresolved(cls, session: AsyncSession, account_id: str) -> list[ChestRecord]:
        result = await session.execute(
            select(ChestRecord)
            .where(col(ChestRecord.account_id) == account_id, col(ChestRecord.state).in_(["sending", "uncertain"]))
            .order_by(col(ChestRecord.created_at))
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def recent_chests(cls, session: AsyncSession, account_id: str, run_id: str, side: str) -> list[ChestRecord]:
        result = await session.execute(
            select(ChestRecord)
            .where(
                col(ChestRecord.account_id) == account_id,
                col(ChestRecord.run_id) == run_id,
                col(ChestRecord.side) == side,
            )
            .order_by(col(ChestRecord.created_at).desc())
            .limit(4)
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def notices(cls, session: AsyncSession) -> list[Notice]:
        result = await session.execute(
            select(Notice).where(col(Notice.submitted).is_(False)).order_by(col(Notice.created_at), col(Notice.id))
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def subscription(cls, session: AsyncSession, subscription_id: int) -> Subscribe | None:
        return await session.get(Subscribe, subscription_id)

    @classmethod
    @with_session
    async def save(cls, session: AsyncSession, *rows: SQLModel) -> None:
        # 一次调用内保存任务、开箱结果和待发通知，同一事务提交。
        for row in rows:
            await session.merge(row)
