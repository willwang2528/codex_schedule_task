# 飞书群聊每日复盘归档

调研日期：2026-08-23。目标是在不改变原定时消息的前提下，为每日和每周回看提供稳定入口。

## 官方能力结论

1. [会话标签页](https://open.feishu.cn/document/group/chat-tab/chat-tab-overview)可在会话顶部展示消息、Pin、云文档、URL 等模块，一个会话最多有 20 个自定义标签页。
2. [添加会话标签页](https://open.feishu.cn/document/server-docs/group/chat-tab/create)的 OpenAPI 只能创建 `doc` 或 `url` 类型，不能由机器人直接创建 `pin` 标签页；Pin 类型需要在飞书客户端操作。
3. [Pin 消息](https://open.feishu.cn/document/server-docs/im-v1/pin/create)可以把机器人可见的消息加入 Pin 列表；已经 Pin 的消息再次调用仍返回成功信息。
4. [Pin 消息概述](https://open.feishu.cn/document/im-v1/pin/pin-overview)明确说明 Pin 列表对会话所有成员可见。
5. [获取群内 Pin 消息](https://open.feishu.cn/document/server-docs/im-v1/pin/list)支持时间范围、分页，并按 Pin 创建时间倒序返回，适合按天或按周浏览。
6. [消息类型校验变更](https://open.feishu.cn/document/uAjLw4CM/ugTN1YjL4UTN24CO1UjN/breaking-change/unsupported-message-type-verification)只禁止 Pin 系统消息、红包和视频通话；当前使用的交互卡片不在排除范围。

群置顶只适合展示一个当前入口，无法保存每日历史；独立 URL 标签页需要额外部署网页；文档标签页需要创建、授权和持续维护云文档。当前需求优先选择群内 Pin，不引入新的托管服务或文档权限。

## 当前实现

Task A 保持原有 09:35、11:20、15:01 三次通知和每次三张卡不变。只有 15:01 在三张卡全部发送成功后执行：

```text
发送盘面总览 → 发送情绪与主线 → 发送异动与风险
                                       ↓ 全部成功
                              Pin 第一张盘面总览
```

每天只产生一条 Pin 入口。用户从群内 Pin 列表选择某天的“盘面总览”，即可回到当天收盘复盘的位置，另外两张详细卡与它相邻。周末可在同一列表连续查看周一至周五。

## 兼容和失败边界

- Pin 发生在原消息全部成功之后，不改变消息内容、顺序、幂等键和部分失败恢复逻辑。
- Pin 使用发送消息已有的机器人身份和凭据。官方接口接受现有 `im:message` 或 `im:message:send_as_bot` 权限，也可使用更小的 `im:message.pins:write_only`。
- 如果群设置为仅群主/管理员可以 Pin，需要把机器人设为群管理员，或放宽群 Pin 权限。
- Pin 失败记录为 `daily_archive.status=failed`，原通知仍是 `sent`，执行结果仍保持 `SUCCESS_NOTIFY`。
- 如果客户端没有显示 Pin 标签页，可由群管理员在飞书客户端一次性添加 Pin 类型会话标签页；OpenAPI 不能代替这一步。

## 配置

```yaml
delivery:
  notification_triggers: ["09:35", "11:20", "15:01"]
  daily_archive:
    enabled: true
    trigger_slots: ["15:01"]
```

`trigger_slots` 必须是 `notification_triggers` 的子集，防止纯采集时点产生群消息或 Pin。
