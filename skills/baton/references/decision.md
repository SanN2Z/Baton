# 处理执行者的决策请求

触发：`DECISION` 事件，产物是 `.baton/decisions/D<NNN>-<slug>.md`。

1. 读请求和相关代码，确认它确实超出简报授权。答案明确、可逆且只影响局部时直接决定；涉及公共接口、数据格式、依赖、删除数据、花钱或对外发布时先问用户。重大决策按生效配置里的 `conductor.council_for_major_decisions` 决定是否召集 council。
2. 把决定写成单独的决定文件（例如 `.baton/messages/D001-decision.md`，或其他不改写请求文件的路径）：结论、理由、具体文件和先后顺序；若使用 council，记录 `.council/` 下的结果。请求文件保持原样，判例以账本为准。
3. 运行 `baton decide <task> D001 <decision-file>`。例如：

   ```markdown
   ## 指挥决定
   - 结论：保留现有公共接口，新增适配层
   - 理由：改接口会破坏已冻结调用方
   - 执行：只改 skills/baton/scripts/adapter.py；补对应测试
   ```

4. 入账成功后运行 `baton resume <task> --decision D001`，再 `baton wait <task>`。判例会进入后续卷宗，不能跳过 `baton decide` 直接发消息。
