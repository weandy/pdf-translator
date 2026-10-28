"""稳定 ID 生成。"""

import uuid


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def block_id(page: int, index: int) -> str:
    return f"p{page}-b{index}"


def segment_id(job_id: str, index: int) -> str:
    return f"{job_id}-s{index}"
