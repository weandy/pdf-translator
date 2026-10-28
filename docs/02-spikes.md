# 验证实验记录（P0）

> 对应 [01-tech-stack.md §8](01-tech-stack.md#8-验证实验p0)。脚本在 [`spikes/`](../spikes/)，样本列表见 [`spikes/samples.txt`](../spikes/samples.txt)（arXiv 公开论文，PDF 本身不入库）。

## 实验环境

| 项 | 值 |
|---|---|
| 机器 | 2 核 CPU、3.8 GB 内存（其他服务已占约 2.7 GB）、无 GPU、无 swap |
| Python | 3.12，PyMuPDF 2.0，Playwright 1.64.1（Chromium 156） |
| 翻译 | 无真实 LLM Key，用 [`spikes/mock_llm.py`](../spikes/mock_llm.py) 伪翻译：保留占位符/标签，其余按"英文字符数 × 0.4"换成中文素材，只用来测版式 |

## S5 + S6：去字背景页 + 浏览器叠加层导出（2026-10-28）

脚本：[`spikes/s5_s6_overlay.py`](../spikes/s5_s6_overlay.py)。用 PyMuPDF 自带的文字块代替版面解析器，按字体名（CM*/MS*/Math/Symbol 等）粗略区分公式。

**数据**

| 样本 | 页 | 可翻译块 / 总块 | 最小缩放 | 溢出块 | Chromium 耗时 | 叠加层 | 输出 | 可提取中文字数 |
|---|---|---|---|---|---|---|---|---|
| 1706.03762（单栏） | 第 4 页 | 14 / 17 | 0.83 | 0 | 0.54 s | 62 KB | 212 KB | 907 |
| 1512.03385（双栏） | 第 1、6 页 | 32 / 66 | 0.80 | 0 | 0.57 s | 52 KB | 114 KB | 1533 |
| 1412.6980（公式密集） | 第 2、3 页 | 27 / 58 | 0.75 | 0 | 0.66 s | 217 KB | 321 KB | 1323 |

**结论：主链路成立 ✅**

- **去字干净**：`apply_redactions(images=NONE, graphics=REMOVE_IF_TOUCHED, text=REMOVE)` 只删文字块内的文字和完全落在块内的线条；图片、流程图、表格线、脚注分隔线、页码都保留。
  - 第一版用 `graphics=LINE_ART_NONE`，行内公式的**根号横线、分数线残留**在背景里；改成 `REMOVE_IF_TOUCHED` 后消失。
- **导出是全矢量**：输出 PDF 能直接提取出中文（上表最后一列），Chromium 自动嵌入字体子集（`AAAAAA+WenQuanYiZenHei` 形式）。
- **坐标对齐**：HTML 用 pt 单位绝对定位，`@page` 设为原页尺寸，打印后 `show_pdf_page` 合成，译文块与原文块位置一致，双栏也正确。
- **速度和体积可接受**：Chromium 每页约 0.2–0.3 s；叠加层每页几十 KB（行内公式裁剪图会让体积变大）。
- **排版拟合**：先调行距再二分字号，英译中比例下最小缩放 0.75，无溢出。

**暴露的问题：没有版面模型不行 ❌**

| 问题 | 现象 | 结论 |
|---|---|---|
| 独立公式被误判 | 1706.03762 公式 (1) 含 `Attention`、`softmax` 等罗马字体单词，被当成正文删掉并"翻译" | 必须由版面模型识别公式块，不能靠字体 |
| 行内公式字形碎片化 | TeX 行内公式里数字、括号用正文字体，只有斜体字母是数学字体；按字形裁剪出的图片互相重叠 | 行内公式需要解析器给出**整段公式的 bbox + LaTeX**，再用 KaTeX 渲染 |
| 算法框、伪代码 | 1412.6980 的 Algorithm 1 被拆成大量小块，译文与公式叠成一团 | 版面模型要识别 algorithm/code 块，默认不翻或按行翻 |
| 表格 | 1512.03385 表头被当成段落翻译，数字单元格未动 | 需要表格结构（单元格 bbox） |
| PyMuPDF 文字块互相重叠 | 公式密集处多个块的 bbox 交叠，译文重叠 | 融合阶段需按版面模型的块重新归组 span |

→ 印证选型：**PyMuPDF 负责坐标和去字、浏览器负责排版这两部分可行；版面语义（公式/算法/表格）必须交给 MinerU 等解析器**。

## S1：BabelDOC 基线（进行中）

- BabelDOC 0.6.4 用 `uv tool install BabelDOC` **装完无法启动**：`ImportError: cannot import name 'BitStream' from 'bitstring'`，原因是依赖 `bitstring` 已发布 5.0。需要 `uv tool install BabelDOC --with "bitstring<5"`。印证调研里"部署门槛高"的问题。
- 资源下载（`babeldoc --warmup`）：约 337 MB（版面模型、字体、cmap、tiktoken），首次下载有重试，耗时约 10 分钟。
- **内存不足被杀**：处理 1412.6980 单页、`--pool-max-workers 1`，峰值常驻内存约 **1.47 GB**，在版面检测阶段被系统 OOM 杀掉。本机可用内存约 1.1 GB。

→ 对自部署推荐配置的启示：**仅 BabelDOC 级别的 CPU 推理就需要 ≥2 GB 空闲内存**，加上 MinerU basic（官方最低 2 GB）、Chromium、worker，推荐配置至少 **4 核 / 8 GB**，最低 **2 核 / 4 GB 空闲**。

## 待做

| 实验 | 状态 | 阻塞 |
|---|---|---|
| S1 BabelDOC 基线 | 被 OOM 中断 | 需要更多内存；可改用沉浸式翻译 BabelDOC 在线版人工对比 |
| S2 MinerU 云端 API | 未开始（默认解析器，优先做） | 需要 mineru.net API Token |
| S3 Doc2X | 未开始 | 付费 Key（¥10 起） |
| MinerU 本地 CPU | 后置到 P4（本地模式） | 需要更大内存的机器 |
| S4 块与 span 对齐 | 未开始 | 依赖 S2 输出 |
| S7 术语系统 | 未开始 | 需要真实 LLM Key |
