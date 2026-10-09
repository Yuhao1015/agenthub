"""Cumulative paid-call gate. Unknown charges persist across restarts."""
from __future__ import annotations
import asyncio
import json
import math
import os
import threading
from .atomicio import atomic_write
from .executors import Executor, ExecResult
from .connection_store import file_lock


def board_budget_path(board_path):
    return str(board_path) + '.api-budget.json'


class PaidLedger:
    def __init__(self, limit_usd, spent_usd=0, unknown=False, *, path='', reset=False):
        self.path = path
        self.recorded_usd = 0.0
        self.pending = False
        self.lock = threading.Lock()
        if path and os.path.exists(path) and not reset:
            try:
                with open(path, encoding='utf8') as handle:
                    state = json.load(handle)
                spent_usd, unknown = state['spent_usd'], state['unknown']
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError('费用记录不可读，请核对账目后恢复') from None
        if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in (limit_usd, spent_usd)) or type(unknown) is not bool:
            raise ValueError('费用记录或预算无效')
        self.limit_usd, self.spent_usd, self.unknown = float(limit_usd), float(spent_usd), unknown

    def snapshot(self):
        return {'spent_usd': self.spent_usd, 'unknown': self.unknown or self.pending}

    def latest_snapshot(self):
        return PaidLedger(self.limit_usd, path=self.path).snapshot() if self.path and os.path.exists(self.path) else self.snapshot()

    def persist(self):
        if self.path:
            atomic_write(self.path, json.dumps(self.snapshot()), mode=0o600)

    def check(self):
        if self.unknown or self.pending:
            raise ValueError('之前的 API 费用未知；请核对服务商账目，配置价格后开启新的目标')
        if self.spent_usd >= self.limit_usd:
            raise ValueError('本次累计 API 费用已达到预算，已停止下一次付费请求')

    def reserve(self):
        # A follow-up creates a new Router; other processes may have spent
        # money since construction. Caller holds the scope's file lock.
        if self.path and os.path.exists(self.path):
            latest = PaidLedger(self.limit_usd, path=self.path)
            self.spent_usd, self.unknown = latest.spent_usd, latest.unknown
        self.check()
        self.pending = True
        self.persist()  # On crash, the disk snapshot remains uncertain.

    def record(self, result):
        self.pending = False
        usage = result.usage or {}
        if usage.get('request_sent') is False:
            self.persist()
            return
        cost = result.cost_usd
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            self.unknown = True
        else:
            self.spent_usd += cost
            self.recorded_usd += cost
        if usage.get('cost_status') not in ('reported', 'estimated'):
            self.unknown = True
        self.persist()


class BudgetedApiExecutor(Executor):
    name = 'api'
    def __init__(self, inner, ledger_provider):
        self.inner = inner.inner if isinstance(inner, BudgetedApiExecutor) else inner
        self.ledger_provider = ledger_provider

    async def run(self, task, context, on_event=None):
        ledger = self.ledger_provider()
        while not ledger.lock.acquire(blocking=False):
            await asyncio.sleep(.01)
        scope_lock = None
        try:
            try:
                if ledger.path:
                    # Do not block an asyncio loop while another executor owns
                    # this persisted scope. Each attempt is nonblocking.
                    while scope_lock is None:
                        candidate = file_lock(ledger.path, timeout=0)
                        try:
                            candidate.__enter__()
                            scope_lock = candidate
                        except ValueError:
                            await asyncio.sleep(.02)
                ledger.reserve()
            except (ValueError, OSError) as exc:
                return ExecResult(ok=False, error=str(exc), error_kind='unavailable',
                                  usage={'cost_status':'reported','request_sent':False,
                                         'cost_complete':not ledger.snapshot()['unknown']})
            try:
                result = await self.inner.run(task, context, on_event=on_event)
            except asyncio.CancelledError:
                ledger.unknown = True
                ledger.pending = False
                ledger.persist()
                raise
            except Exception:
                result = ExecResult(ok=False, error='API 调用异常，费用待核对',
                                    error_kind='transient', usage={'cost_status':'unknown'})
            try:
                ledger.record(result)
            except OSError:
                # The reservation on disk already blocks future requests after restart.
                ledger.unknown = True
                result.ok = False
                result.error = '费用记录保存失败，请核对账目后重试'
                result.error_kind = 'unavailable'
            result.usage = {**(result.usage or {}), 'cost_complete':not ledger.unknown}
            return result
        finally:
            if scope_lock is not None:
                scope_lock.__exit__(None, None, None)
            ledger.lock.release()
