/** 后端接口客户端。M3 起会在这里统一附加 Key 请求头（Key 保存在浏览器 localStorage）。 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { Accept: "application/json", ...init?.headers } });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export interface Health {
  status: string;
  version: string;
  data_dir_writable: boolean;
}

export const fetchHealth = (signal?: AbortSignal) => getJSON<Health>("/api/health", { signal });
