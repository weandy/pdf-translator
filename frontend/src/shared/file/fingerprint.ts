/**
 * 文件指纹（SHA-256 十六进制）：只在浏览器里算、只存在本机，用来提醒“这篇翻译过”，不发给服务器。
 * 浏览器不提供 MD5；SHA-256 作用相同、更不易碰撞。Web Crypto 只在 HTTPS 或本机地址可用，不可用时返回 null（跳过检查）。
 */
export async function fingerprint(file: Blob): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  try {
    const digest = await subtle.digest("SHA-256", await file.arrayBuffer());
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}
