"""Demo dataset per case §8 (idempotent, dev/test only, blocked in production).

4 areas, 25 equipment, 3 crews, 2 masters, 1 manager, 15 workers,
20 fault codes, 40 materials, 603 work orders over 96 days (3 with fresh due +12–14d for demo).
Planted patterns for the demo:
  1. Conveyor K-3 (equipment #1): repeated М-02 bearing failures (12 failures in 30 days).
  2. Crusher KMD-1750 (#2): unplanned jobs right after planned maintenance.
  3. Pump GrAT-250 (#3): oil over-consumption vs typical usage.
  4. Unplanned growth on Mill equipment over the last 30 days.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.db import close_pool, get_pool
from app.security import hash_pin


def reference_time() -> datetime:
    raw = os.environ.get("SEED_REFERENCE_TIME")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("SEED_REFERENCE_TIME must include a timezone")
    return value.astimezone(UTC)


async def ensure_user(pool, login, pin, full_name, role, crew_id=None, area_ids=()):
    row = await pool.fetchrow(
        """INSERT INTO employees(login,pin_hash,full_name,role,crew_id,shift,current_status)
           VALUES ($1,$2,$3,$4::user_role,$5::uuid,'1',CASE WHEN $4='worker' THEN 'free'::employee_status ELSE 'off'::employee_status END)
           ON CONFLICT (login) DO UPDATE SET pin_hash=EXCLUDED.pin_hash,
             full_name=EXCLUDED.full_name, role=EXCLUDED.role,
             crew_id=EXCLUDED.crew_id, enabled=true, deleted_at=NULL,
             failed_pin_attempts=0, locked_at=NULL, auth_version=employees.auth_version+1
           RETURNING id""",
        login,
        hash_pin(pin),
        full_name,
        role,
        crew_id,
    )
    uid = str(row["id"])
    await pool.execute("DELETE FROM employee_areas WHERE employee_id=$1::uuid", uid)
    for a in area_ids:
        await pool.execute(
            "INSERT INTO employee_areas(employee_id,area_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING",
            uid,
            a,
        )
    return uid


FAULTS = [
    ("М-02", "М", "Подшипник", "Мойынтірек", 60),
    ("М-03", "М", "Муфта", "Муфта", 45),
    ("М-04", "М", "Вал", "Білік", 90),
    ("Э-01", "Э", "Электродвигатель", "Электрқозғалтқыш", 60),
    ("Э-02", "Э", "Кабель", "Кабель", 40),
    ("Г-05", "Г", "Утечка масла", "Майдың ағуы", 60),
    ("Г-06", "Г", "Насос", "Сорғы", 90),
    ("П-01", "П", "Пневмоклапан", "Пневмоклапан", 30),
    ("С-01", "С", "Смазка", "Жағармай", 20),
    ("С-02", "С", "Масляный фильтр", "Май сүзгісі", 30),
    ("М-05", "М", "Редуктор", "Редуктор", 120),
    ("М-06", "М", "Ролик конвейера", "Конвейер ролигі", 40),
    ("Э-03", "Э", "Датчик", "Датчик", 30),
    ("Г-07", "Г", "Гидроцилиндр", "Гидроцилиндр", 90),
    ("П-02", "П", "Пневмолиния", "Пневможелі", 45),
    ("С-03", "С", "Масло И-40", "И-40 майы", 30),
    ("М-07", "М", "Соосность привода", "Осьтестік", 60),
    ("Э-04", "Э", "Пускатель", "Іске қосқыш", 50),
    ("Г-08", "Г", "Манжета", "Манжета", 45),
    ("М-08", "М", "Защитный кожух", "Қорғаныс қаптамасы", 35),
]

MATERIALS = [
    ("Масло И-40", "И-40 майы", "л", 4),
    ("Подшипник 6312", "6312 мойынтірегі", "шт", 1),
    ("Подшипник 22218", "22218 мойынтірегі", "шт", 1),
    ("Манжета 80×100", "80×100 манжетасы", "шт", 1),
    ("Смазка Литол-24", "Литол-24 жағармайы", "кг", 0.5),
    ("Ремень клиновой", "Сына белдік", "шт", 1),
    ("Болт М16", "М16 болты", "шт", 8),
    ("Гайка М16", "М16 сомыны", "шт", 8),
    ("Кабель ВВГ", "ВВГ кабелі", "м", 5),
    ("Фильтр масляный", "Май сүзгісі", "шт", 1),
    ("Уплотнительное кольцо", "Тығыздағыш сақина", "шт", 2),
    ("Электродвигатель 15 кВт", "15 кВт электрқозғалтқыш", "шт", 1),
    ("Контактор КМИ", "КМИ контакторы", "шт", 1),
    ("Гидравлическое масло", "Гидравликалық май", "л", 8),
    ("Прокладка паронитовая", "Паронит төсемесі", "шт", 2),
    ("Подшипник 6310", "6310 мойынтірегі", "шт", 1),
    ("Подшипник 22220", "22220 мойынтірегі", "шт", 1),
    ("Сальник 60×80", "60×80 сальнигі", "шт", 1),
    ("Смазка ЦИАТИМ-201", "ЦИАТИМ-201 жағармайы", "кг", 0.4),
    ("Ремень поликлиновой", "Көпсыналы белдік", "шт", 1),
    ("Болт М12", "М12 болты", "шт", 12),
    ("Гайка М12", "М12 сомыны", "шт", 12),
    ("Шайба М16", "М16 шайбасы", "шт", 16),
    ("Кабель КГ 4×25", "КГ 4×25 кабелі", "м", 8),
    ("Фильтр воздушный", "Ауа сүзгісі", "шт", 1),
    ("Кольцо стопорное", "Тоқтатқыш сақина", "шт", 4),
    ("Муфта МУВП", "МУВП муфтасы", "шт", 1),
    ("Реле тепловое", "Жылу релесі", "шт", 1),
    ("Масло гидравлическое ИГП-46", "ИГП-46 гидравликалық майы", "л", 10),
    ("Прокладка резиновая", "Резеңке төсеме", "шт", 4),
    ("Электрод 4 мм", "4 мм электрод", "кг", 2),
    ("Круг отрезной 230", "230 кескіш диск", "шт", 2),
    ("Лента ФУМ", "ФУМ таспасы", "шт", 2),
    ("Герметик силиконовый", "Силикон герметигі", "шт", 1),
    ("Втулка бронзовая", "Қола төлке", "шт", 2),
    ("Шпонка 16×10", "16×10 шпонка", "шт", 2),
    ("Цепь приводная", "Жетек шынжыры", "шт", 1),
    ("Звёздочка Z-19", "Z-19 жұлдызшасы", "шт", 1),
    ("Фильтр топливный", "Отын сүзгісі", "шт", 1),
    ("Рукав РВД 16 МПа", "16 МПа РВД жеңі", "м", 3),
]

REASONS = [
    ("reject", "no_materials", "Нет материалов", "Материалдар жоқ", False),
    ("reject", "no_access", "Нет допуска", "Рұқсат жоқ", False),
    ("reject", "busy_emergency", "Занят аварийным нарядом", "Апатты нарядпен айналысуда", False),
    ("reject", "no_valid_reason", "Нет уважительной причины", "Дәлелді себеп жоқ", True),
    ("pause", "waiting_spare", "Ждёт запчасти", "Қосалқы бөлшек күтуде", False),
    ("pause", "waiting_stop", "Ждёт остановки оборудования", "Жабдықты тоқтатуды күтуде", False),
    ("pause", "other", "Иная причина", "Басқа себеп", False),
    ("cancel", "duplicate", "Дублирующий наряд", "Қайталанатын наряд", False),
    ("cancel", "scope_changed", "Работы больше не требуются", "Жұмыстар енді қажет емес", False),
    (
        "downtime",
        "process_stop",
        "Остановка технологической линии",
        "Технологиялық желінің тоқтауы",
        False,
    ),
]

AREAS = [
    ("Дробление", "ДРБ", "Ұсақтау"),
    ("Обогащение", "ОБГ", "Байыту"),
    ("Ремонтно-механический цех", "РМЦ", "Жөндеу-механикалық цех"),
    ("Транспортировка", "ТРС", "Тасымалдау"),
]

EQ_TYPE_KK = {
    "конвейер": "конвейер",
    "дробилка": "ұсатқыш",
    "насос": "сорғы",
    "мельница": "диірмен",
    "компрессор": "компрессор",
}
EQ_TYPE_KK_CAP = {
    "конвейер": "Конвейер",
    "дробилка": "Ұсатқыш",
    "насос": "Сорғы",
    "мельница": "Диірмен",
    "компрессор": "Компрессор",
}


async def main() -> None:
    if settings.NODE_ENV == "production":
        raise RuntimeError("Demo seed data is disabled in production")
    pool = await get_pool()
    count = await pool.fetchval("SELECT count(*) FROM work_orders")
    if count:
        marker = await pool.fetchval("SELECT value FROM settings WHERE key='demo_dataset_v2'")
        await close_pool()
        if marker:
            print(
                f"Seed OK (unchanged): {count} existing orders; no accounts or history overwritten"
            )
            return
        raise RuntimeError(
            "Non-empty database: use a NEW demo database; existing data will not be replaced"
        )
    existing_catalog = await pool.fetchval(
        """SELECT EXISTS(SELECT 1 FROM employees) OR EXISTS(SELECT 1 FROM areas)
             OR EXISTS(SELECT 1 FROM equipment) OR EXISTS(SELECT 1 FROM crews)
             OR EXISTS(SELECT 1 FROM materials) OR EXISTS(SELECT 1 FROM fault_codes)"""
    )
    if existing_catalog:
        await close_pool()
        raise RuntimeError(
            "Existing accounts or catalogs: use a NEW demo database; no data will be replaced"
        )
    now = reference_time()

    area_ids = []
    for name, code, name_kk in AREAS:
        row = await pool.fetchrow(
            "INSERT INTO areas(name,code,name_kk) VALUES ($1,$2,$3) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name, name_kk=EXCLUDED.name_kk, deleted_at=NULL RETURNING id",
            name,
            code,
            name_kk,
        )
        area_ids.append(str(row["id"]))

    crew_ids = []
    for i in range(3):
        row = await pool.fetchrow(
            "INSERT INTO crews(name,name_kk,area_id) VALUES ($1,$2,$3::uuid) ON CONFLICT (area_id,name) DO UPDATE SET name_kk=EXCLUDED.name_kk, deleted_at=NULL RETURNING id",
            f"Бригада {i + 1}",
            f"{i + 1}-бригада",
            area_ids[i],
        )
        crew_ids.append(str(row["id"]))

    await ensure_user(
        pool,
        settings.SEED_ADMIN_LOGIN,
        settings.SEED_ADMIN_PIN,
        "ДЕМО Администратор",
        "admin",
        None,
        area_ids,
    )
    await ensure_user(pool, "manager1", "2222", "ДЕМО Руководитель", "manager", None, area_ids)
    masters = [
        await ensure_user(pool, "master1", "3333", "ДЕМО Мастер 1", "master", None, area_ids),
        await ensure_user(pool, "master2", "4444", "ДЕМО Мастер 2", "master", None, area_ids),
    ]
    workers = []
    specs = ["слесарь", "электрик", "сварщик", "КИПиА"]
    for i in range(15):
        wid = await ensure_user(
            pool,
            f"worker{i + 1:02d}",
            str(1001 + i),
            f"ДЕМО Исполнитель {i + 1:02d}",
            "worker",
            crew_ids[i // 5],
            [area_ids[i // 5]],
        )
        await pool.execute(
            "UPDATE employees SET specialty=$2, grade=$3, shift=$4, current_status='free' WHERE id=$1::uuid",
            wid,
            specs[i % 4],
            (i % 5) + 3,
            ["1", "2", "night"][i % 3],
        )
        workers.append(wid)

    fault_ids = []
    for code, group, name, name_kk, minutes in FAULTS:
        row = await pool.fetchrow(
            "INSERT INTO fault_codes(code,fault_group,name,name_kk,default_norm_minutes) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name, name_kk=EXCLUDED.name_kk, deleted_at=NULL RETURNING id",
            code,
            group,
            name,
            name_kk,
            minutes,
        )
        fault_ids.append(str(row["id"]))

    material_ids = []
    for name, name_kk, unit, typical in MATERIALS:
        row = await pool.fetchrow(
            "INSERT INTO materials(name,name_kk,unit,typical_usage_per_order) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT(name) DO UPDATE SET name_kk=EXCLUDED.name_kk, deleted_at=NULL RETURNING id",
            name,
            name_kk,
            unit,
            typical,
        )
        if row:
            material_ids.append(str(row["id"]))
        else:
            ex = await pool.fetchrow("SELECT id FROM materials WHERE name=$1", name)
            material_ids.append(str(ex["id"]))

    for kind, code, name, name_kk, dis in REASONS:
        await pool.execute(
            "INSERT INTO reasons(kind,code,name,name_kk,disrespectful) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT(kind,code) DO UPDATE SET name=EXCLUDED.name, name_kk=EXCLUDED.name_kk, deleted_at=NULL",
            kind,
            code,
            name,
            name_kk,
            dis,
        )

    equip_rows = []
    named = [
        ("Конвейер К-3", "К-3 конвейері", "конвейер", 0, "A"),
        ("Дробилка КМД-1750", "КМД-1750 ұсатқышы", "дробилка", 0, "A"),
        ("Насос ГрАТ-250", "ГрАТ-250 сорғысы", "насос", 1, "A"),
    ]
    for i in range(25):
        ch = named[i] if i < len(named) else None
        t5 = ["конвейер", "дробилка", "насос", "мельница", "компрессор"]
        m5 = ["КМД-1200", "К-2", "ГрАТ-250", "МШЦ-3.2", "ВК-15"]
        name = (
            ch[0]
            if ch
            else f"{['Конвейер', 'Дробилка', 'Насос', 'Мельница', 'Компрессор'][i % 5]} {m5[i % 5]}-{i + 1}"
        )
        typ = ch[2] if ch else t5[i % 5]
        name_kk = ch[1] if ch else f"{m5[i % 5]}-{i + 1} {EQ_TYPE_KK[typ]}"
        ai = ch[3] if ch else i % 4
        crit = ch[4] if ch else ("A" if i % 4 == 0 else ("B" if i % 3 == 0 else "C"))
        row = await pool.fetchrow(
            "INSERT INTO equipment(name,name_kk,inventory_no,area_id,type,criticality,qr_token) VALUES ($1,$2,$3,$4::uuid,$5,$6,$7) "
            "ON CONFLICT(inventory_no) DO UPDATE SET name=EXCLUDED.name, name_kk=EXCLUDED.name_kk, deleted_at=NULL RETURNING id, area_id",
            name,
            name_kk,
            f"EQ-{i + 1:04d}",
            area_ids[ai],
            typ,
            crit,
            f"NAR-{i + 1:05d}",
        )
        equip_rows.append({"id": str(row["id"]), "area": str(row["area_id"]), "type": typ})

    for fid in fault_ids:
        for typ in ["конвейер", "дробилка", "насос", "мельница", "компрессор"]:
            await pool.execute(
                "INSERT INTO time_norms(fault_code_id,equipment_type,minutes) VALUES ($1::uuid,$2,60) "
                "ON CONFLICT(fault_code_id,equipment_type) DO NOTHING",
                fid,
                typ,
            )

    from app.services.insights import generate_insights
    from scripts.demo_dataset import history_specs

    async with pool.acquire() as conn:
        async with conn.transaction():
            num = int(
                await conn.fetchval(
                    "SELECT value FROM work_order_counters WHERE key='number' FOR UPDATE"
                )
            )
            for spec in history_specs(now):
                eq = equip_rows[spec["equipment_index"]]
                eligible = [i for i in range(15) if area_ids[i // 5] == eq["area"]]
                # Fourth-area work is assigned to a master-managed crew member;
                # membership is extended explicitly below, never silently cross-area.
                wi = eligible[spec["index"] % len(eligible)] if eligible else spec["index"] % 15
                worker, master = workers[wi], masters[spec["index"] % 2]
                await conn.execute(
                    "INSERT INTO employee_areas(employee_id,area_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING",
                    worker,
                    eq["area"],
                )
                issued = spec["issued_at"]
                started = issued + timedelta(minutes=10)
                done = started + timedelta(minutes=spec["duration_minutes"])
                num += 1
                row = await conn.fetchrow(
                    """INSERT INTO work_orders(number,kind,description,area_id,equipment_id,assignee_id,master_id,priority,
                       due_at,norm_minutes,status,issued_at,created_at,updated_at,accepted_at,started_at,done_at,closed_at,
                       fault_code_id,work_done_text,comment,equipment_stopped_at,equipment_restored_at)
                       VALUES ($1,$2,$3,$4::uuid,$5::uuid,$6::uuid,$7::uuid,'normal',
                       $8,60,'closed',$9,$9,$10,$11,$12,$10,$13,$14::uuid,$15,$16,$17,$18) RETURNING id""",
                    num,
                    spec["kind"],
                    spec["description"],
                    eq["area"],
                    eq["id"],
                    worker,
                    master,
                    issued + timedelta(minutes=120),
                    issued,
                    done,
                    issued + timedelta(minutes=5),
                    started,
                    done + timedelta(minutes=5),
                    fault_ids[spec["fault_index"]],
                    spec["work_done"],
                    "СИНТЕТИЧЕСКАЯ ИСТОРИЯ seed-v2; оценки заданы для проверки отчётов, не получены от модели",
                    issued if spec["kind"] == "unplanned" else None,
                    done if spec["kind"] == "unplanned" else None,
                )
                oid = str(row["id"])
                for action, when, actor in [
                    ("issued", issued, master),
                    ("accepted", issued + timedelta(minutes=5), worker),
                    ("started", started, worker),
                    ("completed", done, worker),
                    ("closed", done + timedelta(minutes=5), master),
                ]:
                    await conn.execute(
                        "INSERT INTO work_order_events(work_order_id,actor_id,action,at,payload) VALUES ($1::uuid,$2::uuid,$3,$4,$5::jsonb)",
                        oid,
                        actor,
                        action,
                        when,
                        json.dumps({"synthetic": True, "dataset": "seed-v2"}),
                    )
                await conn.execute(
                    """INSERT INTO ai_reviews(work_order_id,attempt,verdict,score,confidence,checks,explanation,
                       strengths,improvements,model,input_hash,created_at) VALUES ($1::uuid,1,'accepted_with_remarks',$2,0,
                       '{"synthetic":true}'::jsonb,'Синтетическая метка для проверки рейтинга, не результат AI.',
                       '[]'::jsonb,'[]'::jsonb,'seed-v2',$3,$4)""",
                    oid,
                    70 + spec["index"] % 26,
                    f"seed-v2-{spec['index']}",
                    done,
                )
                if spec.get("material_index") is not None:
                    mi = spec["material_index"]
                    await conn.execute(
                        """INSERT INTO material_writeoffs(work_order_id,material_id,quantity,unit,created_by,created_at)
                           VALUES ($1::uuid,$2::uuid,$3,$4,$5::uuid,$6)""",
                        oid,
                        material_ids[mi],
                        spec["quantity"],
                        MATERIALS[mi][2],
                        worker,
                        done,
                    )
            # Fresh, correctly timed work for three worker accounts. No duplicate
            # rerun and no destructive reset; use a new demo database to replay.
            for i in range(3):
                num += 1
                eq = equip_rows[i]
                await conn.execute(
                    "INSERT INTO employee_areas(employee_id,area_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING",
                    workers[i],
                    eq["area"],
                )
                row = await conn.fetchrow(
                    """INSERT INTO work_orders(number,kind,description,area_id,equipment_id,assignee_id,master_id,
                       priority,due_at,norm_minutes,status,issued_at,created_at)
                       VALUES ($1,'planned',$2,$3::uuid,$4::uuid,$5::uuid,$6::uuid,'normal',$7,60,'issued',$8,$8) RETURNING id""",
                    num,
                    f"Плановый осмотр для демо №{i + 1}",
                    eq["area"],
                    eq["id"],
                    workers[i],
                    masters[i % 2],
                    now + timedelta(days=12 + i),
                    now,
                )
                await conn.execute(
                    "INSERT INTO work_order_events(work_order_id,actor_id,action,at,payload) VALUES ($1,$2::uuid,'issued',$3,$4::jsonb)",
                    row["id"],
                    masters[i % 2],
                    now,
                    json.dumps({"synthetic": True}),
                )
            await conn.execute(
                "UPDATE work_order_counters SET value=GREATEST(value,$1) WHERE key='number'", num
            )
            await conn.execute(
                "INSERT INTO settings(key,value) VALUES ('demo_dataset_v2',$1::jsonb)",
                json.dumps(
                    {
                        "synthetic": True,
                        "seed": 42,
                        "reference_time": now.isoformat(),
                        "historical_orders": 600,
                    }
                ),
            )
        # Same production detector; no pre-authored findings are inserted.
        detected = await generate_insights(conn)
    print(
        f"Seed OK: synthetic=true, reference={now.isoformat()}, areas=4, equipment=25, employees=19, faults=20, materials=40, orders=603, computed_insights={len(detected)}"
    )
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
