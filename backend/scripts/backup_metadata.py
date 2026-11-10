"""生成只含长期统计与提交元数据的独立备份，不包含正文与管理会话。"""

import argparse
import json
from pathlib import Path

from pdftr.storage.metadata_backup import export_metadata

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path, help="运行中的 app.db，可通过只读连接导出")
parser.add_argument("target", type=Path, help="新的元数据备份文件，不得已存在")
args = parser.parse_args()
print(json.dumps(export_metadata(args.source, args.target), ensure_ascii=False))
