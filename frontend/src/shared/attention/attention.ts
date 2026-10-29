/**
 * 任务完成提醒：浏览器标签标题与桌面通知。批量任务要等几分钟，用户多半切去做别的事。
 */
import { useEffect, useState } from "react";

export const APP_TITLE = "PDF 保版式翻译";

/** 设置标签标题，卸载时恢复默认。 */
export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = title;
    return () => {
      document.title = APP_TITLE;
    };
  }, [title]);
}

/** 页面当前是否在前台。 */
export function useVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

export type NotifyState = "unsupported" | "denied" | "granted" | "default";

export function notifyState(): NotifyState {
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

/** 申请通知权限（必须由用户点击触发）。 */
export async function askNotify(): Promise<NotifyState> {
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.requestPermission();
}

/** 页面在后台、且用户同意过时发桌面通知；点通知回到这个页面。 */
export function notify(title: string, body: string): void {
  if (notifyState() !== "granted" || document.visibilityState !== "hidden") return;
  try {
    const n = new Notification(title, { body, tag: body });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    // 有的浏览器（如安卓 Chrome）只允许在 Service Worker 里发通知，忽略即可
  }
}
