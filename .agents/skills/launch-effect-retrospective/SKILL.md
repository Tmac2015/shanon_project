---
name: launch-effect-retrospective
description: 对照冻结的指标卡比较已验证的上线结果，生成有证据支持的复盘和下一步决策。用于正式上线后效果复盘。
---

# 上线效果复盘

完整阅读并遵守项目规范：
`../../../data-closed-loop-platform/skills/launch-effect-retrospective-skill.txt`。

配置的 LLM 智能体可以解释原因并提出下一步动作，但所有生产测量值、样本量、时间窗、计算公式和人工决策都是来自平台或数据系统的不可修改事实。不得虚构或篡改测量结果。正式输出必须包含已完成的智能体审计记录；条件不满足时必须失败关闭。
