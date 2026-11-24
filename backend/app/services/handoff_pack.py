"""交接包服务：批次打包、清单校验与下游交接。

把已确认批次连同字段映射、校验摘要和清单打包成交接包，
供下游数仓直接消费并回溯来源。退回后批次不得再次交接。

补写链路约定：交接只能消费数据落库的已确认行（确认回执），按回执真实
写入行数核对实际写入范围；目标内没有已确认回执时不允许打包。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class HandoffPackService(BaseModuleService):
    MODULE = "handoff_pack"
    ENTITY = "交接包"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "交接编号", "关联批次", "目标系统", "清单文件数", "记录行数",
        "校验摘要", "交接版本", "交接时间",
    ]
    REQUIRED_FIELDS = ["交接编号", "关联批次", "目标系统"]
    STATUS_ORDER = ["待打包", "打包中", "已交接", "交接退回"]
    ACTION_RULES = {"启动打包": "打包中", "确认交接": "已交接", "记录退回": "交接退回"}
    NEGATIVE_ACTIONS = []

    def reject_handoff(self, pack_id: int, reason: str) -> dict[str, Any]:
        """驳回交接包，退回后批次不得进入交接。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        self.run_action(pack_id, "记录退回")
        return {"ok": True, "pack_id": pack_id, "reason": reason, "action": "交接退回"}

    def get_delivered_packs(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """获取已交接的包列表，可按目标系统筛选。"""
        items, _ = self.list_entries(size=10000)
        delivered = [it for it in items if it.get("status") == "已交接"]
        if target_system:
            delivered = [it for it in delivered if it.get("目标系统") == target_system]
        return delivered

    def pack_confirmed_rows(
        self,
        *,
        关联批次: str,
        目标存储: str,
        目标系统: str,
        交接编号: str | None = None,
    ) -> dict[str, Any]:
        """按确认回执打包交接：只消费已确认的行，行数取回执真实写入行数。"""
        # 延迟导入避免与服务注册阶段产生循环依赖
        from app.services.landing_rewrite import landing_rewrite_service

        receipts = landing_rewrite_service.list_receipts(
            关联批次=关联批次, 目标存储=目标存储)
        if not receipts:
            return {"ok": False, "reason": "目标内没有已确认回执，交接包只能消费已确认的行"}

        total_rows = sum(int(r.get("写入行数", "0") or "0") for r in receipts)
        checksum_parts = [r.get("回执编号", "") for r in receipts]
        values = {
            "交接编号": 交接编号 or f"HAND-RW-{关联批次}-{目标存储}",
            "关联批次": 关联批次,
            "目标系统": 目标系统,
            "清单文件数": str(len(receipts)),
            "记录行数": str(total_rows),
            "校验摘要": "|".join(checksum_parts),
            "交接版本": "v1",
            "交接时间": "",
        }
        entry, missing = self.create_entry(values)
        if missing:
            return {"ok": False, "reason": f"缺少必填字段：{'、'.join(missing)}"}
        # 记录实际写入范围（目标存储+回执清单），供后续按真实行数核对
        entry["目标存储"] = 目标存储
        entry["回执清单"] = list(checksum_parts)
        self.run_action(entry["id"], "启动打包")
        self.run_action(entry["id"], "确认交接")
        return {
            "ok": True,
            "pack": self.get_entry(entry["id"]),
            "receipts": receipts,
            "confirmed_sources": [r.get("来源名称") for r in receipts],
            "confirmed_row_count": total_rows,
        }

    def verify_pack_rows(self, pack_id: int) -> dict[str, Any]:
        """用确认回执核对交接包实际写入范围，行数必须与回执汇总一致。"""
        from app.services.landing_rewrite import landing_rewrite_service

        pack = self.get_entry(pack_id)
        if pack is None:
            return {"ok": False, "reason": "交接包不存在"}
        receipts = landing_rewrite_service.list_receipts(
            关联批次=pack.get("关联批次"), 目标存储=pack.get("目标存储"))
        # 只核对打包时登记进交接包的回执，避免同批次其他目标的数据串入
        receipt_nos = pack.get("回执清单")
        if receipt_nos:
            receipts = [r for r in receipts if r.get("回执编号") in receipt_nos]
        receipt_row_count = sum(int(r.get("写入行数", "0") or "0") for r in receipts)
        pack_row_count = int(pack.get("记录行数", "0") or "0")
        matched = receipt_row_count == pack_row_count and len(receipts) > 0
        return {
            "ok": matched,
            "pack_id": pack_id,
            "目标存储": pack.get("目标存储", ""),
            "pack_row_count": pack_row_count,
            "receipt_row_count": receipt_row_count,
            "receipt_count": len(receipts),
            "receipts": receipts,
            "reason": "行数一致" if matched else "交接包行数与确认回执汇总不一致",
        }