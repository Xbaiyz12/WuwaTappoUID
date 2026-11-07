"""插件专属表和 GS 数据库会话封装。"""

import time
from uuid import uuid4

from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.database import base_models
from gsuid_core.utils.database.base_models import with_read_session, with_session
from gsuid_core.utils.database.models import CoreUser, Subscribe
from gsuid_core.utils.database.startup import exec_list
from sqlalchemy import Column, Connection, Index, Text, func, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel, col, select

from .schema import DatabaseSchemaError


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
    token_expires_at: float = 0
    token_renew_after: float = 0
    token_state: str = Field(default="ready", max_length=32)
    sdk_token: str = Field(default="", sa_column=Column(Text, nullable=False), repr=False)
    sdk_openid: str = Field(default="", max_length=128)
    sdk_username: str = Field(default="", max_length=128)
    sdk_expires_at: float = 0
    sdk_owner_id: str = Field(default="", max_length=64)
    subscribed: bool = False

    def clear_sdk(self) -> None:
        self.sdk_token = self.sdk_openid = self.sdk_username = self.sdk_owner_id = ""
        self.sdk_expires_at = 0

    def can_recover(self, owner_id: str) -> bool:
        return bool(
            self.domestic == 1
            and self.sdk_owner_id == owner_id
            and self.sdk_token
            and self.sdk_openid
            and self.sdk_username
            and self.sdk_expires_at > time.time()
        )


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


class LoginSession(SQLModel, table=True):
    __tablename__ = "wt_login_session"
    __table_args__ = {"extend_existing": True}
    id: str = Field(primary_key=True, max_length=64)
    owner_id: str = Field(index=True, max_length=64)
    platform: str = Field(max_length=64)
    user_id: str = Field(max_length=128)
    subscription_id: int
    expires_at: float
    status: str = Field(default="pending", max_length=16)
    account_id: str = Field(default="", max_length=64)
    token_digest: str = Field(default="", max_length=64, repr=False)
    message: str = Field(default="", sa_column=Column(Text, nullable=False))
    result: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    submission: str = Field(default="", sa_column=Column(Text, nullable=False), repr=False)

    external_url: str = Field(default="", sa_column=Column(Text, nullable=False))
    external_state: str = Field(default="", max_length=128, repr=False)
    external_poll_token: str = Field(default="", max_length=128, repr=False)
    external_synced: bool = False
    external_result_pending: bool = False


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
        Index("ix_wt_chest_record_history", "account_id", "created_at"),
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


# 按 GS 官方流程注册补列；旧数据使用默认值，不要求回填历史账号关联。
ADDED_COLUMNS = {
    "wt_login_session": {
        "submission": "TEXT NOT NULL DEFAULT ''",
        "external_url": "TEXT NOT NULL DEFAULT ''",
        "external_state": "VARCHAR(128) NOT NULL DEFAULT ''",
        "external_poll_token": "VARCHAR(128) NOT NULL DEFAULT ''",
        "external_synced": "BOOLEAN NOT NULL DEFAULT FALSE",
        "external_result_pending": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "wt_account": {
        "sdk_token": "TEXT NOT NULL DEFAULT ''",
        "sdk_openid": "VARCHAR(128) NOT NULL DEFAULT ''",
        "sdk_username": "VARCHAR(128) NOT NULL DEFAULT ''",
        "sdk_expires_at": "FLOAT NOT NULL DEFAULT 0",
        "sdk_owner_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        "subscribed": "BOOLEAN NOT NULL DEFAULT FALSE",
        "token_expires_at": "FLOAT NOT NULL DEFAULT 0",
        "token_renew_after": "FLOAT NOT NULL DEFAULT 0",
        "token_state": "VARCHAR(32) NOT NULL DEFAULT 'ready'",
    },
    "wt_chest_task": {"paused_at": "FLOAT DEFAULT NULL"},
    # 已有记录不重新推送；新记录由模型显式写入空 notice_id。
    "wt_chest_record": {"notice_id": "VARCHAR(32) NOT NULL DEFAULT 'legacy'"},
    "wt_notice": {
        "asset_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        "rarity": "VARCHAR(32) NOT NULL DEFAULT ''",
        "record_ids": "TEXT NOT NULL DEFAULT ''",
        "available_clicks": "BIGINT DEFAULT NULL",
    },
}
exec_list.extend(
    f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
    for table, columns in ADDED_COLUMNS.items()
    for name, column_type in columns.items()
)
PLUGIN_TABLE_NAMES = (
    "wt_account",
    "wt_binding",
    "wt_account_link",
    "wt_login_session",
    "wt_chest_task",
    "wt_chest_record",
    "wt_notice",
    "wt_synthesis_record",
)


def _ensure_schema(connection: Connection) -> list[str]:
    tables = [SQLModel.metadata.tables[name] for name in PLUGIN_TABLE_NAMES]
    # 启动前钩子可能被其他插件的异常跳过，补建仅本插件的表。
    SQLModel.metadata.create_all(connection, tables=tables)
    changes = []
    for table, columns in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, column_type in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"))
                changes.append(f"新增 {table}.{name}")
    # 0.1.40 首装创建的旧列没有服务端默认值；删除废弃快照列，避免新账号插入触发非空约束。
    account_columns = {column["name"] for column in inspect(connection).get_columns("wt_account")}
    for name in ("role_info", "roles_updated_at"):
        if name in account_columns:
            connection.execute(text(f"ALTER TABLE wt_account DROP COLUMN {name}"))
            changes.append(f"删除 wt_account.{name}")
    missing = []
    inspector = inspect(connection)
    for table in tables:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in existing)
    if missing:
        raise DatabaseSchemaError(f"插件数据库缺少字段：{'、'.join(missing)}；后台任务不会启动。")
    # create_all 跳过已有表时也跳过新增索引，升级时单独创建。
    for index in SQLModel.metadata.tables["wt_chest_record"].indexes:
        index.create(connection, checkfirst=True)
    return changes


async def ensure_database_schema() -> None:
    async with base_models.engine.begin() as connection:
        changes = await connection.run_sync(_ensure_schema)
    if changes:
        logger.info(f"[WuwaTappoUID] 数据库迁移完成：{'、'.join(changes)}")
    logger.info("[WuwaTappoUID] 数据库表结构校验通过。")


class Store:
    @classmethod
    @with_read_session
    async def login_session(cls, session: AsyncSession, key: str) -> LoginSession | None:
        return await session.get(LoginSession, key)

    @classmethod
    @with_read_session
    async def open_logins(cls, session: AsyncSession, owner_id: str | None = None) -> list[LoginSession]:
        query = select(LoginSession).where(col(LoginSession.status).in_(["pending", "processing", "verifying"]))
        if owner_id is not None:
            query = query.where(col(LoginSession.owner_id) == owner_id)
        return list((await session.execute(query)).scalars().all())

    @classmethod
    @with_read_session
    async def external_logins(cls, session: AsyncSession) -> list[LoginSession]:
        query = select(LoginSession).where(
            col(LoginSession.external_url) != "", col(LoginSession.external_synced).is_(False)
        )
        return list((await session.execute(query)).scalars().all())

    @classmethod
    @with_read_session
    async def local_logins(cls, session: AsyncSession) -> list[LoginSession]:
        query = select(LoginSession).where(col(LoginSession.external_url) == "", col(LoginSession.submission) != "")
        return list((await session.execute(query)).scalars().all())

    @classmethod
    @with_session
    async def prepare_login_sessions(cls, session: AsyncSession, *, startup: bool = False) -> None:
        now = time.time()
        rows = (await session.execute(select(LoginSession))).scalars().all()
        for row in rows:
            if row.expires_at < now - 24 * 3600:
                pending = await session.scalar(
                    select(func.count())
                    .select_from(Notice)
                    .where(col(Notice.subscription_id) == row.subscription_id, col(Notice.submitted).is_(False))
                )
                if pending:
                    continue
                subscription = await session.get(Subscribe, row.subscription_id)
                if subscription is not None and subscription.task_name == "WuwaTappo登录":
                    await session.delete(subscription)
                await session.delete(row)
            elif row.expires_at <= now and row.status in ("pending", "verifying"):
                row.status = "expired"
                row.submission = ""
                row.external_result_pending = False
                row.message = "登录链接已过期，请重新发送 wt登录。"
                session.add(row)
                session.add(Notice(subscription_id=row.subscription_id, account_id=row.account_id, text=row.message))
            elif startup and row.status == "processing":
                row.status = "failed"
                row.submission = ""
                row.message = "登录处理已中断，请重新发送 wt登录。"
                session.add(row)
                session.add(Notice(subscription_id=row.subscription_id, account_id=row.account_id, text=row.message))
        if startup:
            accounts = (
                (await session.execute(select(Account).where(col(Account.token_state) == "login_unconfirmed")))
                .scalars()
                .all()
            )
            for account in accounts:
                account.token_state = "pending" if account.token else "expired"
                session.add(account)

    @classmethod
    @with_session
    async def reset_notice_attempts(cls, session: AsyncSession, account_id: str) -> None:
        result = await session.execute(
            select(Notice).where(col(Notice.account_id) == account_id, col(Notice.submitted).is_(False))
        )
        for notice in result.scalars().all():
            notice.attempts = 0

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
        account = await session.get(Account, account_id)
        if account is not None and account.sdk_owner_id == owner_id:
            account.clear_sdk()
        link = await session.get(AccountLink, (owner_id, account_id))
        if link is not None:
            await session.delete(link)
        binding = await session.get(Binding, owner_id)
        if binding is not None and binding.account_id == account_id:
            if replacement is None:
                await session.delete(binding)
            else:
                binding.account_id = replacement
        await session.flush()
        links = await session.scalar(
            select(func.count()).select_from(AccountLink).where(col(AccountLink.account_id) == account_id)
        )
        current_bindings = await session.scalar(
            select(func.count()).select_from(Binding).where(col(Binding.account_id) == account_id)
        )
        if not links and not current_bindings:
            account = await session.get(Account, account_id)
            if account is not None:
                account.token = ""
                account.clear_sdk()
                account.token_state = "expired"
                account.token_expires_at = account.token_renew_after = 0

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
    @with_session
    async def linked_accounts(cls, session: AsyncSession, owner_id: str) -> list[Account]:
        # 旧版本只保存当前绑定；补齐这一条现有关系，不回填已删除账号或历史任务。
        binding = await session.get(Binding, owner_id)
        if binding is not None and await session.get(Account, binding.account_id) is not None:
            current_link = await session.get(AccountLink, (owner_id, binding.account_id))
            if current_link is None:
                session.add(AccountLink(owner_id=owner_id, account_id=binding.account_id))
                await session.flush()
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
    async def chest_history(
        cls, session: AsyncSession, account_id: str, since: float, until: float
    ) -> tuple[float | None, list[tuple[str, int]]]:
        conditions = (col(ChestRecord.account_id) == account_id, col(ChestRecord.state) == "success")
        first_at = await session.scalar(select(func.min(col(ChestRecord.created_at))).where(*conditions))
        result = await session.execute(
            # 相同物品结果先在数据库计数，避免全部历史逐条加载和解析。
            select(col(ChestRecord.result), func.count())
            .where(*conditions, col(ChestRecord.created_at) >= since, col(ChestRecord.created_at) <= until)
            .group_by(col(ChestRecord.result))
            .order_by(func.min(col(ChestRecord.created_at)), col(ChestRecord.result))
        )
        return first_at, [(payload, count) for payload, count in result.all()]

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
