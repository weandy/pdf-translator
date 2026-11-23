# 08 — 长论文阅读器虚拟化、搜索防抖与原文文字层设计文档

- **日期**：2026-11-23
- **分支**：`feat/reader-virtualization-text-layer`
- **改动范围**：仅前端（上线时仅需重建 `pdf-translator-web`，不涉及后端接口或数据库 schema 变更）
- **状态**：已批准，分两阶段实施

---

## 1. 背景与现状诊断

基于对 [ReaderView.tsx](../frontend/src/features/reader/ReaderView.tsx)、[pdf.tsx](../frontend/src/shared/pdf/pdf.tsx)、[search.ts](../frontend/src/features/reader/search.ts) 与 [styles.css](../frontend/src/app/styles.css) 的代码审查，长论文阅读器存在三类瓶颈：

1. **全量页挂载与观察器膨胀**
   - `ReaderView` 对 `doc.pages` 的每一页都渲染完整的 `<PageRow>`，且每页通过 `useNearViewport` 独立创建一个 `IntersectionObserver`（100 页论文即挂 100 个观察器）。
   - 远端页（`near === false`）虽未挂载 `<PdfCanvas>` 与 `.overlay`，但原文侧所有 `.hit` 热区 `div` 仍无条件常驻 DOM（100 页约数千个无用节点）。
   - `PageRow` 未做 `React.memo`，每次 `hovered`、`current`（滚动更新页码）或 `searchQuery` 变化都会触发所有页的 React diff。
2. **搜索实时同步重算**
   - 每次按键立即执行 `searchDocument(doc, searchQuery)`，同步重建 `pageByBlock` 映射并对全部 `doc.segments` 执行正则清洗与子串匹配，同时触发全量页面重渲染。
3. **原文页文字层体验不足**
   - 当前原文页的 `.source-text-layer` 是按 MinerU 块级 `bbox` 放置的透明 `div`，不是真正的 pdf.js `TextLayer`。
   - 存在三个问题：① 块级文本的行距与字宽无法贴合画布文字，划词选区错位；② 未入翻译块的区域（图表内文字、页眉页脚、脚注、独立公式等）无法选中复制；③ `.source-text-layer`（`z-index: 1`）与 `.hit` 层叠存在交互冲突。

---

## 2. 约束与设计决策

### 2.1 硬性约束
- **分层规则**：遵守 `frontend/eslint.config.js`（`app → pages → features → entities → shared`，features 互不导入，跨目录统一使用 `@/` 别名）。
- **零新增依赖**：复用已安装的 `pdfjs-dist@6.4.299`（`pdfjs-dist/legacy/build/pdf.mjs` 原生导出 `TextLayer` 与 `setLayerDimensions`）与 React 19。
- **既有交互零回归**：目录跳转、页码输入跳转、内部链接跳转、搜索命中跳转、术语 `spot` 定位、修改记录 `revealKey` 定位、打开时恢复上次阅读页（`pdftr.pos.v1`）、手机（390×844）与桌面（1440×900）自适应缩放均保持准确。
- **扫描页隐私与质量约束**：按 `AGENTS.md §8`，扫描页（`page.kind !== "native"`）的 PDF 隐形 OCR 层质量差已被后端丢弃，不得直接渲染 pdf.js 原生文字层。

### 2.2 决策记录（已批准）
- **决策 1（虚拟化 — 方案 A1）**：基于精确页高的零依赖窗口虚拟化。拒绝引入 `@tanstack/react-virtual`（无需动态测量、避免新增依赖）和半虚拟化方案（仍留大量 DOM 与滚动遍历）。
- **决策 2（搜索 — 方案 B1）**：预构建归一化搜索索引 + 输入防抖（150ms）与按页收敛命中状态。拒绝每次按键重新正则清洗全量文档。
- **决策 3（原文文字层 — 方案 C1）**：原生页（`page.kind === "native"`）采用 pdf.js `TextLayer` + 交互分层解耦；扫描页（`page.kind !== "native"`）保留基于 `blocks` 的降级可选层并修复其与高亮层的层叠关系。
- **决策 4（搜索范围 — 选项 A）**：顶栏“全文搜索”继续检索 `doc.segments`（原文 + 译文段落，预建索引后毫秒级响应、支持全篇跨页跳转）；原文页非段落区域交由 pdf.js `TextLayer` 支持精准选中复制与当前已挂载页的浏览器原生 `Ctrl+F`。
- **决策 5（实施节奏 — 选项 1）**：拆分为两个清晰阶段，每阶段独立 TDD、全量校验与 Git 提交。

---

## 3. 阶段 1 设计：长论文阅读器虚拟化 + 搜索索引与防抖

### 3.1 几何虚拟化纯函数模块：`frontend/src/features/reader/virtual.ts`

每页 PDF 原始宽高（pt）在 `doc.pages` 中静态已知，行间距固定为 `ROW_GAP = 28`（对应 `.page-row` 间距）。

```ts
export const PAGE_ROW_GAP = 28;
export const DEFAULT_OVERSCAN_PX = 1200;

export interface PageMetric {
  index: number;      // page.index（数据页索引）
  order: number;      // 在 doc.pages 数组中的顺序位置
  top: number;        // 距离虚拟列表顶部的精确偏移（px）
  height: number;     // 渲染高度 page.height * scale（px）
  bottom: number;     // top + height
}

export interface PageLayout {
  items: PageMetric[];
  byPageIndex: Map<number, PageMetric>;
  totalHeight: number;
}
```

核心算法（全部纯函数，无 DOM 副作用，便于单测）：
1. `buildPageLayout(pages, scale, gap = PAGE_ROW_GAP): PageLayout`
   - 一次遍历累计前缀和算出每页 `top`、`height`、`bottom` 与 `totalHeight`（最后一页不加尾部 gap）。
2. `visiblePageRange(layout, scrollTop, viewportHeight, overscan = DEFAULT_OVERSCAN_PX): [number, number]`
   - 计算可视扩展区间 `[minY, maxY] = [Math.max(0, scrollTop - overscan), scrollTop + viewportHeight + overscan]`。
   - 二分查找第一个 `bottom >= minY` 的 `start` 与最后一个 `top <= maxY` 的 `end`。
   - 返回顺序索引闭区间 `[start, end]`（空文档返回 `[0, -1]`）。
3. `currentPageAt(layout, scrollTop, anchorOffset = 80): number`
   - 二分查找最后一个 `top <= scrollTop + anchorOffset` 的页，返回其 `page.index`（替代原先每帧遍历所有 `.page-row` 的 DOM 查询）。
4. `pageJumpTop(layout, pageIndex, topInPt, scale, inset = 56, paddingTop = 0, actualScale?: number): number | null`
   - 直接由 `paddingTop + metric.top + topInPt * (actualScale ?? scale) - inset` 计算滚动纵坐标（最小为 0）。
   - **关键兼容性**：如果目标页当前恰好已挂载且其 `.page` 实测宽度与 `page.width * scale` 有偏差（如打开侧栏或出现滚动条导致布局刚更新但 `ResizeObserver` 尚未完成同步），优先采用实测 `shownWidth / page.width` 作为 `actualScale`，确保完全通过现有回归测试（`ReaderView.test.tsx`）。

### 3.2 `ReaderView.tsx` 窗口化与渲染收敛

- **容器结构**：
  - `.pages` 维持滚动容器身份（`ref={scroller}`，`padding: 24px` / 手机 `12px`）。
  - 内部新增 `<div className="pages-canvas" style={{ height: layout.totalHeight }}>`（`position: relative; width: 100%`）。
  - 仅渲染 `doc.pages.slice(range[0], range[1] + 1)` 的 `<PageRow>`，每行采用绝对定位 `style={{ transform: \`translateY(\${metric.top}px)\` }}`（`.page-row` 设为 `position: absolute; top: 0; left: 0; right: 0;`）。
- **零 `IntersectionObserver`**：
  - 删除 `useNearViewport` Hook；窗口内渲染的 `<PageRow>` 默认即为可见或缓冲区页面，直接挂载其内容；滚出缓冲区自动卸载释放 `<canvas>` 与 DOM。
- **视口状态同步**：
  - 复用已有的 `ResizeObserver` 同时记录容器 `width` 与 `height`（`viewportHeight`，在 jsdom 无尺寸环境下提供合理默认回退 `800`）。
  - 滚动时在 `requestAnimationFrame` 内更新 `scrollTop` 与 `current`（用状态相等判断避免无意义重渲染，仅当 `[start, end]` 或 `current` 真正变化时触发更新！）。
  - **重要优化**：将 `scrollTop` 映射到 `[visibleStart, visibleEnd]` 状态，只有窗口范围发生变化时才触发 `ReaderView` 重渲染，避免每像素滚动都重渲染 React 树。
- **跳转定位解耦 DOM**：
  - `goTo(page, top, smooth, inset)` 不再依赖 `querySelector('.page-row[data-page=...]')`，直接用 `pageJumpTop` 算偏移调用 `el.scrollTo`，并同步把该目标页所在滚动位置纳入可见窗口（即使目标页之前未挂载也能立即命中并在下一帧渲染）。
  - `selectedSegment` / `revealKey` 跳页逻辑同样直接通过 `layout` 判断目标页是否在 `[scrollTop, scrollTop + viewportHeight]` 视野外，若是则直接调用 `goTo(page.index, 0, false, 0)`。
- **`PageRow` 记忆化与按页 Props 收敛**：
  - 用 `React.memo(PageRow)` 包裹；
  - 将整篇文档级的状态按页筛选后再下传：
    - `hoveredSegId`：仅当 `hovered` 属于该页时传入，否则为 `null`；
    - `selectedSegId`：仅当 `selected` 属于该页时传入，否则为 `null`；
    - `flashSegId`：仅当 `flash` 属于该页时传入，否则为 `null`；
    - `currentSearchSegId`：仅当 `currentSearchId` 属于该页时传入，否则为 `null`；
    - `pageSearchIds` / `pageMarkedIds`：仅在该页存在命中段时传递稳定引用的 `Set`（或 `undefined`）；
    - `pageBlocks`：按页预先 `useMemo` 过滤好，避免每次渲染重复 `page.blocks.filter(...)`。
  - 这样在单页悬停/选中段落时，窗口内其余页 props 引用完全不变，零重渲染。

### 3.3 搜索索引预构建与防抖：`frontend/src/features/reader/search.ts`

- 新增接口：
  ```ts
  export interface SearchEntry {
    segmentId: string;
    page: number;
    source: string;
    target: string;
    sourceNorm: string;
    targetNorm: string;
  }

  export interface SearchIndex {
    entries: SearchEntry[];
  }

  export function buildSearchIndex(doc: DocumentData): SearchIndex;
  export function querySearchIndex(index: SearchIndex, query: string): SearchHit[];
  export function searchDocument(doc: DocumentData, query: string): SearchHit[]; // 保留向后兼容
  ```
- 在 `ReaderView.tsx` 中：
  - `const searchIndexData = useMemo(() => buildSearchIndex(doc), [doc]);`（只在 `doc` 变化时执行一次正则清洗与页号映射）。
  - 输入框即时绑定 `searchQuery`，通过 `150ms` 防抖产生 `debouncedQuery`（当输入清空或按下 `Enter` 时立即同步，不让回车等待防抖定时器）。
  - `searchResults = useMemo(() => querySearchIndex(searchIndexData, debouncedQuery), [searchIndexData, debouncedQuery])`。

---

## 4. 阶段 2 设计：原文页 pdf.js `TextLayer` 与交互分层

### 4.1 通用组件：`PdfTextLayer`（`frontend/src/shared/pdf/pdf.tsx`）

在 `shared/pdf/pdf.tsx` 中新增 `PdfTextLayer` 组件（与 `PdfCanvas` 同级，保持架构纯净不依赖业务层）：

- **Props**：
  ```ts
  interface TextLayerProps {
    doc: PDFDocumentProxy;
    page: number; // 0 起
    scale: number; // CSS 像素 / pt
  }
  ```
- **生命周期与缩放优化**：
  1. 首次挂载或 `doc` / `page` 变化时：
     - 调用 `doc.getPage(page + 1)`，获取 `viewport = p.getViewport({ scale })`；
     - 在容器元素上设置 CSS 变量 `--total-scale-factor: String(scale)`，并清空旧子节点；
     - 创建 `new pdfjs.TextLayer({ textContentSource: p.streamTextContent(), container, viewport })` 并执行 `render()`；
     - 若渲染期间卸载或切换页，调用 `textLayer.cancel()` 忽略取消异常。
  2. 仅 `scale` 变化且当前 `TextLayer` 已渲染完成时：
     - 更新 `--total-scale-factor` 并调用 `textLayer.update({ viewport: p.getViewport({ scale }) })`，**无需重新向 worker 请求文本流与重建数百个 `<span>`**，缩放丝滑流畅。
  3. 卸载清理：取消未完成任务、重置 ref 并清空容器，防止内存泄漏。

### 4.2 最小化隔离样式：`frontend/src/app/styles.css`

从 `pdfjs-dist@6.4.299` 的 `web/pdf_viewer.css` 提炼最小必需的 `.pdf-text-layer`（带 `.textLayer` 类名）样式（约 40 行），限定作用域在 `.page > .pdf-text-layer`，不引入整份 6000+ 行 `pdf_viewer.css`，避免样式污染与包体积膨胀：
- 支持 `--total-scale-factor`、`--min-font-size: 1`、`--text-scale-factor`、`--scale-x`、`--rotate`；
- 透明文字（`color: transparent`）、`user-select: text`、`cursor: text`、`::selection` 半透明选区高亮；
- `.markedContent { display: contents; }`、`br::selection { background: transparent; }`。

### 4.3 原文页交互分层与坐标命中（解决选区与点击联动冲突）

原文页 `.page` 内部从底到顶严格分为 4 层：

| 层级 | `z-index` | 元素 | 职责 | 指针事件 |
| :--- | :---: | :--- | :--- | :--- |
| 0 | `0` | `<PdfCanvas>` | 原文 PDF 位图渲染 | `none` |
| 1 | `1` | `.hit-layer`（含 `.hit` 块） | 仅做视觉高亮底纹（`hovered` / `selected` / `flash` / `marked` / `search-hit` / `search-current`） | `pointer-events: none` |
| 2 | `2` | **原生页**：`<PdfTextLayer>`<br>**扫描页**：降级 `.source-text-layer` | 原生页由 pdf.js 提供逐字精准划选/复制/`Ctrl+F`；扫描页（`page.kind !== "native"`）退回按 `blocks` 的透明可选层 | `auto`（可拖选文字） |
| 3 | `3` | `<LinkLayer>` | 原文 PDF 外部链接与内部跳转热区 | `auto`（点击 `stopPropagation`） |

**悬停联动与点击编辑机制**（新增纯函数 `hitBlockAt(blocks, xPt, yPt)`）：
- 在原文 `.page` 容器上监听 `onMouseMove`、`onMouseLeave`、`onClick`：
  - 将客户端坐标 `(e.clientX, e.clientY)` 相对 `.page` 左上角换算为 PDF 点坐标 `(xPt, yPt) = ((clientX - rect.left) / scale, (clientY - rect.top) / scale)`；
  - 查 `hitBlockAt(blocks, xPt, yPt)`（命中包含该点的 `block`）：
    - `onMouseMove`：若命中的 `segment.id` 变化则调用 `onHover(segId)`，否则不触发状态更新（避免高频无效重渲染）；
    - `onMouseLeave`：调用 `onHover(null)`；
    - `onClick`：若 `window.getSelection()?.toString()` 非空（用户正在划词复制），直接忽略不抢焦；否则命中某段落时调用 `onSelect(segId)` 打开/切换右侧编辑面板。
- **术语高亮**：
  - 原文原生页在 `PdfTextLayer` 渲染后经 `highlightTextLayerSpans` 实现无损词级 `<mark class="term-highlight">` 高亮（支持单 span 及跨多 span 匹配，清空时还原原文本且不改 pdf.js 定位样式），同时保留 `.hit.marked` 段落底纹；扫描页降级层保留 `highlightPlainText` 词级高亮；译文侧 `OverlayBlock` 的词级高亮保持不变。
- **旋转页与异常降级**：
  - `.pdf-text-layer` 补齐 `data-main-rotation="90" | "180" | "270"` 旋转变换规则；`getPage` / `render` / `update` 遇非 `AbortException` 错误时取消实例、清空残留 DOM 并通过 `onError` 自动降级到该页的 `.source-text-layer`。

---

## 5. 影响文件清单

- **新增**：
  - `frontend/src/features/reader/virtual.ts`（虚拟化几何纯函数 + 坐标命中纯函数）
  - `frontend/src/features/reader/virtual.test.ts`（几何布局、二分窗口、当前页、跳转偏移、坐标命中单测）
  - `frontend/src/shared/pdf/pdf.test.tsx`（`PdfTextLayer` 渲染、缩放 `update`、切换/卸载 `cancel` 竞态单测）
- **修改**：
  - `frontend/src/features/reader/search.ts` & `search.test.ts`（索引预构建 + 兼容测试）
  - `frontend/src/features/reader/ReaderView.tsx` & `ReaderView.test.tsx`（虚拟窗口、搜索防抖、按页记忆化、原文页分层与 `PdfTextLayer` 接入）
  - `frontend/src/shared/pdf/pdf.tsx`（新增 `PdfTextLayer`）
  - `frontend/src/app/styles.css`（`.pages-canvas`、绝对定位 `.page-row`、`.hit-layer`、`.pdf-text-layer` 样式）
  - `docs/WORKLOG.md`（目标、验收标准与实测结果）

---

## 6. 测试与验收计划

1. **单元测试（Vitest / jsdom，Node 22）**：
   - `virtual.test.ts`：覆盖不等高多页布局、首/中/尾滚动窗口边界、大文档（150 页）二分性能与正确性、`currentPageAt`、`pageJumpTop`、坐标命中 `hitBlockAt`。
   - `search.test.ts`：覆盖 `buildSearchIndex` + `querySearchIndex` 中英文、公式占位符清洗、大小写与空查询。
   - `pdf.test.tsx`：覆盖 `PdfTextLayer` 首次 `render`、仅 `scale` 变化走 `update` 不重建、快速翻页/卸载调用 `cancel`。
   - `ReaderView.test.tsx`：
     - 既有 3 项阅读位置恢复、手动定位优先级、实际页宽缩放校准测试全部通过；
     - 新增：100 页文档初始只挂载窗口内极少数 `.page-row`（DOM 数显著低于 100），零 `IntersectionObserver` 调用；
     - 新增：滚动后窗口切片切换并更新当前页码；
     - 新增：目录/搜索/页码跳转到未挂载的远端页（如第 80 页）仍计算精确 `scrollTo`；
     - 新增：搜索输入 150ms 防抖（输入中不立即重算，150ms 后或按 Enter 立即生效）；
     - 新增：原生页渲染 `PdfTextLayer`，扫描页降级 `.source-text-layer`；原文页点击坐标命中段落、有文字选区时不误触选中。
2. **静态与构建门禁**：
   - `npm test`、`npm run lint`（含架构分层检查）、`npm run typecheck`、`npm run build` 全部通过。
3. **跨模型评审**：
   - 完成后委托 Grok 4.7 对整个分支 diff、设计一致性、边界竞态与测试充分性进行独立评审，并修复有效意见。
