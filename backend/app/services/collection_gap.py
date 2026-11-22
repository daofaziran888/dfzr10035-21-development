"""采集缺口复核领域服务。

把「管道复核记录 → 采集证据 → 待核对采集异常组 → 补采派发」串成一条可追溯的链路：

1. 按管道复核记录和采集证据检索待核对的异常组；
2. 同管道、同区间的重复上报折叠为同一事实，不重复生成补采；
3. 用「成功」结论的采集证据（如审计更正）扣减上报区间，算出真实仍缺的数据；
4. 派发前可以标定已经实采的子区间，范围调整后重新判定仍缺的数据；
5. 派发按「管道 + 实际缺口区间」幂等：同一事实被多次驳回也只保留一个补采任务；
6. 派发中断时先落检索条件，重试沿原条件继续；派发结果回写复核清单、
   治理报告和重放队列，便于核对证据最终影响了哪次重放。
"""
from __future__ import annotations

from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.store import store

Interval = tuple[int, int]

# 证据去重时只要这些字段完全一致，就视为同一份事实（多次上报只计一次）。
_EVIDENCE_FACT_FIELDS = ("管道名称", "区间起点", "区间终点", "来源", "来源标识", "结论")


# ---------------------------------------------------------------------------
# 区间运算：全部使用闭区间，先合并再扣减，避免重叠上报被重复计算。
# ---------------------------------------------------------------------------

def normalize_interval(start: Any, end: Any) -> Interval | None:
    """把入参规范成闭区间；非法或空区间返回 None。"""
    try:
        left, right = int(start), int(end)
    except (TypeError, ValueError):
        return None
    if left > right:
        return None
    return left, right


def parse_ranges(text: Any) -> list[Interval]:
    """解析 "1-100, 150-160" 这样的区间串，保持闭区间语义。"""
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        ranges: list[Interval] = []
        for item in text:
            ranges.extend(parse_ranges(item))
        return merge_ranges(ranges)
    ranges = []
    for chunk in str(text).replace("，", ",").replace("；", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        separator = "-" if "-" in chunk else "~" if "~" in chunk else None
        if separator is None:
            value = normalize_interval(chunk, chunk)
            if value:
                ranges.append(value)
            continue
        left, _, right = chunk.partition(separator)
        value = normalize_interval(left.strip(), right.strip())
        if value:
            ranges.append(value)
    return merge_ranges(ranges)


def merge_ranges(ranges: Iterable[Interval]) -> list[Interval]:
    """合并重叠或首尾相接的区间。"""
    ordered = sorted((int(a), int(b)) for a, b in ranges if int(a) <= int(b))
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_ranges(base: Iterable[Interval], cuts: Iterable[Interval]) -> list[Interval]:
    """从 base 中扣掉 cuts，返回仍然缺失的区间。"""
    result = merge_ranges(base)
    for cut_start, cut_end in merge_ranges(cuts):
        next_result: list[Interval] = []
        for start, end in result:
            if cut_end < start or cut_start > end:
                next_result.append((start, end))
                continue
            if cut_start > start:
                next_result.append((start, cut_start - 1))
            if cut_end < end:
                next_result.append((cut_end + 1, end))
        result = next_result
    return merge_ranges(result)


def intersect_ranges(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    """两段区间的交集。"""
    result: list[Interval] = []
    left_ranges = merge_ranges(left)
    right_ranges = merge_ranges(right)
    for a_start, a_end in left_ranges:
        for b_start, b_end in right_ranges:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if start <= end:
                result.append((start, end))
    return merge_ranges(result)


def format_ranges(ranges: Iterable[Interval]) -> str:
    """输出 "1-40, 41-100" 形式。"""
    return ", ".join(f"{start}-{end}" for start, end in merge_ranges(ranges))


def ranges_signature(ranges: Iterable[Interval]) -> str:
    """派发幂等键：区间先归一化再拼接，避免写法不同被当成两次补采。"""
    return format_ranges(ranges) or "-"


def _cell_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _row_interval(row: dict[str, Any]) -> Interval | None:
    return normalize_interval(row.get("区间起点"), row.get("区间终点"))


class CollectionGapService(BaseModuleService):
    MODULE = "collection_gap"
    ENTITY = "采集异常组"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "组编号", "管道名称", "复核记录编号", "区间起点", "区间终点",
        "上报来源", "上报次数", "最近上报", "关联采集来源",
    ]
    REQUIRED_FIELDS = ["组编号", "管道名称", "区间起点", "区间终点"]
    STATUS_ORDER = ["待核对", "已派发", "已补齐", "已驳回"]
    ACTION_RULES = {"确认派发": "已派发", "标记补齐": "已补齐", "驳回上报": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回上报"]

    # ------------------------------------------------------------------
    # 证据与缺口判定
    # ------------------------------------------------------------------

    def _pipeline_success_ranges(self, pipeline: str) -> list[Interval]:
        """同一管道里所有结论为成功的采集证据；审计更正等独立来源都算。"""
        ranges = []
        for evidence in store.rows("collection_evidence"):
            if evidence.get("管道名称") != pipeline:
                continue
            if str(evidence.get("结论", "")) not in {"成功", "已实采", "已补齐"}:
                continue
            interval = _row_interval(evidence)
            if interval:
                ranges.append(interval)
        return merge_ranges(ranges)

    def missing_ranges(self, group: dict[str, Any], scope: Iterable[Interval] | None = None) -> list[Interval]:
        """真实缺口 = 上报区间（或调整后的范围） − 已成功采集的证据。"""
        interval = _row_interval(group)
        base = [interval] if interval else []
        if scope:
            base = intersect_ranges(base, scope)
        return subtract_ranges(base, self._pipeline_success_ranges(str(group.get("管道名称", ""))))

    def dedupe_evidence(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """同源、同区间、同结论的重复上报折叠；不同来源即使区间重叠也全部保留。"""
        merged: dict[tuple, dict[str, Any]] = {}
        source_ids: dict[str, int] = {}
        for row in rows:
            key = tuple(str(row.get(field, "")) for field in _EVIDENCE_FACT_FIELDS)
            source_key = str(row.get("来源标识") or row.get("来源") or "")
            source_ids[source_key] = source_ids.get(source_key, 0) + 1
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    **row,
                    "重复次数": 1,
                    "证据编号列表": [row.get("证据编号", "")],
                    "_来源键": source_key,
                }
                continue
            current["重复次数"] = int(current.get("重复次数", 1)) + 1
            current["证据编号列表"].append(row.get("证据编号", ""))
        result = list(merged.values())
        for item in result:
            # 来源标识不重复的证据才是真正独立的第二来源，可以用来互相印证。
            item["来源独立"] = source_ids.get(item.pop("_来源键", ""), 0) == 1
            item["区间"] = format_ranges([_row_interval(item)]) if _row_interval(item) else ""
        result.sort(key=lambda item: (str(item.get("管道名称", "")), _cell_int(item.get("区间起点")), _cell_int(item.get("区间终点"))))
        return result

    def group_evidence(self, group: dict[str, Any]) -> list[dict[str, Any]]:
        """异常组的关联证据：先按异常组编号，其次按同管道 + 区间重叠补齐。"""
        group_no = str(group.get("组编号", ""))
        pipeline = str(group.get("管道名称", ""))
        group_range = _row_interval(group)
        rows = []
        for row in store.rows("collection_evidence"):
            if row.get("异常组编号") == group_no:
                rows.append(row)
                continue
            if row.get("管道名称") != pipeline or not group_range:
                continue
            interval = _row_interval(row)
            if interval and intersect_ranges([group_range], [interval]):
                rows.append(row)
        return self.dedupe_evidence(rows)

    def _dedupe_groups(self, rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """把同一管道里重复上报同一段缺口的异常组折叠成一个事实。

        折叠规则：区间完全相同，或该组区间被同管道里更长的上报区间完整覆盖。
        返回（代表组，重复组）；重复组保留自己的证据，仍可被检索核对。
        """
        candidates = []
        for row in rows:
            interval = _row_interval(row)
            if interval is None:
                continue
            candidates.append((interval, row))
        # 区间大的先处理，后面的子区间才能判断是否被覆盖。
        candidates.sort(key=lambda item: (-(item[0][1] - item[0][0]), _cell_int(item[1].get("id"))))
        representatives: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for interval, row in candidates:
            owner = None
            for representative in representatives:
                owner_interval = _row_interval(representative)
                if representative.get("管道名称") != row.get("管道名称") or owner_interval is None:
                    continue
                if intersect_ranges([owner_interval], [interval]) == merge_ranges([interval]):
                    owner = representative
                    break
            if owner is None:
                representatives.append({
                    **row,
                    "重复组编号": [],
                    "重复上报次数": _cell_int(row.get("上报次数"), 1),
                    "重复来源": [row.get("上报来源", "")],
                })
                continue
            owner["重复组编号"].append(row.get("组编号", ""))
            owner["重复上报次数"] += _cell_int(row.get("上报次数"), 1)
            source = str(row.get("上报来源", ""))
            if source and source not in owner["重复来源"]:
                owner["重复来源"].append(source)
            duplicates.append({
                **row,
                "是否重复": True,
                "重复于": owner.get("组编号", ""),
                "_代表组ID": int(owner["id"]),
                "重叠区间": format_ranges(intersect_ranges([_row_interval(owner) or interval], [interval])),
            })
        representatives.sort(key=lambda item: _cell_int(item.get("id")))
        return representatives, duplicates

    def _annotate(self, group: dict[str, Any], scope: Iterable[Interval] | None = None) -> dict[str, Any]:
        """补齐真实缺口、关联证据和可核对的展示字段。"""
        interval = _row_interval(group)
        missing = self.missing_ranges(group, scope)
        evidence = self.group_evidence(group)
        success_evidence = [item for item in evidence if str(item.get("结论")) in {"成功", "已实采", "已补齐"}]
        return {
            **group,
            "上报区间": format_ranges([interval]) if interval else "",
            "真实缺口": format_ranges(missing) or "已补齐",
            "真实缺口区间": missing,
            "是否重复": False,
            "重复上报组数": len(group.get("重复组编号", []) or []),
            "关联证据": evidence,
            "关联证据编号": [item.get("证据编号", "") for item in evidence],
            "成功证据编号": [item.get("证据编号", "") for item in success_evidence],
            "关联证据来源": [item.get("来源", "") for item in evidence if item.get("来源")],
            "仍缺数量": sum(end - start + 1 for start, end in missing),
        }

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_groups(
        self,
        *,
        review_no: str | None = None,
        pipeline: str | None = None,
        start: int | None = None,
        end: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
        include_duplicates: bool = False,
        only_pending: bool = True,
    ) -> dict[str, Any]:
        """按管道复核记录 + 采集证据关联筛选待核对异常组。"""
        rows = list(store.rows(self.MODULE))
        if review_no:
            rows = [row for row in rows if review_no in str(row.get("复核记录编号", ""))]
        if pipeline:
            rows = [row for row in rows if pipeline in str(row.get("管道名称", ""))]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in " ".join(str(value) for value in row.values()).casefold()
            ]
        scope = None
        if start is not None or end is not None:
            query_range = normalize_interval(start if start is not None else 0, end if end is not None else 10**9)
            scope = [query_range] if query_range else None
        if scope:
            # 区间检索只返回与该区间相交的异常组，避免把别的时段算成“已补齐”。
            rows = [
                row for row in rows
                if _row_interval(row) and intersect_ranges(scope, [_row_interval(row)])
            ]
        # 先去重再按状态筛选：已经派发的异常组也要能折叠掉后来重复上报的事实。
        representatives, duplicates = self._dedupe_groups(rows)
        if status:
            representatives = [row for row in representatives if str(row.get("status")) == status]
            kept_ids = {int(row["id"]) for row in representatives}
            duplicates = [group for group in duplicates if group.get("_代表组ID") in kept_ids]
        elif only_pending:
            # 代表组可能已被派发，重复上报仍属于同一事实，不应再算成待核对缺口。
            representatives = [row for row in representatives if str(row.get("status")) == "待核对"]
            kept_ids = {int(row["id"]) for row in representatives}
            duplicates = [group for group in duplicates if group.get("_代表组ID") in kept_ids]
        items = [self._annotate(group, scope) for group in representatives]
        reported = [
            self._annotate(group, scope) | {"是否重复": True, "重复于": group.get("重复于", "")}
            for group in duplicates
        ]
        payload: dict[str, Any] = {
            "items": items,
            "total": len(items),
            "duplicates": reported,
            "duplicates_hidden": len(reported),
            "query": {
                "复核记录编号": review_no or "",
                "管道名称": pipeline or "",
                "区间起点": start,
                "区间终点": end,
                "显示重复上报": include_duplicates,
            },
        }
        if include_duplicates:
            payload["items"] = items + reported
            payload["total"] = len(payload["items"])
        return payload

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """通用详情接口也返回判定后的真实缺口，避免和复核工作台口径不一致。"""
        group = store.find(self.MODULE, entry_id)
        if group is None:
            return None
        return self._annotate(group)

    def get_group_detail(self, entry_id: int) -> dict[str, Any] | None:
        group = store.find(self.MODULE, entry_id)
        if group is None:
            return None
        detail = self._annotate(group)
        detail["证据"] = self.group_evidence(group)
        detail["复核记录"] = self._find_review(str(group.get("复核记录编号", "")))
        detail["派发记录"] = [
            record for record in store.rows("collection_dispatch")
            if entry_id in [int(value) for value in record.get("选中异常组ID", [])]
        ]
        return detail

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """通用资源列表复用同一套缺口判定，避免工作台和复核页口径不一致。"""
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in " ".join(str(value) for value in row.values()).casefold()
            ]
        representatives, _ = self._dedupe_groups(rows)
        items = [self._annotate(group) for group in representatives]
        total = len(items)
        start = (page - 1) * size
        return items[start:start + size], total

    # ------------------------------------------------------------------
    # 选中 / 标定 / 范围调整
    # ------------------------------------------------------------------

    def select_group(self, entry_id: int, selected: bool = True) -> dict[str, Any] | None:
        group = store.find(self.MODULE, entry_id)
        if group is None:
            return None
        group["已选中"] = bool(selected)
        return self._annotate(group)

    def mark_collected(
        self,
        *,
        pipeline: str,
        start: int,
        end: int,
        source: str = "人工标定",
        source_id: str = "manual-check",
        group_id: int | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """把一段区间标成已经实采（成功证据）。同一管道/区间/来源只登记一次。"""
        result = self.add_evidence(
            pipeline=pipeline,
            start=start,
            end=end,
            conclusion="成功",
            source=source,
            source_id=source_id,
            group_id=group_id,
            remark=remark or f"提交前标定 {start}-{end} 已实际采集",
        )
        if not result.get("ok"):
            return result
        interval = normalize_interval(start, end)
        if not result.get("created"):
            return {
                **result,
                "message": f"该管道 {format_ranges([interval]) if interval else ''} 已标定为实采，只计一次",
            }
        evidence = result["evidence"]
        evidence["证据类型"] = "人工标定" if source == "人工标定" else "实采补录"
        return {
            **result,
            "message": f"已将{pipeline} {format_ranges([interval]) if interval else ''} 标为已实采",
        }

    def add_evidence(
        self,
        *,
        pipeline: str,
        start: int,
        end: int,
        conclusion: str,
        source: str,
        source_id: str = "",
        group_id: int | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """登记外部采集证据（失败/成功都可以）。完全重复的事实只保留一份。"""
        interval = normalize_interval(start, end)
        if interval is None:
            return {"ok": False, "message": "区间不合法：起点必须小于等于终点"}
        if not pipeline.strip():
            return {"ok": False, "message": "缺少管道名称"}
        source_id = source_id or source
        for evidence in store.rows("collection_evidence"):
            if (
                evidence.get("管道名称") == pipeline
                and _row_interval(evidence) == interval
                and str(evidence.get("来源")) == source
                and str(evidence.get("来源标识")) == source_id
                and str(evidence.get("结论")) == conclusion
            ):
                return {
                    "ok": True,
                    "created": False,
                    "message": "相同事实已登记过，未重复写入",
                    "evidence": evidence,
                }
        group_no = ""
        if group_id is not None:
            group = store.find(self.MODULE, group_id)
            if group is not None:
                group_no = str(group.get("组编号", ""))
        rows = store.rows("collection_evidence")
        next_id = max((_cell_int(row.get("id")) for row in rows), default=0) + 1
        evidence = {
            "id": next_id,
            "证据编号": f"EVID-{next_id:04d}",
            "管道名称": pipeline,
            "复核记录编号": (self._find_review_by_pipeline(pipeline) or {}).get("记录编号", ""),
            "异常组编号": group_no,
            "来源": source,
            "来源标识": source_id,
            "区间起点": interval[0],
            "区间终点": interval[1],
            "结论": conclusion,
            "证据类型": "异常上报" if conclusion != "成功" else "独立复核",
            "采集时间": "2026-11-22",
            "说明": remark,
        }
        rows.append(evidence)
        return {"ok": True, "created": True, "message": "采集证据已登记", "evidence": evidence}

    def adjust_group_range(self, entry_id: int, start: int, end: int) -> dict[str, Any] | None:
        """范围调整：改写异常组上报区间，并按新范围重新判定仍缺的数据。"""
        group = store.find(self.MODULE, entry_id)
        if group is None:
            return None
        interval = normalize_interval(start, end)
        if interval is None:
            return {"ok": False, "message": "区间不合法"}
        group["区间起点"], group["区间终点"] = interval
        detail = self._annotate(group)
        return {"ok": True, "message": f"范围已调整为{format_ranges([interval])}，已重新判定缺口", "group": detail}

    # ------------------------------------------------------------------
    # 派发（含幂等和中断续派）
    # ------------------------------------------------------------------

    def dispatch(
        self,
        *,
        group_ids: list[int],
        start: int | None = None,
        end: int | None = None,
        operator: str = "治理员",
        interrupt: bool = False,
    ) -> dict[str, Any]:
        """按选中的异常组派发补采：幂等、可中断、结果回写三张清单。"""
        groups = []
        for group_id in group_ids:
            group = store.find(self.MODULE, group_id)
            if group is None:
                return {"ok": False, "message": f"采集异常组 {group_id} 不存在"}
            groups.append(group)
        if not groups:
            return {"ok": False, "message": "请先选择待核对的采集异常组"}
        scope = None
        if start is not None or end is not None:
            scope_interval = normalize_interval(
                start if start is not None else min(_cell_int(g.get("区间起点")) for g in groups),
                end if end is not None else max(_cell_int(g.get("区间终点")) for g in groups),
            )
            if scope_interval is None:
                return {"ok": False, "message": "派发区间不合法"}
            scope = [scope_interval]
        pipeline_names = sorted({str(group.get("管道名称", "")) for group in groups})
        review_nos = sorted({str(group.get("复核记录编号", "")) for group in groups if group.get("复核记录编号")})
        criteria = {
            "选中异常组ID": [int(group["id"]) for group in groups],
            "选中异常组": [str(group.get("组编号", "")) for group in groups],
            "复核记录编号": review_nos,
            "管道名称": pipeline_names,
            "区间": format_ranges(scope) if scope else "按异常组上报区间",
            "操作人": operator,
        }
        dispatch_row = self._create_dispatch_record(criteria, groups)
        if interrupt:
            dispatch_row["状态"] = "中断"
            dispatch_row["结果摘要"] = "派发中断，检索条件已保留，可重试续派"
            return {
                "ok": False,
                "message": "派发中断，已保留原检索条件；重试将沿原条件继续",
                "dispatch": dispatch_row,
                "search_criteria": criteria,
            }
        try:
            return self._execute_dispatch(dispatch_row, groups, scope, criteria)
        except Exception as error:  # pragma: no cover - 兜底保留现场，便于重试
            dispatch_row["状态"] = "中断"
            dispatch_row["结果摘要"] = f"派发中断：{error}"
            return {
                "ok": False,
                "message": f"派发中断：{error}；检索条件已保留，可重试续派",
                "dispatch": dispatch_row,
                "search_criteria": criteria,
            }

    def retry_dispatch(self, dispatch_id: int) -> dict[str, Any]:
        """派发中断后重试：沿用记录里的原检索条件，不接受新的筛选参数。"""
        record = store.find("collection_dispatch", dispatch_id)
        if record is None:
            return {"ok": False, "message": f"派发记录 {dispatch_id} 不存在"}
        if record.get("状态") == "已完成":
            return {
                "ok": True,
                "message": "该派发已完成，未重复生成补采",
                "dispatch": record,
                "search_criteria": record.get("检索条件", {}),
            }
        criteria = record.get("检索条件", {}) or {}
        group_ids = [int(value) for value in criteria.get("选中异常组ID", [])]
        groups = [group for group in (store.find(self.MODULE, gid) for gid in group_ids) if group]
        scope_ranges = parse_ranges(criteria.get("区间")) if criteria.get("区间") != "按异常组上报区间" else []
        return self._execute_dispatch(record, groups, scope_ranges or None, criteria)

    def _create_dispatch_record(self, criteria: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
        rows = store.rows("collection_dispatch")
        next_id = max((_cell_int(row.get("id")) for row in rows), default=0) + 1
        record = {
            "id": next_id,
            "派发编号": f"DISP-{next_id:04d}",
            "状态": "派发中",
            "选中异常组ID": criteria["选中异常组ID"],
            "选中异常组": criteria["选中异常组"],
            "复核记录编号": criteria["复核记录编号"],
            "管道名称": criteria["管道名称"],
            "检索条件": criteria,
            "区间": criteria["区间"],
            "操作人": criteria["操作人"],
            "重放任务编号": [],
            "治理报告编号": [],
            "证据编号": [],
            "结果摘要": "",
            "派发时间": "2026-11-22",
        }
        rows.append(record)
        # 先落库再执行，保证中断后仍能按原条件重试。
        return record

    def _execute_dispatch(
        self,
        record: dict[str, Any],
        groups: list[dict[str, Any]],
        scope: list[Interval] | None,
        criteria: dict[str, Any],
    ) -> dict[str, Any]:
        """真正生成治理报告和补采任务，并把结果回写到复核清单。"""
        task_numbers: list[str] = []
        report_numbers: list[str] = []
        used_evidence: list[str] = []
        group_results: list[dict[str, Any]] = []
        pipeline_tasks: dict[str, list[str]] = {}
        group_reports: dict[int, list[str]] = {}

        # 同一次派发里按管道归并，避免多个异常组指向同一段事实。
        pipeline_missing: dict[str, list[Interval]] = {}
        pipeline_groups: dict[str, list[dict[str, Any]]] = {}
        group_missing: dict[int, list[Interval]] = {}
        for group in groups:
            missing = self.missing_ranges(group, scope)
            group_missing[int(group["id"])] = missing
            pipeline = str(group.get("管道名称", ""))
            pipeline_missing.setdefault(pipeline, []).extend(missing)
            pipeline_groups.setdefault(pipeline, []).append(group)
            used_evidence.extend(self._success_evidence_numbers(pipeline))

        for pipeline, ranges in pipeline_missing.items():
            missing = merge_ranges(ranges)
            task = None
            if missing:
                task, _created = self._ensure_collection_task(
                    pipeline, missing, pipeline_groups.get(pipeline, []))
                if task is not None:
                    task_number = str(task.get("任务编号", ""))
                    if task_number and task_number not in task_numbers:
                        task_numbers.append(task_number)
                    if task_number:
                        pipeline_tasks.setdefault(pipeline, []).append(task_number)
            for group in pipeline_groups.get(pipeline, []):
                # 组状态按组自己的真实缺口判定，避免被同管道其他组带着走。
                if not group_missing.get(int(group["id"])):
                    self._set_group_status(group, "已补齐")
                elif task is not None and str(task.get("status")) == "已成功":
                    self._set_group_status(group, "已补齐")
                else:
                    self._set_group_status(group, "已派发")

        for group in groups:
            missing = group_missing.get(int(group["id"]), [])
            pipeline = str(group.get("管道名称", ""))
            report = None
            if missing:
                report, _ = self._ensure_govern_report(
                    group, missing, pipeline_tasks.get(pipeline, []))
                if report is not None:
                    report_number = str(report.get("报告编号", ""))
                    if report_number and report_number not in report_numbers:
                        report_numbers.append(report_number)
                    if report_number:
                        group_reports.setdefault(int(group["id"]), []).append(report_number)
            group_results.append({
                "组编号": group.get("组编号", ""),
                "管道名称": pipeline,
                "上报区间": format_ranges([_row_interval(group)]) if _row_interval(group) else "",
                "真实缺口": format_ranges(missing) or "已补齐",
                "状态": group.get("status", ""),
            })

        for group in groups:
            pipeline = str(group.get("管道名称", ""))
            self._writeback_review(
                group,
                group_missing.get(int(group["id"]), []),
                pipeline_tasks.get(pipeline, []),
                group_reports.get(int(group["id"]), []),
                self._success_evidence_numbers(pipeline),
                criteria.get("区间", ""),
            )

        record["状态"] = "已完成"
        record["重放任务编号"] = task_numbers
        record["治理报告编号"] = report_numbers
        record["证据编号"] = sorted(set(used_evidence))
        record["结果摘要"] = (
            f"已派发{len(task_numbers)}个补采任务、{len(report_numbers)}条治理记录"
            if task_numbers or report_numbers
            else "选中区间已有成功结果，无需补采"
        )
        return {
            "ok": True,
            "message": record["结果摘要"],
            "dispatch": record,
            "search_criteria": criteria,
            "groups": group_results,
            "tasks": [task for task in store.rows("retry_queue") if task.get("任务编号") in task_numbers],
            "reports": [report for report in store.rows("govern_report") if report.get("报告编号") in report_numbers],
            "evidence_used": sorted(set(used_evidence)),
        }

    def _ensure_collection_task(
        self,
        pipeline: str,
        missing: list[Interval],
        groups: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, bool]:
        """管道 + 实际缺口区间 作为事实键：已经派发/补齐过的只计一次。"""
        signature = ranges_signature(missing)
        group_numbers = [str(group.get("组编号", "")) for group in groups]
        review_nos = [str(group.get("复核记录编号", "")) for group in groups if group.get("复核记录编号")]
        for task in store.rows("retry_queue"):
            if task.get("失败端点") != pipeline:
                continue
            existing_signature = task.get("采集区间") or self._task_signature_from_reason(task)
            if existing_signature != signature:
                continue
            # 同一管道、同一时间段：不论此前被驳回几次，都复用同一个补采任务。
            if not task.get("关联异常组"):
                task["关联异常组"] = ", ".join(group_numbers)
            if not task.get("关联复核记录"):
                task["关联复核记录"] = ", ".join(review_nos)
            task["采集管道"] = pipeline
            task["采集区间"] = signature
            if str(task.get("status")) == "永久失败":
                # 证据仍判定为缺口，复用原任务重新排队，不另开一条补采。
                task["status"] = "待重试"
                task["pending"] = True
                task["abnormal"] = True
                task["重申次数"] = _cell_int(task.get("重申次数")) + 1
            return task, False
        rows = store.rows("retry_queue")
        next_seq = sum(1 for row in rows if str(row.get("任务编号", "")).startswith("RETR-GAP-")) + 1
        task = {
            "id": max((_cell_int(row.get("id")) for row in rows), default=0) + 1,
            "任务编号": f"RETR-GAP-{next_seq:04d}",
            "失败端点": pipeline,
            "失败原因": f"采集缺口 {signature}",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "linear:5s",
            "最后重试": "",
            "队列状态": "collection_gap",
            "采集区间": signature,
            "关联异常组": ", ".join(group_numbers),
            "关联复核记录": ", ".join(review_nos),
            "采集管道": pipeline,
            "status": "待重试",
            "pending": True,
            "abnormal": True,
        }
        rows.append(task)
        return task, True

    def _task_signature_from_reason(self, task: dict[str, Any]) -> str:
        reason = str(task.get("失败原因", ""))
        if reason.startswith("采集缺口 "):
            return reason[len("采集缺口 "):].strip()
        return ""

    def _ensure_govern_report(
        self,
        group: dict[str, Any],
        missing: list[Interval],
        task_numbers: list[str],
    ) -> tuple[dict[str, Any] | None, bool]:
        """治理报告按选中的异常组登记仍缺范围；同组同缺口不重复开报告。"""
        signature = ranges_signature(missing)
        group_no = str(group.get("组编号", ""))
        for report in store.rows("govern_report"):
            if (
                report.get("关联异常组") == group_no
                and report.get("可执行区间") == signature
            ):
                return report, False
        rows = store.rows("govern_report")
        next_seq = sum(1 for row in rows if str(row.get("报告编号", "")).startswith("GOVE-GAP-")) + 1
        report = {
            "id": max((_cell_int(row.get("id")) for row in rows), default=0) + 1,
            "报告编号": f"GOVE-GAP-{next_seq:04d}",
            "评估范围": f"采集缺口:{group.get('管道名称', '')}:{signature}",
            "合规指标": "数据采集完整性",
            "当前得分": 55.0,
            "改进事项": f"{group.get('组编号', '')}仍缺{signature}，需按可执行区间补采",
            "评估周期": "2026-Q4",
            "负责人": "数据采集组",
            "生成时间": "2026-11-22",
            "关联异常组": group_no,
            "关联复核记录": group.get("复核记录编号", ""),
            "可执行区间": signature,
            "关联重放任务": ", ".join(task_numbers),
            "证据编号": ", ".join(self._success_evidence_numbers(str(group.get("管道名称", "")))),
            "status": "已生成",
            "pending": True,
            "abnormal": True,
        }
        rows.append(report)
        return report, True

    def _success_evidence_numbers(self, pipeline: str) -> list[str]:
        numbers = []
        for evidence in store.rows("collection_evidence"):
            if evidence.get("管道名称") != pipeline:
                continue
            if str(evidence.get("结论")) in {"成功", "已实采", "已补齐"}:
                numbers.append(str(evidence.get("证据编号", "")))
        return numbers

    def _set_group_status(self, group: dict[str, Any], status: str) -> None:
        group["status"] = status
        group["pending"] = status == "待核对"
        group["abnormal"] = status == "待核对"
        group["已选中"] = False

    def _find_review(self, review_no: str) -> dict[str, Any] | None:
        for row in store.rows("pipeline_audit"):
            if str(row.get("记录编号", "")) == review_no:
                return row
        return None

    def _find_review_by_pipeline(self, pipeline: str) -> dict[str, Any] | None:
        for row in store.rows("pipeline_audit"):
            if str(row.get("管道名称", "")) == pipeline:
                return row
        return None

    def _writeback_review(
        self,
        group: dict[str, Any],
        missing: list[Interval],
        task_numbers: list[str],
        report_numbers: list[str],
        evidence_numbers: list[str],
        criteria_range: str = "",
    ) -> None:
        """派发结果回写复核清单，便于核对证据影响了哪次重放。"""
        review = self._find_review(str(group.get("复核记录编号", "")))
        if review is None:
            return
        signature = format_ranges(missing)
        review["待派发区间"] = signature
        review["派发结果"] = "已派发" if signature else "已补齐"
        review["重放任务编号"] = ", ".join(task_numbers)
        review["治理报告编号"] = ", ".join(report_numbers)
        review["影响证据"] = ", ".join(evidence_numbers)
        review["派发次数"] = _cell_int(review.get("派发次数")) + 1
        review["最近派发"] = "2026-11-22"
        review["关联异常组"] = group.get("组编号", "")
        review["派发检索区间"] = criteria_range
        if not signature:
            review["审计结论"] = "审计更正后已补齐，无需补采"

    # ------------------------------------------------------------------
    # 核对视图
    # ------------------------------------------------------------------

    def review_list(self) -> list[dict[str, Any]]:
        """复核清单：只有关联了异常组的记录才参与补采核对。"""
        items = []
        for row in store.rows("pipeline_audit"):
            if not row.get("关联异常组"):
                continue
            items.append({
                "id": row["id"],
                "记录编号": row.get("记录编号", ""),
                "管道名称": row.get("管道名称", ""),
                "操作人": row.get("操作人", ""),
                "审计结论": row.get("审计结论", ""),
                "关联异常组": row.get("关联异常组", ""),
                "待派发区间": row.get("待派发区间", ""),
                "派发结果": row.get("派发结果", ""),
                "重放任务编号": row.get("重放任务编号", ""),
                "治理报告编号": row.get("治理报告编号", ""),
                "影响证据": row.get("影响证据", ""),
                "派发次数": _cell_int(row.get("派发次数")),
                "最近派发": row.get("最近派发", ""),
                "status": row.get("status", ""),
            })
        return items

    def list_dispatches(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows("collection_dispatch")]

    def collection_tasks(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.rows("retry_queue")
            if row.get("队列状态") == "collection_gap"
            or str(row.get("任务编号", "")).startswith("RETR-GAP-")
        ]

    def govern_items(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.rows("govern_report")
            if row.get("关联异常组") or str(row.get("报告编号", "")).startswith("GOVE-GAP-")
        ]

    def verify(self, group_id: int | None = None) -> dict[str, Any]:
        """核对检索结果、治理记录的可执行区间和重放队列对应的采集任务。"""
        search = self.search_groups(only_pending=False)
        if group_id is not None:
            search["items"] = [item for item in search["items"] if int(item["id"]) == group_id]
            search["total"] = len(search["items"])
        return {
            "检索结果": search,
            "复核清单": self.review_list(),
            "治理记录": self.govern_items(),
            "采集任务": self.collection_tasks(),
            "派发记录": self.list_dispatches(),
        }

    # ------------------------------------------------------------------
    # 通用动作 / 快照
    # ------------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        group = store.find(self.MODULE, entry_id)
        if group is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "确认派发":
            result = self.dispatch(group_ids=[entry_id], operator="复核工作台")
            if not result.get("ok"):
                return group, str(result.get("message", "派发失败"))
            return group, str(result.get("message", "已派发补采"))
        if action == "标记补齐":
            self._set_group_status(group, "已补齐")
            return group, "采集异常组已标记补齐"
        if action == "驳回上报":
            # 驳回只关闭上报，不再额外生成补采，避免同一事实重复补采。
            self._set_group_status(group, "已驳回")
            return group, "已驳回上报，未重复生成补采"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": {
                status: sum(1 for row in rows if row.get("status") == status)
                for status in self.STATUS_ORDER
            },
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
            "evidence": len(store.rows("collection_evidence")),
            "dispatches": len(store.rows("collection_dispatch")),
        }
