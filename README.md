# translate-jp2cn-novel

把整本**日文小说完整翻译成简体中文**的 CodeBuddy / Codex skill。

不是"调用一次大模型让它翻译"，而是一套**可控、可校验、可恢复的翻译工程**：
切块 → 串行逐块翻译 → 逐段校验 → 确定性合并 → 导出阅读版。

本 skill 的全部流程与脚本，来自一次真实项目的完整落地：
**23 万字、4595 段落、23 个 part，零漏译**（原文 279,053 字符 → 译文 191,406 字符，
字符比 0.71）。

---

## 三大亮点

### 1. 禁止剧透的措辞护栏

翻译推理小说时，最危险的不是"凭空加一段剧透"——原文就是故事本身，模型也不会无中生有。
真正会翻车的是**措辞层面的无意识泄漏**：

- **他 / 她的性别强制标记**：日文常省略主语、不标性别（`私`、`あの人`），
  中文却**必须**选"他"或"她"。模型若知道叙述者真实性别（叙述诡计的经典手法），
  译前文时就泄露了。
- **暧昧指代被"知情"补全**：模糊指代在模型知道真相后被译成具体名字或带暗示的措辞。
- **重译前文**：修订靠前 part 时，模型已经知道结局。
- **知识库污染**：`work/facts.md` 全书累积，译前文时检索到后文事实会顺着泄进去。

本 skill 把这条规则做成**独立的 genre 模块**（`references/genre-mystery.md`），
推理小说才加载，普通小说不受干扰。

### 2. 1:1 段落对齐 + sha256 完整性校验

- 每个非空行 = 一个段落，**译文行数必须严格等于原文段落数**（`delta=0`）
- `verify.py` 的 **V1–V12** 校验：原文 sha256 / manifest 自洽 / part 连续性 /
  **拼接回原文字符级全等** / 段落元数据 / 空译 / 异常压缩 / 重复 / 禁用标记 / 特殊行
- 段落级比例区间（ratio bands）自动捕捉"这段被翻没了"或"日文没翻完"，
  并支持 `--calibrate` 按真实译文校准
- 版式（段落间几个换行）由 `manifest.sep_newlines` **程序化重建**，
  不依赖模型是否保留了空行

### 3. 崩溃可恢复的状态机

- `state.json` 记录每个 part 的状态与输出 sha256
- claim 令牌 + heartbeat 防止并发抢同一个 part
- `.incoming` 暂存 + 原子写（`tmp` + `os.replace`）避免半写状态
- `settle` / `unlock` / `reset` 三种人工处置路径，旧译文自动归档到 `archive/`
- **绝不自动跳过失败的 part**：`attempts >= 3` 置 FAILED 并停止推进，等人处理

---

## 快速开始

```bash
# 1. 切分（只做一次）
python scripts/split.py --source 断锁.txt --plan     # 先干跑看计划
python scripts/split.py --source 断锁.txt

# 2. 初始化
python scripts/resume.py --source 断锁.txt init

# 3. 逐 part 翻译（串行循环）
python scripts/resume.py --source 断锁.txt next
python scripts/run_agent.py --source 断锁.txt --print-prompt
#   将译文写入 output/<part_id>.txt（一行对一段）
python scripts/run_agent.py --source 断锁.txt --part part_001 --commit

# 4. 合并
python scripts/merge.py --source 断锁.txt            # -> 断锁.zh.txt

# 5. 导出阅读版
python scripts/export_md.py --source 断锁.txt        # -> 断锁.zh.md
```

源文档**不写死文件名**。定位优先级：
提示词指定 → `--source` → 环境变量 `NOVEL_SOURCE` → 目录下唯一 `*.txt` → 多个则报错请你选。

输出名**以源文档名为准**：`断锁.txt` → `断锁.zh.txt` + `断锁.zh.md`。

---

## 目录结构

```
translate-jp2cn-novel/
├── SKILL.md                 主指令（加载进上下文）
├── README.md                本文件
├── LICENSE                  MIT
├── AGENTS.md                日文小说翻译 Agent 工作规范（24 条，六组）
├── references/
│   ├── punctuation.md       日译中标点映射规范
│   └── genre-mystery.md     推理小说可选模块（禁止剧透）
└── scripts/
    ├── _paths.py            源文档定位 + ROOT 推导
    ├── split.py             边界感知切分 + manifest
    ├── resume.py            状态机 / 审计 / 崩溃恢复
    ├── run_agent.py         单 part 原子翻译
    ├── verify.py            V1–V12 完整性校验
    ├── merge.py             确定性合并
    ├── export_md.py         导出阅读版 Markdown
    └── fix_quotes.py        标点规范化
```

---

## 为什么需要脚本，而不是让模型直接翻

因为翻译一本书里，**确定性工作必须由程序做**：

- 切分要"不破段落"——否则 1:1 对齐这个根基就没了
- 漏没漏译要靠行数与 sha256 比对——模型自己说"我翻完了"不可信
- 版式要程序化重建——模型经常丢空行
- 进度要外部化——模型遇到"完""终""THE END"就以为翻完了

这套脚本里藏着不少踩过的坑，例如：
"行尾连续换行的所有权归属**前一个** part"、"`sep_newlines` 是该行**之前**的换行数"、
"贪心窗口上界必须与文末取 min，否则末 part 越界"。现写一遍几乎必然出 bug。

---

## 设计取舍

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 翻译顺序 | **串行** | 小说的事实 / 时间线是增量累积的；并行会让后块拿不到前块新发现的人名与事实 |
| 二校润色 / 对照原文校 | **不做** | 实测 180 段逐段核对，0 漏译 0 错译；V10 的结构校验已足够 |
| 脚本形态 | **内置不简化，只参数化** | 保留崩溃恢复能力；写死的路径 / 温度 / 尺寸改为可配 |

---

## 标点映射

日文符号体系比中文丰富，直接统一会丢掉层级信息。推荐"中文规范优先"：

| 日文 | 功能 | 中文 |
| --- | --- | --- |
| `「」` | 对话 | `“”` |
| `『』` 嵌套 | 引用中的引用 | `‘’` |
| `『』` 独立 | 广播 / 告示 / 转述 | 保留 `『』`（以此区别于对话） |
| `『』` 书名 | 作品名 | `《》` |
| `〈〉` | 船名 / 店名 / 建筑名 | 保留 `〈〉` |
| `──` | 插入说明 | `——` |

详见 `references/punctuation.md`，可用 `scripts/fix_quotes.py` 自动统一。

---

## 注意

- 本仓库**不包含任何小说正文**。使用者的原文与译文受版权保护，请勿上传。
  `.gitignore` 已排除 `*.txt` 与 `parts/`、`output/`、`work/` 等过程产物。
- 若译本目标是**繁体中文**，`「」/『』` 反而是正确选择（台港规范）；
  本 skill 默认产出**简体中文**。

## 许可

MIT
