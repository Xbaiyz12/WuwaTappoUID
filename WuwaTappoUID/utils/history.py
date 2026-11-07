"""分批归档与清理；累计入账和删除原始流水始终在同一写事务内。"""

from datetime import datetime

from gsuid_core.utils.database.base_models import with_session
from gsuid_core.utils.database.models import Subscribe
from sqlalchemy import and_, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import col, select

from .database import ChestRecord, ChestSummary, ChestTask, LoginSession, Notice, SynthesisRecord
from .statistics import CHINA, read_result

BATCH_SIZE = 100


class HistoryArchive:
    @classmethod
    @with_session
    async def archive_batch(cls, session: AsyncSession, cutoff: float, daily_cutoff: float) -> int:
        current_run = (
            select(ChestTask.account_id)
            .where(
                ChestTask.account_id == ChestRecord.account_id,
                ChestTask.run_id == ChestRecord.run_id,
                col(ChestTask.status).in_(["active", "paused"]),
            )
            .correlate(ChestRecord)
            .exists()
        )
        later = aliased(ChestRecord)
        # 与recent_chests的排序一致：只归档当前有效任务每侧最近4条以外的流水。
        four_later = (
            select(later.id)
            .where(
                later.account_id == ChestRecord.account_id,
                later.run_id == ChestRecord.run_id,
                later.side == ChestRecord.side,
                or_(
                    later.created_at > ChestRecord.created_at,
                    and_(later.created_at == ChestRecord.created_at, later.id > ChestRecord.id),
                ),
            )
            .offset(3)
            .limit(1)
            .correlate(ChestRecord)
            .exists()
        )
        delivered = (
            select(Notice.id)
            .where(Notice.id == ChestRecord.notice_id, col(Notice.submitted).is_(True))
            .correlate(ChestRecord)
            .exists()
        )
        completed_notice = or_(ChestRecord.notice_id == "legacy", delivered)
        eligible = or_(
            and_(ChestRecord.state == "success", completed_notice),
            and_(
                col(ChestRecord.state).in_(["rejected", "not_sent", "not_applied", "retry_exhausted"]),
                or_(ChestRecord.notice_id == "", completed_notice),
            ),
        )
        result = await session.execute(
            select(ChestRecord)
            .where(ChestRecord.created_at < cutoff, eligible, or_(~current_run, four_later))
            .order_by(col(ChestRecord.created_at), col(ChestRecord.id))
            .limit(BATCH_SIZE)
        )
        records = list(result.scalars().all())
        summaries: dict[tuple[str, int, str, str], ChestSummary] = {}
        for record in records:
            if record.state != "success":
                continue
            item = read_result(record.result) if record.result else None
            asset_id = str(item.asset_id) if item is not None else ""
            rarity = str(item.rarity_type) if item is not None else ""
            clicks = item.click_consume if item is not None else 0
            day = int(
                datetime.fromtimestamp(record.created_at, CHINA)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            periods = (0, day) if day >= daily_cutoff else (0,)
            for period in periods:
                key = record.account_id, period, asset_id, rarity
                if key not in summaries:
                    saved = await session.get(ChestSummary, key)
                    summaries[key] = (
                        saved
                        if saved is not None
                        else ChestSummary(
                            account_id=record.account_id,
                            day=period,
                            asset_id=asset_id,
                            rarity=rarity,
                            first_at=record.created_at,
                        )
                    )
                summary = summaries[key]
                summary.quantity += 1
                summary.clicks += clicks
                summary.first_at = min(summary.first_at, record.created_at)
                summary.result = record.result
                session.add(summary)
        # 写入汇总后再删除这批明细；失败或中断时整个事务回滚。
        await session.flush()
        if records:
            await session.execute(
                delete(ChestRecord)
                .where(col(ChestRecord.id).in_([record.id for record in records]))
                .execution_options(synchronize_session=False)
            )
        return len(records)

    @classmethod
    @with_session
    async def cleanup_batch(cls, session: AsyncSession, cutoff: float, daily_cutoff: float) -> int:
        referenced = select(ChestRecord.id).where(ChestRecord.notice_id == Notice.id).correlate(Notice).exists()
        result = await session.execute(
            select(Notice)
            .where(col(Notice.submitted).is_(True), Notice.submitted_at < cutoff, ~referenced)
            .order_by(col(Notice.submitted_at), col(Notice.id))
            .limit(BATCH_SIZE)
        )
        notices = list(result.scalars().all())
        for notice in notices:
            await session.delete(notice)
        result = await session.execute(
            select(SynthesisRecord)
            .where(SynthesisRecord.created_at < cutoff, col(SynthesisRecord.state).in_(["success", "rejected"]))
            .order_by(col(SynthesisRecord.created_at), col(SynthesisRecord.id))
            .limit(BATCH_SIZE - len(notices))
        )
        synthesis = list(result.scalars().all())
        for record in synthesis:
            await session.delete(record)
        result = await session.execute(
            select(ChestSummary)
            .where(ChestSummary.day > 0, ChestSummary.day < daily_cutoff)
            .order_by(col(ChestSummary.day))
            .limit(BATCH_SIZE - len(notices) - len(synthesis))
        )
        daily = list(result.scalars().all())
        for summary in daily:
            await session.delete(summary)
        await session.flush()
        # 仅回收这批已完成通知遗留的本插件路由，不触碰新建中或仍被引用的订阅。
        for subscription_id in {notice.subscription_id for notice in notices}:
            sub = await session.get(Subscribe, subscription_id)
            if sub is None or sub.task_name != "WuwaTappo宝箱":
                continue
            references = or_(
                *(
                    select(model.subscription_id).where(model.subscription_id == subscription_id).exists()
                    for model in (ChestTask, ChestRecord, Notice, LoginSession)
                )
            )
            if not await session.scalar(select(references)):
                await session.delete(sub)
        return len(notices) + len(synthesis) + len(daily)
