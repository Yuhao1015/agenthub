"""Router: task type -> ordered list of executors (fallback chain)."""
from __future__ import annotations

import yaml
import copy

from .executors import Executor, build_executor, _dsh_cmd
from .agent_options import agent_group, suggested_efforts, dsh_effort_supported, normalize_options, selection_capabilities
from .models import Task, TaskType
from .local_agents import KINDS as LOCAL_KINDS
from .api_budget import PaidLedger, BudgetedApiExecutor

# Only these agent kinds are billed pay-as-you-go; claude_code runs on the
# subscription's included quota, codex reuses ChatGPT login, codebuddy runs
# on workbuddy's already-owned credit/free balance (tracked separately, never
# gated), and mock is free -- none count against max_usd_per_run.
PAID_API_KINDS = {"openrouter", "api"}

# 会不会写盘：写盘门、写锁、追问拦截共用这一个判断（change-safety-net.md §4）
WRITE_TOOLS = {"Edit", "Write", "Bash"}      # claude_code 的写盘工具
ALWAYS_WRITE_KINDS = {"codex", "dsh"}        # 全权 agent：工具权限对 Agent Hub 不透明，
                                             # 保守当写盘（宁可多问，不装作能判断）


def agent_can_write(kind: str, allowed_tools) -> bool:
    """codebuddy 不算：它的两条执行路径都带 --tools ""，2026-07-21 实测会关掉
    全部工具（hub/executors.py）。openrouter、mock 是纯文本补全，不碰盘。
    allowed_tools 可以是矩阵里的原始串（"Read, Edit"）或已拆好的列表。"""
    if kind in ALWAYS_WRITE_KINDS:
        return True
    if kind != "claude_code":
        return False
    if isinstance(allowed_tools, str):
        allowed_tools = allowed_tools.split(",")
    return any(t.strip() in WRITE_TOOLS for t in allowed_tools)


# writing/analysis/light are routed by difficulty tier instead of a fixed
# chain; planning/review/coding/manager/image keep fixed chains in routes:.
TIERED_TASK_TYPES = {TaskType.WRITING, TaskType.ANALYSIS, TaskType.LIGHT}
DIFFICULTY_TO_TIER = {"easy": "economy", "medium": "mid", "hard": "mid"}
DEFAULT_TIER = "mid"


class Router:
    def __init__(self, matrix_path: str, workdir: str = ".", mock_all: bool = False,
                 stream: bool = True):
        with open(matrix_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.budget = cfg.get("budget", {})
        self._local_policy = cfg.get('local_agent_policy')
        self._api_store = None
        self.paid_ledger = PaidLedger(self.budget.get('max_usd_per_run', 2.0))
        self._specs = copy.deepcopy(cfg['agents'])
        self._workdir, self._stream, self._mock_all = workdir, stream, mock_all
        self._executors: dict[str, Executor] = {}
        self._animals: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._specialties: dict[str, str | None] = {}
        self._blurbs: dict[str, str] = {}
        self._declared_kinds: dict[str, str] = {}
        self._declared_tools: dict[str, str] = {}
        self._circuit_open: set[str] = set()  # agents down for this run
        for name, spec in cfg["agents"].items():
            self._animals[name] = spec.get("animal", "cat")
            self._specialties[name] = spec.get("specialty")
            self._blurbs[name] = str(spec.get("blurb") or "")
            # 矩阵里**声明**的 kind，只给展示用（kind() -> 前端额度徽章）。
            # 跟 _kinds 分开是有意的：_kinds 是 mock_all 替换后的**生效**
            # kind，claude_code_agents()/codebuddy_agents()/is_paid_api()
            # 都读它 —— 把它改成声明值会让 --mock 跑起来去做预检、并改变
            # paid_cost 记账，那是不必要的行为改动。
            self._declared_kinds[name] = spec["kind"]
            self._declared_tools[name] = str(spec.get("allowed_tools") or "")
            effective_spec = {"kind": "mock"} if mock_all else spec
            self._kinds[name] = effective_spec["kind"]
            self._executors[name] = build_executor(effective_spec, workdir, stream=stream,
                                                   api_secret_provider=lambda ref, n=name: self._api_secret(n, ref))
            if effective_spec['kind'] == 'api':
                self._executors[name] = BudgetedApiExecutor(self._executors[name], lambda: self.paid_ledger)
        self.routes: dict[str, list[str]] = cfg["routes"]
        self.tiers: dict[str, list[str]] = cfg.get("tiers", {})

    def chain_for(self, task: Task) -> list[tuple[str, Executor]]:
        if task.type in TIERED_TASK_TYPES:
            tier = DIFFICULTY_TO_TIER.get(task.difficulty, DEFAULT_TIER)
            names = self.tiers.get(tier, [])
            names = list(dict.fromkeys(names + self.routes.get(task.type.value, [])))
            names = sorted(names, key=lambda n:
                           0 if task.specialty is not None and self._specialties.get(n) == task.specialty else 1)
        else:
            names = self.routes.get(task.type.value)
        if names:
            names = [n for n in names if self.kind(n) != 'api' or self.agent_enabled(n)]
        if self._local_policy is not None:
            names = [n for n in (names or []) if self.agent_enabled(n) and n not in self._circuit_open]
            if not names and task.type != TaskType.IMAGE:
                fallback = self._local_policy.get('generic_fallback', []) if isinstance(self._local_policy, dict) else []
                names = [n for n in fallback if self.agent_enabled(n) and n not in self._circuit_open]
        if not names:
            raise ValueError(f"no route for task type '{task.type.value}'"
                             f" (difficulty={task.difficulty})")
        return [(n, self._executors[n]) for n in names if n not in self._circuit_open]

    def animal(self, agent_name: str) -> str:
        return self._animals.get(agent_name, "cat")

    def bind_api_store(self, store) -> None:
        self._api_store = store

    def bind_paid_ledger(self, ledger) -> None:
        self.paid_ledger = ledger

    def _api_secret(self, name, ref):
        if self._api_store is None:
            return ''
        # Cached executors must re-check the live project's authorization.
        state = self._api_store.raw_snapshot()
        target = self._specs[name].get('connection', {})
        if not any(c['agent_id'] == name and c['enabled'] and c.get('credential_ref') == ref
                   and c['base_url'] == target.get('base_url') and c['format'] == target.get('format')
                   for c in state['items'].values()):
            return ''
        return self._api_store.resolve_secret(ref)

    def executor_for(self, agent_name: str) -> Executor:
        self.require_agent_enabled(agent_name)
        return self._executors[agent_name]

    def agent_enabled(self, name: str) -> bool:
        if name not in self._specs:
            return False
        kind = self.kind(name)
        if kind == 'api':
            return (self._specs[name].get('enabled') is True and
                    not (isinstance(self._local_policy, dict) and self._local_policy.get('config_error')))
        if self._local_policy is None or kind == 'mock':
            return True
        if not isinstance(self._local_policy, dict) or self._local_policy.get('config_error'):
            return False
        if kind in self._local_policy.get('external_enabled_kinds', []):
            return True
        if not self._local_policy.get('confirmed'):
            return False
        if kind in LOCAL_KINDS:
            return kind in self._local_policy.get('enabled_kinds', [])
        return kind in self._local_policy.get('external_enabled_kinds', [])

    def require_agent_enabled(self, name: str) -> None:
        if name not in self._specs:
            raise KeyError(name)
        if not self.agent_enabled(name):
            raise ValueError('Agent 未启用，请在接入设置中确认：' + name)

    def profile_spec(self, name: str) -> dict:
        return copy.deepcopy(self._specs[name])

    def model(self, name: str) -> str:
        return str(self._specs[name].get('model') or '')

    def resolved_options(self, name: str, options: dict) -> dict:
        spec = self.profile_spec(name)
        values = normalize_options(spec['kind'], options)
        default_model = {'claude_code': 'sonnet', 'dsh': 'deepseek-v4-flash'}.get(spec['kind'], '')
        return {'model': values.get('model', str(spec.get('model', default_model) or '')),
                'effort': values.get('effort', str(spec.get('reasoning_effort') or ''))}

    def executor_with_options(self, name: str, options: dict) -> Executor:
        self.require_agent_enabled(name)
        spec = self.profile_spec(name)
        values = self.resolved_options(name, options)
        spec.update(model=values['model'], reasoning_effort=values['effort'])
        executor = build_executor({'kind': 'mock'} if self._mock_all else spec,
                                  self._workdir, stream=self._stream,
                                  api_secret_provider=lambda ref: self._api_secret(name, ref))
        return (BudgetedApiExecutor(executor, lambda: self.paid_ledger)
                if spec['kind'] == 'api' and not self._mock_all else executor)

    def model_capabilities(self, name: str, model: str) -> dict:
        spec = self._specs[name]
        catalog = spec.get('model_capabilities', {})
        cap = selection_capabilities(spec['kind'], model, catalog, {})
        if spec['kind'] == 'dsh' and not dsh_effort_supported(tuple(_dsh_cmd(spec.get('dsh_path')))):
            cap = {'effort_control': 'unsupported', 'effort_levels': [], 'source': 'runtime'}
        return cap

    def agent_metadata(self, name: str) -> dict:
        spec = self._specs[name]
        kind = self.kind(name)
        supported = kind in ('claude_code', 'codex')
        if kind == 'dsh':
            supported = dsh_effort_supported(tuple(_dsh_cmd(spec.get('dsh_path'))))
        models = list(dict.fromkeys(str(s.get('model')) for s in self._specs.values()
                      if s['kind'] == kind and s.get('model')))
        catalog = spec.get('model_capabilities', {})
        keys = list(dict.fromkeys(models + list(catalog if isinstance(catalog, dict) else {}) + [self.model(name), '']))
        capabilities = {model: self.model_capabilities(name, model) for model in keys}
        return {'name': name, 'kind': kind,
                'group': str(spec.get('display_name') or agent_group(kind)),
                'model_capabilities': capabilities,
                'model': self.model(name), 'effort': str(spec.get('reasoning_effort') or ''),
                'model_choices': models, 'effort_supported': supported,
                'suggested_efforts': suggested_efforts(kind) if supported else [],
                'animal': self.animal(name), 'tier': self.tier_of(name),
                'specialty': self.specialty(name), 'blurb': self.blurb(name),
                'allowed_tools': self.allowed_tools(name),
                'route_types': [typ for typ, names in self.routes.items() if name in names],
                'manager_rank': (self.routes.get('manager', []).index(name)
                                 if name in self.routes.get('manager', []) else 999)}

    def claude_code_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "claude_code" and self.agent_enabled(n)]

    def codebuddy_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "codebuddy" and self.agent_enabled(n)]

    def codex_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "codex" and self.agent_enabled(n)]

    def dsh_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "dsh" and self.agent_enabled(n)]

    def is_paid_api(self, agent_name: str) -> bool:
        return self._kinds.get(agent_name) in PAID_API_KINDS

    def open_circuit(self, agent_name: str) -> None:
        self._circuit_open.add(agent_name)

    # ---------- 元数据读取（计划门的 agent 选择器用，见 spec §4.3） ----------

    def all_agents(self) -> list[str]:
        return list(self._executors)

    def circuit_open(self) -> set[str]:
        """本次运行已熔断下线的 agent。返回副本：调用方拿去过滤清单时
        不小心改到它，不该污染路由自己的状态。"""
        return set(self._circuit_open)

    def kind(self, agent_name: str) -> str:
        """矩阵里**声明**的 kind（展示用）。刻意不是 _kinds -- 那个是
        mock_all 替换后的生效值，--mock 下全是 "mock"，前端额度徽章
        会全错。"""
        return self._declared_kinds.get(agent_name, "")

    def specialty(self, agent_name: str) -> str | None:
        return self._specialties.get(agent_name)

    def tier_of(self, agent_name: str) -> str | None:
        """agent 所属档位，不在任何 tier 里就是 None -- 不是每个 agent
        都接线了（or-nemotron/dsh-flash 注册但未接线），没有就别编一个。"""
        for tier, names in self.tiers.items():
            if agent_name in names:
                return tier
        return None

    def allowed_tools(self, agent_name: str) -> str:
        """矩阵里**声明**的 allowed_tools（原始逗号分隔串）。

        刻意从矩阵读而不是从执行器实例读：mock_all=True 时执行器被换成
        MockExecutor，没有 allowed_tools 属性 -- 那样 --mock 下写盘门
        永远不开，等于「跑真实链路前先 --mock 验证逻辑」（CLAUDE.md §7）
        对权限流程完全失效，用户没法预演它。
        """
        return self._declared_tools.get(agent_name, "")

    def can_write(self, agent_name: str) -> bool:
        """按矩阵**声明**的 kind 和 allowed_tools 判断 -- 跟 kind()/allowed_tools()
        一样刻意不看 mock_all 替换后的执行器，--mock 才能预演写盘门和写锁。"""
        return agent_can_write(self.kind(agent_name), self.allowed_tools(agent_name))

    def blurb(self, agent_name: str) -> str:
        """capability_matrix.yaml 里的可选 display-only 描述。没写就是
        空串，调用方拼保底文案（BYO 原则，见 spec §4.3）。"""
        return self._blurbs.get(agent_name, "")
