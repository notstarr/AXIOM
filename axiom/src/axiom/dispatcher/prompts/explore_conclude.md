# AXIOM 探索执行收口（Explore Conclude）

当前 Intent 执行已进入收口阶段。**立即停止网络探测与耗时扫描**，仅整理现有材料。中文输出。

执行阶段已经完成 `axiom skill path whatisvul` 的读取，并完整读取 `whatisvul/SKILL.md`（失败时运行 `axiom skill doctor whatisvul`）；收口不重新探测，没做完没有关系，`evidence/` 完全可选；不要为了补证据重新运行 Nuclei、SQLMap、浏览器、爬虫。验证码、真实设备和工具失败都记录为可恢复阻塞。

1. **检查与补齐核心产物**：
   - `{run_dir}/artifacts/findings.md`（本轮已确认事实/漏洞，无则写“本轮无新增确认事实”）；
   - `{run_dir}/artifacts/leads.md`（未完成/待后续跟进分支，无则写“本轮无遗留待验证线索”）。
   - 两个文件必须存在且非空。
2. **漏洞补报（可选）**：若会话中确认了高置信度漏洞，可用 `axiom explore vulnerability report` 补报。
3. **人工干预标记（仅限物理卡点）**：需真实支付/MFA/扫码时，调用 `axiom explore human-intervention mark --reason "..."`。

产物就绪后立即发送最后一条助手消息，不要描述检查结果或总结执行过程，严格只有：

FINISH

禁止反引号、代码块、句号、解释或总结。
