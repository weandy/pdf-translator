# 阶段 A（前端）：阅读器显存预算、批量计页限流与静态缓存分层设计

> 状态：实施中（`feat/stage-a-frontend-guardrails`，基于 `5f617a9`）  
> 约束依据：`AGENTS.md` §3（前端单向分层）、§7（单机 Caddy + 1Panel OpenResty）、§8（零新增依赖、不放宽 CSP）。

---

## 1. 背景与问题实锤

经三方交叉源码审计确认，前端在长论文、高缩放与发版瞬间存在三类确定性护栏缺口：

1. **大图/高缩放页 Canvas 显存暴涨与解码缓存不释放**（`frontend/src/shared/pdf/pdf.tsx:168-188`）：
   - `PdfCanvas` 仅将 `devicePixelRatio` 封顶为 2，未限制画布总像素与最大边长。A4 页（595×842 pt）在最大缩放 `scale = 4`、`dpr = 2` 下单张 RGBA 位图达 `4760 × 6736 × 4 ≈ 128 MiB`，对照模式双画布达 **~256 MiB/页**（与 `AGENTS.md` §10 记录的 230 MB/页吻合），在 iOS Safari 更易触碰画布像素硬限导致白屏或崩溃。
   - 页面滑出虚拟窗口卸载时，`canvas.width / height` 未归零释放 GPU backing store；全库未调用 `PDFPageProxy.cleanup()`，扫描页大图解码缓存随滚动不断累加。
2. **多选 PDF 全并发读入内存**（`frontend/src/features/upload/UploadPanel.tsx:124-146`）：
   - 一次最多选 10 个 PDF，`pick()` 用 `Promise.all` 同时对所有文件执行 `countPages()`（内部调用 `file.arrayBuffer()`），在 200 MB 单文件上限下可能瞬间并发分配高达 ~2 GB 浏览器内存；且快速重选文件时，旧批次异步回调可能覆盖新选列表。
3. **发版后旧 `index.html` 启发式缓存导致哈希分包白屏**（`frontend/Caddyfile:30-34`）：
   - `file_server` 未给 `/assets/*`（带 Vite 内容哈希，含 1.32 MB `pdf.worker`、491 KB `pdf.js`）设长缓存，也未给 SPA 入口 `index.html` 显式设 `Cache-Control: no-cache`。部署新版本后，浏览器若缓存旧 `index.html`，会请求已删除的旧哈希 JS/CSS 导致白屏。

---

## 2. 方案设计

### 2.1 `PdfCanvas` 像素预算、卸载归零与协调式 `page.cleanup()`

全部封装在 `frontend/src/shared/pdf/pdf.tsx`（保持 `shared` 层职责纯净，零跨层依赖、零新增依赖）：

1. **像素与边长双封顶（纯函数 `canvasRenderScale`）**：
   - 桌面视口单画布像素上限 `MAX_CANVAS_PIXELS_DESKTOP = 6_000_000`（约 24 MiB RGBA，对照双画布 ≤ 48 MiB）；
   - 窄屏/移动端视口（`window.innerWidth <= 768`）单画布像素上限 `MAX_CANVAS_PIXELS_MOBILE = 4_000_000`（约 16 MiB RGBA）；
   - 单边最大像素 `MAX_CANVAS_DIMENSION = 4096`（规避移动端/WebGL 纹理极限）。
   - 计算过程：先取 `wanted = scale * Math.min(Math.max(dpr, 1), 2)`，按 `page.getViewport({ scale: 1 })` 原始宽高算目标像素，超出边长或面积上限时仅缩小 **canvas 内部背板渲染比例**；`.page` 与 `.pdf-canvas` 的 CSS 尺寸、文字层（`PdfTextLayer`）、`.hit-layer`、`.overlay` 与坐标命中全部维持原 `scale`，**坐标零漂移**。
2. **卸载归零 GPU Backing Store**：
   - `PdfCanvas` 维护挂载状态标记；组件卸载时取消在途渲染，并在该 canvas 的串行队列 `rendering` 结算后若仍处于卸载状态，将 `canvas.width = 0; canvas.height = 0`，避免在 pdf.js 渲染中途改尺寸触发异常，同时立即释放显存。
3. **安全协调的页级 `page.cleanup()`（避免与 `PdfTextLayer` 竞态）**：
   - `PdfCanvas` 与 `PdfTextLayer` 在原生页共用同一个 `doc.getPage(page + 1)` 返回的 `PDFPageProxy`。若 Canvas 绘制完毕立即 `cleanup()`，会干扰并发中的 `streamTextContent()`。
   - 在 `pdf.tsx` 内使用 `WeakMap<PDFDocumentProxy, Map<number, PageUsage>>` 维护页级引用计数：
     - `PdfCanvas` 与 `PdfTextLayer` 各自在 effect 启动时 `acquire`，拿到 `PDFPageProxy` 后登记；
     - 在各自清理且在途异步渲染/文本流结算后 `release`；
     - **仅当 `refs` 归零**（该页的 Canvas 与 TextLayer 全部离开虚拟窗口且无在途任务）时调用 `page.cleanup()`，既回收扫描大图解码缓存，又彻底消除跨层竞态。

### 2.2 `UploadPanel` 计页并发限流与批次序号防串写

- 在 `frontend/src/features/upload/UploadPanel.tsx`（及纯工具辅助函数）引入固定并发池 `MAX_INSPECT_CONCURRENCY = 2`：多选文件时最多同时检查 2 个文件的页数与指纹，峰值原始内存较 10 路全并发下降 **80%**。
- 引入递增序号 `pickSeqRef`：用户重新选择文件时，旧批次中尚未结束的 `countPages` / `fingerprint` / `api.job` 回调直接放弃更新状态，避免串写。

### 2.3 `frontend/Caddyfile` 静态资源缓存分层

在不影响 `/api/*`（后端自行控制 `Cache-Control`）与 `@admin_pages`（保持 `no-store`）的前提下，按路径显式分层：
- `/assets/*`（Vite 内容哈希产物）→ `Cache-Control "public, max-age=31536000, immutable"`
- `/pdfjs/*`（纯 JS 无哈希解码器）→ `Cache-Control "public, max-age=86400"`
- 其余 SPA HTML（排除 `/api/*`、`/assets/*`、`/pdfjs/*`、`/favicon.svg` 与管理入口）→ `Cache-Control "no-cache"`

---

## 3. 验收标准

- `pdf.test.tsx` 覆盖：`canvasRenderScale` 桌面/移动端面积与 4096 边长封顶、`PdfCanvas` 渲染按上限降比例、卸载结算后归零 `canvas.width/height`、`PdfCanvas` 与 `PdfTextLayer` 共用同页时仅在两者全部卸载后才调用一次 `page.cleanup()`。
- `UploadPanel.test.tsx` 覆盖：批量选 6 个 PDF 时 `countPages` 最大并发 ≤ 2；快速重选文件时旧批次延迟结果不覆盖新列表。
- 全量门禁通过：`npm run lint`、`npm run typecheck`、`npm test`、`npm run build`。
