// 分层依赖规则（见 docs/03-mvp-design.md §4.1）：
// app → pages → features → entities → shared，只能向下依赖；
// features 之间互不导入；跨目录导入一律用 "@/" 别名，禁止用 "../" 跨出当前目录。
import { existsSync, readdirSync } from "node:fs";

import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

const LAYERS = ["app", "pages", "features", "entities", "shared"];

/** 某一层禁止导入的上层别名 */
function upperLayers(layer) {
  return LAYERS.slice(0, LAYERS.indexOf(layer)).map((l) => `@/${l}/*`);
}

const featureDir = new URL("./src/features", import.meta.url);
const features = existsSync(featureDir)
  ? readdirSync(featureDir, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
  : [];

const NO_PARENT = { group: ["../*"], message: "跨目录导入请使用 @/ 别名" };

const layerRules = LAYERS.filter((l) => l !== "features" && l !== "app").map((layer) => ({
  files: [`src/${layer}/**/*.{ts,tsx}`],
  rules: {
    "no-restricted-imports": [
      "error",
      {
        patterns: [NO_PARENT, { group: upperLayers(layer), message: `${layer} 层不能依赖上层模块` }],
      },
    ],
  },
}));

const featureRules = features.map((name) => ({
  files: [`src/features/${name}/**/*.{ts,tsx}`],
  rules: {
    "no-restricted-imports": [
      "error",
      {
        patterns: [
          NO_PARENT,
          { group: upperLayers("features"), message: "features 层不能依赖 pages / app" },
          {
            group: ["@/features/*", `!@/features/${name}`, `!@/features/${name}/*`],
            message: "功能模块之间不能互相导入，需要协作时在 pages 层组合",
          },
        ],
      },
    ],
  },
}));

export default defineConfig(
  { ignores: ["dist", "node_modules"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: { ...reactHooks.configs.recommended.rules },
  },
  ...layerRules,
  ...featureRules,
);
