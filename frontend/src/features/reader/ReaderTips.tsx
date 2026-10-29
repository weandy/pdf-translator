/**
 * 阅读器操作提示：第一次打开时显示，“知道了”后不再自动显示（浏览器记住），工具栏“使用说明”随时打开。
 * 走查发现：页面上没有任何提示说明“点段落可以修改”，这是最核心的功能。
 */
import { readJSON, writeJSON } from "@/shared/storage/local";

const KEY = "pdftr.tips.reader.v1";

export function tipsSeen(): boolean {
  return readJSON<boolean>(KEY, false);
}

export function ReaderTips({ onClose }: { onClose: () => void }) {
  return (
    <aside className="reader-tips" aria-label="使用说明">
      <ul>
        <li>
          <strong>点任意段落</strong>：查看原文、修改译文、重翻或精修，把术语加入术语库；修改都有记录，可以按步撤回。
        </li>
        <li>
          <strong>键盘</strong>：Tab 键在段落间移动，回车打开，Esc 关闭。
        </li>
        <li>
          <span className="swatch untranslated" aria-hidden="true" />{" "}
          灰字虚线框：这段没通过校验，显示的是原文，点开后“重翻”。
          <span className="swatch overflow" aria-hidden="true" /> 黄色点线框：译文放不下，已缩小字号，点开看全文。
        </li>
        <li>
          <strong>显示方式</strong>：“对照”左右对照，“阅读”按正常字号连续阅读（手机默认）；“目录”可跳到章节。
        </li>
        <li>
          <strong>下载 PDF</strong>：中文版或双语对照版，包含你的修改。服务器只保留 24 小时，请及时下载。
        </li>
      </ul>
      <button
        type="button"
        className="primary"
        onClick={() => {
          writeJSON(KEY, true);
          onClose();
        }}
      >
        知道了
      </button>
    </aside>
  );
}
