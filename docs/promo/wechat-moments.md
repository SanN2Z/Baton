# 朋友圈文案 · Baton v2

🎼 这两天把自己写的 Baton 升到了 v2。它是 Claude Code 里的一个 skill，Claude 写任务简报、拍板，GPT 去改代码。

🤔 v1 让我最不放心的一点是，Claude 审的其实是 GPT 自己写的汇报，监督也只是每半小时读一次快照。所以 v2 拆成了三个角色。🧑‍✈️ Claude 还是唯一拍板的人。🛠️ 执行者跑在本地 Hermes 上，平时用 gpt-5.6-luna，卡住了才升级到 gpt-6.1-sol。🔍 新加的 monitor 是个只读进程，单独读 diff、代码快照和 baton 亲跑的测试结果，把偏离一条条列出来，Claude 每条都得回应才能接着跑。🔒 验收测试开工时就冻结，执行者改不动。

📚 这么拆参考了三份东西。council.skill 在 SWE-bench Lite 的 23 个任务上试过，主 agent 加上多模型合议，解决数从 8 涨到 12，所以 monitor 默认用和 Claude 不同家族的模型，两边看同一份证据，盲区错开。Mem0 的论文说，按需抽取和检索的记忆层，比把历史全塞进上下文 p95 延迟低 91%，token 省九成以上。Mem0 团队新出的 DolphinBench 又指出，最难的一步是 agent 意识到自己该回忆。所以 Baton 每轮主动把 10 条相关记忆推给执行者，不等它自己想起来查。🧠 记忆用 mem0 加本机 Ollama，数据不出电脑。

😄 比较好玩的是，v2 是用 v1 的 Baton 一轮一轮造出来的，前后三天，审了三十多份汇报。

⭐ 开源在 github.com/Valdemar-Yu/Baton，logo 和第一版是一个系列。

参考
[1] council.skill，github.com/ParadoxZW/council.skill
[2] Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory，arxiv.org/abs/2504.19413
[3] DolphinBench: Mapping the Pareto Frontier of Agent Memory，arxiv.org/abs/2609.24971

## 配图

1. `assets/brand/baton-v2-banner.png`（三个机器人坐在五线谱上）
2. `assets/promo/01-cover.png`（封面海报）
3. `assets/promo/02-three-roles.png`（三个角色）
