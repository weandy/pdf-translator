"""验证实验 S2：MinerU 精准解析 API。

上传本地 PDF → 轮询 → 下载结果 Zip → 解压到 out/mineru/<样本>/<模型>/，
并统计 content_list / layout.json 里的块类型、坐标单位、公式与表格字段。

用法：MINERU_API_TOKEN=... python s2_mineru.py samples/1706.03762.pdf [vlm|pipeline]
"""

import collections
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import httpx

API = "https://mineru.net/api/v4"
OUT = Path(__file__).parent / "out" / "mineru"


def submit(client: httpx.Client, pdf: Path, model: str) -> str:
    r = client.post(f"{API}/file-urls/batch", json={
        "files": [{"name": pdf.name, "data_id": pdf.stem}],
        "model_version": model,
        "enable_formula": True,
        "enable_table": True,
        "language": "en",
    })
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(body)
    batch_id, url = body["data"]["batch_id"], body["data"]["file_urls"][0]
    # 上传链接是预签名地址，不能带鉴权头
    httpx.put(url, content=pdf.read_bytes(), timeout=120).raise_for_status()
    return batch_id


def wait(client: httpx.Client, batch_id: str) -> dict:
    t0 = time.time()
    while True:
        r = client.get(f"{API}/extract-results/batch/{batch_id}")
        r.raise_for_status()
        res = r.json()["data"]["extract_result"][0]
        state = res["state"]
        if state == "done":
            res["elapsed"] = round(time.time() - t0, 1)
            return res
        if state == "failed":
            raise RuntimeError(res)
        progress = res.get("extract_progress", {})
        print(f"  {state} {progress.get('extracted_pages', '')}/{progress.get('total_pages', '')}", flush=True)
        time.sleep(5)


def summarize(d: Path):
    cl = next(d.glob("*content_list.json"))
    items = json.loads(cl.read_text(encoding="utf-8"))
    types = collections.Counter(i["type"] for i in items)
    sample = {}
    for i in items:
        sample.setdefault(i["type"], {k: (v[:80] if isinstance(v, str) else v) for k, v in i.items()})
    bboxes = [i["bbox"] for i in items if "bbox" in i]
    layout = json.loads((d / "layout.json").read_text(encoding="utf-8"))
    pages = layout.get("pdf_info", [])
    span_types = collections.Counter()
    for p in pages:
        for b in p.get("para_blocks", []):
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    span_types[s.get("type")] += 1
    return {
        "files": sorted(x.name for x in d.iterdir()),
        "content_list_types": dict(types),
        "bbox_max": [max(b[k] for b in bboxes) for k in range(4)] if bboxes else None,
        "layout_top_keys": sorted(layout.keys()),
        "layout_page_keys": sorted(pages[0].keys()) if pages else None,
        "page_size": pages[0].get("page_size") if pages else None,
        "layout_span_types": dict(span_types),
        "samples": sample,
    }


def main(pdf_path: str, model: str = "vlm"):
    pdf = Path(pdf_path)
    headers = {"Authorization": f"Bearer {os.environ['MINERU_API_TOKEN']}"}
    with httpx.Client(headers=headers, timeout=60) as client:
        batch_id = submit(client, pdf, model)
        print(f"batch {batch_id}")
        res = wait(client, batch_id)
    z = httpx.get(res["full_zip_url"], timeout=120, follow_redirects=True).content
    d = OUT / pdf.stem / model
    d.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(z)).extractall(d)
    stats = {"elapsed_s": res["elapsed"], "zip_kb": len(z) // 1024, **summarize(d)}
    (d / "_summary.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "samples"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
