"""业务路由汇总：依赖编排为独立路由，其余模块共用一套资源路由。"""
from __future__ import annotations

from app.routers import orchestration, resource

# 编排路由的路径前缀更具体，必须先于通用 /{module} 资源路由注册。
ROUTERS = [orchestration, resource]
