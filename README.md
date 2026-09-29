# AgentHub

**你的目标，你的 Agent 团队。**

AgentHub 是本地运行的多 Agent 协作工作台。先与经理澄清目标，再把目标拆成任务依赖图，交给不同 Agent 执行，并在像素办公室里查看进度、任务、交付物和运行记录。

支持 Claude Code、Codex CLI、WorkBuddy / CodeBuddy、DeepSeek Harness，以及 OpenRouter API 和零成本 Mock 执行器。各执行器需要用户自行安装、登录或提供密钥；AgentHub 不提供账号、模型额度或托管凭证。

## 界面原型

以下为已确认的 **Studio 设计原型**：左侧会话，中间经理对话，右侧团队画布。用户可以决定谁担任经理。

![AgentHub Studio 工作台原型](docs/images/01-workspace.jpg)

![经理选择原型](docs/images/02-manager-picker.jpg)

<details>
<summary>查看暗色主题、移动端与完整设计画布</summary>

![暗色工作台原型](docs/images/04-dark-workspace.jpg)

![移动端工作台原型](docs/images/07-mobile.jpg)

![移动端经理选择原型](docs/images/08-mobile-manager.jpg)

![完整设计画布](docs/images/00-design-board.jpg)

</details>

这些图片使用演示数据。**本次公开代码来自已有 `main`，仍使用当前像素工作台；Studio 界面及 Agent → 模型 → 思考程度升级保留在独立开发分支，尚未随本次发布合入。** 后续完成的工作会合入 `main` 后再更新。

## 已有能力

- 经理对话澄清目标，AI 规划或 JSON 计划输入。
- 按任务类型、难度与专长路由，支持降级链和失败重试。
- DAG 并行调度、上游上下文接力、共享黑板与 JSONL 事件流。
- 自动或手动分配执行者，计划确认及写盘任务确认。
- 像素办公室、运行历史、思考与行动记录、浅色与暗色主题。
- 文件和改动预览、交付物导出、运行取消、崩溃恢复与过期任务重跑。
- Claude Code 任务完成后继续追问；其他执行器的续聊能力仍有边界。

## 快速开始

需要 Python 3.9+。项目使用普通 HTML / JavaScript 和 Python，无前端构建步骤。

```bash
git clone https://github.com/Yuhao1015/agenthub.git
cd agenthub
python -m venv .venv
```

macOS / Linux：

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp hub/capability_matrix.example.yaml hub/capability_matrix.yaml
python main.py --plan example_plan.json --mock
python serve.py
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item hub/capability_matrix.example.yaml hub/capability_matrix.yaml
.\.venv\Scripts\python.exe main.py --plan example_plan.json --mock
.\.venv\Scripts\python.exe serve.py
```

打开 <http://localhost:8787/dashboard.html>。Mock 不需要账号或密钥；真实运行前请配置自己的执行器，并确认任务的写盘权限。

给出目标或直接运行计划：

```bash
python main.py --goal "设计一个简单的个人主页" --workdir ./workspace
python main.py --plan example_plan.json --workdir ./workspace
python main.py --plan example_plan.json --resume
```

上述真实运行可能消耗用户自己的订阅、积分或 API 额度。

## 本地配置与隐私

仓库只提供可移植的 `hub/capability_matrix.example.yaml` 示例。复制后的 `hub/capability_matrix.yaml` 属于本地文件，已忽略；按自己的 Agent、模型、路由与预算修改。

密钥通过环境变量设置，例如 `OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY`。Claude Code 和 Codex CLI 使用各自的登录状态。不要将密钥写入源码、示例配置或提交到 Git。

本次发布不包含个人配置、凭证、会话与运行数据、机器路径记录、内部实验和交接材料、虚拟环境、未提交修改及本地 Git 历史。`.gitignore` 同时覆盖常见本地配置和运行产物。

成本记录和预算护栏并不覆盖所有执行器：OpenRouter 可计费请求才受运行预算限制；订阅、积分和 DeepSeek Harness 的花费可能无法完整计量。CLI 已安装也不代表模型、账号或额度可用。

## 架构

```text
main.py / serve.py  →  Manager / Planner  →  Plan（任务 DAG）
                                              ↓
                                         Router → Executors
                                              ↓
                                Blackboard + JSONL 事件 → Dashboard
```

代码集中在 `hub/`。控制平面负责执行顺序，黑板保存任务事实和上下文；前端通过事件展示运行状态。运行说明见 [USAGE.md](USAGE.md)。

## 测试与贡献

测试使用可直接运行的 Python 脚本：

```bash
python tests/test_scheduler.py
python tests/test_router_metadata.py
python tests/test_plan_from_blackboard.py
```

请先复制示例矩阵。Windows 上部分旧测试依赖 POSIX 文件权限、符号链接或模拟进程行为，尚有兼容性问题；本次公开不宣称所有平台全套测试通过。

欢迎提交 Issue 或 Pull Request。开发中的分支不会自动并入公开版本；后续发布以完成并合入 `main` 的代码为准。

## 开源许可

[MIT License](LICENSE)。本项目与所接入的 Agent 服务没有官方隶属关系；第三方名称和服务仍受各自条款约束。
