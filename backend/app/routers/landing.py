"""数据落库补写路由：补写查询、范围领取、更正确认、补写确认与恢复、交接核对。

与通用资源路由不同，这里承载 data_landing 的真实业务差异：
- GET  /api/data_landing/rewrite/sources    按目标+批次查询可补写来源
- GET  /api/data_landing/rewrite/receipts   回查确认回执
- GET  /api/data_landing/rewrite/failures   回查原失败统计
- GET  /api/data_landing/rewrite/claims     回查补写范围领取记录
- POST /api/data_landing/rewrite/failures   登记失败来源
- POST /api/data_landing/rewrite/claims     领用补写范围
- POST /api/data_landing/rewrite/corrections 常规落库更正确认
- POST /api/data_landing/rewrite/submit     持领取记录补写确认
- POST /api/data_landing/rewrite/recover    恢复中断的领取
- GET  /api/data_landing/rewrite/claims/{id}/takeover 回查被接管范围
- POST /api/handoff_pack/confirmed_pack     按确认回执打包交接
- POST /api/handoff_pack/{id}/verify_rows   按回执核对交接包行数
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.landing_rewrite import landing_rewrite_service

router = APIRouter(prefix="/api", tags=["数据落库补写"])


class FailurePayload(BaseModel):
    关联批次: str
    目标存储: str
    来源名称: str
    记录行数: int
    失败行数: int
    错误明细: str
    来源编号: str | None = None


class ClaimPayload(BaseModel):
    关联批次: str
    目标存储: str
    来源名称列表: list[str] = Field(default_factory=list)
    值班员: str = "duty_operator"


class CorrectionPayload(BaseModel):
    关联批次: str
    目标存储: str
    来源名称: str
    写入行数: int


class RewriteSubmitPayload(BaseModel):
    claim_id: int
    写入行数: int
    read_interrupted: bool = False


class RecoverPayload(BaseModel):
    claim_id: int


class ConfirmedPackPayload(BaseModel):
    关联批次: str
    目标存储: str
    目标系统: str
    交接编号: str | None = None


@router.get("/data_landing/rewrite/sources")
def query_rewrite_sources(目标存储: str | None = None, 关联批次: str | None = None) -> dict[str, Any]:
    items = landing_rewrite_service.query_rewrite_candidates(
        目标存储=目标存储, 关联批次=关联批次)
    return {"items": items, "total": len(items)}


@router.get("/data_landing/rewrite/receipts")
def list_receipts(目标存储: str | None = None, 关联批次: str | None = None) -> dict[str, Any]:
    items = landing_rewrite_service.list_receipts(目标存储=目标存储, 关联批次=关联批次)
    return {"items": items, "total": len(items)}


@router.get("/data_landing/rewrite/failures")
def list_failures(目标存储: str | None = None, 关联批次: str | None = None) -> dict[str, Any]:
    items = landing_rewrite_service.list_failures(目标存储=目标存储, 关联批次=关联批次)
    return {"items": items, "total": len(items)}


@router.get("/data_landing/rewrite/claims")
def list_claims(
    目标存储: str | None = None,
    关联批次: str | None = None,
    状态: str | None = None,
) -> dict[str, Any]:
    items = landing_rewrite_service.list_claims(
        目标存储=目标存储, 关联批次=关联批次, 状态=状态)
    return {"items": items, "total": len(items)}


@router.post("/data_landing/rewrite/failures")
def register_failure(payload: FailurePayload) -> dict[str, Any]:
    return landing_rewrite_service.register_failed_source(**payload.model_dump())


@router.post("/data_landing/rewrite/claims")
def claim_ranges(payload: ClaimPayload) -> dict[str, Any]:
    return landing_rewrite_service.claim_ranges(**payload.model_dump())


@router.post("/data_landing/rewrite/corrections")
def confirm_correction(payload: CorrectionPayload) -> dict[str, Any]:
    return landing_rewrite_service.confirm_correction(**payload.model_dump())


@router.post("/data_landing/rewrite/submit")
def submit_rewrite(payload: RewriteSubmitPayload) -> dict[str, Any]:
    return landing_rewrite_service.submit_rewrite(
        payload.claim_id,
        payload.写入行数,
        read_interrupted=payload.read_interrupted,
    )


@router.post("/data_landing/rewrite/recover")
def recover_claim(payload: RecoverPayload) -> dict[str, Any]:
    return landing_rewrite_service.recover_claim(payload.claim_id)


@router.get("/data_landing/rewrite/claims/{claim_id}/takeover")
def find_takeover(claim_id: int) -> dict[str, Any]:
    return landing_rewrite_service.find_takeover_range(claim_id)


@router.post("/handoff_pack/confirmed_pack")
def pack_confirmed_rows(payload: ConfirmedPackPayload) -> dict[str, Any]:
    handoff_svc = service_for("handoff_pack")
    return handoff_svc.pack_confirmed_rows(**payload.model_dump())


@router.post("/handoff_pack/{pack_id}/verify_rows")
def verify_pack_rows(pack_id: int) -> dict[str, Any]:
    handoff_svc = service_for("handoff_pack")
    return handoff_svc.verify_pack_rows(pack_id)
