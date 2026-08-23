# Agent Memory 论文速览卡片调研

## 目标与边界

本轮只优化 Task C Top 5 在飞书中的展示顺序，让用户先理解“这篇论文解决什么问题”。不修改候选集构建、一手来源核验、Top 5 筛选或排序逻辑。

## 调研证据

- [Keshav, How to Read a Paper](https://dl.acm.org/doi/10.1145/1273445.1273458) 建议首轮只做快速鸟瞰，先形成论文类别、语境和贡献的整体认知。
- [ICMJE 稿件准备建议](https://www.icmje.org/recommendations/browse/manuscript-preparation/preparing-for-submission.html) 要求结构化摘要覆盖背景、目标、基本方法、主要发现和结论，同时交代重要局限并避免过度解读。
- [DARPA Heilmeier Catechism](https://www.darpa.mil/about/heilmeier-catechism) 以“要做什么—今天怎么做及其局限—新方法为什么会成功”组织研究判断。
- [Hartley 2003 结构化摘要研究](https://journals.sagepub.com/doi/10.1177/1075547002250301) 显示，结构化摘要虽然更长，但信息量、可读性和清晰度更好。
- [NN/g 渐进式披露原则](https://www.nngroup.com/articles/progressive-disclosure/) 支持先展示最重要内容，将次级信息按需展开。

## 设计推断

上述来源没有直接验证过“飞书 Agent Memory Top 5 卡片”。从它们推导出的产品结论是：

1. 卡片首先给论文身份和一句话问题定义。
2. 默认展开的中文摘要覆盖问题、关键机制、最强证据和适用边界。
3. 详细区用固定问题链：现存问题 → 已有方法的不足 → 当前方法为什么可行 → 未来展望。
4. 详细区默认折叠，保持五张卡片的字段顺序和标签一致，方便横向扫读。

## 事实边界

- “当前方法为什么可行”必须同时说明机制理由和论文证据；没有实验支持时必须标注为作者主张或尚未验证。
- “未来展望”优先复述作者声明的 future work 或 limitations。基于局限的编辑推断必须显式标注；证据不足时不外推。
- 非方法论文不强行套实验模板。综述、基准、数据集、系统和概念性工作可用框架、任务设计、数据覆盖或论证路径代替“方法”，但保留相同认知顺序。
