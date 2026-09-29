"""Router: task type -> ordered list of executors (fallback chain)."""
from __future__ import annotations

import yaml

from .executors import Executor, build_executor
from .models import Task, TaskType

# Only these agent kinds are billed pay-as-you-go; claude_code runs on the
# subscription's included quota, codex reuses ChatGPT login, codebuddy runs
# on workbuddy's already-owned credit/free balance (tracked separately, never
# gated), and mock is free -- none count against max_usd_per_run.
PAID_API_KINDS = {"openrouter"}

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
            self._executors[name] = build_executor(effective_spec, workdir, stream=stream)
        self.routes: dict[str, list[str]] = cfg["routes"]
        self.tiers: dict[str, list[str]] = cfg.get("tiers", {})

    def chain_for(self, task: Task) -> list[tuple[str, Executor]]:
        if task.type in TIERED_TASK_TYPES:
            tier = DIFFICULTY_TO_TIER.get(task.difficulty, DEFAULT_TIER)
            names = self.tiers.get(tier, [])
            names = sorted(names, key=lambda n:
                           0 if task.specialty is not None and self._specialties.get(n) == task.specialty else 1)
        else:
            names = self.routes.get(task.type.value)
        if not names:
            raise ValueError(f"no route for task type '{task.type.value}'"
                             f" (difficulty={task.difficulty})")
        return [(n, self._executors[n]) for n in names if n not in self._circuit_open]

    def animal(self, agent_name: str) -> str:
        return self._animals.get(agent_name, "cat")

    def executor_for(self, agent_name: str) -> Executor:
        return self._executors[agent_name]

    def claude_code_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "claude_code"]

    def codebuddy_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "codebuddy"]

    def codex_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "codex"]

    def dsh_agents(self) -> list[str]:
        return [n for n, k in self._kinds.items() if k == "dsh"]

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

    def blurb(self, agent_name: str) -> str:
        """capability_matrix.yaml 里的可选 display-only 描述。没写就是
        空串，调用方拼保底文案（BYO 原则，见 spec §4.3）。"""
        return self._blurbs.get(agent_name, "")
