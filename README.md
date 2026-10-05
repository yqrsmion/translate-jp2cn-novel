# 日文小说翻译 skill

**指令**：`translate-jp2cn-novel`

把整本日文小说完整翻译成简体中文（日文 → 中文）。

不是"调一次大模型让它翻译"，而是一套可控、可校验、可恢复的翻译工程：
切块 → 串行逐块翻译 → 逐段校验 → 确定性合并 → 导出阅读版。

本 skill 的流程与脚本来自一次真实项目的完整落地：**23 万字、4595 段落、23 个分片，
零漏译**（原文 279,053 字符 → 译文 191,406 字符，字符比 0.71）。

---

## 能力

| 能力 | 说明 |
| --- | --- |
| **长篇分块** | 按自然边界（章节 / 空行 / 句末）切成小片，串行逐片翻译。绝不切断段落 |
| **完整性保障** | 1:1 段落对齐 + sha256 + V1–V12 校验，可捕捉漏译、重译、乱序、空译、异常压缩、重复、未翻译残留 |
| **跨片一致性** | `work/` 长期知识库（人物 / 术语 / 地点 / 关系 / 时间线 / 事实）按关键词自动注入每个分片 |
| **确定性合并** | 段落间换行由 `manifest.sep_newlines` 程序化重建，不依赖模型是否保留了空行 |
| **崩溃可恢复** | `state.json` 状态机 + claim 令牌 + 原子写，中断后可续译；失败的片不自动跳过 |
| **多格式输出** | `<原名>.zh.txt`（正文）+ `<原名>.zh.md`（阅读版，带章节标题层级） |
| **可选：题材模块** | 翻译推理小说时可加载「禁止剧透」措辞护栏（见 `references/genre-mystery.md`） |
| **可选：标点规范化** | 日文多套符号映射到中文符号体系（见 `references/punctuation.md`） |

---

## 快速开始

```bash
# 1. 切分（只做一次）
python scripts/split.py --source 断锁.txt --plan     # 建议先干跑看计划
python scripts/split.py --source 断锁.txt

# 2. 初始化
python scripts/resume.py --source 断锁.txt init

# 3. 逐片翻译（串行循环，直到 ALL_DONE）
python scripts/resume.py --source 断锁.txt next
python scripts/run_agent.py --source 断锁.txt --print-prompt
#   将译文写入 output/<part_id>.txt（一行对一段，行数必须与原文一致）
python scripts/run_agent.py --source 断锁.txt --part part_001 --commit

# 4. 合并
python scripts/merge.py --source 断锁.txt            # -> 断锁.zh.txt

# 5. 导出阅读版（可选）
python scripts/export_md.py --source 断锁.txt        # -> 断锁.zh.md
```

### 源文档定位（不写死文件名）

优先级：

1. 提示词中显式指定的文档（由 `--source` 传入）
2. `--source <路径>`
3. 环境变量 `NOVEL_SOURCE`
4. 工作目录下**唯一的** `*.txt`
5. 存在多个 `*.txt` → 停止并请用户指定，**绝不猜测**

`ROOT` = 源文档所在目录。所有产物（`parts/`、`output/`、`work/`、`manifest.json`、
`state.json`、`<原名>.zh.txt`、`<原名>.zh.md`）都落在 ROOT 下。

### 输出命名

以源文档名为准：`断锁.txt` → `断锁.zh.txt` + `断锁.zh.md`。

---

## 工作流程

```
源文档 .txt
   ↓  split.py      边界感知切分（不破段落）
parts/part_XXX.txt + manifest.json（逐段 sha256 / sep_newlines）
   ↓  resume.py init
state.json + work/ 知识库骨架
   ↓  run_agent.py  单片原子翻译（注入 work/ 相关条目 + 前一片尾部）
   ↓  verify.py V9–V12  行数 1:1 / 顺序 / 空译 / 比例 / 重复 / 禁用标记
output/part_XXX.txt
   ↓  merge.py      按 manifest 确定性重建
<原名>.zh.txt
   ↓  fix_quotes.py（可选）+ export_md.py（可选）
<原名>.zh.md
```

### 铁律

1. **1:1 段落对齐** —— 每个非空行 = 一个段落，译文行数必须严格等于原文段落数
2. **切分不破段落** —— 只在空行 / 句末边界切
3. **版式程序化重建** —— 不依赖模型保留空行
4. **完成判定外部化** —— 仅当 `state.json` 全部 DONE 才算完成，模型绝不自行判断"翻完了"

---

## 目录结构

```
translate-jp2cn-novel/
├── SKILL.md                 主指令（加载进上下文）
├── README.md                本文件
├── LICENSE                  MIT
├── AGENTS.md                日文小说翻译 Agent 工作规范（24 条，分六组）
├── references/
│   ├── punctuation.md       日译中标点映射规范
│   └── genre-mystery.md     推理小说可选模块
└── scripts/
    ├── _paths.py            源文档定位 + ROOT 推导
    ├── split.py             边界感知切分 + manifest
    ├── resume.py            状态机 / 审计 / 崩溃恢复
    ├── run_agent.py         单片原子翻译
    ├── verify.py            V1–V12 完整性校验
    ├── merge.py             确定性合并
    ├── export_md.py         导出阅读版 Markdown
    └── fix_quotes.py        标点规范化
```

---

## 为什么需要脚本

翻译一本书里的**确定性工作必须由程序做**：

- 切分要"不破段落" —— 否则 1:1 对齐这个根基就没了
- 漏没漏译要靠行数与 sha256 比对 —— 模型自己说"我翻完了"不可信
- 版式要程序化重建 —— 模型经常丢空行
- 进度要外部化 —— 模型遇到"完""终""THE END"就以为翻完了

脚本里藏着不少踩过的坑，例如："行尾连续换行的所有权归属**前一个**分片"、
"`sep_newlines` 是该行**之前**的换行数"、"贪心窗口上界必须与文末取 min，否则末片越界"。
临场重写几乎必然出 bug。

---

## 可选能力

### 推理小说模块

`references/genre-mystery.md`，仅当作品是推理小说时加载。

核心是**禁止剧透的措辞护栏**。它防的不是"凭空加一段剧透"，而是措辞层面的无意识泄漏：

- **他 / 她的性别强制标记** —— 日文常不标性别，中文必须选；若模型已知叙述者性别
  （叙述诡计的经典手法），译前文时就泄露了
- **暧昧指代被"知情"补全**
- **重译前文时模型已知道结局**
- **`work/` 知识库累积后文事实，污染前文**

### 标点映射

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

## 设计取舍

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 翻译顺序 | **串行** | 小说的事实 / 时间线是增量累积的；并行会让后片拿不到前片新发现的人名与事实 |
| 二校润色 / 对照原文校 | **不做** | 实测 180 段逐段核对，0 漏译 0 错译；V10 的结构校验已足够 |
| 脚本形态 | **内置不简化，只参数化** | 保留崩溃恢复能力；写死的路径 / 温度 / 尺寸改为可配 |

---

## 安装

本 skill 是**通用目录结构**（`SKILL.md` + `scripts/` + `references/`），
不依赖任何特定工具。

把整个 `translate-jp2cn-novel/` 目录复制到你所用工具的 user skills 目录即可
（各工具位置不同，通常在 `~/.<工具>/skills/` 下）。目录名保持 `translate-jp2cn-novel`。

---

## 注意

- 本仓库**不包含任何小说正文**。使用者的原文与译文受版权保护，请勿上传。
  `.gitignore` 已排除 `*.txt` 与 `parts/`、`output/`、`work/` 等过程产物。
- 本 skill 默认产出**简体中文**。若目标是繁体中文，`「」/『』` 反而是正确选择（台港规范）。

## 许可

MIT
