# Baton v2 设计稿

v2 改三件事。

角色拆成 **conductor**（指挥）、**executor**（执行者）、**monitor**（监督）三个。conductor 是唯一的决策者，定规则、裁决汇报、宣布验收；executor 照规则干活；monitor 只读证据，对照 conductor 定下的规则找偏离，写成**监督简报**交给 conductor，不做决定，也不和 executor 接触。`baton` CLI 不调用对话模型，负责**冻结**规则、产生证据、拉起 monitor 和传递消息。

executor 从 Codex CLI 换成走 Codex 订阅的 **Hermes CLI**，跑在 Baton 专用的 Hermes **profile** 里。输出格式这类固定协议写进这个 profile 的 **SOUL.md**，作用相当于 Claude Code 的 CLAUDE.md。默认模型 **gpt-5.6-luna**，难的轮次升级到 **gpt-6.1-sol**。

记忆参照 **Mem0** 团队 **DolphinBench** 论文里得分最高的配置（Hermes + GPT-5.6-Luna + Mem0）的结构来搭。分**常驻块**、mem0 **事实库**、**原始档案**三层，每轮开始自动推送，轮中给只读检索工具，写入只经 conductor 核准。

## v1 的三个问题

v1 里 Claude 主会话（`judge.model`，默认 Opus）一人兼指挥和审阅，写简报、拍板决策、30 分钟监管、审阅汇报、最终验收全由它做；Codex CLI 里的 gpt-6.1-sol 执行；council 在重大决策时给意见；`baton` CLI 负责起停进程、建回滚点、算 diff、计时和读额度。这套能跑通，但有三处结构性的问题。

第一处，判卷没有独立的证据阅读。主会话写的验收标准，最后也由它读完执行者的汇报后自己宣布达成。简报写得含糊时，它会自然地按自己当初的意图去理解，执行者的产出往往也就过了；这个判断还不留依据，事后查不出它看了什么、放过了什么。

第二处，审阅者的上下文被污染。主会话审 R003 时，前面整段对话都在，包括执行者在汇报里的自我叙述、上几轮自己的指令和它对进度的期望。审阅的输入应当是证据，叙述只能算参考。

第三处，记忆随会话消失。Codex 会话内的上下文能撑一个任务，任务结束就没了。下一个任务里执行者重新摸一遍测试命令、重新踩一遍同一个环境坑。`log.md` 和审阅文件里其实记着这些经验，只是没有任何机制把它们送回执行者面前。

## 三个角色

| | conductor（指挥） | executor（执行者） | monitor（监督） |
|---|---|---|---|
| 承担者 | Claude 主会话，`conductor.model`，默认 opus | Hermes CLI，`executor.model`，默认 gpt-5.6-luna | `baton` 拉起的隔离只读进程，`monitor.model`，默认 gpt-5.6-sol，思考深度取默认 |
| 产出 | 简报与验收标准、决策（存为判例）、裁决与最终验收、纠偏消息、修正案、记忆核准 | 代码、log、汇报、决策请求、记忆候选 | 监督简报（偏离清单、证据、建议结论、记忆建议）、轮中检查结论 |
| 能读 | 全部 | 简报、发给它的消息、推送的记忆、项目代码 | 卷宗和本轮终点的代码快照，只读 |
| 不能做 | 没拿到监督简报就裁决、对简报里的偏离不作回应、放宽验收标准、改代码 | 改简报与验收标准、改 `.baton/` 已有记录、写记忆库 | 做决定、改文件、和 executor 对话 |

决定权只有一个来源。conductor 是三方里唯一能做决定的，用户在它之上。monitor 的简报是意见，conductor 可以不采纳，但简报里每一条偏离都要在裁决里写明采纳还是驳回、理由是什么，和裁决一起入账。`baton` 守两条硬线，一是验收测试的亲跑结果，二是锁定级冻结文件。这两条是事实，conductor 也绕不过去，要变只能走修正案，放宽的修正案须用户确认。

v1 的前两个问题由此解决。conductor 裁决时手里有一份它插不进话的独立证据阅读，读的是简报，执行者的自述只在卷宗里作为当事人陈述出现。monitor 默认用 GPT 系模型，conductor 用 Opus，两边看同一份证据，盲区不重合。被驳回的偏离在 `baton close` 时逐条列给用户。

monitor 用不着最强的模型。它要回答的问题窄，对照章程、判例和消息，看证据里有没有偏离；误报的代价是 conductor 多读一条，漏报还有 conductor 的裁决和 `baton` 的硬线兜着。它花的是 Codex 订阅额度，和 executor 共用同一个账号，`QUOTA_LOW` 一并覆盖，不占 Claude 额度。终审要读的东西最多，可以用 `monitor.final_reasoning_effort` 单独调高思考深度。

`baton` CLI 只做确定性的事，包括冻结简报、亲自跑验证命令、算 diff、查越界、组**卷宗**、拉起 monitor、写记忆库、存档和传递消息。它不调用对话模型，检索记忆时只调用本地的嵌入模型。

两类风险分开对待。executor 是被监管方，Baton 要挡住它越界，并在它越界时及时发现。conductor 是用户自己的主会话，Baton 防的是它不看证据就裁决、略过对自己预期不利的偏离或误改记录；用户和 conductor 有意改写 `~/.baton/` 下的文件，不在防护范围内，账本只负责让这类改动留下痕迹。

## 项目边界与项目外的状态

`~/yuzz` 是一个 git 仓库，下面放着多个项目，它的 .gitignore 还是白名单（第一条就是 `*`，只放行 md 和 html）。按 v1 的做法，Bica 这类子项目会和别的项目共用一份 `.baton/` 和记忆库，代码文件被忽略后也进不了快照，diff 是空的，回滚恢复不了代码。

v2 的项目目录取「从当前目录往上找到的第一个含 `baton.json` 的目录，不越过 git 根」。快照、diff 和回滚点放进 Baton 私有的**影子仓库**（`GIT_DIR` 在项目外，`GIT_WORK_TREE` 是项目目录），排除规则只用影子仓库自己的 exclude，和外层仓库的 .gitignore 无关，回滚点也不再往用户的仓库里写 `refs/baton/*`。`baton init` 列出项目顶层条目和大小、项目里嵌套的 data、models、outputs、logs、checkpoints 目录，以及嵌套项目，请用户确认。默认排除规则只在影子仓库建立时写一次（决策 D001）。`data/`、`models/` 常是代码包的名字，只排除项目顶层的那个；`outputs/`、`logs/`、`checkpoints/` 在任何层级都排除；另外排除 `.venv*/`、`node_modules/`、`.claude/settings.local.json` 等。用户之后改的排除规则从下一次快照起生效，已经进过快照的文件也一样。用户仓库已经跟踪的文件，即使被忽略规则命中也照样进快照，和 v1 一致（决策 D002）；为此 Baton 会对用户仓库做只读的 `git ls-files`，不写它的 index、HEAD 和 refs。嵌套项目（子目录里有自己的 baton.json）由它自己的 Baton 管，外层任务的回滚不会改动它的代码和记录。外层 git 根只在 Hermes 拼 AGENTS.md 链时起作用。

凡是不能让 executor 改动的状态，都放在项目外的 `~/.baton/projects/<项目 id>/`，项目 id 取目录名加路径哈希（路径按磁盘上的真实大小写算）。项目里的 `.baton/project-id` 记着当时的 id，项目目录换了位置或名字时，Baton 拒绝在新的空状态上继续，提示用 `baton init --relink` 把原来的状态接过来。

```
~/.baton/projects/<项目 id>/
  snap.git/                 影子仓库，快照、diff、回滚点
  frozen/                   冻结文件的内容副本和基线哈希
  ledger/verdicts.jsonl     监督简报、裁决、决策、修正案的账本（哈希链）
  memory/ledger.jsonl       记忆台账（哈希链）
  memory/qdrant/            mem0 的向量库，MEM0_DIR 也指到 memory/
  dossiers/R003/            卷宗，生成后只读
  tasks/<task>/state.json   任务状态，以这份为准
```

项目里的 `.baton/` 只留给人看和给 executor 写的东西，包括 log.md、reports/、decisions/（决策请求）、reviews/（渲染好的监督简报和裁决）、tasks/<task>/brief.md 和 messages/（conductor 发出的每条消息原文）、runs/（每轮的输出和验证结果）、memory/ledger.export.md（台账的只读导出）。

## 制衡机制

只在提示词里写「不许改验收标准」管不住一个跑偏的 executor，也挡不住 conductor 裁决时略过对自己预期不利的证据。v2 把下面几条交给 `baton` 执行，细则见文末的实现要点。

1. **冻结分两级**。简报开头新增一段机器可读的范围块，列 `allow`、`deny`、`acceptance`、`frozen` 四组路径和 `verify` 设置，解析失败 `baton start` 就拒绝启动；「公共 API 不能变」这类语义约束由 monitor 对照检查。锁定级包括验收测试、决定测试怎么收集和执行的配置（conftest.py、pytest.ini、pyproject.toml 里的 pytest 段、package.json 的测试脚本等）、测试命令引用到的项目文件、项目内的 Hermes 上下文文件；改动、删除或在任意位置新建这类文件都发 `VIOLATION` 停住 executor，由 conductor 看过监督简报后裁决。监看级包括依赖清单、Makefile 等其余构建文件，允许改，改动在卷宗里单列，monitor 逐条给意见。项目外的冻结文件（链上的 AGENTS.md、profile 的文件）变了先发 WARN，到下一轮边界由 conductor 确认后重新冻结。runner 每 60 秒核对一次，每轮结束和每次轮中检查再核对；被改的锁定文件用 `baton restore-frozen` 从副本恢复。
2. **配置和验证都在启动时定下来**。`baton start` 把生效配置存成快照，之后各轮只用这份；`baton.json` 中途被改，下一轮开始时提示「本任务不生效」，要生效得走 `baton amend --config`。`supervision.test_command` 为空时 `baton start` 拒绝启动，除非范围块写明 `verify: none`，这时卷宗首行写明本任务没有 baton 亲跑的验证。
3. **证据自产**。每轮结束 `baton` 自己跑验证命令，pytest 按每条测试记录结果，验收测试逐条比对，没收集到、跳过、xfail 都算没通过；结果写进 `runs/<task>/leg-NNN.verify.json`，项目外留一份。卷宗里的验证结果只用这份，executor 汇报里自述的数字只作对照。
4. **证据隔离**。每轮以 REPORT、DONE、VIOLATION 结束后，`baton` 自动生成卷宗和固定的 `MONITOR_PROMPT.md`，导出本轮终点的只读代码快照，拉起 monitor，不等 conductor 下命令。monitor 的运行环境与本机所有定制隔离，只能读卷宗和快照，不加载任何用户级或项目级的指令文件、技能、插件和 MCP，输出按 JSON schema。提示词全由 `baton` 拼好，conductor 加不进自己的话，也不能自己审或派子 agent 审来代替。
5. **裁决入账，续跑有门槛**。monitor 的简报由 `baton` 校验后渲染成 `reviews/R<NNN>-brief.md`，追加进项目外的账本，绑定卷宗哈希。conductor 读完简报，用 `baton rule <task> R003 <裁决文件>` 给出结论；裁决文件对简报里的每条偏离（F1、F2…）写采纳或驳回及理由，缺一条 `baton` 就拒收。裁决渲染成 `reviews/R<NNN>-verdict.md`，入账并绑定同一份卷宗。REPORT、DONE、VIOLATION 之后，账本里没有绑定该卷宗的简报和裁决时，`resume` 和 `steer` 都拒绝执行，monitor 运行期间同样拒绝。`baton close` 只认账本里绑定最新终审卷宗的 ACCEPT，要求验收测试的亲跑结果全部通过，并先重新快照，代码在 ACCEPT 之后又变过就拒绝。

> [!warning] executor 的写入边界
> Hermes 本地终端加 `--yolo` 没有写沙箱。v2 让 runner 用 macOS 自带的 `sandbox-exec`（Codex CLI 的 workspace-write 用的也是这套 Seatbelt 机制）包住 Hermes，写入只放行项目目录、baton-exec-v2 的会话和凭据文件、临时目录和常用缓存，项目里的 `.baton/tasks/`、`.baton/reviews/`，profile 的 SOUL.md、config.yaml、.env，以及 `~/.baton/`、`~/.claude/` 和其他项目都写不了。这份规则还没在本机跑过，Hermes 在它下面能否正常读写会话库、刷新凭据、写缓存要先实测；实测不通过时退回到只有冻结检查，那时越界改动能在 60 秒内被发现和恢复，但挡不住。终端的网络访问不受限，`network_access` 在 hermes 后端下只决定有没有 web_search 工具。

卷宗的内容是固定的。

```
~/.baton/projects/<项目 id>/dossiers/R003/
  MONITOR_PROMPT.md baton 生成的监督提示
  charter.md        冻结的简报、验收标准、历次修正案
  evidence.md       diff 统计与摘要、亲跑的验证结果、越界与冻结检查、监看文件的改动、本轮的外部动作、轮中检查记录
  diff.patch        本份汇报覆盖的完整 diff，不设大小上限
  code/             本轮终点的只读代码快照，和 diff.patch 是同一份
  report.html       executor 汇报原件（标注为当事人陈述）
  memory.md         本轮推送的记忆、memory_search 实际返回的编号、记忆候选（编号 C1、C2…）及各自最相近的 3 条已有条目、当前常驻块
  precedents.md     以往的裁决要点、conductor 的决策和消息，monitor 拿它们对照 executor 的做法
```

监督简报的格式同样固定。偏离逐条编号为 F1、F2…，每条写类别、严重度、证据位置（文件和行号、diff 片段或验证条目）和它对照的规则出处（章程条目、D 号判例或消息编号）。类别有偏离章程、偏离判例或消息、验收缺口、破坏信号、越界、漏记录、自述与证据不符七种，严重度分 blocking 和 note。简报另给建议结论、对每条记忆候选的建议，以及一段不超过 600 字的摘要。没有偏离时简报只有摘要和建议结论，conductor 一两分钟就能裁决。判例只约束做法，不改写验收标准。

终审的卷宗是 `dossiers/<task>-final/`，monitor 出终审简报，conductor 的结论取 ACCEPT 或 REVISE，渲染到 `reviews/<task>-final.md`。每份汇报的裁决结论取 APPROVE、REVISE、ROLLBACK。

**修正案**分两类。只新增或收紧验收条目、只放开监看级路径的修正案，conductor 可以空闲时自行用 `baton amend` 提交；放宽或删除验收条目、放开锁定级路径都算放宽，要带 `--user-approved`。VIOLATION 裁决为「用修正案认可」时，用 `baton amend --adopt <路径>` 以当前内容重新冻结；被改的是锁定级文件，等于放开锁定级路径，同样要带 `--user-approved`。`baton amend --user-approved` 只能由用户在自己的终端窗口里运行，命令先打开 `/dev/tty` 要用户逐字输入任务名确认，打不开就拒绝；Claude Code 的 Bash 工具没有控制终端，conductor 只能把命令行准备好请用户去跑。用户不同意某份裁决时直接告诉 conductor，由它用 `baton rule --rerule` 重新裁决，原裁决和重裁都留在账本里。

## 一次大修改的流转

```
conductor  写简报 ──▶ baton start（冻结，起 Hermes）
executor   改代码 · 记 log · 写 R003 · 提记忆候选 ──▶ 本轮结束
baton      亲跑验证 · 核对冻结 · 算 diff · 组卷宗 ──▶ 拉起 monitor
monitor    读卷宗 ──▶ 监督简报（偏离清单 + 建议结论 + 记忆建议）
baton      校验简报、入账 ──▶ REPORT 事件（附简报）
conductor  读简报 ──▶ baton rule <task> R003 <裁决文件>
baton      校验裁决、入账、核准的记忆写进台账
conductor  baton resume <task> --verdict R003（消息只含裁决原文和本轮推送的记忆）
```

monitor 脱离 conductor 的会话运行，关掉 Claude Code 不会打断它。简报就绪后 `baton wait` 才报告 REPORT、DONE 或 VIOLATION，事件里附简报路径和摘要；monitor 没能交出简报时报告 MONITOR_FAILED。输出不合 schema 就带上错误重跑一次，仍不合格、超时或撞上用量上限都算失败，用量上限的事件里带重置时间。这期间 executor 保持暂停，conductor 可以等待、用 `baton monitor <task> R003 --rerun` 重跑或请用户决定，不能跳过简报直接裁决。同一份卷宗只接受一份简报，重跑要加 `--rerun` 并入账。

DECISION 不经 monitor，直接归 conductor。决策请求是简报没覆盖的地方，需要补规则。conductor 用 `baton decide <D号> <决定文件>` 把决定写进账本，存为**判例**，同时冻结那份决策请求的副本；之后的卷宗从账本渲染判例，monitor 拿它对照 executor 的做法。重大决策仍可召集 council。conductor 每次 resume、steer 的原文存进 `tasks/<task>/messages/`，也列进下一份卷宗。

executor 对 REVISE 有异议时，在下一份汇报里写**申辩**一节，monitor 在简报里逐条核对申辩引用的证据，conductor 只复议一次。针对同一事项的修订汇报在开头写「修订自 R003」，`baton` 据此累计；同一事项累计 REVISE 3 次，或复议后仍过不了，conductor 停下任务，把卷宗、简报和裁决交给用户，这条优先于升级规则。ROLLBACK 由 conductor 向用户说明原因和回滚点，用户同意后才执行 `baton rollback`，不同意就按 REVISE 续跑。VIOLATION 的裁决只有三种结果，撤回越界改动后续跑、用修正案认可、停下任务问用户。超出授权的事（需求取舍、删数据、花钱、对外发布）照旧问用户。

## 轮中监督

v1 每 30 分钟发一次 TICK，由主会话读快照。这样出事后最多要过 30 分钟才有人看，方向上的偏差又多半出在一轮刚开始；一轮顺利跑 3 小时，主会话也要读 6 次快照。v2 把轮中监督分成三层，conductor 只在出现偏离时被叫醒。

第一层是 `baton` 的**规则信号**。runner 每 60 秒核对冻结文件时，顺带对影子仓库里新增的 diff 和 state.db 里的终端调用跑一组规则，命中就记一条 ALERT。规则分三类。破坏类看测试文件被删，测试函数或断言数减少，新增 skip、xfail 或注释掉的测试，新增吞异常，删除行数超过新增行数的 `delete_ratio` 倍。停滞类看同一个文件的哈希回到本轮出现过的旧值，连续 `stall_minutes` 分钟没有文件变化，同一条命令连续失败 `repeat_fail` 次。记录类看本轮 diff 超过大修改阈值仍没有汇报，有代码改动而 log.md 超过 20 分钟没有新增。

第二层是 monitor 的**方向检查**。规则看不出这些改动还在不在简报的目标上，这一步交给 monitor。每轮第 10 分钟做第一次，本轮改动第一次涉及 3 个以上文件时提前；之后每次结论为 on_track，间隔翻倍，最长 60 分钟。出现 ALERT、结论为 drifting、conductor 刚 steer 过或新一轮开始，间隔回到最短。ALERT 会立即触发一次检查。轮中检查的卷宗只装上次检查以来的 diff、终端调用、命中的规则，以及章程、判例和消息，结论取 on_track、drifting、breaking，偏离照样逐条编号。

第三层是兜底。monitor 连续两次检查失败时，`baton` 退回 v1 的做法，把 `baton status` 的快照作为 TICK 事件交给 conductor。

三种结论分别处理。on_track 由 `baton` 入账，不叫醒 conductor，`baton wait` 继续等。drifting 发 DRIFT 事件附简报，executor 继续跑，conductor 决定 steer 还是驳回，驳回用 `baton supervised <task> --dismiss F1 --reason ...` 入账。breaking 由 `baton` 立即结束本轮，效果同 `baton stop`，改动保留，回滚点照常，然后发 BREAKING 事件附简报，等 conductor 处置。DRIFT 发出后 30 分钟内 conductor 没有处理，`baton` 结束本轮，这条取代 v1 的 70 分钟无人监管自动暂停。

按默认参数，一轮顺利跑 3 小时，monitor 在第 10、30、70、130 分钟各查一次，==conductor 一次都不用读==；出现删测试一类信号时，一两分钟内就有 monitor 的结论。

## 极限省 token 模式

`baton.json` 里写 `"mode": "lean"` 打开这个模式，默认是 `standard`。它只保留一种交互，executor 把活干完，conductor 审一次，中间没有任何人看模型的输出。

- executor 用 gpt-5.6-luna，思考深度 medium，不自动升级到 Sol。
- conductor 用 Opus 5.5，思考深度 medium。`baton` 管不了主会话的思考深度，`baton config` 和每个事件里提示一行，请用户把主会话设成 medium。
- 不设 monitor，不生成卷宗，也没有监督简报。轮中监督整套取消，没有 TICK、方向检查、DRIFT 和 BREAKING，也没有无人监管自动暂停。`baton wait` 只在一轮结束和额度不足时醒来。
- executor 一轮做到底。中途不为大修改交汇报，也不为决策请求停下，自己拿主意，把理由写进 log；只有缺权限、缺网络这类做不下去的情况才以 blocked 结束。整件任务做完写一份汇报，以 done 结束。
- conductor 只在 done（或 blocked、failed）时出场。它读汇报、`baton diff` 和 `baton` 亲跑的验证结果，给出 ACCEPT 或 REVISE；REVISE 时写明要改的点续跑，下一轮仍是做完再审。conductor 不亲手改代码，executor 因容量或网络失败时只重试或等待，不由 conductor 接手。
- 不花模型 token 的部分照旧，包括冻结、亲跑验证、影子仓库和回滚点、额度提醒。规则信号仍然计算，不叫醒 conductor，只附在轮末事件里。记忆照常推送，候选由 conductor 在审查时顺手核准，没有整理回合。

## 沿用 v1 的部分

`QUOTA_LOW` 提醒、项目 statusline、`steer` 和 `stop`、回滚点和 council 都照旧。statusline 在升级轮显示实际使用的模型，monitor 运行时也标出来。v1 里任务中途改配置下一轮就生效的做法取消，改走修正案。配置示例只列新增和改动的键，其余取 skill 自带的默认值。

## executor 的专用 profile

Hermes 的 profile 是一个独立的 HERMES_HOME（`~/.hermes/profiles/<name>/`），config.yaml、.env、SOUL.md、内置记忆、会话库、技能和凭据都各自一份。Baton 用按协议版本命名的 profile 跑 executor，例如 `baton-exec-v2`。`baton init` 发现它不存在时用下面的命令创建，已存在就跳过（重复创建会报错退出）。

```bash
hermes profile create baton-exec-v2 --no-skills --no-alias
```

不带 `--clone`、`--clone-from`、`--clone-all`，它们会把用户自己的 SOUL.md、USER.md、MEMORY.md 和 .env 拷进来，`--clone-all` 还会拷 auth.json；这三个选项和 `--no-skills` 也互斥。

创建后 `baton` 写入 SOUL.md 和 config.yaml，之后两者只读。Baton 升级协议时新建下一个版本的 profile，旧的不动；`baton start` 把 profile 名记进任务状态，任务的各轮固定用它，Hermes 的会话本来也按 profile 隔离。按项目变化的设置一律走命令行和环境变量，不写进这份共用的 config.yaml。config.yaml 写这几项。

```yaml
model: {provider: openai-codex, default: gpt-5.6-luna}
memory: {memory_enabled: false, user_profile_enabled: false, provider: ''}
tools: {tool_search: {enabled: "off"}}
display: {show_reasoning: false, streaming: false, inline_diffs: false}
mcp_single_query_discovery_timeout: 60
mcp_servers:
  baton-memory:
    command: ~/.baton/venv/bin/python
    args: ["-m", "baton_memory.server"]
    env: {MEM0_TELEMETRY: "false", BATON_TASK: "${BATON_TASK}", BATON_LEG: "${BATON_LEG}", BATON_LEG_STARTED: "${BATON_LEG_STARTED}"}
    timeout: 30
    tools: {resources: false, prompts: false}
```

每一项都有原因。写 model 让首启检查直接命中本地配置，上下文压缩这类辅助调用也解析到 openai-codex。profile 有自己的 memories/ 目录，用户的 USER.md 本来进不来；关掉两个内置记忆开关，是为了不让 executor 写进 memories/ 的内容在以后的会话里注入系统提示。Hermes 0.20.1 默认把所有 MCP 工具收进 tool_search、tool_describe、tool_call 三个桥接工具里，模型只看得到名字和一句说明，关掉之后 executor 才能直接看到 `mcp__baton_memory__memory_search` 和 `mcp__baton_memory__archive_search` 的完整说明。`chat -Q` 默认会把推理流和写文件后的 diff 也打到 stdout，display 三项关掉后 stdout 只剩最终回复。Hermes 单次运行默认最多等 MCP server 注册 15 秒，错过的 server 整轮都不在工具列表里，也不报错；调到 60 秒，健康的 server 一注册完就返回，不会多等。Hermes 只给 stdio MCP server 传 PATH、HOME 等白名单环境变量和配置里的 env，env 里的 `${VAR}` 由 Hermes 用自己进程的环境展开，`baton` 启动 Hermes 时设好这三个变量，server 就知道当前是哪个任务的哪一轮。`resources` 和 `prompts` 关掉，否则 Hermes 会多注册四个通用工具。

profile 的 .env 由 `baton` 每轮起 Hermes 前核对，必须为空。Hermes 启动时以覆盖方式加载它，里面若出现 `HERMES_EPHEMERAL_SYSTEM_PROMPT` 会顶掉 `baton` 传入的章程和常驻块，出现 `HERMES_IGNORE_RULES=1` 则 SOUL.md 和项目上下文文件都不加载。

登录不借用根目录的凭据。profile 没有 openai-codex 凭据时，Hermes 会回退读 `~/.hermes/auth.json`，本机那里的池子有 3 个不同的 ChatGPT 账号，0 号被 429 标记后 Hermes 还会自动换到另外两个；凭据池一旦在 profile 里落盘，来源为 manual 的账号刷新时只写 profile，根目录那份只能用一次的 refresh token 随即作废，用户自己的 Hermes 会掉登录。所以 `baton init` 之后由用户在 profile 里登录一次，账号选和 Codex CLI 相同的那个。

```bash
hermes -p baton-exec-v2 auth add openai-codex --type oauth
hermes -p baton-exec-v2 auth list openai-codex
hermes -p baton-exec-v2 auth remove openai-codex <从根目录带进来的条目>
```

`auth add` 写盘时会把回退读到的根目录 3 条连同新登录一起写进 profile，所以登录完马上用 `auth list` 找出带进来的那 3 条逐条删掉，只留新登录的一条，之后再跑任何 chat。`baton init` 在登录前后各算一次根目录 auth.json 的哈希，变了就报警。`baton doctor` 检查 profile 自己的 auth.json 里有且只有一条 openai-codex 凭据，账号 id 哈希与 Codex CLI 登录的一致，不满足就报 FAIL，`baton start` 也拒绝启动；一致时 `codex_quota.py` 读到的就是 executor 实际消耗的额度。hermes 后端下额度仍靠本机 Codex CLI 的 app-server 读取，codex 缺失时 doctor 报 WARN。

## 输出约定写进 SOUL.md

Hermes 的上下文文件分两类。项目级的只加载一种，按 `.hermes.md` / `HERMES.md`、AGENTS.md、CLAUDE.md、`.cursorrules` 的顺序取第一种命中的。AGENTS.md 从 git 根到工作目录逐级合并，链上任何一级有 AGENTS.md 就不再读 CLAUDE.md；CLAUDE.md 只看工作目录。`~/yuzz` 根目录有 AGENTS.md，所以 Bica 这类挂在外层仓库下的项目，executor 读到的是 vault 级的 AGENTS.md 加项目自己的 AGENTS.md，项目的 CLAUDE.md 读不到。`baton init` 用 Hermes 的函数实际拼一遍，把会加载的文件列给用户确认。实例级的只有当前 HERMES_HOME 下的 SOUL.md，排在系统提示最前面，当作 agent 的身份。另外，工具调用碰到子目录时，子目录里的 AGENTS.md、CLAUDE.md、`.cursorrules` 会附加到工具结果里。

输出约定放在 SOUL.md。往项目里放 `.hermes.md` 会把项目原有的 AGENTS.md、CLAUDE.md 整个盖掉，改项目的 AGENTS.md 又要动用户已跟踪的文件。SOUL.md 在 profile 里，不碰项目，所有项目共用一份，也就只写与项目无关的内容，包括角色与边界、小修改和大修改的记录方式、决策请求、记忆的用法、结尾的输出约定和 `leg-result.schema.json` 的全文。输出约定大致这样写。

```text
本轮最后一条回复以单独一行 BATON_RESULT_BEGIN 开始，接一个 JSON 对象，再以单独一行 BATON_RESULT_END 结束。
JSON 的字段为 status（done / decision / report / blocked）、summary、artifact、change_size（none / minor / major）、memory_used（本轮用到的记忆编号，没用到时为空数组）。
```

`leg-result.schema.json` 相应加上 `memory_used`，放进 required。保留的 codex 后端也用这份 schema，OpenAI 的严格结构化输出要求所有字段都在 required 里。

SOUL.md 另写三条硬规则。汇报和决策请求是交给 monitor 和 conductor 的当事人陈述，未完成项、失败和风险如实写，不套用项目上下文文件里的写作风格规则或 skill。不执行 git push、pull、fetch，不 ssh、scp、rsync 到远端，不在远端机器或付费 API 上启动任务，确有需要就写决策请求。项目的 CLAUDE.md 或 AGENTS.md 里关于调度和角色分工的条目（例如「写代码一律走 /baton」）由 conductor 处理，executor 只遵守其中的代码约定。

系统提示的存储和续接复用已用项目级 `.hermes.md` 在本机测过。Hermes 在会话第一轮拼好系统提示存进会话库，之后 `-r` 续接时，只要模型、provider、工作目录、平台四项不变，就原样复用，不重读文件。SOUL.md 走同一条拼装路径；换模型的那一轮按当时的文件重建，这一点来自读源码。

解析和重试按 Hermes 给子 agent 用的 OUTPUT CONTRACT 的思路做，jsonschema 校验，最多纠错一次，错误原样回传。BATON_RESULT 标记是 Baton 自己加的，用来在 stdout 里定位结果；解析取 stdout 里最后一对标记之间的内容，不合格就带着校验错误 `-r` 续跑一次，只要求重新输出结果，仍不合格判 FAILED。退出码非 0 时直接判 FAILED，错误文本在 stdout 或 stderr 的 `Error:` 行里。

SOUL.md 写入前先调用 Hermes 自己的注入扫描函数 `tools.threat_patterns.scan_for_threats(text, 'context')` 检查一遍，命中一处 Hermes 就会把整个文件换成 `[BLOCKED: ...]` 占位。

## 每轮的上下文

同一个 Hermes 会话会跨很多轮。Luna 在 Codex OAuth 下的上下文约 27 万 token，Hermes 对 openai-codex 上的 gpt-5.6 系列把压缩阈值自动上调到 85%，用到约 23 万 token 时压缩。v1 只在首轮消息里带简报，从第二次压缩起，允许和禁止的路径、验收标准就只剩摘要。v2 把每轮的上下文分三处放。

- SOUL.md 放跨项目不变的协议，存在会话的系统提示里。
- 环境变量 `HERMES_EPHEMERAL_SYSTEM_PROMPT` 放本任务的章程摘要（目标、允许和禁止的路径、逐条验收标准、修正案编号）、项目设置（语言、大修改阈值、验证命令、`.baton/` 路径）和常驻块。Hermes 在每次 API 调用时把它追加到系统提示末尾，不写入会话库，不参与压缩，也不经注入扫描。这些内容取自启动时的配置快照，修正案生效后下一轮按新快照生成。
- 本轮消息放首轮的完整简报，或之后各轮的裁决、决策、纠偏原文，加上本轮推送的记忆。

## 调用方式

| v1（Codex CLI） | v2（Hermes） |
|---|---|
| `codex exec` | `sandbox-exec -f <规则> hermes -p baton-exec-v2 chat -Q --query=<prompt> --provider openai-codex -m <model> --reasoning <effort> --source tool --yolo -t <toolsets>`，在项目目录运行，stdin 接 /dev/null，带上 ephemeral 提示和三个 BATON_* 变量 |
| `codex exec resume <thread>` | 同上加 `-r <session_id>` |
| thread id | stderr 末行 `session_id: <id>`，每轮重新读；压缩后 id 会变，`-r` 旧 id 时 Hermes 会沿压缩链续到最新的会话 |
| `--output-schema` | SOUL.md 里的输出约定、标记解析、一次纠错续跑 |
| 事件流 JSONL | 规则信号和轮中检查只读打开 profile 的 state.db（`mode=ro`），按 parent_session_id 跟到压缩后的子会话 |
| `--sandbox workspace-write` | `sandbox-exec` 的写入规则，见上文的 warning |
| 工具全开 | `-t file,terminal,todo,baton-memory` |

提示词一律用 `--query=<prompt>` 单个参数传。Hermes 在 argparse 之前扫描整条命令找 `-p`，扫描时不知道 `-q` 带值，提示词里出现 `-p` 字样会被误认。`-t` 里写 MCP server 的原名 `baton-memory`，写成 `mcp-baton-memory` 会在启动校验时往 stdout 打一行 Unknown toolsets 告警。本机 Hermes 没有配 web 搜索后端，`network_access=true` 时 `baton` 先确认工具列表里真有 web_search，没有就提示先配后端。

Hermes 在执行每批工具调用之前就把带 tool_calls 的 assistant 消息写进 state.db，state.db 是 WAL 模式，所以规则信号和 monitor 能读到正在进行的这一轮跑过哪些命令、退出码是多少。读取用 Hermes venv 的 Python，本机系统 Python 带的 SQLite 3.51.0 落在 Hermes 标记为 WAL 有缺陷的版本区间里。runner 每轮前后比较 `git for-each-ref refs/remotes`，并扫描会话库里的终端命令，出现 git push、ssh、scp、rsync 这类外部动作就在事件和卷宗里单列。

每轮启动前 `baton` 先用 profile 的配置拉起一次 baton-memory 做 initialize 和 tools/list（这次带 `BATON_MCP_PREFLIGHT=1`，Hermes 不转发这个变量，所以正式实例看不到它），几秒内不成功就不开跑。正式实例在应答 tools/list 时写 `runs/<task>/leg-NNN.mcp-ready`，带时间戳和 Hermes 的进程号；runner 开轮前删掉旧标记，轮末标记缺失或时间晚于开轮 60 秒，事件和卷宗里注明「本轮没有记忆工具」。

续接和换模型已在本机测过，Luna 起的会话下一轮用 Sol 续接，Sol 能回答前文内容。

## 分级执行

默认每轮用 `executor.model`（gpt-5.6-luna）。下面三种情况，`baton` 把下一轮升级到 `executor.escalation.model`（gpt-6.1-sol），这一轮结束后降回。

- 连续 2 次裁决为 REVISE，不论汇报编号；
- 连续 2 轮 BLOCKED 或 FAILED，只算 summary 归类为「方法卡住」的，provider 过载、额度、网络、缺权限、等用户输入都不算；
- conductor 用 `baton resume --escalate` 指定。executor 在决策请求里写了「同一问题修复失败 3 次」时，由 conductor 读完决定要不要这样做。

连续 REVISE 的计数在出现 APPROVE 或 ACCEPT 后清零，清零后再累计到 2 可以再升一次；Sol 那一轮之后仍没通过，不再自动升级，交 conductor 决定继续用 Sol、拆分任务还是问用户。每个任务自动升级最多 3 次。会话不换，升级的那一轮看得到 Luna 前面的工作。Sol 没有 85% 这项上调，窗口不足 512K 时阈值是 75%，所以 Luna 会话用到 75% 以上时，升级轮开头会先压缩一次，Sol 看到的是摘要。换模型的那一轮会按当时的文件重建系统提示，项目的上下文文件已在锁定范围内。每轮用了哪个模型、为什么升级，记在任务状态里。

## 容量不足的自动重试

Codex 经常报 **model at capacity**（「Selected model is at capacity. Please try a different model.」），有时出在轮中，有时出在会话压缩那一步。以前这类失败都以 FAILED 叫醒 conductor，再由它手动 resume，一个下午能来回好几次。v2 把这件事交给 runner 自己处理，executor 的两种后端和 monitor 的 Codex 运行方式都适用，lean 模式也一样。

runner 按下面几条规则处理，不叫醒 conductor。

1. 判定。一次调用以失败返回、错误文本里含 `at capacity`，算一次容量失败。别的失败照旧以 FAILED 结束这一轮。
2. 立即重试。容量失败返回后马上重试，不等待。一轮里最多连续立即重试 3 次。
3. 延后重试。3 次立即重试都以容量失败返回，等 15 分钟再试 1 次，只试这一次。
4. 结束。延后的那次仍是容量失败，这一轮以 FAILED 结束，事件里写明「容量不足，已自动重试 4 次」和每次的时间。
5. 续跑方式。重试用同一个会话续跑，带一条固定的短消息，让执行者从中断处接着做，原来那条指挥消息不重发。会话还没建起来就失败的，原样重跑这一轮的命令。
6. 清零。某次重试跑出了新的模型输出、之后又碰到容量失败，计数从头算，免得一轮跑几个小时、中间偶尔撞上两次就被判失败。
7. 状态。重试和等待期间这一轮仍是 running，任务状态里记 `capacity_wait`（第几次、下次重试时间）。`baton status` 和 statusline 显示「容量不足，N 分钟后重试」；`baton wait` 只在第 4 条的最终失败时报 FAILED。
8. 计时与计数。等待的 15 分钟不计入无人监管时长，也不触发停滞类规则信号。容量失败不计入分级执行的「连续 FAILED」。
9. conductor 收到第 4 条的 FAILED 后照旧只等待、重试或问用户，不接手写代码。

`executor.capacity_retry` 控制这组数字，默认 `{"immediate": 3, "delayed": 1, "delayed_minutes": 15}`；monitor 用同一组设置。

## 记忆的依据

DolphinBench（arXiv 2609.24971，作者来自 Mem0）用动作来考记忆，出题时避开「按我平常的习惯来」这类点明存储偏好的提示，agent 要自己意识到需要回忆，再把记住的内容落实到工具调用上。论文里有三点和 Baton 的记忆设计直接相关。

第一点是它的出发点（§1、§2.1）。问答式评测替系统完成了最难的一步，也就是 ==意识到需要回忆==（know to remember）。所以 Baton 的召回以每轮推送为主，不全靠 executor 自己想起来去查。

第二点是 §4 的结论，记忆效果取决于模型、harness 和记忆系统三者的组合。所以 Baton 照这套配置的整体结构来搭，executor 侧沿用 Hermes + Luna，记忆侧沿用它在 harness 一侧的做法，不只挑一个记忆库。

第三点是 §4、§6 的结论，更准不一定更慢。Baton 据此把算力花在写入，由 monitor 提炼、conductor 核准，读取只做嵌入检索和关键词匹配，不调用对话模型。

作者公开的评测仓库和运行记录交代了这套配置在 harness 一侧的做法，下面四段先写它的做法，再写 Baton 的对应设计。Mem0 托管平台内部用的算法版本，仓库没有记录。

召回。每轮开始自动检索 10 条，以 `<memory-context>` 块附在用户消息后，块首有一句系统说明，标明这些是召回的记忆（原文 NOT new user input），并要求当作权威参考数据。另有只读的 `search_memories(queries[])`，工具说明要求每个事实写一条短查询，不要把几件事拼成一条。Baton 每轮开始推送 10 条，另给 MCP 工具 `memory_search(queries)`，说明照搬；推送块的权威性放在章程和本轮指令之下，冲突时以后者为准。

常驻与原文。Hermes 内置的 MEMORY.md（2,200 字符）和 USER.md（1,375 字符）每轮常驻，原始会话的全文检索 `session_search` 同时开着。Baton 用 2,200 字符的项目常驻块每轮带上，`archive_search` 全文检索 `.baton/` 下的原始记录。

写入。评测分摄入和测试两个阶段。摄入时由 agent 侧写入，每轮按一段抽取指令以 infer=True 抽取事实，要求原样保留数值、日期和原话，写明是谁说的、谁做的，助手做过的事不能写成用户的规则；agent 也自己维护 MEMORY.md。测试时写工具和自动写入都由代码关掉，仓库原话是光在提示里叫 agent 别写不够。每次写入同步发出并记下回执，摄入全部结束、开测之前统一等后台处理完，仓库原话是一轮结束不能证明后台写入已经完成。Baton 写读交替，没有单独的摄入阶段，executor 始终不能写，这是分权的要求；写入由 monitor 按同样的抽取要求提炼并给建议，conductor 在裁决里核准，每轮入账即回执。

存储。评测用的是 Mem0 托管平台，带 LLM 抽取和多信号排序，按 Mem0 的博客和迁移文档，新算法只追加，状态变了新旧事实并存，由检索排序把相关且较新的排前。Baton 用本地的 mem0 开源版，以 infer=False 原样写入，实体链接和 BM25 都不生效，语义检索之外的关键词召回由 `baton` 查台账完成。台账同样只追加，被取代的条目不再推送，`memory_search` 仍会返回它，排在取代它的条目之后并标「已被 Mxxx 取代」。评测每个 persona 从空库开始、每次运行用一次性的 Hermes profile；Baton 每个项目一个独立的库，executor 用长期存在、按协议版本命名的共用 profile，隔离靠关掉内置记忆和不给 session_search。

## 记忆的三层

常驻块由台账里标了 `pinned` 的条目按固定格式渲染，字符预算 2,200，由 `baton` 计算，经 ephemeral 提示每轮带上，长会话压缩时也不会丢。放这个项目最常用的构建和测试命令、最要紧的几条约定和坑。整理回合之前超出预算时，按 pin 的先后截断，并在事件里提示。

事实库以项目外的**台账** `memory/ledger.jsonl` 为准，mem0 是可以重建的检索索引。类型有六种，`command`（构建、测试、启动的正确方式）、`convention`（代码约定与目录职责）、`pitfall`（踩过的坑和修法）、`precedent`（裁决要点和 conductor 的决策）、`map`（模块入口和调用关系）、`event`（executor 做过的事，例如「R005 把解析器拆成流式模块」）。`event` 只说明发生过什么，不能当规则用。台账放在项目外，切分支、`git stash`、`git checkout -- .` 都碰不到它；项目里的 `.baton/memory/ledger.export.md` 是 `baton` 生成的只读导出。

原始档案是 `.baton/` 下的 log.md、reports/、reviews/、decisions/、各任务的 brief.md、`runs/*/leg-*.verify.json` 和台账导出。事实库的每条都带出处编号（如 `R003`、`D002`、`log.md:118`），需要精确值时回原文查。

已经用过 v1 的项目（例如 StateSep）台账是空的。`baton memory seed` 从现有的 log.md、reviews、decisions 和项目的 AGENTS.md 或 CLAUDE.md 生成候选，由 monitor 出一次建议、conductor 核准，写出初版台账和常驻块。

## 记忆的写入

候选有三个来源。executor 在汇报末尾的「记忆候选」一节提出条目；conductor 经 `baton decide` 记下的决定自动成为 precedent 候选；monitor 也可以从卷宗里自己提炼，例如把 REVISE 的原因写成一条 pitfall。`baton` 把候选编号为 C1、C2…，monitor 在简报里对每条写 approve、rewrite 或 reject 的建议，自己提炼的条目标为 new。conductor 的裁决默认照单采纳（`baton rule` 的 `--memory as-advised`），不同意的逐条改写。

`command` 类候选先由 `baton` 机械核实。在本轮会话库的终端调用里，去掉开头的 `cd <目录> &&` 后与候选逐字相同，原调用不含管道、分号、`||`、后台运行和写文件的重定向，退出码为 0，才标 verified；其余一律以 claimed 入库，推送时标「executor 自述，未核实」。与验证命令相同的命令，以 `baton` 亲跑的结果为准。

每份简报在记忆上只做轻的一步，对本轮证据支持的候选逐条给建议。合并重复条目、维护常驻块、标 superseded 这些整理工作放到单独的整理回合，在任务结束时或每 3 份汇报后由 `baton monitor --curate` 跑一次，模型由 `memory.curation_model` 配置，默认 sonnet。整理回合的卷宗装台账里全部 active 和 superseded 条目、常驻块全文和各条字符数、上次整理以来的裁决要点，输出 merge、supersede、pin、unpin、retire 几种操作，conductor 用 `baton rule --curation` 确认后由 `baton` 校验并追加台账。提炼规则照评测用的抽取指令改写成编码版。命令、路径、版本号、阈值、错误信息原样保留；写明出自谁（用户、conductor、monitor 或 executor 自述）；同一件事的细节放在一条里，无关的分开写；executor 的自述只有核实过才能写成 convention 或 command。

入库分两步。`baton` 持台账的写锁，把核准的条目追加到台账并 fsync，每条记编号、类型、正文、出处、来源轮次及其终点快照、1 到 3 个锚点、时间、状态，以及上一条记录的哈希；链头另存一份，用临时文件加改名的方式写入。台账写入成功就是回执，之后就可以 resume。然后用 `m.add(text, agent_id="baton", metadata={"ledger_id": ..., "kind": ...}, infer=False)` 写进 mem0。mem0 里只存不会变的 metadata，状态只在台账里。mem0 写入失败（例如 Ollama 不在线）的条目标 `index_pending`，照常能经关键词一路召回，由 `baton memory reindex` 按 ledger_id 去重后补建。

条目只追加。被取代时追加一条 superseded 记录并写明取代它的编号，退役追加 retired，都不删除。`baton rollback` 成功后，来源快照晚于回滚目标的条目统一标 suspended，不推送、不检索，列进下一份卷宗，monitor 逐条建议恢复或退役，由 conductor 裁定。

## 记忆的读取

每轮开始 `baton` 组几条短查询，分别来自简报目标、本轮指令（裁决或决策原文）、上一轮改过的路径和简报范围内的路径。检索走两路。

语义一路调 mem0 的 `search`，filters 只写实体键和类型，例如规则类用 `filters={"agent_id": "baton", "kind": {"in": ["command", "convention", "pitfall", "precedent"]}}`，`top_k=30`，其余类型另查一次。参数一律用 top_k，mem0 v2 会把 limit 静默吞掉；实体键 `agent_id` 必须写进 filters，库已按项目分开，这里用常量即可。qwen3-embedding 要求查询带一行任务说明，`baton` 给查询加上 `Instruct: 根据问题检索相关的项目记忆条目` 和 `Query: <查询>` 两行，写入的条目正文不加。结果按 ledger_id 回台账，按当前状态处理。

关键词一路直接查台账，路径、函数名、命令、错误码这类标识符凭字面召回。mem0 的 BM25 只给语义候选加分，召回不了新条目，对中文也基本无效，而且要另装 fastembed 并从 Hugging Face 下载模型，所以不装，这一路由 `baton` 自己做。

推送只取 active 条目。两路融合后取 10 条，event 最多 2 条，map 最多 2 条，其余位置按相关度排，不为凑某一类拿低分条目填数；已经在常驻块里的不再推送。锚点是「路径加标识符」的组合，命令类条目锚到它调用的脚本或配置文件；关联文件被删或改名时标「文件已不存在」，标识符在文件里找不到时标「相关代码已改动，使用前核对」，文件只是内容变了不加标记。

推送块仿照 `<memory-context>` 的格式。

```text
## 相关记忆（检索结果，供参考；与章程或本轮指令冲突时以后者为准）
撤回：M017 已由 M052 取代，忽略此前轮次推送的 M017
- [M042 · pitfall · R003] 集成测试要先 docker compose up db，否则 tests/integration 全部超时
- [M052 · command · log.md:88 · 已核实] 单测命令 pytest -q tests/unit，CI 用同一条
```

推送块存进会话后会一直留在历史里，所以任务状态记下本会话推送过哪些编号，其中有条目后来变成 superseded、retired 或 suspended 时，下一轮推送块开头逐条撤回。

轮中的检索交给 MCP server `baton-memory`，只有两个只读工具。

- `mcp__baton_memory__memory_search(queries)` 一次传多条查询，每条返回 10 条，active 在前，superseded 排在取代它的条目之后并注明；retired 和 suspended 不返回。工具说明写明每个事实单独写一条短查询，不要把几件事拼成一条。多条查询批量嵌入，整次调用 20 秒内必须返回，超时的查询改走关键词一路。
- `mcp__baton_memory__archive_search(query)` 在原始档案里做全文检索，返回文件名、行号和上下文。

server 把每次返回的条目编号写进 `runs/<task>/leg-NNN.memory-calls.jsonl`，卷宗以这份记录为准，executor 自报的 `memory_used` 只作对照。三个 BATON_* 变量没展开时，server 只提供检索、不写记录，并在返回里注明。SOUL.md 里写明，简报没写清的细节先看推送的记忆，再用这两个工具查，查不到才提决策请求。

## 记忆的隔离与只读

executor 没有写记忆的工具，MCP server 只暴露两个读工具，台账、链头和向量库都在项目外，sandbox-exec 的规则也不让 executor 写 `~/.baton/`。推送和 MCP server 读台账时持读锁并校验哈希链，校验失败时 server 写 `runs/<task>/leg-NNN.alert`，runner 轮询到就发 VIOLATION。即使有人绕过 `baton` 直接往向量库里写，检索结果也要回台账查到编号才会返回。

Hermes 自带的 mem0 插件不启用。它在每轮结束后把本轮消息和回复以 infer=True 写进 mem0，需要一个能跑抽取的对话模型，没有配置项能关掉；它的 `mem0_update` 和 `mem0_delete` 不限作用域，executor 搜到编号就能改写或删除任何条目。这两点和 DolphinBench 仓库里测试阶段的做法冲突，那里的写工具和自动写入都由代码关掉。Baton 用自己的推送加只读 MCP 工具，在 executor 这一侧起的作用和插件在评测里一样。

## 记忆的部署

mem0ai 要求 Python 3.10 以上，本机的 `python3` 是 3.9.6。Baton 在 `~/.baton/venv` 建一个独立环境，用本机现成的 uv CPython 3.11 创建（已验证能建 venv），装 `mem0ai>=2.0.16,<3`、`ollama` 和 `mcp>=1.26,<2`，再在 venv 的 site-packages 里放一个指向 skill 脚本目录的 .pth 文件，让 `baton_memory` 包可以导入，skill 升级后重写。mem0 的下限取 2.0.16，更早的版本在缺 ollama 包时会卡在 stdin 上等输入，然后结束整个进程；mcp 钉在 1.x，2.x 去掉了 FastMCP。baton.py 本身仍兼容 3.9，记忆相关操作和 MCP server 都在这个环境里以子进程运行。

Ollama 已用 install.sh 装好（0.40.3），`ollama` 命令链接在 `~/.local/bin`，登录时由 LaunchAgent 以隐藏方式启动应用。嵌入模型用 `qwen3-embedding:0.6b`（639MB，1024 维，多语言），记忆条目以中文加代码为主；这台 Mac 只有 8 GB 内存，4b 以上的嵌入模型不考虑。配置里的模型名必须带 tag，不带 tag 时会被当成 `:latest`，也就是 4.7GB 的 8b 版本。本机代理是 TUN 加 fake-ip 模式，下载地址被解析到 198.18 段，`ollama pull` 会被 Ollama 的跳转检查拒绝；模型改用 curl 从 registry 下载 manifest 和 blob，校验 sha256 后放进 `~/.ollama/models`，`baton` 的安装命令要内置这条退路。

实测这个模型按默认 4096 的上下文加载后常驻约 2.4 GB，上下文 1024 时约 1.4 GB，512 时约 1.0 GB；冷加载约 0.8 秒，加载后单次嵌入几十毫秒。记忆条目和查询都短，`baton` 用 Modelfile 从它派生一个 `baton-qwen3-embed`（`PARAMETER num_ctx 1024`），mem0 配置里用派生后的名字。

> [!example]- mem0 的配置与运行细节
> embedder 用 ollama 和派生出的 `baton-qwen3-embed`，`ollama_base_url` 写明 `http://127.0.0.1:11434`，不依赖环境变量；发给 Ollama 的请求带 `keep_alive: "10m"`，一轮之内不反复冷加载，空闲后又能把这 1.4 GB 还给系统。vector_store 用本地 qdrant，路径是项目外的 `memory/qdrant`，collection 名带上模型和维度，例如 `baton_qwen3emb06b_1024`，`embedding_model_dims` 写 1024。维度写错时 mem0 自己不校验，本地 qdrant 下第一次检索抛 numpy 的 shapes not aligned，第一次写入抛 Vector dimension error，这类错误要在 doctor 和日志里报出来，不能当成 Ollama 不在线静默退回。换嵌入模型时建新 collection，`baton memory reindex` 用台账里的条目全量重灌。
>
> llm 段填一个 ollama 占位，infer=False 永远不会调用它；不填的话 mem0 默认构造 OpenAI 客户端，没有 key 会直接报错。`history_db_path` 写成项目外 `memory/` 下的绝对路径。
>
> `MEM0_TELEMETRY=false` 和 `MEM0_DIR` 必须在 import mem0 之前生效。telemetry 开着时 mem0 会在 `~/.mem0/migrations_qdrant` 再开一个本地库且不关闭，两个项目同时用就会撞锁。`baton` 自己起的子进程直接设环境变量；MCP server 的 `MEM0_TELEMETRY` 写死在 config.yaml 的 env 里，`MEM0_DIR` 由 server 按继承来的工作目录算出项目 id 后，在 import mem0 之前自己设。
>
> 本地 qdrant 同一目录同时只能有一个 QdrantClient，同一进程里也不行。每个项目一个库，`baton` 保证同一项目同时只有一轮在跑，写入只在两轮之间做。MCP server 每次调用时打开库，在 finally 里调 `m.vector_store.client.close()` 释放锁，`m.close()` 只关 SQLite，替代不了这一步。外面再套一把文件锁，最多等 5 秒，拿不到就当次走关键词一路。
>
> mem0 的 Ollama 嵌入器在构造时就会连 Ollama，模型不在本地还会不设超时地自动下载。所以每个构造 mem0 的地方（轮首推送、写入、MCP server 每次调用）都先以 2 秒超时请求 `/api/tags`，精确匹配带 tag 的模型名；嵌入放进 10 秒超时的线程。Ollama 不在线、缺模型或超时，当次走关键词一路，并在返回里注明。

`baton doctor` 依次检查 Ollama 在线、模型已在本地、嵌入一条文本的长度等于配置维度、已有 collection 的维度一致，再用 profile 的配置直接拉起 baton-memory 跑一次 tools/list，确认 executor 的工具列表里直接出现那两个 MCP 工具。

## 配置

```json
{
  "conductor": {"model": "opus", "council_for_major_decisions": true},
  "monitor":   {"model": "gpt-5.6-sol", "reasoning_effort": null, "final_reasoning_effort": null, "timeout_minutes": 40},
  "executor": {
    "backend": "hermes",
    "hermes_profile": "baton-exec-v2",
    "provider": "openai-codex",
    "model": "gpt-5.6-luna", "reasoning_effort": "xhigh",
    "toolsets": ["file", "terminal", "todo", "baton-memory"],
    "network_access": false,
    "escalation": {"model": "gpt-6.1-sol", "reasoning_effort": "xhigh",
                   "after_revise": 2, "after_blocked_or_failed_legs": 2,
                   "max_per_task": 3},
    "capacity_retry": {"immediate": 3, "delayed": 1, "delayed_minutes": 15}
  },
  "memory": {
    "backend": "mem0",
    "embedder": {"provider": "ollama", "model": "qwen3-embedding:0.6b", "num_ctx": 1024, "dims": 1024},
    "inject_top_k": 10,
    "core_max_chars": 2200,
    "curation_model": "sonnet"
  },
  "freeze": {"paths": []},
  "mode": "standard",
  "supervision": {
    "test_command": "",
    "first_check_minutes": 10, "max_check_minutes": 60, "unhandled_stop_minutes": 30,
    "alerts": {"delete_ratio": 3, "stall_minutes": 20, "repeat_fail": 3}
  }
}
```

`mode` 取 `standard` 或 `lean`。`lean` 是一组覆盖，`executor.model` 为 gpt-5.6-luna、`executor.reasoning_effort` 为 medium、`executor.escalation` 关闭、`conductor.reasoning_effort` 为 medium、monitor 与轮中监督关闭、`supervision.max_unsupervised_minutes` 为 0；项目层显式写的键仍然优先。`monitor.reasoning_effort` 为 null 时用所选 CLI 的默认思考深度，`final_reasoning_effort` 为 null 时终审沿用前者。`supervision` 里的分钟数对应「轮中监督」一节的默认值。`freeze.paths` 是项目级常驻的锁定路径，和简报范围块里的 `frozen` 合并使用。`memory.backend` 设成 `local` 时不用 mem0 和 Ollama，`memory_search` 只查台账，`archive_search` 不受影响；两种后端都注册 baton-memory。

v1 的 `judge.model` 指的是主会话，v1 的 `baton init` 还把 `executor.model: gpt-6.1-sol` 等键写进了项目的 `baton.json`（例如 StateSep）。v2 读到没有 `executor.backend` 的旧文件时按 codex 后端运行，并提示迁移；`baton init --migrate` 把它改写成 hermes 后端和 gpt-5.6-luna。是否缺 `conductor` 一节按项目原文件判断，不看合并后的配置；缺的时候 `judge.model` 用作 conductor 的模型，monitor 取默认值；v2 早期草案里的 `judge` 一节读到时按 `monitor` 处理并提示改名；`judge.council_for_major_decisions` 等同于 `conductor.council_for_major_decisions`。codex 后端保留 v1 的 `service_tier`、`disable_fast_mode`、`sandbox`。

## 实施计划

- [ ] 前置 · 修改 [[CLAUDE|~/yuzz/CLAUDE.md]] 的 Baton 一段（内容见下方待决事项），等你同意后再改；doctor 发现它仍写着「交给 `model` 设为 judge.model 的子 agent」时报 FAIL
- [ ] Ollama · 按你选定的方式安装，`ollama pull qwen3-embedding:0.6b`，确认常驻方式，doctor 的记忆检查全部通过
- [ ] executor 环境 · 创建 baton-exec-v2，写入 config.yaml 和过了注入扫描的 SOUL.md，引导用户登录并删掉带进来的根目录条目，doctor 检查凭据和账号，.env 为空检查
- [ ] 项目边界 · 按最近的 baton.json 定项目目录，影子仓库与排除规则确认，项目外状态目录，列出 Hermes 实际会加载的上下文文件
- [ ] 制衡 · 范围块解析、两级冻结与段级哈希、60 秒核对、`restore-frozen`、配置快照与 `amend --config`、pytest 逐条验证、`amend` 与 `--adopt`、tty 确认的 `--user-approved`、续跑门槛
- [ ] monitor · 卷宗、代码快照和 MONITOR_PROMPT.md 的生成，轮末自动拉起，Codex CLI 与无头 Claude Code 两种运行方式及隔离参数，简报、轮中检查、整理三套 schema，MONITOR_FAILED 与重跑，账本
- [ ] 裁决 · `rule` 与逐条回应的校验、`--memory as-advised`、`--rerule`、`--curation`，`decide`，续跑门槛，`close` 的快照核对、验收测试全过和驳回清单
- [ ] 轮中监督 · 规则信号与 ALERT、方向检查的退避间隔、DRIFT 与 BREAKING 事件、`supervised --dismiss`、未处理满 30 分钟结束本轮、monitor 连续失败时退回 TICK
- [ ] executor 后端 · Hermes 调用、sandbox-exec 规则、BATON_RESULT 解析与一次纠错续跑、MCP 预检与 ready 标记、读 state.db 取终端调用、外部动作扫描，保留 codex 后端
- [ ] 分级执行 · 触发条件与清零、FAILED 与 BLOCKED 分类、每任务上限，statusline 显示实际模型
- [ ] 容量重试 · runner 识别 `at capacity`，失败返回后立即重试、最多 3 次，再 15 分钟后试 1 次，有新输出后计数清零，`capacity_wait` 状态与 status/statusline 显示，等待不计入无人监管时长，codex、hermes 后端与 monitor 共用
- [ ] 记忆 · `~/.baton/venv` 与 .pth、mem0 配置、带写锁和哈希链的台账、`verify` 与 `reanchor`、`index_pending` 与 `reindex`、常驻块渲染、两路检索与上限、锚点、撤回、MCP server、command 核实、suspended、`seed`、整理回合
- [ ] 模板 · 简报加范围块，report.html 加「记忆候选」「申辩」两节、「请指挥重点审阅」改为「请重点核对」，leg-result.schema.json 加 memory_used，简报、轮中检查与整理三套 schema，MONITOR_PROMPT 模板，`references/monitor.md`，改写 review（改为读简报、写裁决）、decision、supervision
- [ ] 配置迁移 · `conductor.*`、`monitor.*`、旧 `judge.model` 兼容、`baton init --migrate`
- [ ] 测试 · 用假 `hermes`、`codex`、`claude` 脚本覆盖调用与解析，规则信号与退避间隔、逐条回应校验、冻结与恢复、裁决账本、续跑门槛、升级、检索融合与上限、台账校验与修复、配置迁移各有单测
- [ ] 文档 · SKILL.md 与 README

## 实现要点

monitor 的启动参数。`monitor.model` 是 GPT 系模型时用 Codex CLI，是 opus、sonnet、haiku、fable 时用无头 Claude Code，整理回合按 `memory.curation_model` 同样选择。两种方式的工作目录都是项目外的卷宗目录，不在任何 git 仓库里，卷宗生成后算哈希并设为只读，monitor 结束后重算，不一致就作废这份简报。monitor 用 setsid 脱离会话运行，硬超时取 `monitor.timeout_minutes`。

Codex CLI（本机 0.162）用下面这组参数。`--ignore-user-config` 不读 `~/.codex/config.toml`，用户配的 MCP server、模型和沙箱设置都不生效，登录仍用 `~/.codex/auth.json`，额度因此记在和 executor 同一个账号上；`--ignore-rules` 不读 execpolicy 规则；`-s read-only` 让它能跑 grep、cat 这类只读命令，写不了文件。不传 `model_reasoning_effort` 时用 Codex 的默认思考深度。

```bash
codex exec --ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check \
  -s read-only -C <卷宗目录> -m <monitor.model> [-c model_reasoning_effort=<值>] \
  --output-schema <简报 schema> -o <卷宗目录>/brief.json - < MONITOR_PROMPT.md
```

`--help` 没有说明 `~/.codex/AGENTS.md` 这类全局指令文件在这组参数下是否仍会加载。`baton doctor` 用同一组参数跑一次探针，要求模型原样输出它收到的指令里是否出现那份文件的第一行，出现就报 WARN，此后的卷宗首行注明 monitor 带着用户的全局指令。

无头 Claude Code 默认会继承本机设置里的 bypassPermissions，会加载用户级的 codex MCP 和 claude.ai 连接器，还会把 ~/.claude/CLAUDE.md 和工作目录往上每一级的 CLAUDE.md 装进上下文，`--tools` 只裁内置工具，这些都已实测。所以用下面这组参数。

```bash
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 ENABLE_CLAUDEAI_MCP_SERVERS=false \
claude -p --safe-mode --strict-mcp-config --setting-sources "" --permission-mode dontAsk \
  --tools Read,Grep,Glob --add-dir <卷宗目录>/code --model <monitor.model> \
  --no-session-persistence --output-format json --json-schema <简报 schema>
```

`--bare` 不能用，它只认 API key，本机是 claude.ai 账号登录。简报取返回对象里的 `structured_output`，`subtype` 不是 success 或 `is_error` 为 true 时算失败。`baton doctor` 用同一组参数加 `--output-format stream-json --verbose` 跑一次探针，核对 init 事件里工具只有 Read、Grep、Glob、StructuredOutput，没有 MCP server，权限模式为 dontAsk；再在临时的上级目录里放一份写着口令的 CLAUDE.md，输出里出现口令就判 FAIL。

两级冻结的细则。锁定级按段或按键算哈希，pyproject.toml 只锁 `[tool.pytest.ini_options]`，setup.cfg 只锁 `[tool:pytest]`，package.json 只锁 `scripts.test` 和 jest、vitest 键，conftest.py、pytest.ini、tox.ini 整文件锁定。冻结文件只在影子仓库的受控文件里找，不进 `.venv*`、`node_modules`。验收目录只允许新增 `test_*.py`、`*_test.py`、`*.spec.*`、`*.test.*`；项目任何位置新建 conftest.py、pytest.ini、tox.ini、setup.cfg、pyproject.toml、`*.pth`、`sitecustomize.py`，或 jest、vitest、mocha 的配置，都按 VIOLATION 处理。项目内的 Hermes 上下文文件包括 `.hermes.md`、`HERMES.md`、AGENTS.md、CLAUDE.md、`.cursorrules`、`.cursor/rules/*.mdc`，改动或新建都按 VIOLATION 处理。

验证的细则。pytest 命令由 `baton` 在末尾追加 `--junitxml=<runs>/leg-NNN.junit.xml -rA`，计数包括 passed、failed、skipped、xfailed、xpassed、error、deselected。用户命令带 `-x` 时另跑一次不带 `-x` 的统计，或在 verify.json 里注明计数不完整。非 pytest 命令只记退出码和输出末尾，卷宗写明「计数不可得」。

台账的校验与修复。所有写入方都持 `ledger.lock` 的排他锁，在锁内重读链尾、追加、fsync，再写链头；读取方持共享锁。校验分三种情况。链完整、存档的链头是链上某条记录的哈希，说明崩溃后链头没跟上，自动前移并记日志。末行不完整且在存档链头之后，截掉这一行。其余情况才判 VIOLATION。`baton memory verify` 打印第一条坏记录，`baton memory reanchor` 在用户的终端里确认后重设链头。

> [!question] 待你决定
> Ollama 用官方应用还是 install.sh 安装，是否开机自启。baton-exec-v2 用哪个 ChatGPT 账号登录，建议和 Codex CLI 相同，额度显示才准。[[CLAUDE|~/yuzz/CLAUDE.md]] 的 Baton 一段在 v2 上线前必须改，否则主会话会照旧自己审阅或派自己写提示的子 agent 审阅，绕开 monitor。要改的有六处。开头一句改成主会话是指挥和唯一的决策者，每份汇报先由 `baton` 拉起的 monitor 出监督简报，主会话读简报后用 `baton rule` 裁决，不自己审代码；项目根改为从当前目录往上第一个含 `baton.json` 的目录，不越过 git 根；主会话模型对照 `conductor.model`，删去 `judge.model` 的说法，monitor 的模型由 `monitor.model` 决定；council 开关改为 `conductor.council_for_major_decisions`；执行者默认改为 Hermes 上的 gpt-5.6-luna、难轮升级 gpt-6.1-sol，`baton init` 默认值的描述同步；JSON 示例换成 v2 的键。

> [!warning] 首次运行要确认的几件事
> 下面几项来自读源码，还没在本机跑过。sandbox-exec 规则下 Hermes 能否正常读写会话库、刷新凭据、写缓存；baton-exec-v2 的 SOUL.md 是否作为身份块出现在存下的系统提示开头，且没有 `[BLOCKED:`；profile 内登录并删掉带进来的条目后，`auth list` 只剩一条，根目录 auth.json 没有变化，刷新和 429 只改 profile 自己的文件；executor 的工具列表里直接出现两个 MCP 工具；带 profile 时 ephemeral 提示照常生效；monitor 两种运行方式的参数和订阅登录能一起工作，探针通过，Codex 方式下 `~/.codex/AGENTS.md` 是否仍被加载；gpt-5.6-sol 在默认思考深度下能否按 schema 交出简报，轮中检查的误报多不多；executor 在实际编码任务里遵守 BATON_RESULT 约定的情况。另有几项已经实际跑过。无头 Claude Code 的隔离问题在临时目录里用 claude 2.1.289 复现过，`auth add` 把根目录条目带进 profile 在假 HOME 的沙箱里复现过，mem0 的几处行为（维度报错的形式、缺 OpenAI key 时报错、telemetry 撞锁、Ollama 不在线时构造失败、infer=False 不去重、按 metadata 过滤）在临时环境里用 mem0 2.0.16 和 2.2.1 跑过。
