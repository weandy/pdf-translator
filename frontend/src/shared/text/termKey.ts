const ACRONYM = /^[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*s?$/;
const ASCII_TERM = /^[A-Za-z0-9 '-]+$/;
// Python 的 str.split 也把 U+001C 至 U+001F 视为空白。
const SPACE = new RegExp("[\\p{White_Space}" + String.fromCharCode(28) + "-" + String.fromCharCode(31) + "]+", "gu");

function casefold(text: string): string {
  return Array.from(text, (ch) => {
    // Unicode 折叠保留无点 i，切罗基字母统一为大写。
    if (ch === "ı") return ch;
    if (/[Ꭰ-ᏽꭰ-ꮿ]/u.test(ch)) return ch.toUpperCase();
    return ch.toLowerCase().toUpperCase().toLowerCase();
  }).join("");
}

function singular(part: string): string {
  const word = part.toLowerCase();
  if (word.length <= 3) return word;
  if (word.endsWith("ies")) return word.slice(0, -3) + "y";
  if (/(?:s|x|z|ch|sh)es$/.test(word)) return word.slice(0, -2);
  if (/s$/.test(word) && !/(?:ss|us|is)$/.test(word)) return word.slice(0, -1);
  return word;
}

/** 与后端 normalize_term_key 共用缩写、字面技术词和普通词形的判定。 */
export function normalizeTermKey(term: string): string {
  const collapsed = term.replace(SPACE, " ").replace(/^ | $/g, "");
  if (!collapsed) return "";
  if (ACRONYM.test(collapsed)) {
    const stem = collapsed.endsWith("s") ? collapsed.slice(0, -1) : collapsed;
    if (/[A-Z].*[A-Z]/.test(stem)) return "acr:" + stem;
  }
  if (!ASCII_TERM.test(collapsed) || !/[A-Za-z0-9]/.test(collapsed)) return "lit:" + casefold(collapsed);
  return "word:" + collapsed.split(/[ -]+/).filter(Boolean).map(singular).join(" ");
}
