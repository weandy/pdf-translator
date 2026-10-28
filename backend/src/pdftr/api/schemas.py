"""接口的请求/响应结构（DTO），与 IR 解耦；前端类型由 OpenAPI 生成。"""

from pydantic import BaseModel


class HealthOut(BaseModel):
    status: str
    version: str
    data_dir_writable: bool
