import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { InlineMathText, renderLatex } from "@/shared/math/inline";

describe("InlineMathText", () => {
  it("公式用 KaTeX 渲染，无效公式回退为裁剪图", () => {
    const html = renderToStaticMarkup(
      <InlineMathText
        text="设<m1/>，且<m2/>。"
        formulas={{ m1: { latex: "f(x)" }, m2: { latex: "\\frac{1}{" } }}
        fallbackUrl={(k) => `/crop/${k}.png`}
      />,
    );
    expect(html).toContain('class="katex"');
    expect(html).toContain('src="/crop/m2.png"');
    expect(html).toMatch(/<span class="zh">设<\/span><span class="math">/); // 文字与公式按原顺序排列，中文用中文字体
  });

  it("上下标渲染为真实元素", () => {
    const html = renderToStaticMarkup(<InlineMathText text="作者<sup>∗</sup>" formulas={{}} fallbackUrl={() => ""} />);
    expect(html).toBe('<span class="zh">作者</span><sup>∗</sup>');
  });

  it("渲染结果有缓存", () => {
    expect(renderLatex("x^2")).toBe(renderLatex("x^2"));
    expect(renderLatex("\\notacommand{")).toBeNull();
  });
});
