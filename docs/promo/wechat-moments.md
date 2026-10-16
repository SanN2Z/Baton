# 朋友圈文案 · Baton v2

这两天把自己写的 Baton 升到了 v2。它是 Claude Code 里的一个 skill，Claude 写任务简报、拍板，GPT 去改代码。

v1 让我最不放心的一点是，Claude 审的其实是 GPT 自己写的汇报。v2 加了一个只读的 monitor，单独去读 diff、代码快照和 baton 亲跑的测试结果，把偏离一条条列出来，Claude 每条都得回应才能接着跑。验收测试开工时就冻结，执行者改不动。记忆用 mem0 加本机 Ollama，每轮自动推给执行者，数据不出电脑。

比较好玩的是，v2 是用 v1 的 Baton 一轮一轮造出来的，前后三天，审了三十多份汇报。

开源在 github.com/Valdemar-Yu/Baton，logo 和第一版是一个系列。

## 配图

1. `assets/brand/baton-v2-banner.png`（三个机器人坐在五线谱上）
2. `assets/promo/01-cover.png`（封面海报）
