"""统计接口只返回匿名汇总，后台请求不增加浏览器访问次数。"""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.job import Job, JobStatus


def test_stats_api_and_visits(tmp_path: Path):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources=""))
    client = TestClient(create_app(container))
    response = client.get("/api/stats")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/stats?days=0").status_code == 422
    assert client.get("/api/stats?days=91").status_code == 422
    client.get("/api/health")
    client.get("/api/config")
    assert client.get("/api/stats").json()["total"]["page_views"] == 0
    event = {"event_id": str(uuid4()), "visitor_id": str(uuid4())}
    assert client.post("/api/stats/visits", json=event).status_code == 204
    assert client.post("/api/stats/visits", json=event).status_code == 204
    assert client.post("/api/stats/visits", json={"event_id": "bad", "visitor_id": "bad"}).status_code == 422
    result = client.get("/api/stats?days=7").json()
    assert len(result["daily"]) == 7
    assert result["timezone"] == "Asia/Shanghai"
    assert result["total"]["page_views"] == result["total"]["visitors"] == 1
    assert event["event_id"] not in str(result)
    assert event["visitor_id"] not in str(result)
    container.pipeline.deps.store.close()  # type: ignore[attr-defined]
    container.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def test_task_read_requests_do_not_count_and_delete_keeps_usage(tmp_path: Path):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources=""))
    client = TestClient(create_app(container))
    store = container.pipeline.deps.store
    job = Job(id="0000000000000007", filename="synthetic.pdf", page_count=3, status=JobStatus.DONE, stage="done")
    store.save_job(job)
    for _ in range(3):
        assert client.get(f"/api/jobs/{job.id}").status_code == 200
    with client.stream("GET", f"/api/jobs/{job.id}/events") as response:
        assert '"status":"done"' in response.read().decode()
    assert client.delete(f"/api/jobs/{job.id}").status_code == 204
    result = client.get("/api/stats").json()["total"]
    assert result["page_views"] == result["visitors"] == 0
    assert result["jobs_submitted"] == result["jobs_completed"] == 1
    assert result["pages_submitted"] == 3
    store.close()  # type: ignore[attr-defined]
    container.pipeline.deps.cache.close()  # type: ignore[attr-defined]
