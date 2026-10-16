```baton-verdict
{
  "conclusion": "APPROVE",
  "findings": {
    "F1": {"decision": "adopt", "reason": "确认该偏离，按下方指令修正。"},
    "F2": {"decision": "reject", "reason": "现有证据和章程允许这一行为，理由见下方说明。"}
  }
}
```

给执行者的指令写在围栏块之后；可以为空。使用前把示例 F 编号换成监督简报中的全部编号，一条不多、一条不少；没有偏离时用 `"findings": {}`。每条 `reason` 必须是非空字符串。

结论按卷宗类型填写：汇报为 APPROVE、REVISE 或 ROLLBACK；终审为 ACCEPT 或 REVISE；违规为 REVERT、ADOPT 或 ASK_USER。

`baton rule <task> <卷宗号> <裁决文件>` 会把裁决全文和所绑定简报写进账本。已有裁决时加 `--rerule`，原记录保留。ROLLBACK 须先告知用户原因与回滚点，获得同意后才能回滚；ADOPT 须由用户在自己的终端运行提示的 amend 命令。
