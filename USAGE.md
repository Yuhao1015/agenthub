# 使用说明

先按照 [README](README.md#快速开始) 安装依赖，并将 `hub/capability_matrix.example.yaml` 复制为本地 `hub/capability_matrix.yaml`。

```bash
# 不使用真实模型的完整链路
python main.py --plan example_plan.json --mock

# 启动本地工作台
python serve.py

# 真实 Agent 执行；请先完成自己的 CLI 登录 / API 环境变量配置
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

运行产生的事件、黑板、会话和配置属于用户本地数据，不应提交。费用护栏、执行器续聊能力和当前原型与正式界面的区别见 README。
