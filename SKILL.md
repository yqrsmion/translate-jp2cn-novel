---
name: translate-jp2cn-novel
description: "将日文小说完整翻译为简体中文（JP→CN / 日译中）。适用于长篇日文小说的分块翻译、跨块一致性维护、完整性校验与合并导出。当用户要求翻译日文小说、日文长篇、日文书籍，或把日文长文本完整译成中文时使用。"
---

# 翻译日文小说（日 → 中简体）

把整本日文小说切成小块，**串行**逐块翻译，全程用程序保证不漏译、不乱序、可恢复，
最后确定性合并为简体中文正文 + 阅读版 Markdown。

这套流程已在真实项目上完整跑通：23 万字、4595 段落、23 个 part，零漏译。

## 何时使用

- 用户要求翻译一本日文小说 / 日文长篇 / 日文书籍
- 需要把日文长文本**完整**（而非摘要、非节选）译成简体中文

## 铁律（绝不可破坏）

1. **1:1 段落对齐**：每个非空行 = 一个段落，译文行数必须严格等于原文段落数
2. **切分不破段落**：只在空行 / 句末边界切，绝不切断段落
3. **版式程序化重建**：段落间换行由 `manifest.sep_newlines` 决定，不依赖模型保留空行
4. **完成判定外部化**：仅当 `state.json` 全部 DONE 才算完成，模型绝不自行判断"翻完了"

## 源文档定位（不写死文件名）

优先级：

1. 用户在提示词中显式指定的文档 —— 由你通过 `--source` 传入（最高优先级）
2. `--source <路径>`
3. 环境变量 `NOVEL_SOURCE`
4. 工作目录下**唯一的** `*.txt`
5. 存在多个 `*.txt` → **停止并请用户指定，绝不猜测**

`ROOT` = 源文档所在目录。所有产物（`parts/`、`output/`、`work/`、
`manifest.json`、`state.json`、`<原名>.zh.txt`、`<原名>.zh.md`）都落在 ROOT 下。

## SOP

> 下文 `<S>` 代表本 skill 的 `scripts/` 目录路径。

**0. 准备**

读 `AGENTS.md`（完整工作规范）。若为推理小说，额外读 `references/genre-mystery.md`。

**1. 切分（只做一次）**

```
python <S>/split.py --source <源文档> --plan    # 建议先干跑看计划
python <S>/split.py --source <源文档>
```

生成 `parts/` 与 `manifest.json`。禁止重复切分；出错则删除 `parts/` 重来。

**2. 初始化**

```
python <S>/resume.py --source <源文档> init
```

生成 `state.json` 与 `work/` 知识库骨架。

**3. 逐 part 翻译（串行循环，直到 ALL_DONE）**

```
python <S>/resume.py --source <源文档> next
python <S>/run_agent.py --source <源文档> --print-prompt
```

把译文写入 `output/<part_id>.txt`（**一行对一段，行数必须与原文一致**），然后：

```
python <S>/run_agent.py --source <源文档> --part <part_id> --commit
```

**4. 审计（随时可做）**

```
python <S>/resume.py --source <源文档> audit
python <S>/resume.py --source <源文档> status
```

**5. 合并（全部 DONE 后）**

```
python <S>/merge.py --source <源文档>
```

产出 `<原名>.zh.txt`。

**6. 标点规范化（可选）**

```
python <S>/fix_quotes.py --source <源文档>            # dry-run
python <S>/fix_quotes.py --source <源文档> --apply
```

**7. 导出阅读版 Markdown**

```
python <S>/export_md.py --source <源文档> --inspect   # 先看标题结构对不对
python <S>/export_md.py --source <源文档>
```

产出 `<原名>.zh.md`。

## 崩溃恢复

`resume.py status` 会诊断悬挂状态并给出处置建议：

- `settle --part X --i-know`：人工确认后把已完成译文置 DONE
- `unlock --part X`：清除过期 claim
- `reset --part X --i-know`：退回 PENDING 重译（旧译文自动归档到 `archive/`）

## 跨块一致性

`work/` 是长期知识库（人物 / 术语 / 地点 / 关系 / 时间线 / 事实 / 冲突）。
`run_agent.py` 会自动把**与本 part 相关**的条目注入 prompt，并附带前一片译文尾部。

你只需在每 part 翻译后，把新发现的**客观事实**追加进 `work/`。
发现与已有条目冲突时，记入 `work/conflicts.md` 并**暂停等人工确认**，不要自动改。

## 标点映射

见 `references/punctuation.md`。
日文符号体系比中文丰富，映射不当会丢掉"对话 vs 广播/引用"的层级区分。

## 可选：推理小说

见 `references/genre-mystery.md` —— 核心是**禁止剧透的措辞护栏**。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOVEL_SOURCE` | — | 源文档路径 |
| `NOVEL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — | LLM 接口；不配则用 `--print-prompt` 由你自己翻译 |
| `NOVEL_LLM_TEMPERATURE` | 0.2 | 采样温度 |
| `NOVEL_CONTEXT_BUDGET` | 8000 | `work/` 注入上下文上限（字符） |
| `NOVEL_TARGET_MIN` / `_MAX` / `NOVEL_HARD_MAX` / `NOVEL_MIN_PART` | 8000 / 15000 / 20000 / 2000 | 切分尺寸 |
| `NOVEL_CALL_TIMEOUT` | 900 | 单次 LLM 调用超时（秒） |
| `NOVEL_PREV_TRANS_TAIL` / `_RAW_TAIL` | 1200 / 600 | 前一片尾部注入长度 |

## 注意

`resume.py` 使用子命令，`--source` 是顶层参数，须写在子命令之前：
`resume.py --source X status`（而非 `resume.py status --source X`）。
