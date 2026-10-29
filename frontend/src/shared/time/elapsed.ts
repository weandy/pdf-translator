/** 已用时长的显示与每秒刷新。 */
import { useEffect, useState } from "react";

export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

/** 返回当前时间戳，active 为真时每秒更新一次。 */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

/** 距到期还剩多久：“还剩 5 小时”“还剩 40 分钟”；已过时返回“即将删除”。 */
export function formatRemaining(expiresAt: string, now: number = Date.now()): string {
  const minutes = Math.floor((Date.parse(expiresAt) - now) / 60000);
  if (minutes <= 0) return "即将删除";
  if (minutes < 60) return `还剩 ${minutes} 分钟`;
  return `还剩 ${Math.floor(minutes / 60)} 小时`;
}

/** 剩余时间不足这么多小时时醒目提醒下载。 */
export const EXPIRY_WARN_HOURS = 3;

export function expiresSoon(expiresAt: string | null | undefined, now: number = Date.now()): boolean {
  return Boolean(expiresAt) && Date.parse(expiresAt ?? "") - now < EXPIRY_WARN_HOURS * 3600_000;
}
