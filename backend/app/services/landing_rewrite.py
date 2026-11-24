"""落库补写领域服务：补写来源查询、范围领取、更正确认仲裁与确认回执管理。

背景：数据落库原先只记录冲突行数，缺少按目标筛选、可继续补写的来源记录。
本服务把同一采集批次下的失败记录、补写来源、确认回执和补写范围领取记录
对应起来，支撑值班员按「目标存储 + 采集批次」领用补写范围并完成补写。

领域规则：
- 补写查询：按目标/批次筛出未确认来源，并关联原失败记录与现行确认回执，
  让确认回执、失败记录和可发起的补写范围一一对应；
- 范围领取：领取记录持久化在 store 中，中断恢复后仍可回查领取时的范围；
- 并发仲裁：常规落库更正确认与补写确认并发时，同一(目标, 批次, 来源)只
  允许确认一次；先到的确认接管已领取的补写范围，持旧领取继续补写会被拒绝，
  但仍可查到接管该范围的确认回执；
- 补写边界：补写只能改变本次目标的未确认数据，其他目标的同名来源不受影响；
- 交接核对：交接包只消费已确认行，按回执真实写入行数核对实际写入范围。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

# 内存表名（store.rows 按需创建，reset 后自动重建为空表）
SOURCE_TABLE = "landing_source"
FAILURE_TABLE = "landing_failure"
RECEIPT_TABLE = "landing_receipt"
CLAIM_TABLE = "landing_claim"

# 确认路径
PATH_REGULAR = "常规落库更正"
PATH_REWRITE = "补写"

# 领取状态
CLAIM_HELD = "持有中"
CLAIM_TAKEN_OVER = "已接管"
CLAIM_DONE = "已完成"
CLAIM_INTERRUPTED = "已中断"
ACTIVE_CLAIM_STATES = (CLAIM_HELD, CLAIM_INTERRUPTED)
CLOSED_CLAIM_STATES = (CLAIM_TAKEN_OVER, CLAIM_DONE)

# 来源确认状态
SOURCE_UNCONFIRMED = "未确认"
SOURCE_CONFIRMED = "已确认"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class LandingRewriteService:
    """落库补写：管理来源记录、失败记录、确认回执与补写范围领取四张表。"""

    # ── 内部查找工具 ─────────────────────────────────────────────

    def _find_source(self, batch: str, target: str, source_name: str) -> dict[str, Any] | None:
        for row in store.rows(SOURCE_TABLE):
            if (row.get("关联批次") == batch
                    and row.get("目标存储") == target
                    and row.get("来源名称") == source_name):
                return row
        return None

    def _find_receipt(self, batch: str, target: str, source_name: str) -> dict[str, Any] | None:
        """按(目标,批次,来源)找现行确认回执；该组合最多只允许一条回执。"""
        for row in store.rows(RECEIPT_TABLE):
            if (row.get("关联批次") == batch
                    and row.get("目标存储") == target
                    and row.get("来源名称") == source_name):
                return row
        return None

    def _active_claim(self, batch: str, target: str, source_name: str) -> dict[str, Any] | None:
        for row in store.rows(CLAIM_TABLE):
            if (row.get("关联批次") == batch
                    and row.get("目标存储") == target
                    and row.get("来源名称") == source_name
                    and row.get("状态") in ACTIVE_CLAIM_STATES):
                return row
        return None

    def _latest_claim(self, batch: str, target: str, source_name: str) -> dict[str, Any] | None:
        matched = [
            row for row in store.rows(CLAIM_TABLE)
            if (row.get("关联批次") == batch
                and row.get("目标存储") == target
                and row.get("来源名称") == source_name)
        ]
        return matched[-1] if matched else None

    def _get_claim(self, claim_id: int) -> dict[str, Any] | None:
        return store.find(CLAIM_TABLE, claim_id)

    # ── 登记：失败来源与原失败统计 ───────────────────────────────

    def register_failed_source(
        self,
        *,
        关联批次: str,
        目标存储: str,
        来源名称: str,
        记录行数: int,
        失败行数: int,
        错误明细: str,
        来源编号: str | None = None,
    ) -> dict[str, Any]:
        """登记一条落库失败的来源记录，同时固化原失败统计（失败记录不可变）。"""
        if self._find_source(关联批次, 目标存储, 来源名称) is not None:
            return {"ok": False, "reason": "该目标下来源记录已存在，不能重复登记"}

        source_rows = store.rows(SOURCE_TABLE)
        source_id = _next_id(source_rows)
        source = {
            "id": source_id,
            "来源编号": 来源编号 or f"SRC-{source_id:04d}",
            "关联批次": 关联批次,
            "目标存储": 目标存储,
            "来源名称": 来源名称,
            "记录行数": str(记录行数),
            "状态": SOURCE_UNCONFIRMED,
            "确认路径": "",
            "登记时间": _now_iso(),
        }
        source_rows.append(source)

        failure_rows = store.rows(FAILURE_TABLE)
        failure_id = _next_id(failure_rows)
        failure = {
            "id": failure_id,
            "失败编号": f"FAIL-{failure_id:04d}",
            "关联批次": 关联批次,
            "目标存储": 目标存储,
            "来源名称": 来源名称,
            "失败行数": str(失败行数),
            "错误明细": 错误明细,
            "发生时间": _now_iso(),
        }
        failure_rows.append(failure)
        return {"ok": True, "source": source, "failure": failure}

    # ── 补写查询：回执、失败记录与可补写范围对应 ─────────────────

    def query_rewrite_candidates(
        self,
        *,
        目标存储: str | None = None,
        关联批次: str | None = None,
    ) -> list[dict[str, Any]]:
        """按目标/批次筛来源记录，逐条关联失败记录、领取记录与确认回执。

        只有未确认来源才允许发起补写（``可补写=True`` 且无在途领取）。
        """
        candidates: list[dict[str, Any]] = []
        for source in store.rows(SOURCE_TABLE):
            if 目标存储 and source.get("目标存储") != 目标存储:
                continue
            if 关联批次 and source.get("关联批次") != 关联批次:
                continue
            batch = source["关联批次"]
            target = source["目标存储"]
            name = source["来源名称"]
            failures = [
                row for row in store.rows(FAILURE_TABLE)
                if row.get("关联批次") == batch
                and row.get("目标存储") == target
                and row.get("来源名称") == name
            ]
            receipt = self._find_receipt(batch, target, name)
            claim = self._latest_claim(batch, target, name)
            unconfirmed = source.get("状态") == SOURCE_UNCONFIRMED
            candidates.append({
                "来源编号": source["来源编号"],
                "关联批次": batch,
                "目标存储": target,
                "来源名称": name,
                "记录行数": source["记录行数"],
                "状态": source["状态"],
                "确认路径": source.get("确认路径", ""),
                "可补写": unconfirmed and self._active_claim(batch, target, name) is None,
                "失败记录": failures[-1] if failures else None,
                "领取记录": claim,
                "确认回执": receipt,
            })
        return candidates

    def list_receipts(
        self,
        *,
        关联批次: str | None = None,
        目标存储: str | None = None,
    ) -> list[dict[str, Any]]:
        """回查现行确认回执（与原失败统计分开查询）。"""
        return [
            row for row in store.rows(RECEIPT_TABLE)
            if (关联批次 is None or row.get("关联批次") == 关联批次)
            and (目标存储 is None or row.get("目标存储") == 目标存储)
        ]

    def list_failures(
        self,
        *,
        关联批次: str | None = None,
        目标存储: str | None = None,
    ) -> list[dict[str, Any]]:
        """回查原失败统计，失败记录一经登记不可变。"""
        return [
            row for row in store.rows(FAILURE_TABLE)
            if (关联批次 is None or row.get("关联批次") == 关联批次)
            and (目标存储 is None or row.get("目标存储") == 目标存储)
        ]

    def list_claims(
        self,
        *,
        关联批次: str | None = None,
        目标存储: str | None = None,
        状态: str | None = None,
    ) -> list[dict[str, Any]]:
        """回查补写范围领取记录（含中断前领取、被接管的旧领取）。"""
        return [
            row for row in store.rows(CLAIM_TABLE)
            if (关联批次 is None or row.get("关联批次") == 关联批次)
            and (目标存储 is None or row.get("目标存储") == 目标存储)
            and (状态 is None or row.get("状态") == 状态)
        ]

    # ── 领用补写范围 ─────────────────────────────────────────────

    def claim_ranges(
        self,
        *,
        关联批次: str,
        目标存储: str,
        来源名称列表: list[str],
        值班员: str,
    ) -> dict[str, Any]:
        """值班员按目标和采集批次领用一片或多片补写范围。

        仅未确认且无在途领取的来源可领用；每片来源生成一条持久化领取记录。
        """
        if not 来源名称列表:
            return {"ok": False, "reason": "至少选择一个来源才能领用补写范围"}
        claims: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for name in 来源名称列表:
            source = self._find_source(关联批次, 目标存储, name)
            if source is None:
                rejected.append({"来源名称": name, "reason": "来源记录不存在"})
                continue
            if source.get("状态") == SOURCE_CONFIRMED:
                rejected.append({"来源名称": name, "reason": "来源已确认，不在补写范围"})
                continue
            active = self._active_claim(关联批次, 目标存储, name)
            if active is not None:
                rejected.append({
                    "来源名称": name,
                    "reason": f"补写范围已被领取记录{active['领取编号']}在途占用",
                })
                continue
            rows = store.rows(CLAIM_TABLE)
            claim_id = _next_id(rows)
            claim = {
                "id": claim_id,
                "领取编号": f"CLM-{claim_id:04d}",
                "关联批次": 关联批次,
                "目标存储": 目标存储,
                "来源名称": name,
                "值班员": 值班员,
                "状态": CLAIM_HELD,
                "领取时间": _now_iso(),
                "接管回执": "",
                "完成回执": "",
                "中断时间": "",
                "恢复时间": "",
            }
            rows.append(claim)
            claims.append(claim)
        return {
            "ok": bool(claims),
            "claims": claims,
            "rejected": rejected,
            "claimed_count": len(claims),
        }

    # ── 确认：常规落库更正确认（另一路径） ───────────────────────

    def _create_receipt(
        self,
        *,
        batch: str,
        target: str,
        source_name: str,
        write_rows: int,
        path: str,
        claim_no: str = "",
    ) -> dict[str, Any]:
        rows = store.rows(RECEIPT_TABLE)
        receipt_id = _next_id(rows)
        receipt = {
            "id": receipt_id,
            "回执编号": f"RCPT-{receipt_id:04d}",
            "关联批次": batch,
            "目标存储": target,
            "来源名称": source_name,
            "确认路径": path,
            "写入行数": str(write_rows),
            "领取编号": claim_no,
            "确认时间": _now_iso(),
        }
        rows.append(receipt)
        return receipt

    def confirm_correction(
        self,
        *,
        关联批次: str,
        目标存储: str,
        来源名称: str,
        写入行数: int,
    ) -> dict[str, Any]:
        """常规落库路径的同源更正确认。

        与补写确认并发时按(目标,批次,来源)幂等：已存在回执则直接回出原回执，
        绝不重复确认；若该范围已被值班员领取，则接管其在途领取记录。
        """
        source = self._find_source(关联批次, 目标存储, 来源名称)
        if source is None:
            return {"ok": False, "reason": "来源记录不存在"}
        existing = self._find_receipt(关联批次, 目标存储, 来源名称)
        if existing is not None:
            return {
                "ok": True,
                "already_confirmed": True,
                "receipt": existing,
                "reason": "该来源已确认，幂等返回原回执，不重复确认",
            }

        receipt = self._create_receipt(
            batch=关联批次, target=目标存储, source_name=来源名称,
            write_rows=写入行数, path=PATH_REGULAR,
        )
        source["状态"] = SOURCE_CONFIRMED
        source["确认路径"] = PATH_REGULAR

        # 接管已被领取但尚未完成的补写范围（中断中的领取同样接管）
        taken_over: list[dict[str, Any]] = []
        active = self._active_claim(关联批次, 目标存储, 来源名称)
        if active is not None:
            active["状态"] = CLAIM_TAKEN_OVER
            active["接管回执"] = receipt["回执编号"]
            taken_over.append(active)
        return {
            "ok": True,
            "already_confirmed": False,
            "receipt": receipt,
            "taken_over_claims": taken_over,
        }

    # ── 补写写入：持领取范围补写，只改本次目标的未确认数据 ──────

    def submit_rewrite(
        self,
        claim_id: int,
        写入行数: int,
        *,
        read_interrupted: bool = False,
    ) -> dict[str, Any]:
        """持领取记录发起补写确认。

        - 来源已被更正确认接管时拒绝写入（补写只能改变未确认数据），并回出
          接管回执，便于持旧领取结果排查；
        - 中断重试/重复提交时幂等返回已有回执，同一来源只确认一次；
        - ``read_interrupted`` 模拟回执读取中断：确认已落库、回执已生成，但
          领取记录停留在「已中断」，等待恢复后重新关联。
        """
        claim = self._get_claim(claim_id)
        if claim is None:
            return {"ok": False, "reason": "补写领取记录不存在"}

        batch = claim["关联批次"]
        target = claim["目标存储"]
        name = claim["来源名称"]
        source = self._find_source(batch, target, name)
        if source is None:
            return {"ok": False, "reason": "领取范围对应的来源记录不存在"}

        existing = self._find_receipt(batch, target, name)
        if existing is not None:
            if existing.get("确认路径") == PATH_REGULAR or claim.get("状态") == CLAIM_TAKEN_OVER:
                return {
                    "ok": False,
                    "reason": "来源已由常规落库更正确认接管，补写只能改变本次目标的未确认数据",
                    "takeover_receipt": existing,
                    "claim": claim,
                }
            # 补写回执已存在（中断后的重复提交）：幂等返回，不产生第二条回执
            return {
                "ok": True,
                "already_confirmed": True,
                "receipt": existing,
                "claim": claim,
                "reason": "补写回执已存在，幂等返回，不重复确认",
            }

        receipt = self._create_receipt(
            batch=batch, target=target, source_name=name,
            write_rows=写入行数, path=PATH_REWRITE, claim_no=claim["领取编号"],
        )
        source["状态"] = SOURCE_CONFIRMED
        source["确认路径"] = PATH_REWRITE

        if read_interrupted:
            claim["状态"] = CLAIM_INTERRUPTED
            claim["中断时间"] = _now_iso()
            return {
                "ok": True,
                "interrupted": True,
                "receipt": receipt,
                "claim": claim,
                "reason": "回执读取中断，确认已落库，领取记录保留为已中断待恢复",
            }

        claim["状态"] = CLAIM_DONE
        claim["完成回执"] = receipt["回执编号"]
        return {"ok": True, "interrupted": False, "receipt": receipt, "claim": claim}

    def recover_claim(self, claim_id: int) -> dict[str, Any]:
        """恢复中断的补写领取：重新关联已落库的补写回执，不重复确认。"""
        claim = self._get_claim(claim_id)
        if claim is None:
            return {"ok": False, "reason": "补写领取记录不存在"}
        if claim.get("状态") != CLAIM_INTERRUPTED:
            return {"ok": False, "reason": f"领取记录当前状态为{claim.get('状态')}，无需恢复"}
        receipt = self._find_receipt(
            claim["关联批次"], claim["目标存储"], claim["来源名称"])
        if receipt is None or receipt.get("领取编号") != claim["领取编号"]:
            return {"ok": False, "reason": "未找到归属本领取记录的补写回执，不能恢复"}
        claim["状态"] = CLAIM_DONE
        claim["完成回执"] = receipt["回执编号"]
        claim["恢复时间"] = _now_iso()
        return {"ok": True, "receipt": receipt, "claim": claim}

    def find_takeover_range(self, claim_id: int) -> dict[str, Any]:
        """持旧领取结果补写时，回查被更正确认接管的范围与接管回执。"""
        claim = self._get_claim(claim_id)
        if claim is None:
            return {"ok": False, "reason": "补写领取记录不存在"}
        receipt = self._find_receipt(
            claim["关联批次"], claim["目标存储"], claim["来源名称"])
        return {
            "ok": True,
            "claim": claim,
            "taken_over": claim.get("状态") == CLAIM_TAKEN_OVER,
            "takeover_receipt": receipt,
        }


# 单例：与 SERVICES 中其他服务一样进程内共享
landing_rewrite_service = LandingRewriteService()
