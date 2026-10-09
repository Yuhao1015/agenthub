# 使用说明

先按照 [README](README.md#快速开始) 安装依赖，并将 `hub/capability_matrix.example.yaml` 复制为本地 `hub/capability_matrix.yaml`。

```bash
# 不使用真实模型的完整链路
python main.py --plan example_plan.json --mock

# 启动本地工作台
python serve.py

# 真实 Agent 执行；请先完成自己的 CLI 登录及 Agent 接入 / API 连接配置
python main.py --plan example_plan.json --workdir ./workspace
python main.py --goal "你的目标" --workdir ./workspace

# 先与经理澄清目标
python main.py --goal "你的目标" --interactive

# 恢复已保存的计划，跳过已完成任务
python main.py --plan example_plan.json --resume

# 从黑板恢复，重跑过期任务
python main.py --resume
```

工作台地址：<http://localhost:8787/dashboard.html>。默认仅用于本机访问。

在工作台设置中点击“发现本机 Agent”，勾选需要接入的工具并保存。发现阶段只检查程序文件；保存后检查安装、登录和支持的模型目录。模型目录不等于调用验证，测试需要用户选择一个模型并确认额度消耗。其他平台未提供目录或登录协议时会显示限制，不用预设型号代替读取结果。

API 连接由用户添加名称、地址、密钥和格式（OpenAI 兼容 / Anthropic / OpenRouter）。工作区入口为加号，Windows 会打开系统文件夹选择窗口。新目标可以在现有目标运行时提交并排队，轮到后仍需经理澄清和用户确认。

消息的重新发送和修改先回填输入框，由用户确认后发送。文件审阅支持检查点差异和逐文件回滚；回滚不会覆盖已被后续操作修改的文件。

运行产生的事件、黑板、会话和配置属于用户本地数据，不应提交。费用护栏、执行器续聊能力和平台边界见 README。
