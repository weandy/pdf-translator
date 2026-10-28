"""前后端契约：前端提交的 openapi.json 必须与当前接口一致，否则提示重新生成前端类型。"""

import json
from pathlib import Path

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container

FRONTEND_SCHEMA = Path(__file__).parents[2] / "frontend" / "openapi.json"


def test_frontend_openapi_is_up_to_date(tmp_path: Path):
    current = create_app(build_container(Settings(data_dir=tmp_path, glossary_sources=""))).openapi()
    committed = json.loads(FRONTEND_SCHEMA.read_text(encoding="utf-8"))
    assert current == committed, (
        "接口已变更：请运行 `uv run python scripts/export_openapi.py ../frontend/openapi.json` "
        "并在 frontend 目录运行 `npm run gen:api`"
    )
