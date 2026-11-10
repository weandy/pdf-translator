/**
 * 使用指南：用最短路径说明设置、翻译、阅读、修改和下载。
 */
import { useEffect } from "react";
import { Link } from "react-router";

const SECTIONS = [
  ["start", "开始前"],
  ["upload", "上传与翻译"],
  ["read", "阅读"],
  ["edit", "修改译文"],
  ["glossary", "术语库"],
  ["download", "下载"],
  ["privacy", "期限与隐私"],
  ["faq", "常见问题"],
] as const;

export function GuidePage() {
  useEffect(() => {
    document.title = "使用指南";
  }, []);

  return (
    <main className="guide">
      <nav className="guide-nav" aria-label="指南目录">
        <strong>使用指南</strong>
        <ol>
          {SECTIONS.map(([id, title]) => (
            <li key={id}>
              <a href={`#${id}`}>{title}</a>
            </li>
          ))}
        </ol>
      </nav>

      <article className="guide-body">
        <h1>使用指南</h1>
        <p className="lead">上传英文论文，按原版式阅读中文译文；需要时逐段修改，再下载中文版或双语版。</p>

        <section id="start">
          <h2>开始前</h2>
          <p>
            打开右上角“设置”，填写翻译服务 API Key 即可开始。若本站没有提供版面解析额度，再填写自己的 MinerU Token。Key
            只保存在浏览器里，翻译时随请求转发给服务商。
          </p>
          <ol>
            <li>
              <strong>翻译服务 API Key</strong>：默认使用 DeepSeek，也支持 OpenAI 兼容接口和 Anthropic。可在{" "}
              <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">
                DeepSeek 开放平台
              </a>{" "}
              创建。 使用其他服务时，展开接口设置，填写地址和 Key
              后点击“获取模型列表”，选择模型再测试连接；也可以手动填写模型名称。
            </li>
            <li>
              <strong>MinerU Token（可选）</strong>：负责识别正文、公式和图表版面。本站已配置共享 Token
              时可以留空；填写自己的 Token 会优先使用它，在{" "}
              <a href="https://mineru.net/apiManage" target="_blank" rel="noreferrer">
                mineru.net
              </a>{" "}
              创建。Token 是 MinerU 解析服务凭据，不等同于翻译服务 API Key。
            </li>
          </ol>
        </section>

        <section id="upload">
          <h2>上传与翻译</h2>
          <ul>
            <li>首页可以选择或拖入 PDF，一次最多上传 10 篇；扫描版也支持。</li>
            <li>
              只选一篇时可填写页码范围，例如 <code>1-10, 15</code>。
            </li>
            <li>开始前会显示费用和用时预估，首次上传需要确认文件会发送到 MinerU 云端。</li>
            <li>多篇任务会组成一个批次，解析并行、翻译排队；首页“我的批次”显示每篇状态。</li>
            <li>任务可以取消。失败或取消后点“重试/重新开始”，会从中断阶段继续，已完成内容不重复计费。</li>
            <li>
              未成功的段落会自动单独重试，每段最多尝试 10 次；补救阶段最多 2 分钟，先达到次数或时限就停止并提示原因。
            </li>
          </ul>
        </section>

        <section id="read">
          <h2>阅读</h2>
          <ul>
            <li>
              <strong>对照</strong>同时显示原文和译文；也可切换为只看译文或原文，手机默认只看译文。
            </li>
            <li>使用缩放、页码和目录跳转；重新打开任务会回到上次阅读位置。</li>
            <li>点击“搜索”可以搜索原文和译文，结果会定位到段落；页面文字可以选择、复制。</li>
            <li>引用、图表编号和网址链接可以直接点击；鼠标移到译文段落时，原文对应位置会高亮。</li>
          </ul>
        </section>

        <section id="edit">
          <h2>修改译文</h2>
          <ul>
            <li>点击译文段落，或用 Tab 选中后按回车，打开编辑面板。</li>
            <li>
              原文只读，译文可直接修改；公式占位符如 <code>&lt;m1/&gt;</code> 必须各保留一次，面板会实时检查。
            </li>
            <li>可以复制原文或译文，也可以“重翻”或“精修”；所有修改都记录在“修改记录”里，可逐步撤回。</li>
            <li>
              修改记录会突出新增内容、划掉删除内容；默认显示变化附近文字，展开可看完整前后对比，点击页码可定位原段落。
            </li>
            <li>点击顶部“未完成翻译 · 点击定位”，会定位并高亮问题段落；在编辑面板用“上一处/下一处”逐段检查。</li>
            <li>手动重翻也会自动重试，每次操作最多等待 45 秒；未成功时会显示原因，已有的可用译文继续保留。</li>
            <li>
              <span className="swatch untranslated" aria-hidden="true" />
              灰色虚线表示尚无可用译文，
              <span className="swatch overflow" aria-hidden="true" />
              黄色点线表示译文过长。
            </li>
          </ul>
        </section>

        <section id="glossary">
          <h2>术语库</h2>
          <ul>
            <li>在“术语库”页面添加、搜索、排序、批量删除、导入和导出术语。</li>
            <li>我的术语优先于自动抽取术语和公共词库；说明字段只保存在浏览器，不发送给翻译服务。</li>
            <li>阅读器的“术语”面板会显示本文用到的术语和可能不一致的段落，改译法后可以批量重翻。</li>
          </ul>
        </section>

        <section id="download">
          <h2>下载</h2>
          <ul>
            <li>任务完成后，在阅读器工具栏的“下载 PDF”中选择中文版或双语版。</li>
            <li>下载会包含你的手动修改和链接；中断后再次点击可以继续。</li>
          </ul>
        </section>

        <section id="privacy">
          <h2>保留期限与隐私</h2>
          <ul>
            <li>原文和译文默认在服务器保留 24 小时，到期自动删除；完成后请及时下载。</li>
            <li>Key、设置、术语库、任务入口、历史指纹和阅读位置保存在当前浏览器，清除站点数据后会消失。</li>
            <li>PDF 会发送给 MinerU 识别版面，正文会发送给你设置的翻译服务，请勿上传涉密文件。</li>
          </ul>
        </section>

        <section id="faq">
          <h2>常见问题</h2>
          <dl>
            <dt>解析失败怎么办？</dt>
            <dd>先检查 Token 和网络，按页面提示重试；任务会从已完成阶段继续。</dd>
            <dt>某一段翻得不好怎么办？</dt>
            <dd>直接编辑、重翻或精修；术语问题可以先改术语库，再批量重翻。</dd>
            <dt>费用怎么算？</dt>
            <dd>按翻译服务实际用量计费，重翻和精修也会计入任务费用；MinerU 使用其云端额度。</dd>
            <dt>可以自己部署吗？</dt>
            <dd>
              可以，部署方式见{" "}
              <a href="https://github.com/weandy/pdf-translator" target="_blank" rel="noreferrer">
                源代码仓库
              </a>{" "}
              的 README。
            </dd>
          </dl>
        </section>

        <p className="guide-back">
          <Link to="/">回到首页开始翻译</Link>
        </p>
      </article>
    </main>
  );
}
