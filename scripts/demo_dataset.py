"""Fixed synthetic ground truth. No model or precomputed insight is involved."""

import random
from datetime import datetime, timedelta


def history_specs(reference: datetime) -> list[dict]:
    if reference.tzinfo is None:
        raise ValueError("reference time must be timezone-aware")
    rng = random.Random(42)
    rows = []
    for i in range(600):
        row = {
            "index": i,
            "equipment_index": 4 + i % 21,
            "kind": "planned",
            "fault_index": 8,
            "duration_minutes": rng.randint(35, 100),
            "description": "СИНТЕТИЧЕСКИЙ ППР: осмотр и смазка узла",
            "work_done": "Проведён осмотр узла, очищены поверхности, выполнена смазка",
            "material_index": 4,
            "quantity": 0.5,
        }
        days_ago = 1 + i % 96
        if 540 <= i < 552:
            row.update(
                equipment_index=0,
                kind="unplanned",
                fault_index=0,
                description="СИНТЕТИЧЕСКИЙ ОТКАЗ: шум и перегрев подшипника К-3",
                work_done="Заменён подшипник, проверены соосность и температура",
                material_index=1,
                quantity=1,
            )
            days_ago = 2 + (i - 540) * 2
        elif 552 <= i < 568:
            offset = i - 552
            unplanned = offset % 2 == 1
            row.update(
                equipment_index=1,
                kind="unplanned" if unplanned else "planned",
                fault_index=10,
                description="СИНТЕТИЧЕСКИЙ сценарий ППР/отказ редуктора",
                work_done="Выполнены регулировка редуктора и контроль вибрации",
            )
            days_ago = 3 + (offset // 2) * 3 + (0 if unplanned else 1)
        elif 568 <= i < 580:
            row.update(
                equipment_index=2,
                fault_index=5,
                material_index=0,
                quantity=20 if i < 574 else 4,
                description="СИНТЕТИЧЕСКИЙ ремонт уплотнения насоса",
                work_done="Заменено уплотнение, залито масло, проверена герметичность",
            )
            days_ago = 2 + (i - 568) * 2
        elif i >= 580:
            row.update(
                equipment_index=3,
                kind="unplanned",
                fault_index=i % 20,
                description="СИНТЕТИЧЕСКИЙ внеплановый ремонт мельницы",
                work_done="Устранена неисправность, проверен привод мельницы",
            )
            days_ago = 40 + (i - 580) * 2 if i < 584 else 2 + i - 584
        row["issued_at"] = reference - timedelta(days=days_ago, minutes=rng.randint(0, 59))
        rows.append(row)
    return rows
