"""数据落库补写链路确定性测试。

覆盖场景（对应补写需求）：
- 按目标+批次筛选可补写来源，关联失败记录、领取记录、确认回执；
- 领用两片补写范围，常规落库更正确认接管其中一片，持旧领取补写被拒绝且
  可回查接管范围，另一目标同名来源保持未确认；
- 回执读取中断后恢复，同一来源只确认一次（并发幂等）；
- 交接包只消费已确认行，按回执真实行数核对；原失败统计保持可查。
"""
from __future__ import annotations

from app.services import service_for
from app.services.landing_rewrite import (
    CLAIM_DONE,
    CLAIM_HELD,
    CLAIM_INTERRUPTED,
    CLAIM_TAKEN_OVER,
    PATH_REGULAR,
    PATH_REWRITE,
    SOURCE_CONFIRMED,
    SOURCE_UNCONFIRMED,
    landing_rewrite_service as rw,
)

BATCH = "CAPT-RW-01"
TARGET_A = "target_jia"
TARGET_B = "target_yi"
SOURCE_A = "source_A"
SOURCE_B = "source_B"


def _seed_two_targets() -> None:
    """目标甲/乙各登记来源 A，目标甲再登记来源 B（含原失败明细）。"""
    rw.register_failed_source(
        关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A,
        记录行数=10, 失败行数=4, 错误明细="目标甲A冲突行待更正",
    )
    rw.register_failed_source(
        关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_B,
        记录行数=20, 失败行数=6, 错误明细="目标甲B写入失败",
    )
    rw.register_failed_source(
        关联批次=BATCH, 目标存储=TARGET_B, 来源名称=SOURCE_A,
        记录行数=8, 失败行数=2, 错误明细="目标乙A同名来源错误",
    )


class TestLandingRewriteEndToEnd:
    """完整剧本：领用→更正确认接管→旧领取补写被拒→B 中断→恢复→交接核对。"""

    def test_full_rewrite_story(self):
        _seed_two_targets()

        # 1. 筛出目标甲的来源 A、B，且与失败记录/可补写范围对应
        candidates = rw.query_rewrite_candidates(目标存储=TARGET_A, 关联批次=BATCH)
        assert len(candidates) == 2
        names = {c["来源名称"] for c in candidates}
        assert names == {SOURCE_A, SOURCE_B}
        assert all(c["可补写"] is True for c in candidates)
        assert all(c["状态"] == SOURCE_UNCONFIRMED for c in candidates)
        # 失败记录与来源已关联
        a_candidate = next(c for c in candidates if c["来源名称"] == SOURCE_A)
        assert a_candidate["失败记录"]["失败行数"] == "4"
        assert a_candidate["确认回执"] is None

        # 目标乙同名 A 独立存在、未确认
        yi = rw.query_rewrite_candidates(目标存储=TARGET_B, 关联批次=BATCH)
        assert len(yi) == 1 and yi[0]["来源名称"] == SOURCE_A
        assert yi[0]["状态"] == SOURCE_UNCONFIRMED

        # 2. 值班员按目标和批次领用两片补写范围
        claim_result = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_A,
            来源名称列表=[SOURCE_A, SOURCE_B], 值班员="duty_01",
        )
        assert claim_result["ok"] is True
        assert claim_result["claimed_count"] == 2
        claims = {c["来源名称"]: c for c in claim_result["claims"]}
        claim_a_id = claims[SOURCE_A]["id"]
        claim_b_id = claims[SOURCE_B]["id"]
        assert all(c["状态"] == CLAIM_HELD for c in claims.values())

        # 领取后来源不再可重复领用
        again = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_A,
            来源名称列表=[SOURCE_A], 值班员="duty_02",
        )
        assert again["ok"] is False
        assert "在途占用" in again["rejected"][0]["reason"]

        # 3. 补写开始前，常规落库经另一路径更正确认 A
        correction = rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=10,
        )
        assert correction["ok"] is True
        assert correction["already_confirmed"] is False
        assert correction["receipt"]["确认路径"] == PATH_REGULAR
        # 旧领取范围被接管
        assert len(correction["taken_over_claims"]) == 1
        taken = correction["taken_over_claims"][0]
        assert taken["id"] == claim_a_id
        assert taken["状态"] == CLAIM_TAKEN_OVER
        assert taken["接管回执"] == correction["receipt"]["回执编号"]

        # 4. 持旧领取继续补写 A：被拒绝，但能查到接管回执
        stale = rw.submit_rewrite(claim_a_id, 写入行数=10)
        assert stale["ok"] is False
        assert "接管" in stale["reason"]
        assert stale["takeover_receipt"]["回执编号"] == correction["receipt"]["回执编号"]
        takeover = rw.find_takeover_range(claim_a_id)
        assert takeover["ok"] is True
        assert takeover["taken_over"] is True
        assert takeover["takeover_receipt"]["来源名称"] == SOURCE_A

        # 并发：更正确认重复提交只返回原回执，不产生第二条
        correction_again = rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=10,
        )
        assert correction_again["ok"] is True
        assert correction_again["already_confirmed"] is True
        assert correction_again["receipt"]["id"] == correction["receipt"]["id"]
        receipts_a = rw.list_receipts(关联批次=BATCH, 目标存储=TARGET_A)
        assert len([r for r in receipts_a if r["来源名称"] == SOURCE_A]) == 1

        # 5. 另一目标仍有同名来源 A，且保持未确认（补写只改本次目标数据）
        yi_after = rw.query_rewrite_candidates(目标存储=TARGET_B, 关联批次=BATCH)
        assert yi_after[0]["状态"] == SOURCE_UNCONFIRMED
        assert yi_after[0]["可补写"] is True

        # 6. B 提交补写，模拟回执读取中断
        interrupted = rw.submit_rewrite(claim_b_id, 写入行数=18, read_interrupted=True)
        assert interrupted["ok"] is True
        assert interrupted["interrupted"] is True
        b_receipt = interrupted["receipt"]
        assert b_receipt["确认路径"] == PATH_REWRITE
        b_claim = interrupted["claim"]
        assert b_claim["状态"] == CLAIM_INTERRUPTED
        # 中断后 B 来源已确认、回执已落库
        b_source = rw.query_rewrite_candidates(目标存储=TARGET_A, 关联批次=BATCH)
        b_row = next(c for c in b_source if c["来源名称"] == SOURCE_B)
        assert b_row["状态"] == SOURCE_CONFIRMED

        # 中断前领取记录留存可查
        held_history = rw.list_claims(关联批次=BATCH, 状态=CLAIM_INTERRUPTED)
        assert any(c["id"] == claim_b_id for c in held_history)

        # 7. 恢复：不重复确认，重新关联既有回执
        recovered = rw.recover_claim(claim_b_id)
        assert recovered["ok"] is True
        assert recovered["receipt"]["id"] == b_receipt["id"]
        assert recovered["claim"]["状态"] == CLAIM_DONE
        # 恢复期间重复提交补写：幂等返回原回执
        duplicate_submit = rw.submit_rewrite(claim_b_id, 写入行数=18)
        assert duplicate_submit["ok"] is True
        assert duplicate_submit.get("already_confirmed") is True
        assert duplicate_submit["receipt"]["id"] == b_receipt["id"]

        # A、B 各确认一次，且写入来源可分清
        final_receipts = rw.list_receipts(关联批次=BATCH, 目标存储=TARGET_A)
        assert len(final_receipts) == 2
        paths = {r["来源名称"]: r["确认路径"] for r in final_receipts}
        assert paths[SOURCE_A] == PATH_REGULAR
        assert paths[SOURCE_B] == PATH_REWRITE

        # 8. 交接包按确认回执真实行数核对
        handoff_svc = service_for("handoff_pack")
        pack_result = handoff_svc.pack_confirmed_rows(
            关联批次=BATCH, 目标存储=TARGET_A, 目标系统="dwh_downstream",
        )
        assert pack_result["ok"] is True
        # 只含目标甲 A(10) + B(18)，不含目标乙数据
        assert pack_result["confirmed_row_count"] == 28
        assert set(pack_result["confirmed_sources"]) == {SOURCE_A, SOURCE_B}
        pack = pack_result["pack"]
        assert pack["记录行数"] == "28"
        assert pack["status"] == "已交接"

        verify = handoff_svc.verify_pack_rows(pack["id"])
        assert verify["ok"] is True
        assert verify["pack_row_count"] == 28
        assert verify["receipt_row_count"] == 28

        # 9. 原失败统计保持可查（含目标乙同名 A 的错误明细）
        failures = rw.list_failures(关联批次=BATCH)
        assert len(failures) == 3
        yi_failure = next(
            f for f in failures
            if f["目标存储"] == TARGET_B and f["来源名称"] == SOURCE_A
        )
        assert yi_failure["错误明细"] == "目标乙A同名来源错误"
        jia_failures = [f for f in failures if f["目标存储"] == TARGET_A]
        assert {f["来源名称"] for f in jia_failures} == {SOURCE_A, SOURCE_B}


class TestRewriteQueryAndClaimRules:
    """补写查询与领取规则。"""

    def test_query_filters_by_target_and_batch(self):
        _seed_two_targets()
        only_a_target = rw.query_rewrite_candidates(目标存储=TARGET_A)
        assert {c["目标存储"] for c in only_a_target} == {TARGET_A}
        other_batch = rw.query_rewrite_candidates(关联批次="CAPT-NOPE")
        assert other_batch == []

    def test_claim_unknown_or_confirmed_source_rejected(self):
        _seed_two_targets()
        rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=10)
        result = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_A,
            来源名称列表=[SOURCE_A, "source_X"], 值班员="duty_01",
        )
        assert result["ok"] is False
        reasons = {r["来源名称"]: r["reason"] for r in result["rejected"]}
        assert "已确认" in reasons[SOURCE_A]
        assert reasons["source_X"] == "来源记录不存在"

    def test_duplicate_failed_source_registration_rejected(self):
        _seed_two_targets()
        dup = rw.register_failed_source(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A,
            记录行数=1, 失败行数=1, 错误明细="重复登记",
        )
        assert dup["ok"] is False


class TestConcurrentConfirmationIdempotency:
    """更正确认与补写并发：同一(目标,批次,来源)只确认一次。"""

    def test_rewrite_first_then_correction_returns_same_receipt(self):
        _seed_two_targets()
        claim = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_A,
            来源名称列表=[SOURCE_A], 值班员="duty_01",
        )
        claim_id = claim["claims"][0]["id"]
        rewrite = rw.submit_rewrite(claim_id, 写入行数=10)
        assert rewrite["ok"] is True
        first_receipt = rewrite["receipt"]

        # 补写先确认后，常规更正只幂等返回，不新增回执、不改写确认路径
        correction = rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=99,
        )
        assert correction["ok"] is True
        assert correction["already_confirmed"] is True
        assert correction["receipt"]["id"] == first_receipt["id"]
        assert correction["receipt"]["确认路径"] == PATH_REWRITE
        assert len(rw.list_receipts(关联批次=BATCH, 目标存储=TARGET_A)) == 1

    def test_correction_then_rewrite_blocked_and_recoverable(self):
        _seed_two_targets()
        claim = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_A,
            来源名称列表=[SOURCE_B], 值班员="duty_01",
        )
        claim_id = claim["claims"][0]["id"]
        rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_B, 写入行数=20)
        blocked = rw.submit_rewrite(claim_id, 写入行数=20)
        assert blocked["ok"] is False
        assert blocked["takeover_receipt"]["确认路径"] == PATH_REGULAR

        # 被接管的领取不能走恢复（恢复仅面向中断的补写确认）
        recover = rw.recover_claim(claim_id)
        assert recover["ok"] is False


class TestRewriteBoundaryAndHandoff:
    """补写边界与交接消费规则。"""

    def test_same_named_source_in_other_target_remains_rewritable(self):
        _seed_two_targets()
        rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=10)
        yi_claim = rw.claim_ranges(
            关联批次=BATCH, 目标存储=TARGET_B,
            来源名称列表=[SOURCE_A], 值班员="duty_02",
        )
        assert yi_claim["claimed_count"] == 1
        yi_rewrite = rw.submit_rewrite(yi_claim["claims"][0]["id"], 写入行数=8)
        assert yi_rewrite["ok"] is True
        assert yi_rewrite["receipt"]["目标存储"] == TARGET_B

        # 两个目标的同名 A 各自只有一条回执，互不干扰
        assert len(rw.list_receipts(目标存储=TARGET_A)) == 1
        assert len(rw.list_receipts(目标存储=TARGET_B)) == 1

    def test_pack_without_confirmed_receipt_rejected(self):
        _seed_two_targets()
        handoff_svc = service_for("handoff_pack")
        result = handoff_svc.pack_confirmed_rows(
            关联批次=BATCH, 目标存储=TARGET_A, 目标系统="dwh_downstream",
        )
        assert result["ok"] is False
        assert "已确认" in result["reason"]

    def test_pack_only_consumes_target_confirmed_rows(self):
        _seed_two_targets()
        rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_A, 来源名称=SOURCE_A, 写入行数=10)
        rw.confirm_correction(
            关联批次=BATCH, 目标存储=TARGET_B, 来源名称=SOURCE_A, 写入行数=8)
        handoff_svc = service_for("handoff_pack")
        pack_a = handoff_svc.pack_confirmed_rows(
            关联批次=BATCH, 目标存储=TARGET_A, 目标系统="dwh_jia",
            交接编号="HAND-JIA-1",
        )
        pack_b = handoff_svc.pack_confirmed_rows(
            关联批次=BATCH, 目标存储=TARGET_B, 目标系统="dwh_yi",
            交接编号="HAND-YI-1",
        )
        assert pack_a["confirmed_row_count"] == 10
        assert pack_b["confirmed_row_count"] == 8
        assert handoff_svc.verify_pack_rows(pack_a["pack"]["id"])["ok"] is True
        assert handoff_svc.verify_pack_rows(pack_b["pack"]["id"])["ok"] is True
