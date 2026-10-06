---
name: translate-jp2cn-novel
description: "将日文小说完整翻译为简体中文（JP→CN / 日译中）。适用于长篇日文小说的分块串行翻译、跨块一致性维护、结构完整性校验与确定性合并。当用户要求翻译日文小说、日文长篇、日文书籍，或把日文长文本完整译成中文时使用。"
---

# 翻译日文小说（日 → 中简体）

> **本文回答**：Agent 使用这个 Skill 时，该怎么完成一次翻译（调用什么、按什么顺序、失败怎么办）。
> **本文不回答**：翻译本身的行为准则（→ [`AGENTS.md`](AGENTS.md)）、系统内部如何实现（→ [`docs/architecture.md`](docs/architecture.md)）。

把一本日文小说切成小块，**串行**逐块翻译，用脚本保证不漏译、不乱序、可恢复，
最后确定性合并为一部简体中文译文。

## 何时使用

- 用户要求翻译一本日文小说 / 日文长篇 / 日文书籍
- 需要把日文长文本**完整**（而非摘要、非节选）译成简体中文

## 领域边界

面向**日文长篇小说的整本翻译**。不做短句翻译，不做多语言互译，不做译后润色出版。

## 输入与输出

| | 内容 |
| --- | --- |
| **输入** | 一个日文 `.txt` 源文档（路径不写死，按优先级定位） |
| **最终产物** | `<中文书名>.txt`，与源文档**同一级目录**；未取到中文书名时回退 `<原名>_中文版.txt` |
| **中间产物** | `<源目录>/.translate/`（parts / parts_out / work / incoming / archive / manifest.json / state.json） |

产物**永远落在源文档所在目录**，Skill 目录不保存任何小说状态。

**中文书名怎么来**：源文件名本身就是日文标题，由你在阶段 6 合并时把它译成中文，
经 `merge.py --title "中文书名"` 传入（脚本会清洗非法字符后写入 `state.json` 的
`output.title_cn`，后续重跑 merge / `--check` 复用同一文件名）。

## 铁律

1. **1:1 段落对齐** —— 每个非空原文行对应一个译文段落，不增不减
2. **切分不破坏段落** —— 只在空行 / 章节 / 句末等安全边界切
3. **版式程序化重建** —— 段落间空行由 manifest 中的结构信息控制，不依赖模型保留
4. **完成判定外部化** —— 全部 part 为 `DONE` 才算完成，模型绝不自行判断"翻完了"

---

## 执行流程

下文 `<S>` = 本 Skill 的 `scripts/` 目录，`<X>` = 源文档路径。

```text
Preflight → Split → Init → Translate（串行循环）→ 标点规范化 → Merge
```

### 阶段 1：Preflight

- **输入**：用户指定的源文档
- **操作**：读 [`AGENTS.md`](AGENTS.md)；用 `--source <X>` 显式指定源文档
- **通过条件**：源文档唯一且存在
- **失败**：目录下多个 `*.txt` → 停止并请用户指定
- **重复执行**：无副作用

### 阶段 2：Split（只做一次）

```bash
python <S>/split.py --source <X> --plan    # 干跑，先看计划
python <S>/split.py --source <X>
```

- **输出**：`.translate/parts/part_XXX.txt` + `.translate/manifest.json`
- **通过条件**：内置 self-check（V4 连续性 / V5 总长 / V6 重建 / V7 尺寸）通过
- **失败**：切分出错 → 删除整个 `.translate/parts/` 后重跑，禁止追加切分
- **重复执行**：`parts/` 已存在时不要重跑

### 阶段 3：Init

```bash
python <S>/resume.py --source <X> init
```

- **输出**：`.translate/state.json` + `.translate/work/` 骨架（7 个文件）
- **失败**：`state.json` 已存在 → 脚本拒绝；需重建时加 `--rebuild`（**不删除任何译文**）
- **重复执行**：幂等，已存在即拒绝

### 阶段 4：Translate（串行循环，直到 ALL_DONE）

```bash
python <S>/resume.py --source <X> next                  # 取当前 part_id
python <S>/run_agent.py --source <X> --print-prompt     # 组装 prompt（注入相关 work/ 条目 + 前一片尾部）
#   把译文写入 .translate/parts_out/<part_id>.txt（一行一段，行数与原文一致）
python <S>/run_agent.py --source <X> --part <part_id> --commit
```

- **输入**：`part_id` + part 原文 + 相关 `work/` 片段（脚本注入）
- **输出**：`.translate/parts_out/part_XXX.txt`
- **通过条件**：`--commit` 内置 V9–V12 校验通过
- **失败**：校验不通过 → 修正译文后重新 `--commit`；不跳过、不并行、不逆序
- **重复执行**：同一 part 需重译时先走恢复流程（`reset`）

输出格式：仅译文文本，**禁止**包含元数据、解释、进度标记、章节标题、空行。

配了 `NOVEL_LLM_*` 环境变量时，`run_agent.py` 会自动调用 LLM；
未配置则用 `--print-prompt` 由你自行翻译再 `--commit`。

### 阶段 5：标点规范化（可选）

```bash
python <S>/fix_quotes.py --source <X>            # dry-run
python <S>/fix_quotes.py --source <X> --apply
```

**必须在 Merge 之前执行**：它只改 `.translate/parts_out/` 与 `state.json` 中的 sha256，
合并之后再跑，最终译文不会更新且不报错。

映射规则见 [`references/punctuation.md`](references/punctuation.md)。

### 阶段 6：Merge

合并时**把源文件名（日文标题）翻译一遍**：

- 译名与原名不同 → `<中文书名>.txt`
- 译名与原名相同（比如原名本身已是中文）→ `<原名>_中文版.txt`
- **卷次、作者保留**，只清掉下载站后缀之类的脏数据；书名用**简体中文**

```text
源：世界の終りとハードボイルド・ワンダーランド 上 (村上春樹) (z-library.sk, 1lib.sk, z-lib.sk).txt
                    ↓ 译书名，留卷次与作者，删脏数据
产物：世界尽头与冷酷仙境 上 (村上春树).txt
```

配了 `NOVEL_LLM_*` 时脚本会自己译一遍源文件名；没配就把译名用 `--title` 传进来。

```bash
python <S>/merge.py --source <X> --title "中文书名" --check     # 先只校验，不重写
python <S>/merge.py --source <X> --title "中文书名" [--strict]
```

- **输出**：`<中文书名>.txt`；省略 `--title` 且 `state.json` 中没有书名时回退 `<原名>_中文版.txt`
- **通过条件**：全部 part 为 `DONE`；`--strict` 下不存在 `needs_human_review` 的 part
- **失败**：有未完成 part → 回到阶段 4；有待复核 part → 人工确认后 `settle`
- **重复执行**：幂等（`--check` 会报告 IDENTICAL / DIFFERENT）；书名只需首次传入，之后可省略 `--title`

`--title` 传入的译名若与源文档同名，脚本会拒绝（防止产物覆盖原文）。

---

## 翻译辅助上下文（`work/`）

`work/` 位于 `.translate/work/`，是跨 part 的翻译辅助上下文（人物 / 术语 / 地点 / 关系 /
时间线 / 事实 / 冲突）。脚本会按关键词把**相关**条目注入每个 part，不会全量注入。

- 记录模板见 [`references/knowledge-base.md`](references/knowledge-base.md)
- 使用与冲突处理规则见 [`AGENTS.md`](AGENTS.md)

## 失败与恢复

先诊断，再按情况处置：

```bash
python <S>/resume.py --source <X> status
```

| 情况 | 命令 |
| --- | --- |
| 译文已完整但状态悬挂 | `resume.py --source <X> settle --part P --i-know [--reason "..."]` |
| claim 残留，需重跑该 part | `resume.py --source <X> unlock --part P` |
| 需重译某 part | `resume.py --source <X> reset --part P --i-know`（旧译文进 `.translate/archive/`） |
| `.translate/incoming/` 有残留草稿 | `resume.py --source <X> incoming --discard P --i-know` |
| `state.json` 损坏 | `resume.py --source <X> init --rebuild`（不删除译文） |

补充命令：`resume.py --source <X> audit`（全量校验）。

**原则**：失败的 part 不自动跳过；挂起的 part 必须人工确认后才能置为 DONE。

## 工作区清理

中间态默认保留。当你只需要成品时，**直接删除 `.translate/` 目录**即可：

- 只删工作区，不影响源文档与最终译文
- 删除后无法再断点恢复、重新合并或重新校验（均依赖 `manifest.json`）

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOVEL_SOURCE` | — | 源文档路径 |
| `NOVEL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — | LLM 接口；不配则用 `--print-prompt` 自行翻译 |
| `NOVEL_LLM_TEMPERATURE` | 0.2 | 采样温度 |
| `NOVEL_CONTEXT_BUDGET` | 8000 | `work/` 注入上下文上限（字符） |
| `NOVEL_TARGET_MIN` / `_MAX` | 8000 / 15000 | 切分目标尺寸 |
| `NOVEL_HARD_MAX` / `NOVEL_MIN_PART` | 20000 / 2000 | 硬上限 / 碎片下限 |
| `NOVEL_CALL_TIMEOUT` | 900 | 单次 LLM 调用超时（秒） |
| `NOVEL_PREV_TRANS_TAIL` / `_RAW_TAIL` | 1200 / 600 | 前一片译文 / 原文尾部注入长度 |

运行参数一律走环境变量，本 Skill **不读取任何配置文件**。
