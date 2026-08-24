# Automation Hub 测试策略

## 目标

测试保护用户可观察行为，而不是锁死内部实现。新增需求必须先写失败测试，确认失败来自缺失或错误行为，再做最小实现并跑完整回归。

默认测试禁止访问真实飞书、真实 Codex Agent、真实市场网络和 LaunchAgent；这些外部边界使用完整结构的本地替身，断言落在真实渲染结果、临时状态文件、输出文件和 Runner 最终结果上。

## 现有功能契约

| 范围 | 必须保持的行为 | 主要测试层 |
| --- | --- | --- |
| Scheduler | 自动发现有效任务；跳过禁用任务和已完成时点；有待发送消息时先恢复，再执行本时点任务 | 临时仓库集成测试 |
| Task A | 八个时点均采集；仅 `09:35`、`11:20`、`15:01` 发三张卡；仅 `15:01` 在三卡全部成功后 Pin 第一张 | 真实任务配置行为矩阵 |
| Task C | 每日成功运行固定发送五张论文卡；先保证高质量与跨日去重，再优先近期；近期不足时按发布时间逐步向历史回溯补满，不以弱论文或重复论文凑数；首屏显示英文原题、中文译名和无实验数据的一句话概述；下拉详情以论文摘要翻译开头并保留原四段分析；执行时限两小时 | 真实任务配置、完整执行提示、Runner、语义校验、渲染测试 |
| Task B | 当前任务和 delivery 均关闭；不进入 Scheduler，手动执行也不调用飞书发送器 | 真实任务配置与 Runner 测试 |
| Runner | Agent 不得覆盖 Harness 状态；结果无效不得污染可信状态；通知幂等；部分发送只恢复未发送消息 | 临时状态端到端测试 |
| 每日归档 | Pin 是附加能力；任何 Pin 失败都不得把原卡片发送改成失败；部分三卡发送完成前不得 Pin | 发送顺序与恢复测试 |
| 飞书适配器 | 只从环境变量取凭据；错误脱敏；成功发送和 Pin 必须返回非空 `message_id` | 无网络适配器契约测试 |
| 卡片 | Card 2.0 字段白名单；文本转义；重要内容在正文，次要证据折叠；红涨绿跌、风险黄色 | 语义验证与渲染结构测试 |

## 测试分层

1. 纯函数测试：配置解析、结果校验、卡片语义和渲染。
2. Runner 集成测试：使用临时仓库、真实状态文件和输出文件，只替换 Agent、飞书 HTTP、图片下载等外部边界。
3. 仓库契约测试：加载真实 `tasks/*/task.yaml`，验证任务之间的业务差异没有被共享代码抹平。
4. 本地冒烟测试：只使用 `--dry-run` 或显式假发送器；真实飞书发送必须由用户单独授权。

## 每次变更的 TDD 流程

1. 写下“哪一种错误生产改动会让测试失败”。无法回答时，不写该测试。
2. 先运行最小测试，看到预期 RED；语法错误、fixture 错误不算 RED。
3. 写最小实现，运行同一测试看到 GREEN。
4. 只在 GREEN 后重构；测试只断言可观察结果，不断言替身自身存在。
5. 对关键边界做定向变异检查：错误时点、错误消息 ID、提前 Pin、状态覆盖、秘密泄漏至少有一项测试会失败。
6. 运行完整质量门禁。

## 完整质量门禁

```bash
PYTHONPYCACHEPREFIX=/tmp/automation-hub-pycache python3 -m py_compile scripts/*.py tasks/a-share-monitor/collect_market_data.py
PYTHONPYCACHEPREFIX=/tmp/automation-hub-pycache python3 scripts/validate_task.py --all
PYTHONPYCACHEPREFIX=/tmp/automation-hub-pycache python3 -m unittest discover -s tests -v
git diff --check
```

可选的依赖无关覆盖探针：

```bash
python3 -m trace --count --missing --summary --coverdir /tmp/automation-hub-trace --ignore-dir /Library --module unittest discover -s tests -v
```

覆盖率只用于发现遗漏分支，不作为增加低价值测试的理由。优先保护会导致错发、漏发、重复发送、状态污染、密钥泄漏或原功能失效的行为。

## 新需求进入检查表

- 新配置是否有合法、缺失、错误类型、越界和重复值测试。
- 新发送行为是否覆盖成功、失败、重试、恢复、幂等和部分成功。
- 新卡片字段是否覆盖语义校验、转义、Card 2.0 渲染与旧卡片兼容。
- 新任务是否通过真实任务配置行为测试，而不只检查文档文本。
- 可选增强失败时，原采集、状态、输出和消息发送是否仍成功。
- 测试是否完全本地，未误发真实飞书消息或执行真实 Agent。
