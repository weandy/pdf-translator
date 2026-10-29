/**
 * 使用指南：一页讲清从设置到下载的整个流程（顶栏“使用指南”进入），替代阅读器里弹出的提示条。
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
  ["privacy", "保留期限与隐私"],
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
        <p className="lead">
          把英文论文 PDF
          翻成中文，版式、公式、图表都保持原样；翻完可以在网页里对照阅读、逐段修改，再下载中文版或双语对照版。
        </p>

        <section id="start">
          <h2>开始前：填两项设置</h2>
          <p>点右上角“设置”，填好下面两项就能用。它们只保存在你的浏览器里，服务器不存。</p>
          <ol>
            <li>
              <strong>翻译服务 API Key</strong>：默认用 DeepSeek，到{" "}
              <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">
                DeepSeek 开放平台
              </a>{" "}
              创建。一篇 10–16 页的论文大约花 ¥0.03–0.07。也可以展开“用其他服务”，填 OpenAI 兼容接口或 Anthropic
              接口的地址和模型。填完会自动测试能不能连通。
            </li>
            <li>
              <strong>MinerU Token</strong>：用来识别版面（哪里是正文、公式、图表）。到{" "}
              <a href="https://mineru.net/apiManage" target="_blank" rel="noreferrer">
                mineru.net
              </a>{" "}
              免费申请，每天 1000 页优先额度。
            </li>
          </ol>
        </section>

        <section id="upload">
          <h2>上传与翻译</h2>
          <ul>
            <li>在首页选择或拖入 PDF，可以一次选多篇。扫描版也能翻。</li>
            <li>
              只选一篇时可以填<strong>页码范围</strong>，如 <code>1-10, 15</code>，只翻这些页，其他页保持原文。
            </li>
            <li>选好后会显示费用和用时预估。第一次上传要勾选“我知道文件会上传到云端解析”，之后会记住。</li>
            <li>
              如果这篇在这台电脑上翻过，会提醒你并给出上次的译文；确认后才会重新翻译。比对只在浏览器里进行，不发给服务器。
            </li>
            <li>
              翻译一般要 1–4
              分钟，可以先离开，浏览器标签上会显示进度，也可以开启完成通知。多篇会排队：版面解析同时进行，翻译一篇接一篇。
            </li>
            <li>排队中或进行中的任务可以取消，之后点“重新开始”会从中断的地方继续，已翻好的段落不重复计费。</li>
          </ul>
        </section>

        <section id="read">
          <h2>阅读</h2>
          <ul>
            <li>
              <strong>显示方式</strong>：“对照”左原文右译文；“译文”“原文”只看一边（手机默认“译文”）。缩放按钮调整大小。
            </li>
            <li>
              <strong>目录</strong>：以原文为准，优先用 PDF
              自带的书签，没有书签时用识别出的标题；每项下面是译文。点一项跳过去，原文和译文上对应的标题会短暂高亮。
            </li>
            <li>
              <strong>页码</strong>：工具栏显示“第 N / M 页”，输入页码回车即可跳转。下次打开会回到上次读到的页。
            </li>
            <li>
              <strong>链接</strong>：引用、图表编号、网址都能点，和原文一样跳转。
            </li>
            <li>鼠标移到译文段落上，原文对应的位置会同时显示底色，方便对照。</li>
          </ul>
        </section>

        <section id="edit">
          <h2>修改译文</h2>
          <ul>
            <li>
              <strong>点任意译文段落</strong>（或用 Tab
              键选中、回车）打开编辑面板：左边原文、右边译文，改完点“保存修改”。按 Esc 关闭。
            </li>
            <li>
              <strong>公式</strong>：译文里的 <code>&lt;m1/&gt;</code>{" "}
              等是公式的位置，每个保留一次。面板下方列出本段公式，点一下插入到光标处；实时预览修改后的效果。少了或多了公式会提示，修好之前不能保存。
            </li>
            <li>
              <strong>重翻</strong>：让模型重新翻译这一段；<strong>精修</strong>：开启思考再翻一次，更仔细，稍贵。
            </li>
            <li>
              <strong>修改记录</strong>：每次修改、重翻都会记下来，可以一步步撤回。
            </li>
            <li>
              <span className="swatch untranslated" aria-hidden="true" />
              <strong>灰字虚线框</strong>：这段译文没通过检查（例如漏了公式），暂时显示原文，点开后“重翻”。
            </li>
            <li>
              <span className="swatch overflow" aria-hidden="true" />
              <strong>黄色点线框</strong>：译文比原文长、放不下，已缩小字号，点开可以看全文。
            </li>
          </ul>
        </section>

        <section id="glossary">
          <h2>术语库</h2>
          <ul>
            <li>
              点右上角“术语库”管理自己的术语：添加、修改、批量删除；可以导入 CSV、TSV、Markdown
              表格、JSON，或直接粘贴两列文字；也能导出备份。
            </li>
            <li>新建任务和重翻时会自动带上你的术语，优先于自动抽取的术语和公共词库。</li>
            <li>
              阅读时点工具栏“术语”，可以看到这篇论文用了哪些术语、各出现在几段，以及哪些段落的译法可能不一致。改了某个术语的译法后，可以一键重翻用到它的段落。
            </li>
            <li>在编辑面板里选中原文的一个词，也能直接加入术语库。</li>
          </ul>
        </section>

        <section id="download">
          <h2>下载</h2>
          <ul>
            <li>阅读器右上角“下载 PDF”：中文版（原版式，正文换成中文）或双语对照版（每页原文与译文并排）。</li>
            <li>下载的文件包含你做的所有修改，链接也能点。下载中断后再点一次会接着下。</li>
          </ul>
        </section>

        <section id="privacy">
          <h2>保留期限与隐私</h2>
          <ul>
            <li>
              原文和译文在服务器上<strong>只保留 24 小时</strong>，到期自动删除。首页和阅读器会显示剩余时间，不足 3
              小时会提醒，请及时下载。
            </li>
            <li>
              只存在你浏览器里的：API Key
              与设置、术语库、“我的任务”、翻译历史（用于重复上传提醒）、阅读位置。换浏览器或清除网站数据后会消失。
            </li>
            <li>PDF 会上传到 MinerU 云端识别版面，正文会发给你设置的翻译服务。</li>
          </ul>
        </section>

        <section id="faq">
          <h2>常见问题</h2>
          <dl>
            <dt>版面解析失败了怎么办？</dt>
            <dd>多半是网络问题，系统会自动重试两次。仍然失败时点“重试”，会从中断的地方继续。</dd>
            <dt>某一段翻得不好？</dt>
            <dd>点开那一段，直接改，或点“精修”。如果是术语不对，把正确译法加进术语库，再重翻相关段落。</dd>
            <dt>费用怎么算？</dt>
            <dd>
              按你用的翻译服务实际消耗计费，阅读器顶部显示这篇已花的金额（包括重翻和精修）。版面解析用 MinerU 免费额度。
            </dd>
            <dt>可以自己部署吗？</dt>
            <dd>
              可以，用 Docker 部署，见{" "}
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
