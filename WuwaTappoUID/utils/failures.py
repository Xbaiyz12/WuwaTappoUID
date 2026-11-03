"""持久化连续失败计数和主人告警；告警失败不会递归生成告警。"""

import asyncio
import time
from pathlib import Path
from uuid import uuid4

from gsuid_core.config import core_config
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.subscribe import gs_subscribe
from pydantic import BaseModel, Field

from .formatting import date_text, mask_cuid
from .settings import retry_limit


class MasterDeliveryError(Exception):
    """可安全记录的 GS 主人告警路由或提交错误。"""


class Failure(BaseModel):
    attempts: int = 0
    blocked: bool = False
    retry_after: float = 0
    account_id: str = ""


class MasterAlert(BaseModel):
    text: str
    key: str = ""
    account_id: str = ""
    resolved: bool = False
    attempts: int = 0
    retry_after: float = 0
    delivered: list[str] = Field(default_factory=list)


class FailureState(BaseModel):
    failures: dict[str, Failure] = Field(default_factory=dict)
    alerts: dict[str, MasterAlert] = Field(default_factory=dict)


class Failures:
    def __init__(self, root: Path) -> None:
        self.path = root / "failure_state.json"
        self.state = FailureState()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    async def load(self) -> None:
        if self.path.exists():
            self.state = FailureState.model_validate_json(await asyncio.to_thread(self.path.read_bytes))
        else:
            async with self._lock:
                await self._save()

    def allows(self, key: str) -> bool:
        failure = self.state.failures.get(key)
        return failure is None or not failure.blocked and failure.retry_after <= time.time()

    def attempts(self, key: str) -> int:
        failure = self.state.failures.get(key)
        return failure.attempts if failure is not None else 0

    def blocked(self, key: str) -> bool:
        failure = self.state.failures.get(key)
        return failure is not None and failure.blocked

    def retry_after(self, key: str) -> float:
        failure = self.state.failures.get(key)
        return failure.retry_after if failure is not None else 0

    @staticmethod
    def delay(attempts: int) -> int:
        return (60, 300, 900)[min(max(attempts - 1, 0), 2)]

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    async def _save(self) -> None:
        await asyncio.to_thread(self._write, self.path, self.state.model_dump_json().encode())

    async def fail(
        self,
        key: str,
        phase: str,
        detail: str,
        account_id: str = "",
        *,
        limit: int | None = None,
        delay: int | None = None,
    ) -> Failure:
        async with self._lock:
            failure = self.state.failures.setdefault(key, Failure(account_id=account_id))
            if failure.blocked:
                return failure.model_copy()
            maximum = retry_limit() if limit is None else limit
            failure.attempts += 1
            failure.blocked = failure.attempts >= maximum
            failure.retry_after = time.time() + (self.delay(failure.attempts) if delay is None else delay)
            target = f"CUID {mask_cuid(account_id.split(':', 1)[-1])}" if account_id else "插件"
            summary = f"{target} · {phase} · 连续失败 {failure.attempts}/{maximum}"
            if failure.blocked:
                text = (
                    f"WT 故障告警\n发生时间：{date_text(time.time())}\n{summary}\n{detail}"
                    "\n已停止对应的自动重试，数据和流水保留。"
                    "\n账号问题处理后可用 wt恢复 重置计数；数据库或初始化问题请修复后重启。"
                )
                self.state.alerts[uuid4().hex] = MasterAlert(text=text, key=key, account_id=account_id)
                logger.error(f"[WuwaTappoUID] {summary}，已停止自动重试并生成主人告警。{detail}")
            elif failure.attempts == 1:
                logger.warning(f"[WuwaTappoUID] {summary}。{detail}")
            else:
                logger.warning(f"[WuwaTappoUID] {summary}，本轮故障持续。")
            await self._save()
            return failure.model_copy()

    async def success(self, key: str) -> None:
        async with self._lock:
            if key in self.state.failures:
                del self.state.failures[key]
                for alert in self.state.alerts.values():
                    if alert.key == key:
                        alert.resolved = True
                await self._save()
                logger.info(f"[WuwaTappoUID] 阶段 {key.split(':', 1)[0]} 已恢复，连续失败计数已清零。")

    async def reset_account(self, account_id: str) -> None:
        async with self._lock:
            keys = [key for key, value in self.state.failures.items() if value.account_id == account_id]
            for key in keys:
                del self.state.failures[key]
            for alert in self.state.alerts.values():
                if alert.account_id == account_id:
                    alert.resolved = True
                    alert.attempts = 0
                    alert.retry_after = 0
            if keys or any(alert.account_id == account_id for alert in self.state.alerts.values()):
                await self._save()

    async def reset_commands(self, owner: str) -> None:
        prefix = f"command:{owner}:"
        async with self._lock:
            keys = [key for key in self.state.failures if key.startswith(prefix)]
            for key in keys:
                del self.state.failures[key]
            for alert in self.state.alerts.values():
                if alert.key.startswith(prefix):
                    alert.resolved = True
            if keys or any(alert.key.startswith(prefix) for alert in self.state.alerts.values()):
                await self._save()

    async def flush_alerts(self) -> None:
        if not gss.active_bot:
            return
        async with self._send_lock:
            for alert_id, alert in list(self.state.alerts.items()):
                maximum = retry_limit()
                if alert.attempts >= maximum or alert.retry_after > time.time():
                    continue
                async with self._lock:
                    alert.attempts += 1
                    alert.retry_after = time.time() + self.delay(alert.attempts)
                    # 请求前保存次数；重启不能重新获得发送预算。
                    await self._save()
                try:
                    async with asyncio.timeout(15):
                        masters = {str(value) for value in core_config.get_config("masters")}
                        subscriptions = await gs_subscribe.get_subscribe("主人用户")
                        targets = {
                            f"{sub.bot_id}:{sub.user_id}": sub for sub in subscriptions if str(sub.user_id) in masters
                        }
                        if not targets:
                            raise MasterDeliveryError("没有可用的主人订阅，请主人向 Bot 发送一条命令建立 GS 主人订阅")
                        for target, sub in targets.items():
                            if target in alert.delivered:
                                continue
                            text = alert.text + ("\n该故障已执行恢复，本条为此前故障记录。" if alert.resolved else "")
                            result = await sub.send(text, force_direct=True)
                            if result != 0:
                                raise MasterDeliveryError("GS 未提交主人告警，主人对应 Bot 可能离线")
                            async with self._lock:
                                alert.delivered.append(target)
                                await self._save()
                except Exception as error:
                    if alert.attempts == 1 or alert.attempts >= maximum:
                        detail = str(error) if isinstance(error, MasterDeliveryError) else type(error).__name__
                        logger.warning(
                            f"[WuwaTappoUID] 主人告警投递失败 {alert.attempts}/{maximum}：{detail}。"
                            + ("已停止重试，告警记录保留。" if alert.attempts >= maximum else "稍后重试。")
                        )
                    continue
                async with self._lock:
                    del self.state.alerts[alert_id]
                    await self._save()
                logger.info("[WuwaTappoUID] 主人告警已提交。")
