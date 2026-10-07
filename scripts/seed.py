"""Demo dataset per case §8 (idempotent, dev/test only, blocked in production).

4 areas, 25 equipment, 3 crews, 2 masters, 1 manager, 15 workers,
20 fault codes, 40 materials, 500+ work orders over 90 days.
Planted patterns for the demo:
  1. Conveyor K-3 (equipment #1): repeated М-02 bearing failures (3x rate).
  2. Crusher KMD-1750 (#2): unplanned jobs right after planned maintenance.
  3. Pump GrAT-250 (#3): oil over-consumption vs typical usage.
  4. Unplanned growth on Mill equipment over the last 30 days.
"""
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import close_pool, get_pool
from app.security import hash_pin

NOW = datetime.now(timezone.utc)
rng = random.Random(42)


async def ensure_user(pool, login, pin, full_name, role, crew_id=None, area_ids=()):
    row = await pool.fetchrow(
        """INSERT INTO employees(login,pin_hash,full_name,role,crew_id,shift,current_status)
           VALUES ($1,$2,$3,$4::user_role,$5::uuid,'1',CASE WHEN $4='worker' THEN 'free'::employee_status ELSE 'off'::employee_status END)
           ON CONFLICT (login) DO UPDATE SET full_name=EXCLUDED.full_name, role=EXCLUDED.role,
             crew_id=EXCLUDED.crew_id, enabled=true, deleted_at=NULL RETURNING id""",
        login, hash_pin(pin), full_name, role, crew_id)
    uid = str(row["id"])
    await pool.execute("DELETE FROM employee_areas WHERE employee_id=$1::uuid", uid)
    for a in area_ids:
        await pool.execute("INSERT INTO employee_areas(employee_id,area_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", uid, a)
    return uid


FAULTS = [
    ("М-02", "М", "Подшипник", 60), ("М-03", "М", "Муфта", 45), ("М-04", "М", "Вал", 90),
    ("Э-01", "Э", "Электродвигатель", 60), ("Э-02", "Э", "Кабель", 40),
    ("Г-05", "Г", "Утечка масла", 60), ("Г-06", "Г", "Насос", 90),
    ("П-01", "П", "Пневмоклапан", 30), ("С-01", "С", "Смазка", 20), ("С-02", "С", "Масляный фильтр", 30),
    ("М-05", "М", "Редуктор", 120), ("М-06", "М", "Ролик конвейера", 40),
    ("Э-03", "Э", "Датчик", 30), ("Г-07", "Г", "Гидроцилиндр", 90), ("П-02", "П", "Пневмолиния", 45),
    ("С-03", "С", "Масло И-40", 30), ("М-07", "М", "Соосность привода", 60),
    ("Э-04", "Э", "Пускатель", 50), ("Г-08", "Г", "Манжета", 45), ("М-08", "М", "Защитный кожух", 35),
]

MATERIALS = [
    ("Масло И-40", "л", 4), ("Подшипник 6312", "шт", 1), ("Подшипник 22218", "шт", 1),
    ("Манжета 80×100", "шт", 1), ("Смазка Литол-24", "кг", 0.5), ("Ремень клиновой", "шт", 1),
    ("Болт М16", "шт", 8), ("Гайка М16", "шт", 8), ("Кабель ВВГ", "м", 5),
    ("Фильтр масляный", "шт", 1), ("Уплотнительное кольцо", "шт", 2),
    ("Электродвигатель 15 кВт", "шт", 1), ("Контактор КМИ", "шт", 1),
    ("Гидравлическое масло", "л", 8), ("Прокладка паронитовая", "шт", 2),
    ("Подшипник 6310", "шт", 1), ("Подшипник 22220", "шт", 1), ("Сальник 60×80", "шт", 1),
    ("Смазка ЦИАТИМ-201", "кг", 0.4), ("Ремень поликлиновой", "шт", 1), ("Болт М12", "шт", 12),
    ("Гайка М12", "шт", 12), ("Шайба М16", "шт", 16), ("Кабель КГ 4×25", "м", 8),
    ("Фильтр воздушный", "шт", 1), ("Кольцо стопорное", "шт", 4), ("Муфта МУВП", "шт", 1),
    ("Реле тепловое", "шт", 1), ("Масло гидравлическое ИГП-46", "л", 10),
    ("Прокладка резиновая", "шт", 4), ("Электрод 4 мм", "кг", 2), ("Круг отрезной 230", "шт", 2),
    ("Лента ФУМ", "шт", 2), ("Герметик силиконовый", "шт", 1), ("Втулка бронзовая", "шт", 2),
    ("Шпонка 16×10", "шт", 2), ("Цепь приводная", "шт", 1), ("Звёздочка Z-19", "шт", 1),
    ("Фильтр топливный", "шт", 1), ("Рукав РВД 16 МПа", "м", 3),
]

REASONS = [
    ("reject", "no_materials", "Нет материалов", False), ("reject", "no_access", "Нет допуска", False),
    ("reject", "busy_emergency", "Занят аварийным нарядом", False), ("reject", "no_valid_reason", "Нет уважительной причины", True),
    ("pause", "waiting_spare", "Ждёт запчасти", False), ("pause", "waiting_stop", "Ждёт остановки оборудования", False),
    ("pause", "other", "Иная причина", False), ("cancel", "duplicate", "Дублирующий наряд", False),
    ("cancel", "scope_changed", "Работы больше не требуются", False),
    ("downtime", "process_stop", "Остановка технологической линии", False),
]


async def main() -> None:
    if settings.NODE_ENV == "production":
        raise RuntimeError("Demo seed data is disabled in production")
    pool = await get_pool()

    area_ids = []
    for name, code in [("Дробление", "ДРБ"), ("Обогащение", "ОБГ"),
                       ("Ремонтно-механический цех", "РМЦ"), ("Транспортировка", "ТРС")]:
        row = await pool.fetchrow(
            "INSERT INTO areas(name,code) VALUES ($1,$2) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name, deleted_at=NULL RETURNING id",
            name, code)
        area_ids.append(str(row["id"]))

    crew_ids = []
    for i in range(3):
        row = await pool.fetchrow(
            "INSERT INTO crews(name,area_id) VALUES ($1,$2::uuid) ON CONFLICT (area_id,name) DO UPDATE SET deleted_at=NULL RETURNING id",
            f"Бригада {i + 1}", area_ids[i])
        crew_ids.append(str(row["id"]))

    admin = await ensure_user(pool, settings.SEED_ADMIN_LOGIN, settings.SEED_ADMIN_PIN, "Администратор системы", "admin", None, area_ids)
    await ensure_user(pool, "manager1", "2222", "Сериков М.", "manager", None, area_ids)
    masters = [await ensure_user(pool, "master1", "3333", "Ахметов Е.", "master", None, area_ids),
               await ensure_user(pool, "master2", "4444", "Ким А.", "master", None, area_ids)]
    workers = []
    last = ["Петров", "Ахметов", "Ким", "Иванов", "Садыков", "Беков", "Омаров", "Нуртаев",
            "Сериков", "Касымов", "Жумабаев", "Тлеуов", "Мусин", "Алимов", "Досов"]
    specs = ["слесарь", "электрик", "сварщик", "КИПиА"]
    for i in range(15):
        wid = await ensure_user(pool, f"worker{i + 1:02d}", str(1001 + i),
                                f"{last[i]} {chr(65 + i)}.", "worker", crew_ids[i // 5], [area_ids[i // 5]])
        await pool.execute("UPDATE employees SET specialty=$2, grade=$3, current_status='free' WHERE id=$1::uuid",
                           wid, specs[i % 4], (i % 5) + 3)
        workers.append(wid)

    fault_ids = []
    for code, group, name, minutes in FAULTS:
        row = await pool.fetchrow(
            "INSERT INTO fault_codes(code,fault_group,name,default_norm_minutes) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name, deleted_at=NULL RETURNING id",
            code, group, name, minutes)
        fault_ids.append(str(row["id"]))

    material_ids = []
    for name, unit, typical in MATERIALS:
        row = await pool.fetchrow(
            "INSERT INTO materials(name,unit,typical_usage_per_order) VALUES ($1,$2,$3) "
            "ON CONFLICT(name) DO NOTHING RETURNING id", name, unit, typical)
        if row:
            material_ids.append(str(row["id"]))
        else:
            ex = await pool.fetchrow("SELECT id FROM materials WHERE name=$1", name)
            material_ids.append(str(ex["id"]))

    for kind, code, name, dis in REASONS:
        await pool.execute(
            "INSERT INTO reasons(kind,code,name,disrespectful) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT(kind,code) DO UPDATE SET name=EXCLUDED.name, deleted_at=NULL",
            kind, code, name, dis)

    equip_rows = []
    named = [("Конвейер К-3", "конвейер", 0, "A"), ("Дробилка КМД-1750", "дробилка", 0, "A"), ("Насос ГрАТ-250", "насос", 1, "A")]
    for i in range(25):
        ch = named[i] if i < len(named) else None
        t5 = ["конвейер", "дробилка", "насос", "мельница", "компрессор"]
        m5 = ["КМД-1200", "К-2", "ГрАТ-250", "МШЦ-3.2", "ВК-15"]
        name = ch[0] if ch else f"{['Конвейер', 'Дробилка', 'Насос', 'Мельница', 'Компрессор'][i % 5]} {m5[i % 5]}-{i + 1}"
        typ = ch[1] if ch else t5[i % 5]
        ai = ch[2] if ch else i % 4
        crit = ch[3] if ch else ("A" if i % 4 == 0 else ("B" if i % 3 == 0 else "C"))
        row = await pool.fetchrow(
            "INSERT INTO equipment(name,inventory_no,area_id,type,criticality,qr_token) VALUES ($1,$2,$3::uuid,$4,$5,$6) "
            "ON CONFLICT(inventory_no) DO UPDATE SET name=EXCLUDED.name, deleted_at=NULL RETURNING id, area_id",
            name, f"EQ-{i + 1:04d}", area_ids[ai], typ, crit, f"NAR-{i + 1:05d}")
        equip_rows.append({"id": str(row["id"]), "area": str(row["area_id"]), "type": typ})

    for fid in fault_ids:
        for typ in ["конвейер", "дробилка", "насос", "мельница", "компрессор"]:
            await pool.execute(
                "INSERT INTO time_norms(fault_code_id,equipment_type,minutes) VALUES ($1::uuid,$2,60) "
                "ON CONFLICT(fault_code_id,equipment_type) DO NOTHING", fid, typ)

    async with pool.acquire() as conn:
        async with conn.transaction():
            cnt = await conn.fetchval("SELECT count(*)::int FROM work_orders")
            num = int(await conn.fetchval("SELECT value FROM work_order_counters WHERE key='number' FOR UPDATE"))
            for idx in range(int(cnt), 500):
                n = idx + 1
                ei = 0 if n % 4 == 0 else (1 if n % 4 == 1 else (2 if n % 4 == 2 else (n % 22) + 3))
                eq = equip_rows[ei % len(equip_rows)]
                worker = workers[(n * 7) % len(workers)]
                master = masters[n % len(masters)]
                # Planted pattern 1: conveyor K-3 fails on bearing М-02 most of the time
                fi = 0 if (ei == 0 and n % 7 < 5) else (10 if ei == 1 else (15 if ei == 2 else n % len(fault_ids)))
                kind = "unplanned" if n % 4 == 0 else "planned"
                status = "ai_review" if n % 31 == 0 else ("in_progress" if n % 23 == 0 else ("paused" if n % 29 == 0 else "closed"))
                priority = "critical" if (kind == "unplanned" and n % 16 == 0) else ("planned" if kind == "planned" else ("high" if n % 3 == 0 else "normal"))
                issued = NOW - timedelta(days=n % 90, minutes=n % 1400)
                due = issued + timedelta(minutes=45 if priority == "critical" else 180)
                # Planted pattern 5: crew 3 (workers[10:15]) is slower, worker15 scores low
                slow = worker in workers[10:]
                low = worker == workers[-1]
                work_min = 60 + n % 150 + (180 if slow else 0) + (120 if low else 0)
                done = issued + timedelta(minutes=work_min) if status in ("closed", "ai_review") else None
                num += 1
                row = await conn.fetchrow(
                    """INSERT INTO work_orders(number,kind,description,area_id,equipment_id,assignee_id,master_id,priority,
                       due_at,norm_minutes,status,accepted_at,started_at,done_at,closed_at,fault_code_id,work_done_text,comment)
                       VALUES ($1,$2,$3,$4::uuid,$5::uuid,$6::uuid,$7::uuid,$8,$9,60,$10,$11,$12,$13,$14,$15::uuid,$16,$16) RETURNING id""",
                    num, kind, f"{['Шум и вибрация', 'Течь масла', 'Перегрев подшипника', 'Осмотр привода'][n % 4]} — оборудование {ei + 1}",
                    eq["area"], eq["id"], worker, master, priority, due, status, issued, issued, done,
                    done if status == "closed" else None, fault_ids[fi % len(fault_ids)], "Выполнены работы по устранению неисправности")
                oid = str(row["id"])
                await conn.execute("INSERT INTO work_order_events(work_order_id,actor_id,action,payload) VALUES ($1::uuid,$2::uuid,'issued',$3::jsonb)",
                                   oid, master, json.dumps({"seed": True}))
                if status in ("closed", "ai_review"):
                    score = (45 + (n % 20)) if worker == workers[-1] else (65 + (n % 36))
                    await conn.execute(
                        """INSERT INTO ai_reviews(work_order_id,attempt,verdict,score,confidence,checks,explanation,
                           strengths,improvements,model,input_hash) VALUES ($1::uuid,1,'accepted',$2,0.78,'{}'::jsonb,
                           'Демо-оценка для проверки интерфейса.','[]'::jsonb,'[]'::jsonb,'seed-v1',$3)""",
                        oid, score, f"seed-{n}")
                    if n % 7 == 0:
                        mat = material_ids[ei % len(material_ids)]
                        unit = await conn.fetchval("SELECT unit FROM materials WHERE id=$1::uuid", mat)
                        await conn.execute(
                            "INSERT INTO material_writeoffs(work_order_id,material_id,quantity,unit,created_by) VALUES ($1::uuid,$2::uuid,$3,$4,$5::uuid)",
                            oid, mat, 10 if (ei == 2 and n % 35 == 0) else 1, unit, worker)
            await conn.execute("UPDATE work_order_counters SET value=GREATEST(value,$1) WHERE key='number'", num)

    insights = [
        ("repeat_failure", "critical", equip_rows[0], "Конвейер К-3: повторные отказы по подшипнику",
         "Проверьте соосность привода и включите замену подшипников в план ППР", 0.88),
        ("post_ppr_failure", "warning", equip_rows[1], "Дробилка КМД-1750: внеплановые работы после ППР",
         "Проверьте качество центровки и назначьте контрольный осмотр через 10 дней", 0.76),
        ("material_anomaly", "warning", equip_rows[2], "Насос ГрАТ-250: расход масла выше типового",
         "Проверьте уплотнение вала и утечки в гидролинии", 0.81),
        ("unplanned_growth", "warning", equip_rows[3], "Мельница: рост внеплановых нарядов за 30 дней",
         "Возможен отказ в ближайшие 2 недели — запланируйте диагностику", 0.64),
    ]
    for typ, sev, eq, head, rec, conf in insights:
        await pool.execute(
            """INSERT INTO insights(type,severity,scope,period_from,period_to,headline,evidence,recommendation,confidence)
               SELECT $1,$2,$3::jsonb,current_date-30,current_date,$4,$5::jsonb,$6,$7
               WHERE NOT EXISTS (SELECT 1 FROM insights WHERE headline=$4)""",
            typ, sev, json.dumps({"equipment_id": eq["id"], "area_id": eq["area"]}), head,
            json.dumps([{"metric": "sample_count", "value": 7, "baseline": 2.3}]), rec, conf)

    print(f"Seed OK: areas=4, equipment=25, employees=19, faults=20, materials={len(material_ids)}, orders>=500")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
