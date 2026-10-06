# 日文小说翻译 skill

**指令**：`translate-jp2cn-novel`

把整本日文小说完整翻译成简体中文（日文 → 中文）。

不是"调一次大模型让它翻译"，而是一套可控、可校验、可恢复的翻译工程：
切块 → 串行逐块翻译 → 逐段校验 → 确定性合并。

## 能解决什么问题

长篇翻译的失败几乎都出在**工程问题**，而不是语言能力：

- 模型遇到「完」「终」「THE END」就宣布翻完了，实际还剩 8 章
- 长文本一次性进上下文，前面翻到的人名后面全变了
- 漏译、乱序、空译没人发现，直到读到一半发现剧情断了
- 中途崩了，不知道从第几块继续

应对方式是：**把确定性工作全部交给脚本，只把语言生成留给模型**。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 长文本一致性维护 | `work/` 翻译辅助上下文按关键词注入每个分片，人物 / 术语 / 地点跨片统一 |
| 1:1 结构完整性 + 自动验证 | 段落级 1:1 对齐 + sha256 + V1–V12 校验，可捕捉漏译、乱序、空译、异常压缩、重复、未翻译残留 |
| 可恢复的串行翻译流程 | `state.json` 状态机 + claim 令牌 + 原子写，中断后可续译；失败的片不自动跳过 |
| 确定性合并 | 段落间空行由 manifest 中的结构信息重建，不依赖模型是否保留了空行 |

---

## 安装

本 skill 是**通用目录结构**（`SKILL.md` + `scripts/` + `references/`），不依赖任何特定工具。

把整个 `translate-jp2cn-novel/` 目录复制到你所用工具的 user skills 目录即可
（各工具位置不同，通常在 `~/.<工具>/skills/` 下）。目录名保持 `translate-jp2cn-novel`。

运行参数一律通过 `NOVEL_*` 环境变量设置，**不读取任何配置文件**。

## 快速开始

```bash
# 1. 切分（只做一次）
python scripts/split.py --source <your-novel.txt> --plan   # 先干跑看计划
python scripts/split.py --source <your-novel.txt>

# 2. 初始化
python scripts/resume.py --source <your-novel.txt> init

# 3. 逐片翻译（串行循环，直到输出 ALL_DONE）
python scripts/resume.py --source <your-novel.txt> next
python scripts/run_agent.py --source <your-novel.txt> --print-prompt
python scripts/run_agent.py --source <your-novel.txt> --part part_001 --commit

# 4. 合并（把源文件名日文标题译成中文书名后传入）
python scripts/merge.py --source <your-novel.txt> --title "中文书名"   # -> 中文书名.txt
```

## 输入与输出

| | 内容 |
| --- | --- |
| **输入** | 一个日文 `.txt` 源文档。按优先级定位：提示词指定 → `--source` → 环境变量 `NOVEL_SOURCE` → 目录下唯一 `*.txt`；多个候选时停止并要求指定 |
| **最终产物** | `<中文书名>.txt`，与源文档**同一级目录**；译名与原名相同时为 `<原名>_中文版.txt` |
| **中间产物** | `.translate/`（见下） |

**中文书名** = 把源文件名（日文标题）翻译一遍。配了 `NOVEL_LLM_*` 时 `merge.py` 自己译；
没配就用 `--title "中文书名"` 传入。书名写进 `state.json` 的 `output.title_cn`，
之后重跑 merge / `--check` 复用同一文件名。

**卷次、作者保留**，只清掉下载站后缀之类的脏数据；书名用**简体中文**：

```text
源：世界の終りとハードボイルド・ワンダーランド 上 (村上春樹) (z-library.sk, 1lib.sk, z-lib.sk).txt
产物：世界尽头与冷酷仙境 上 (村上春树).txt
```

## 翻译过程中会产生什么工作文件

运行某一本小说时，在**源文档所在目录**建立独立工作区：

```text
<小说目录>/
├── <日文标题>.txt                  原文（只读）
├── <中文标题>.txt                  最终译文（回退时为 <日文标题>_中文版.txt）
│
└── .translate/                    可恢复的工作状态
    ├── parts/                     原文分片
    ├── parts_out/                 逐 part 译文
    ├── work/                      翻译辅助上下文（人物 / 术语 / 地点 / 时间线 / 事实 / 冲突）
    ├── incoming/                  未提交的暂存草稿
    ├── archive/                   重译时归档的旧译文
    ├── manifest.json              切分清单
    └── state.json                 进度状态
```

Skill 目录**不保存任何小说状态**，工作区永远跟着源文档走。

## 如何恢复

```bash
python scripts/resume.py --source <your-novel.txt> status   # 查看进度、诊断悬挂
python scripts/resume.py --source <your-novel.txt> audit    # 全量校验
```

| 情况 | 命令 |
| --- | --- |
| 译文已完整但状态悬挂 | `resume.py --source X settle --part P --i-know` |
| claim 残留，需重跑该 part | `resume.py --source X unlock --part P` |
| 需重译某 part | `resume.py --source X reset --part P --i-know` |
| 有残留草稿 | `resume.py --source X incoming --discard P --i-know` |
| 状态损坏 | `resume.py --source X init --rebuild`（不删除译文） |

失败的 part **不会自动跳过**——宁可停下来等人，也不产出残缺译文。

## 如何清理工作区

中间态**默认保留**（便于恢复、排错、重新校验、重新合并）。

只需要成品时，**直接删除 `.translate/` 目录**即可。注意：

- 只删工作区，不影响源文档与最终译文
- 删除后无法再断点恢复、重新合并或重新校验（均依赖 `manifest.json`）

## 文档导航

| 你想知道 | 看这里 |
| --- | --- |
| 这是什么、怎么用 | 本文件 |
| Agent 该怎么完成一次翻译 | [`SKILL.md`](SKILL.md) |
| 翻译的行为准则与工程约束 | [`AGENTS.md`](AGENTS.md) |
| 系统内部是怎么工作的 | [`docs/architecture.md`](docs/architecture.md) |
| `work/` 怎么填 | [`references/knowledge-base.md`](references/knowledge-base.md) |
| 日译中标点怎么映射 | [`references/punctuation.md`](references/punctuation.md) |

## 项目结构

```text
translate-jp2cn-novel/
├── README.md                 本文件：项目说明
├── SKILL.md                  能力定义与执行步骤
├── AGENTS.md                 翻译执行规范与工程约束
├── LICENSE                   MIT
├── docs/
│   └── architecture.md       系统设计说明
├── references/
│   ├── knowledge-base.md     work/ 清单与模板
│   └── punctuation.md        日译中标点映射规范
└── scripts/
    ├── _paths.py             源文档定位 + 路径常量（共用）
    ├── split.py              边界感知切分 + manifest
    ├── resume.py             状态机 / 审计 / 崩溃恢复
    ├── run_agent.py          单片原子翻译
    ├── verify.py             V1–V12 完整性校验
    ├── merge.py              确定性合并
    └── fix_quotes.py         标点规范化
```

## 限制与边界

- **只保证结构完整，不保证语义正确**。V1–V12 能证明"没漏、没乱、没空"，不能证明"没译错"；语义校对仍需人工抽检
- **串行执行**。为跨片一致性不支持并行，吞吐受限于单链路速度
- **只产出简体中文纯文本**（`<中文书名>.txt`，回退时 `<原名>_中文版.txt`），不生成阅读版 Markdown
- 本仓库**不包含任何小说正文**。使用者的原文与译文受版权保护，请勿上传

## 验证记录

以下来自一次真实项目的完整落地，是历史实测数据，**不构成普遍保证**：

| 项 | 结果 |
| --- | --- |
| 规模 | 23 万字、4595 段落、23 个 part |
| 字符比 | 原文 279,053 → 译文 191,406（0.71） |
| 漏译 | 0（V10 结构校验） |
| 人工抽检 | 180 段逐段核对，0 漏译、0 错译 |

## 许可

MIT
