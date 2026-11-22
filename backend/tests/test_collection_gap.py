"""采集缺口复核的回归测试：区间运算、去重、标定、派发幂等、中断续派和回写。

用标准库 unittest 运行（仓库依赖里没有 pytest）：

    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.collection_gap import (
    format_ranges,
    intersect_ranges,
    merge_ranges,
    parse_ranges,
    subtract_ranges,
)
from app.store import Store
import app.services.collection_gap as collection_gap_module


class IntervalMathTest(unittest.TestCase):
    def test_merge_overlapping_and_adjacent(self):
        self.assertEqual(merge_ranges([(1, 40), (30, 60), (61, 70)]), [(1, 70)])

    def test_subtract_keeps_edges(self):
        self.assertEqual(subtract_ranges([(1, 100)], [(1, 40)]), [(41, 100)])
        self.assertEqual(subtract_ranges([(130, 180)], [(131, 155)]), [(130, 130), (156, 180)])

    def test_intersect_scope(self):
        self.assertEqual(intersect_ranges([(1, 100)], [(50, 120)]), [(50, 100)])

    def test_parse_and_format(self):
        self.assertEqual(parse_ranges("1-40, 41-100"), [(1, 100)])
        self.assertEqual(format_ranges([(41, 100), (1, 40)]), "1-100")


class CollectionGapWorkflowTest(unittest.TestCase):
    def setUp(self):
        # 每个用例一份全新的内存仓库，避免用例之间互相污染。
        self._original_store = collection_gap_module.store
        collection_gap_module.store = Store()
        from app.services import service_for
        self.service = service_for("collection_gap")
        self.addCleanup(self._restore_store)

    def _restore_store(self):
        collection_gap_module.store = self._original_store

    def test_search_folds_duplicate_reports_but_keeps_them_visible(self):
        result = self.service.search_groups(review_no="PIPE-1001", start=1, end=100)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["组编号"], "GAP-0001")
        self.assertEqual(result["items"][0]["真实缺口"], "1-100")
        self.assertEqual(result["duplicates_hidden"], 1)
        self.assertEqual(result["duplicates"][0]["组编号"], "GAP-0004")
        self.assertEqual(result["duplicates"][0]["重复于"], "GAP-0001")

        expanded = self.service.search_groups(review_no="PIPE-1001", include_duplicates=True)
        self.assertEqual(expanded["total"], 2)

    def test_success_evidence_reduces_reported_range(self):
        group = self.service.search_groups(review_no="PIPE-1002")["items"][0]
        # 上报 130-180，审计更正认定 131-155 已实采。
        self.assertEqual(group["真实缺口"], "130-130, 156-180")
        self.assertIn("EVID-0004", group["关联证据编号"])

    def test_independent_overlapping_evidence_all_retained(self):
        group = self.service.search_groups(review_no="PIPE-1001")["items"][0]
        self.assertEqual(sorted(group["关联证据编号"]), ["EVID-0001", "EVID-0002", "EVID-0003"])

    def test_mark_collected_is_idempotent(self):
        first = self.service.mark_collected(pipeline="订单增量同步管道", start=1, end=40, group_id=1)
        second = self.service.mark_collected(pipeline="订单增量同步管道", start=1, end=40, group_id=1)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        group = self.service.search_groups(review_no="PIPE-1001")["items"][0]
        self.assertEqual(group["真实缺口"], "41-100")
        self.assertEqual(group["仍缺数量"], 60)

    def test_dispatch_writes_back_and_dedupes_rejections(self):
        self.service.mark_collected(pipeline="订单增量同步管道", start=1, end=40, group_id=1)
        first = self.service.dispatch(group_ids=[1])
        self.assertTrue(first["ok"])
        self.assertEqual([task["采集区间"] for task in first["tasks"]], ["41-100"])

        review = [row for row in self.service.review_list() if row["记录编号"] == "PIPE-1001"][0]
        self.assertEqual(review["待派发区间"], "41-100")
        self.assertEqual(review["重放任务编号"], "RETR-GAP-0001")
        self.assertEqual(review["影响证据"], "EVID-0006")

        # 同一事实再次派发（等价于被多次驳回）不会多生成补采。
        for _ in range(3):
            self.service.dispatch(group_ids=[1])
        tasks = self.service.collection_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["采集区间"], "41-100")
        self.assertEqual(len(self.service.govern_items()), 1)

    def test_interrupted_dispatch_resumes_with_original_criteria(self):
        self.service.mark_collected(pipeline="订单增量同步管道", start=1, end=40, group_id=1)
        interrupted = self.service.dispatch(group_ids=[1], start=1, end=100, interrupt=True)
        self.assertFalse(interrupted["ok"])
        self.assertEqual(interrupted["dispatch"]["状态"], "中断")
        # 中断时不能留下补采任务，但检索条件必须已经落库。
        self.assertEqual(self.service.collection_tasks(), [])
        self.assertEqual(interrupted["search_criteria"]["区间"], "1-100")

        resumed = self.service.retry_dispatch(interrupted["dispatch"]["id"])
        self.assertTrue(resumed["ok"])
        self.assertEqual([task["采集区间"] for task in resumed["tasks"]], ["41-100"])
        self.assertEqual(resumed["search_criteria"]["区间"], "1-100")

        again = self.service.retry_dispatch(interrupted["dispatch"]["id"])
        self.assertTrue(again["ok"])
        self.assertEqual(len(self.service.collection_tasks()), 1)

    def test_range_adjust_recomputes_missing(self):
        self.service.mark_collected(pipeline="订单增量同步管道", start=1, end=40, group_id=1)
        result = self.service.adjust_group_range(1, 1, 60)
        self.assertTrue(result["ok"])
        self.assertEqual(result["group"]["真实缺口"], "41-60")

    def test_verify_exposes_executable_ranges_and_tasks(self):
        self.service.dispatch(group_ids=[2])
        payload = self.service.verify()
        self.assertEqual(payload["治理记录"][0]["可执行区间"], "130-130, 156-180")
        self.assertEqual(payload["采集任务"][0]["采集区间"], "130-130, 156-180")
        self.assertIn("检索结果", payload)

    def test_fully_covered_group_needs_no_dispatch(self):
        self.service.add_evidence(
            pipeline="气象数据投递管道", start=130, end=180,
            conclusion="成功", source="审计更正", source_id="audit-fix-02", group_id=2,
        )
        result = self.service.dispatch(group_ids=[2])
        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["reports"], [])
        self.assertEqual(result["groups"][0]["状态"], "已补齐")


if __name__ == "__main__":
    unittest.main()
