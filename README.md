# shanon_project

## 需求 Skill 试验台

独立本地工作台，用于手动输入问题、需求、VOC 或 BI 材料，验证分析 Skill 的输出质量。

- 自动匹配分析类型，展示分析中、成功或失败状态。
- 分析生成的粗需求自动进入需求池，支持单条及批量需求定义、指标定义。
- 支持图片、Excel、PDF、PowerPoint、JSON、CSV/TXT 材料。
- 支持模拟 Tracking：告警核查、冻结标准、模拟上线、观察结果、Skill 复盘与人工闭环。

### 启动

需要 Python 3.10+；旧版 XLS/PPT 额外需要 LibreOffice。扫描 PDF 不提供 OCR。

```sh
cd data-closed-loop-platform/skill-lab
python3 -m pip install -r requirements.txt
python3 server.py
```

打开 `http://127.0.0.1:8086`。

模型需单独配置：通过本机安全环境提供 `SKILL_AGENT_API_URL`（Chat Completions 完整地址）、`SKILL_AGENT_MODEL`、`LLM_API_KEY`。不要把 Key 写入仓库或页面。也兼容已配置的 macOS 钥匙串运行档；本仓库不包含任何个人凭据。模型不可用时明确失败，不生成假结果。

### 测试

```sh
cd data-closed-loop-platform
python3 -m unittest discover -s tests -p 'test_skill_lab*.py'
node tests/test_skill_lab_ui.cjs
node tests/test_skill_lab_tracking_ui.cjs
```

### 同步范围

本版导入独立试验台源码、对应测试、分析 Skill、规范及必要功能目录，不包含旧平台全量集成、真实 VOC 附件、本机运行数据、设计输出、密钥或原私有仓库的 Git 历史。功能目录中的内部链接和确认人元数据在公开副本中移除。目标 main 保持原状，代码位于 `codex/requirement-skill-lab` 分支。

代码快照来源版本：`febd8394a7c5c36ee9e5815872f59b2e88b66fac`。

更多说明见 [试验台 README](data-closed-loop-platform/skill-lab/README.md)。
