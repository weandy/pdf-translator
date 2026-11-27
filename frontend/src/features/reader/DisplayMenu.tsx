/**
 * 阅读台「显示」弹出菜单：集中收敛外观主题、深色纸面、对照排布与设置入口，
 * 让顶栏在 1280px 笔记本屏上始终保持单行舒展不折行。
 */
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useAnchoredMenu } from "@/shared/feedback/useAnchoredMenu";
import type { DarkPaperMode, LightPaperMode, ResolvedTheme } from "@/shared/theme/theme";

interface Props {
  theme: ResolvedTheme;
  lightPaper: LightPaperMode;
  darkPaper: DarkPaperMode;
  side: "left" | "right";
  onThemeChange: (theme: ResolvedTheme) => void;
  onLightPaperChange: (mode: LightPaperMode) => void;
  onDarkPaperChange: (mode: DarkPaperMode) => void;
  onSideChange: (side: "left" | "right") => void;
  onOpenSettings?: () => void;
}

const LIGHT_PAPERS: readonly { mode: LightPaperMode; label: string; swatchClass: string }[] = [
  { mode: "white", label: "原白纸", swatchClass: "swatch-white" },
  { mode: "gray", label: "柔灰纸", swatchClass: "swatch-gray" },
  { mode: "green", label: "护眼绿", swatchClass: "swatch-green" },
];

export function DisplayMenu({
  theme,
  lightPaper,
  darkPaper,
  side,
  onThemeChange,
  onLightPaperChange,
  onDarkPaperChange,
  onSideChange,
  onOpenSettings,
}: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  const menu = useAnchoredMenu(open, trigger, close, "right", root);

  useLayoutEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>("button")?.focus({ preventScroll: true });
  }, [open, menu]);

  return (
    <div className="display-picker" ref={root}>
      <button
        ref={trigger}
        type="button"
        className={open ? "quiet toolbar-btn on" : "quiet toolbar-btn"}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="显示与设置"
        title="显示与设置（外观主题、护眼纸面、排布、模型与密钥）"
        onClick={() => setOpen((o) => !o)}
      >
        <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
          <line x1="4" y1="6" x2="20" y2="6" />
          <circle cx="9" cy="6" r="2" fill="currentColor" />
          <line x1="4" y1="12" x2="20" y2="12" />
          <circle cx="15" cy="12" r="2" fill="currentColor" />
          <line x1="4" y1="18" x2="20" y2="18" />
          <circle cx="11" cy="18" r="2" fill="currentColor" />
        </svg>
        <span className="wide-only">显示与设置</span>
      </button>
      {open &&
        createPortal(
          <div ref={menu} className="display-menu" role="menu" aria-label="显示与设置" style={{ position: "fixed" }}>
            <div className="display-menu-row">
              <span className="display-menu-label">外观主题</span>
              <div className="segmented segmented-compact" role="group" aria-label="外观主题">
                <button
                  type="button"
                  className={theme === "light" ? "on" : undefined}
                  aria-pressed={theme === "light"}
                  onClick={() => onThemeChange("light")}
                >
                  浅色
                </button>
                <button
                  type="button"
                  className={theme === "dark" ? "on" : undefined}
                  aria-pressed={theme === "dark"}
                  onClick={() => onThemeChange("dark")}
                >
                  深色
                </button>
              </div>
            </div>
            {theme === "light" ? (
              <div className="display-menu-section">
                <div className="display-menu-row">
                  <span className="display-menu-label">浅色纸面</span>
                  <div className="segmented segmented-compact" role="group" aria-label="浅色纸面">
                    {LIGHT_PAPERS.map((item) => (
                      <button
                        key={item.mode}
                        type="button"
                        className={lightPaper === item.mode ? "on" : undefined}
                        aria-pressed={lightPaper === item.mode}
                        onClick={() => onLightPaperChange(item.mode)}
                      >
                        <span className={`paper-swatch ${item.swatchClass}`} aria-hidden="true" />
                        <span>{item.label}</span>
                      </button>
                    ))}
                  </div>
                </div>
                <p className="display-menu-hint">仅调整屏幕阅读观感，导出 PDF 保持原色</p>
              </div>
            ) : (
              <div className="display-menu-section">
                <div className="display-menu-row">
                  <span className="display-menu-label">深色纸面</span>
                  <div className="segmented segmented-compact" role="group" aria-label="深色纸面">
                    <button
                      type="button"
                      className={darkPaper === "night" ? "on" : undefined}
                      aria-pressed={darkPaper === "night"}
                      onClick={() => onDarkPaperChange("night")}
                    >
                      暗夜纸
                    </button>
                    <button
                      type="button"
                      className={darkPaper === "dim" ? "on" : undefined}
                      aria-pressed={darkPaper === "dim"}
                      onClick={() => onDarkPaperChange("dim")}
                    >
                      原色纸
                    </button>
                  </div>
                </div>
                <p className="display-menu-hint">查看彩色实验图或热力图时可切回原色纸</p>
              </div>
            )}
            <div className="display-menu-row">
              <span className="display-menu-label">对照排布</span>
              <div className="segmented segmented-compact" role="group" aria-label="对照排布">
                <button
                  type="button"
                  className={side === "right" ? "on" : undefined}
                  aria-pressed={side === "right"}
                  onClick={() => onSideChange("right")}
                >
                  译文在右
                </button>
                <button
                  type="button"
                  className={side === "left" ? "on" : undefined}
                  aria-pressed={side === "left"}
                  onClick={() => onSideChange("left")}
                >
                  译文在左
                </button>
              </div>
            </div>
            {onOpenSettings && (
              <>
                <div className="display-menu-divider" role="separator" />
                <button
                  type="button"
                  className="display-menu-action"
                  role="menuitem"
                  onClick={() => {
                    close();
                    onOpenSettings();
                  }}
                >
                  <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.6.85 1 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                  </svg>
                  <span>模型与密钥设置…</span>
                </button>
              </>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
