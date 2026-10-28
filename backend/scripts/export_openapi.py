"""导出接口的 OpenAPI 描述，供前端生成类型：

cd backend && uv run python scripts/export_openapi.py ../frontend/openapi.json
cd frontend && npm run gen:api
"""

import json
import sys
import tempfile
from pathlib import Path

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container

with tempfile.TemporaryDirectory() as tmp:
    app = create_app(build_container(Settings(data_dir=Path(tmp), glossary_sources="")))
    schema = app.openapi()
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
out.write_text(json.dumps(schema, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"已写入 {out}")
