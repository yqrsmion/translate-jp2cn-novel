# 系统设计说明

> **本文回答**：这个系统内部到底是怎么工作的。
> **本文不回答**：Agent 该怎么操作（→ [`../SKILL.md`](../SKILL.md)）、翻译的行为准则（→ [`../AGENTS.md`](../AGENTS.md)）。

## 一、为什么需要脚本

翻译长篇时，**确定性工作必须由程序做**：

| 必须由程序做的事 | 原因 |
| --- | --- |
| 切分"不破段落" | 否则 1:1 段落对齐这个根基就没了 |
| 用行数与 sha256 判定漏译 | 模型自己说"我翻完了"不可信 |
| 版式程序化重建 | 模型经常丢空行 |
| 进度外部化 | 模型遇到「完」「终」「THE END」就以为翻完了 |

模型只负责**语言生成**，其余全部可计算、可校验、可重放。

## 二、Skill 与工作区的分离

```text
Skill 本体（可复用）              Translation Workspace（属于某一本小说）
─────────────────────            ────────────────────────────────────
SKILL.md / AGENTS.md             ROOT/.translate/          中间产物
README.md / LICENSE              ROOT/<中文书名>.txt        最终交付物
references/ / scripts/           ROOT/<原名>_中文版.txt     回退产物

`ROOT` = **源文档所在目录**（由 `scripts/_paths.py` 推导，不是脚本所在目录）。
所有路径常量集中在 `_paths.py`，各脚本不再各自拼接路径。

**最终产物路径必须运行时解析**：中文书名要到 Merge 阶段才由 `merge.py --title`
写入 `state.json` 的 `output.title_cn`，因此 `_paths.py` 用 `out_txt_path()`
动态解析，模块级的 `OUT_TXT_NAME / OUT_TXT_PATH` 只保留回退值。

## 三、数据流

```text
源文档 .txt
   ↓  split.py          边界感知切分 + self-check
.translate/parts/*.txt + .translate/manifest.json
   ↓  resume.py init
.translate/state.json + .translate/work/ 骨架
   ↓  run_agent.py      组装 prompt（注入相关 work/ 条目 + 前一片尾部）
   ↓  （模型产出译文）
   ↓  run_agent.py --commit → verify.py V9–V12 → 原子落盘 + 状态推进
.translate/parts_out/part_XXX.txt
   ↓  fix_quotes.py（可选）  写回 .translate/parts_out/ 并刷新 sha256
   ↓  merge.py          按 manifest 确定性重建（--title 指定中文书名）
<中文书名>.txt           未提供中文书名时 -> <原名>_中文版.txt
```

> `fix_quotes.py` **必须在 `merge.py` 之前**。它只改 `.translate/parts_out/` 与 `state.json`，
> 合并之后再跑，最终译文不会更新且不报错。

## 四、核心组件

| 脚本 | 职责 |
| --- | --- |
| `_paths.py` | 源文档定位、`ROOT` 推导、所有路径常量（被其余脚本 import） |
| `split.py` | 三级边界切分 + 生成 manifest + self-check |
| `resume.py` | 状态机、审计、崩溃恢复（`init` / `status` / `next` / `audit` / `settle` / `unlock` / `reset` / `incoming`） |
| `run_agent.py` | 单 part 原子翻译：组装 prompt / 调 LLM / 暂存 / 校验 / 置状态 |
| `verify.py` | V1–V12 结构与完整性校验 |
| `merge.py` | 按 manifest 确定性重建译文 |
| `fix_quotes.py` | 日译中标点规范化（dry-run 默认，`--apply` 写回） |

## 五、两份 JSON 契约

### `.translate/manifest.json`（切分后不再变化）

| 块 | 关键字段 |
| --- | --- |
| 顶层 | `schema`、`schema_version`、`generator`、`part_count`、`total_char_count`、`total_byte_count`、`manifest_sha256` |
| `config` | `target_min` / `target_max` / `hard_max` / `min_part`、`levels{STRONG,MID,PARA}`、`unit_rule` |
| `source` | `path`、`encoding`、`line_ending`、`has_bom`、`sha256`、`char_count`、`byte_count` |
| `parts[]` | `part_id`、`index`、`file`（相对 ROOT，形如 `.translate/parts/part_001.txt`）、`start_char`/`end_char`、`char_count`、`byte_count`、`sha256`、`chapter_label`、`headings_in_part`、`text_unit_count`、`tail_newlines`、`forced_cut` |
| `parts[].paragraphs[]` | `paragraph_id`、`index`、`start_char`/`end_char`、`abs_start_char`/`abs_end_char`、`char_count`、`sha256`、**`sep_newlines`**、`blank_before`、`is_heading` |

`manifest_sha256` 是对规范化 JSON 的自签名，用于 V2 检测 manifest 被篡改。
注意 `sep_newlines` 是**逐段**字段，顶层没有。

### `.translate/state.json`（仅由 `resume.py` 维护）

| 块 | 关键字段 |
| --- | --- |
| 顶层 | `schema`、`schema_version`、`source_sha256`、`manifest_sha256`、`part_count`、**`current_part_id`**、`counters`、`runtime`、`history[]`、**`output`**（`title_cn` / `file`，由 `merge.py --title` 写入，决定最终产物文件名） |
| `parts{pid}` | `status`、`attempts`、`started_at`/`finished_at`、`input_sha256`、`output_file`、`output_sha256`、`output_char_count`、`text_units`、`ratio`、`verify`、`needs_human_review`、`claim{token,pid,heartbeat}`、`notes` |

`source_sha256` + `manifest_sha256` 把状态**绑定到具体的一次切分**；换书或重切分后旧状态失效。

## 六、状态机

`status` 取值为 `PENDING` / `IN_PROGRESS` / `FAILED` / `DONE`，
"待人工复核"由 `needs_human_review` 标记表达。

```text
PENDING ──claim──> IN_PROGRESS ──校验 PASS────────> DONE
                        │
                        ├─校验非 PASS─> 保持 IN_PROGRESS + needs_human_review=true ─settle(--i-know)─> DONE
                        ├─校验 FAIL 且 attempts 达上限─> FAILED
                        └─reset(--i-know)─> PENDING（旧译文进 .translate/archive/）

unlock：只清 claim，不改 status、不动 archive/
```

- `current_part_id` = 第一个非 `DONE` 的 part，全 `DONE` 时为 `null`
- 并发保护：`claim` 写入后立即**回读比对 token**
- `history[]` 记录每次迁移（`from`/`to`/`by`/`note`），可追溯

## 七、校验边界

| 层 | 编号 | 内容 |
| --- | --- | --- |
| **源 / manifest / parts** | V1 | 源文档 sha256、字符数、字节数、BOM、换行形态仍与 manifest 一致 |
| | V2 | manifest 自完整性、schema、part_id 连续性、index 单调 |
| | V3 | part 文件存在性、sha256、字符数、字节数 |
| | V4 | 连续性：无 gap / overlap，首片起点 0、末片终点等于原文字符数 |
| | V5 | Σpart 字符数 / 字节数 == 原文 |
| | V6 | 按 manifest 拼接后与原文**字符级全等** |
| | V7 | `> hard_max` FAIL；`> target_max` WARN；`< min_part` WARN |
| | V8 | 逐 part 重算 units 与 `manifest.paragraphs` 逐条比对 |
| **译文（逐 part）** | V9 | 译文存在、`output_sha256` 与 state 一致 |
| | V10 | **A** 数量 / **B** 顺序 / **C** 空译 / **D** 压缩率 / **E** 重复 |
| | V11 | 禁用标记：代码围栏、编号、说明性语句、省略标记、日语残留率 |
| | V12 | 短行或纯符号行（`len<=3` 或匹配非文本正则）必须有非空译文 |

**压缩率的两级判定**：

| 级别 | 区间 | 结果 |
| --- | --- | --- |
| 段落级 V10.D | `<0.30` 或 `>2.50` | **FAIL（阻断）** |
| | `0.30–0.45` / `1.80–2.50` | WARN |
| 整 part 级 `V10.part-ratio` | `<0.60` 或 `>2.50` | 恒为 WARN |

可用 `verify.py --calibrate` 按本书样本重算区间并写入 `state.runtime.ratio_bands`。

**边界说明**：整套校验只能证明**结构完整**，不能证明语义正确。

## 八、两条执行路径

**A. LLM 直调模式**（配了 `NOVEL_LLM_*`）：`claim()` 置 `IN_PROGRESS` → 调 LLM →
暂存 `.translate/incoming/` → 校验 → 落盘 `.translate/parts_out/` → 置 DONE。

**B. 人工模式**（`--print-prompt` → 自己翻译 → `--commit`）：**不 claim、不置 `IN_PROGRESS`**，
`--commit` 只做选中 part、累加 attempts、校验、置 DONE。

因此"悬挂的 `IN_PROGRESS`"只会出现在模式 A。

## 九、已知坑（改动 `split.py` 前必读）

1. **行尾连续换行的所有权归前一个分片**，否则重建时换行数会串位
2. **`sep_newlines` 是该行「之前」的换行数**，不是之后
3. **贪心窗口上界必须与文末取 `min`**，否则末片越界

这三条只在特定边界触发，普通测试覆盖不到，临场重写几乎必然出 bug。

## 十、设计取舍

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 翻译顺序 | 串行 | 小说的事实与时间线是增量累积的；并行会让后片拿不到前片新发现的人名与事实 |
| 二校润色 | 不做 | V10 已覆盖"漏译"这一主要风险；语义校对靠人工抽检 |
| 中间态 | 默认保留 | 支持断点恢复、排错、重新校验、重新合并 |
| 清理方式 | 手动删除 `.translate/` | 不引入额外脚本；删除代价已在文档中写明 |
| 配置载体 | 环境变量 | 没有引入配置文件，避免"文档说有、代码不读"的假配置层 |
| 交付物 | 单一 `.txt` | 只产出 `<中文书名>.txt`（回退 `<原名>_中文版.txt`），不做阅读版导出 |
| 中文书名 | 源文件名译一遍 | 优先 `--title`；无则读 state；都没有且配了 LLM 时脚本自译。译名 == 原名则用回退名 |
