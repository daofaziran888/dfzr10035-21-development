"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS
from app.seed_collection_gap import AUX_TABLES, EXTRA_ROWS_BY_MODULE, EXTRA_SEED_ROWS


def _initial_tables() -> dict[str, list[dict[str, Any]]]:
    tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    for module, extra_rows in EXTRA_ROWS_BY_MODULE.items():
        bucket = tables.setdefault(module, [])
        bucket.extend(dict(row) for row in extra_rows)
    for module, rows in EXTRA_SEED_ROWS.items():
        tables.setdefault(module, []).extend(dict(row) for row in rows)
    return tables


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = _initial_tables()

    def module_names(self) -> list[str]:
        """业务模块名；复核工作台的内部证据/派发流水不计入模块概览。"""
        return sorted(name for name in self._tables if name not in AUX_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
