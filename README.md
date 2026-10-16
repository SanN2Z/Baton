# Baton

Baton 让 Claude 负责决策和监督，让 Hermes 上的模型执行代码，并把每一轮的证据、裁决和记忆留下来，便于安全续跑。

## 三个角色

- **conductor**：Claude 主会话，唯一决策者；写简报、定义范围、读取监督简报、裁决和最终验收。
- **executor**：Hermes profile `baton-exec-v2` 上的 `gpt-5.6-luna`，困难轮自动升级到 `gpt-6.1-sol`；按简报改代码和验证。
- **monitor**：独立只读进程，读取卷宗和快照写监督简报；不改代码、不替 conductor 做决定。

记忆由 mem0、本地 Ollama 和 `baton-qwen3-embed:latest` 提供。Ollama 离线时会退回关键词检索并记录原因。

## 安装

需要 Claude Code、Python 3.9+、git、已登录的 Codex CLI，以及 Hermes。

```bash
git clone https://github.com/Valdemar-Yu/Baton.git
cd Baton
./install.sh
```

安装脚本只建立 skill/CLI 链接并检查依赖，不会替用户写家目录配置。准备本地 Ollama 和嵌入模型：

```bash
ollama serve
ollama pull qwen3-embedding:0.6b
```

如果 Ollama 没有 `ollama` 命令，按系统说明安装；`install.sh` 会提示缺失但仍完成其他安装。随后在用户自己的终端执行：

```bash
baton memory install
baton init
hermes -p baton-exec-v2 auth add openai-codex --type oauth
hermes -p baton-exec-v2 auth list openai-codex
hermes -p baton-exec-v2 auth remove openai-codex <根目录带进来的旧条目>
```

`baton init --upgrade-profile` 只在确认要覆盖 profile 的 SOUL.md/config.yaml 时使用。运行 `baton doctor` 检查 Hermes、Ollama、台账和 monitor 隔离。

## 一次任务

在目标项目里由 Claude 主会话执行：

```text
/baton 给 parser 加流式解析，保持接口兼容，tests/ 全部通过
```

conductor 会先运行 `doctor`、`config`，写 `.baton/tasks/<task>/brief.md`（验收、允许/禁止路径、验证命令和 `baton-scope` 范围块），再运行：

```bash
baton start <task>
baton wait <task> &
```

executor 每次小修改追加 `.baton/log.md`；大修改写 `.baton/reports/R*.html` 后暂停。monitor 生成独立监督简报，conductor 逐条写 `baton-verdict`，运行 `baton rule`，通过门槛后才 `baton resume --verdict`。任务结束要经过终审和 `baton close`。容量错误会自动立即重试 3 次，再延迟 15 分钟重试 1 次；用尽后产生 FAILED，由 conductor 决定换模型、拆分或询问用户。

## 从 v1 迁移

旧项目先备份并迁移项目根的配置：

```bash
baton init --migrate
```

迁移会保留 `baton.json.v1.bak`，把 `judge` 改为 `conductor`/`monitor`，并把执行者默认切到 Hermes。旧项目没有 `executor.backend` 时仍按 codex 运行，方便逐项目迁移。

项目指令也要更新，例如 `~/yuzz/CLAUDE.md` 或项目内 `CLAUDE.md`：删除“派子 agent 设为 judge.model 当裁判”的 v1 说法，改为 `conductor.model`、独立 monitor、监督简报和 `baton rule`。`baton doctor` 发现旧说法会报 WARN，不会阻挡旧项目运行。

## 常见问题

**容量重试为什么等待？** 这是模型服务容量错误，不是代码失败。Baton 立即重试 3 次，再等待 15 分钟重试一次；全部失败后停止本轮，等 conductor 处理。

**monitor 为什么可能带着 Codex 全局指令？** Codex monitor 使用只读参数，但 `~/.codex/AGENTS.md` 是否被 CLI 读取由 CLI 版本决定。`baton doctor --monitor-probe` 会探测并在卷宗标明；monitor 只读，不会执行其中的写入要求。

**Hermes 没有 sandbox-exec 怎么办？** 在项目 `baton.json` 显式写：

```json
{"executor": {"sandbox_exec": false}}
```

随后由 `baton doctor` 显示退路状态；仍应保留项目范围和冻结检查。

**Ollama 离线会不会卡住任务？** 不会。记忆检索退回关键词，返回中带 fallback 原因；恢复 Ollama 后可运行 `baton memory reindex` 补建语义索引。

**安装脚本遇到已有目录怎么办？** 普通目录不会覆盖。已有软链接（包括旧的 `~/.local/share/baton-v1/baton`）会改指向当前仓库并打印变更；普通目录会跳过并提示手动处理。
