"""模块服务可选的领域接口扩展。

通用资源路由只处理列表/登记/动作；窗口归并与序列导出还有自己的只读视图：
窗口的实际起止与观测出处、一次交付的对账与排除明细。这里按方法名探测，
服务没实现就返回 404，不影响其余 18 个模块。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.services.base import BaseModuleService


def call_domain_view(service: BaseModuleService, method_name: str,
                     entry_id: int, **kwargs: Any) -> Any:
    method = getattr(service, method_name, None)
    if method is None:
        raise HTTPException(
            status_code=404,
            detail=f"{service.MODULE} 不支持 {method_name} 视图",
        )
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404,
                            detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    result = method(entry_id, **kwargs)
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=409, detail=str(result["error"]))
    return result
