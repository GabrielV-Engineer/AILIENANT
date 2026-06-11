<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/logo.svg" alt="AILIENANT" width="340" />

<h1>AILIENANT</h1>

<p><strong>先规划、后编码的 AI 编程搭档——在你的机器上、用你的模型、按你的规则运行。</strong></p>

<p>
  <a href="README.md">English</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md">Français</a> ·
  <strong>中文</strong> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.it.md">Italiano</a>
</p>

<p>
  <a href="LICENSE"><img alt="许可证: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-Extension-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="状态" src="https://img.shields.io/badge/status-active%20development-success">
</p>

</div>

---

## AILIENANT 是什么？

**AILIENANT 是一个生活在 VS Code 里的自主编程智能体。** 你用自然语言描述需求；AILIENANT 会写出真正的计划、进行修改、在沙箱中运行代码、读取结果并自行纠错——而且会向你展示它推理的每一步。

它与主流 AI 助手的区别在于**它在哪里运行、如何决策。** AILIENANT 是**本地优先（local-first）**的：它可以完全在你自己的机器上、使用开放模型（Ollama、LM Studio 等）运行，只有当任务确实需要时才调用云端——并且会用美元金额告诉你何时调用了云。你的代码无需离开你的笔记本，你也永远不会被锁定在单一供应商。

> **一句话：** 一个私有、成本可控、先规划的 AI 工程师，服务于你的代码库——开源且无供应商锁定。

---

## 人们为何使用它

- **🧠 先规划，后编码。** 专门的*规划器（Planner）*把你的请求转化为具体的规格和任务清单，冻结范围，并监控“漂移”，使智能体不会悄悄跑偏、重写半个项目。另一个*编码器（Coder）*执行该计划。两个大脑，各司其职。
- **🔒 你的代码仍归你。** 100% 本地运行，使用你自己的模型。没有强制云端、没有“回家”遥测、不在你的仓库上做训练。
- **💸 成本可见。** 每个任务都有实时的 token 账本和硬性预算上限。本地与云端用量以及预计节省都会显示出来，而非隐藏。
- **🪟 推理可见。** 实时的“思考框（Thought Box）”流式展示模型的推理，逐步轨迹显示每一次文件读取、命令执行和补丁提议。
- **⏪ 可以回退。** 任务的每一步都是持久化检查点。从任意点分叉以探索另一种方案——为智能体实现真正的时间旅行式调试。
- **🛡️ 安全地运行代码。** 生成的命令在沙箱中执行（Docker，并提供 WebAssembly 与人工审批回退），绝不盲目地对你的机器执行。
- **🔌 无锁定。** 自带模型与供应商——Ollama、LM Studio、vLLM、llama.cpp、OpenAI、Anthropic、Google、DeepSeek、Mistral 等——并可随时切换。

---

## 它有何不同？

| | **AILIENANT** | 典型云端助手 |
| --- | --- | --- |
| 完全在你的机器上运行 | ✅ 本地优先，自带模型 | ❌ 仅云端 |
| 先规划再编码（双头） | ✅ 规划器 + 编码器，带漂移守卫 | ❌ 单模型、单次 |
| 智能本地↔云路由 | ✅ 选择能胜任的最便宜层级 | ❌ 固定 |
| 实时显示成本 | ✅ token 账本 + 预算上限 | ⚠️ 通常隐藏 |
| 时间旅行 / 分叉运行 | ✅ 持久化检查点 | ❌ 无状态 |
| 沙箱执行 | ✅ Docker / Wasm / 需审批 | ⚠️ 常在宿主机上 |
| 供应商锁定 | ✅ 无——自由切换 | ❌ 绑定单一 |

更完整的技术对比见 **[HowItWorks.md](HowItWorks.md)**。

---

## 安全与防护，源于设计

AILIENANT 假定自主智能体迟早会尝试做它不该做的事——并据此构建以加以约束。

- **默认隔离。** 命令在隔离的 Docker 容器中执行（工作区只读、无网络、非 root），当 Docker 不可用时提供 WebAssembly 与人工介入回退。
- **fail-closed 权限。** 每个工具按权限分级；任何无法识别的东西都被视为**危险，直到被证明安全**，绝不反过来。
- **关键处需人工审批。** 高风险操作和预算超支会暂停，等待你的明确批准。
- **防篡改审计链。** 审批被记录在以密码学方式串联（blake2b）的账本中，可供你验证。
- **多租户隔离。** 每一段被索引的记忆都以其工作区命名，因此项目之间绝不互相泄漏。

---

## 快速开始

> 完整指南：**[HowToUseIt.md](HowToUseIt.md)**

**前置条件：** Python 3.10+（推荐 3.13）、Node.js 20+、VS Code 1.85+，以及至少一个模型来源（本地的 Ollama/LM Studio 安装、一个 [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) 代理，或云端 API 密钥）。

```powershell
# 1. 后端（编排引擎）
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix: source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix: cp ../.env.example ../.env

# 2. 扩展（VS Code 界面）
cd ..\ailienant-extension
npm install
npm run compile
```

然后在 VS Code 中打开该项目并按 **F5** 启动扩展。首次打开 AILIENANT 会话时，它会为你启动后端并开始索引你的工作区。在内置的 **BYOM** 面板中配置你的模型，输入一条请求，即可开始。

---

## 工作原理（简版）

```
你提问  ─▶  规划器  ─▶  漂移守卫  ─▶  编码器  ─▶  沙箱运行
            (写规格           (范围         (编辑         ▲      │
             + 计划)          锁定)         文件)        │      ▼
                                                      修复 ◀─ 读取结果
```

在幕后，有状态的 **LangGraph** 引擎根据上下文与复杂度评分，在本地与云端模型之间为每个任务进行路由；用 **GraphRAG**（向量检索 + 一跳依赖遍历）取回正确的文件；并在每一步保存检查点，确保不丢失任何内容。深入版本——图示、路由数学、执行循环和安全模型——见 **[HowItWorks.md](HowItWorks.md)**。

---

## 文档

| 文档 | 面向谁 |
| --- | --- |
| **[HowToUseIt.md](HowToUseIt.md)** | 所有人——逐步安装、配置并运行你的第一个任务 |
| **[HowItWorks.md](HowItWorks.md)** | 好奇者——架构、路由与安全模型解析 |
| **[DEVELOPERS.md](DEVELOPERS.md)** | 核心开发者——深入内部、图示、伪代码、代码地图 |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | 贡献者——环境搭建、规范与如何提交一个出色的 PR |
| **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)** | 逐阶段的完整路线图 |

---

## 参与贡献

AILIENANT 是开源项目，欢迎贡献——从修正错别字到完成路线图条目。请先阅读 **[CONTRIBUTING.md](CONTRIBUTING.md)**。

需要先了解一点：由于项目采用双重许可（见下文），每位贡献者在其首个 PR 被合并前都需签署一份简短的 **[贡献者许可协议（CLA）](CLA.md)**。这是一次性步骤，你仍保留对自己作品的版权。

---

## 许可证

AILIENANT 采用**开放核心（open-core）与双重许可**：

- **社区版——[GNU AGPL-3.0](LICENSE)。** 可自由使用、研究、修改和分享。如果你分发它，或将修改版作为网络服务运行，则需在相同许可下公开你的源代码。
- **商业 / 企业版。** 面向无法接受 AGPL 条款，或需要企业功能与支持的组织。

完整说明以及如何获取商业许可，请参见 **[LICENSING.md](LICENSING.md)**。

> **AILIENANT** 名称及其标识为本项目的商标，不在 AGPL 覆盖范围内。

---

<div align="center">

**为那些想要一个真正可信赖、且可审计的 AI 搭档的工程师而打造。**

站在巨人的肩膀上：<a href="https://github.com/langchain-ai/langgraph">LangGraph</a> · <a href="https://lancedb.com/">LanceDB</a> · <a href="https://tree-sitter.github.io/">Tree-sitter</a> · <a href="https://github.com/BerriAI/litellm">LiteLLM</a> · <a href="https://docs.pydantic.dev/">Pydantic</a>。

</div>
