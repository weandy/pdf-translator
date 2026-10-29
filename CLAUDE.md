@AGENTS.md

## Claude Code 补充

- 提交说明末尾加署名行：`Co-Authored-By: Claude <noreply@anthropic.com>`（按当前会话提示里的具体写法）。
- 涉及 Claude / Anthropic 接口的改动（`backend/src/pdftr/translation/anthropic_compat.py`）先用 `claude-api` 技能核对接口细节，不要凭记忆写参数。
- 改界面或做新页面时先用 `frontend-design` 技能；改完用 `spikes/.venv/bin/python` 里的 Playwright 在桌面与手机视口截图验收。
