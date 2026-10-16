# 小红书文案 · Baton v2

## 标题

让 Claude 只做决定，代码交给 GPT 去写

## 正文

这个月初我做了个叫 Baton 的小工具，装在 Claude Code 里。Claude 当指挥，Codex 里的 GPT 当执行者，Claude 写任务简报、审汇报，GPT 去改代码。

v1 用了一阵，有两个问题越来越扎眼。

第一个是 Claude 审的其实是 GPT 写的汇报。汇报里说「全部测试通过」，Claude 大概率就信了，证据和自述混在一起。第二个是监督靠定时，每 30 分钟叫醒 Claude 读一次快照。方向跑偏多半发生在一轮刚开始，等它醒来，代码可能已经改了半小时。

所以 v2 拆成了三个角色。

Claude 还是指挥，也是唯一能拍板的人。执行者换成本地 Hermes 上的 gpt-5.6-luna，用的还是 Codex 订阅，难的轮次自动升到 gpt-6.1-sol。新加了一个 monitor，它是一个隔离的只读进程，只看 baton 封存好的卷宗，里面有完整 diff、代码快照、baton 自己跑的测试结果和简报原文，然后逐条列出偏离。Claude 裁决时必须对每一条写明采纳还是驳回，漏一条，`baton rule` 就拒收。

几条硬线不靠提示词，靠程序守。验收测试和测试配置在任务开始时冻结，runner 每 60 秒核对一次，被改了就停住执行者。每轮结束 baton 自己跑测试，按每条测试记结果。Hermes 外面套了 macOS 的 sandbox-exec，执行者写不了项目以外的地方。

记忆这块参考了 Mem0 团队最近的 DolphinBench。论文里有个判断我很认同，难的是 agent 意识到自己该回忆，查找本身反而简单。所以 Baton 每轮开头主动推送 10 条相关记忆，不等执行者自己想起来去查。记忆用 mem0 加本机 Ollama 的 qwen3-embedding，数据不出电脑。我实测过一次，台账里有一条「所有函数要写类型注解和中文 docstring」，简报里没提，luna 写新函数时照着做了。

另外 Codex 时不时报 model at capacity，以前每次都要我手动续跑。现在 runner 会自己立即重试 3 次，还不行就等 15 分钟再试一次。

v2 是用 v1 的 Baton 造出来的。三天里，Opus 写简报、审了三十多份汇报，Codex 写代码，最后 239 个单元测试加 5 个连真实 mem0 和 Ollama 的测试。上线前我拿真 Hermes 和真 monitor 从头跑了一遍，从开工到 close 全流程走通。

开源在 GitHub，Valdemar-Yu/Baton。想试的话，先装 Claude Code、Codex CLI、Hermes 和 Ollama，再跑仓库里的 install.sh。

## 配图

按顺序发四张，竖版 3:4。

1. [封面](../../assets/promo/01-cover.png)
2. [三个角色](../../assets/promo/02-three-roles.png)
3. [记忆](../../assets/promo/03-memory.png)
4. [验收测试冻结](../../assets/promo/04-frozen-tests.png)

logo 在 `assets/brand/`：方形图标 `baton-logo.svg`、透明底图标 `baton-mark-transparent.svg`、横版字标 `baton-wordmark.svg`，各附一份 2 倍 PNG。

## 话题

#ClaudeCode #AI编程 #多智能体 #Codex #开源项目 #程序员工具
