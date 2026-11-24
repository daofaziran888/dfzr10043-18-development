"""业务路由汇总：补写专用路由在前，通用模块资源路由在后。

landing 路由的显式路径（如 /api/data_landing/rewrite/sources）与资源路由的
/api/{module} 模式不构成同段冲突，置前仅为可读性与匹配优先级留余量。
"""
from __future__ import annotations

from app.routers import landing, resource

ROUTERS = [landing, resource]
