import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { InlineMathText } from "@/shared/math/inline";

describe("行内公式两侧的富文本样式", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    host = document.createElement("div");
    root = createRoot(host);
  });
  afterEach(async () => act(async () => root.unmount()));

  it("公式后的中文和西文保留粗体，格式结束后普通正文不被加粗", async () => {
    await act(async () =>
      root.render(
        <InlineMathText
          text="<b>前文Before<m1/>后文After</b>普通"
          formulas={{ m1: { latex: "x^2" } }}
          fallbackUrl={() => ""}
        />,
      ),
    );
    expect([...host.querySelectorAll("b")].map((b) => b.textContent)).toEqual(["前文Before", "后文After"]);
    expect(host.querySelectorAll("b .zh")).toHaveLength(2);
    expect(host.querySelectorAll(".math .katex")).toHaveLength(1);
    expect(host.querySelector(".math")?.closest("b")).toBeNull();
    expect(host.textContent?.endsWith("普通")).toBe(true);
  });

  it("跨公式嵌套样式与链接定位仍正确，未知HTML不会生成元素", async () => {
    const activate = vi.fn();
    await act(async () =>
      root.render(
        <InlineMathText
          text="<b><i>开始<m1/>见[12]</i>结束</b><script>合成</script>"
          formulas={{ m1: { latex: "x" } }}
          fallbackUrl={() => ""}
          links={[{ text: "[12]", nth: 0, onActivate: activate }]}
        />,
      ),
    );
    const link = host.querySelector("a");
    expect(link?.textContent).toBe("[12]");
    expect(link?.closest("i")?.closest("b")).not.toBeNull();
    await act(async () => link?.click());
    expect(activate).toHaveBeenCalledTimes(1);
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).toContain("<script>合成</script>");
  });

  it("只包裹公式的样式不泄漏到后文，公式裁剪兜底保持可用", async () => {
    await act(async () =>
      root.render(
        <InlineMathText text="<b><m1/></b>普通" formulas={{}} fallbackUrl={(key) => `/synthetic/${key}.png`} />,
      ),
    );
    expect(host.querySelector("img")?.getAttribute("src")).toBe("/synthetic/m1.png");
    expect(host.querySelector("b")).toBeNull();
    expect(host.textContent).toBe("普通");
  });
});
