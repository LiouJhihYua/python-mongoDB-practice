"""ERP 介接測試。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import ValidationError
from app.models.base import utcnow
from app.services import erp_service, lot_service
from tests.helpers import advance_to, make_lot, run_step

pytestmark = pytest.mark.asyncio

CUSTOMER_DOC = {
    "doc_type": "CUSTOMER",
    "external_id": "ERP-CUST-001",
    "payload": {"code": "NVDA", "name": "輝達", "contact": "Jensen", "email": "a@b.c"},
    "source": "REST",
}


# ── 收單與冪等 ──────────────────────────────────────────────
async def test_receive_is_idempotent(factory):
    db = factory
    first = await erp_service.receive(db, CUSTOMER_DOC, "planner01")
    second = await erp_service.receive(db, CUSTOMER_DOC, "planner01")
    assert first["duplicate"] is False and first["status"] == "PENDING"
    assert second["duplicate"] is True and second["id"] == first["id"]
    assert await db.fetchval("SELECT count(*) FROM erp_inbound") == 1


async def test_receive_batch_counts_duplicates(factory):
    db = factory
    docs = [
        CUSTOMER_DOC,
        {**CUSTOMER_DOC, "external_id": "ERP-CUST-002",
         "payload": {"code": "AMD", "name": "超微"}},
        CUSTOMER_DOC,
    ]
    result = await erp_service.receive_batch(db, docs, "planner01")
    assert result["received"] == 3 and result["accepted"] == 2 and result["duplicates"] == 1


# ── 套用到 MES ──────────────────────────────────────────────
async def test_process_creates_customer(factory):
    db = factory
    await erp_service.receive(db, CUSTOMER_DOC, "planner01")
    result = await erp_service.process_pending(db, None, 50, "planner01")
    assert result["processed"] == 1 and result["failed"] == 0

    customer = await db.fetchrow("SELECT * FROM customers WHERE code = 'NVDA'")
    assert customer["name"] == "輝達"
    row = await db.fetchrow("SELECT * FROM erp_inbound WHERE external_id = 'ERP-CUST-001'")
    assert row["status"] == "PROCESSED" and row["result"]["key"] == "NVDA"


async def test_process_upserts_existing_master(factory):
    db = factory
    await erp_service.receive(
        db,
        {"doc_type": "CUSTOMER", "external_id": "E1", "payload": {"code": "MTK", "name": "聯發科新名"}},
        "planner01",
    )
    await erp_service.process_pending(db, None, 50, "planner01")
    customer = await db.fetchrow("SELECT * FROM customers WHERE code = 'MTK'")
    assert customer["name"] == "聯發科新名"


async def test_process_creates_work_order(factory):
    db = factory
    due = (utcnow() + timedelta(days=10)).isoformat()
    await erp_service.receive(
        db,
        {
            "doc_type": "WORK_ORDER", "external_id": "SO-9001",
            "payload": {"device_id": "TEST-QFN48", "plan_qty": "40", "due_date": due,
                        "priority": "2", "customer_po": "PO-9001"},
        },
        "planner01",
    )
    result = await erp_service.process_pending(db, "WORK_ORDER", 50, "planner01")
    assert result["processed"] == 1

    wo_no = result["details"][0]["wo_no"]
    wo = await db.fetchrow("SELECT * FROM work_orders WHERE wo_no = $1", wo_no)
    assert wo["plan_qty"] == 40 and wo["customer_po"] == "PO-9001" and wo["priority"] == 2


async def test_process_records_failure_and_retry(factory):
    db = factory
    await erp_service.receive(
        db,
        {"doc_type": "WORK_ORDER", "external_id": "SO-BAD",
         "payload": {"device_id": "NO-SUCH-DEVICE", "plan_qty": "10",
                     "due_date": utcnow().isoformat()}},
        "planner01",
    )
    result = await erp_service.process_pending(db, None, 50, "planner01")
    assert result["failed"] == 1
    row = await db.fetchrow("SELECT * FROM erp_inbound WHERE external_id = 'SO-BAD'")
    assert row["status"] == "FAILED" and "產品料號不存在" in row["error"] and row["attempts"] == 1

    await erp_service.retry_inbound(db, row["id"], "planner01")
    again = await db.fetchrow("SELECT * FROM erp_inbound WHERE id = $1", row["id"])
    assert again["status"] == "PENDING" and again["attempts"] == 0


async def test_process_rejects_unknown_doc_type(factory):
    db = factory
    await db.execute(
        "INSERT INTO erp_inbound (external_id, doc_type, payload, status) VALUES ($1,$2,$3,$4)",
        "X-1", "UNKNOWN", {}, "PENDING",
    )
    result = await erp_service.process_pending(db, None, 50, "planner01")
    assert result["failed"] == 1
    row = await db.fetchrow("SELECT * FROM erp_inbound WHERE external_id = 'X-1'")
    assert "不支援的單據類型" in row["error"]


async def test_process_stops_after_max_attempts(factory):
    db = factory
    await db.execute(
        "INSERT INTO erp_inbound (external_id, doc_type, payload, status, attempts) "
        "VALUES ($1,$2,$3,$4,$5)",
        "X-2", "CUSTOMER", {}, "PENDING", erp_service.MAX_ATTEMPTS,
    )
    result = await erp_service.process_pending(db, None, 50, "planner01")
    assert result["picked"] == 0


async def test_missing_required_field_is_reported(factory):
    db = factory
    await erp_service.receive(
        db, {"doc_type": "CUSTOMER", "external_id": "E9", "payload": {"code": "ZZ"}}, "planner01"
    )
    await erp_service.process_pending(db, None, 50, "planner01")
    row = await db.fetchrow("SELECT * FROM erp_inbound WHERE external_id = 'E9'")
    assert "缺少必要欄位：name" in row["error"]


# ── CSV 通道 ────────────────────────────────────────────────
def test_parse_csv_requires_key_column():
    with pytest.raises(ValidationError, match="缺少冪等鍵欄位"):
        erp_service.parse_csv("code,name\nA,B\n")


def test_parse_csv_rejects_blank_key():
    with pytest.raises(ValidationError, match="第 2 行"):
        erp_service.parse_csv("external_id,code\n,X\n")


async def test_import_csv_then_process(factory):
    db = factory
    content = "external_id,code,name\nC-1,TSMC,台積電\nC-2,ASE,日月光\n"
    result = await erp_service.import_csv(db, "CUSTOMER", content, "external_id", "planner01")
    assert result["accepted"] == 2

    await erp_service.process_pending(db, None, 50, "planner01")
    codes = [r["code"] for r in await db.fetch("SELECT code FROM customers ORDER BY code")]
    assert "TSMC" in codes and "ASE" in codes
    row = await db.fetchrow("SELECT source FROM erp_inbound WHERE external_id = 'C-1'")
    assert row["source"] == "FILE"


# ── 上行 ────────────────────────────────────────────────────
async def _finish_lot(db, users) -> dict:
    lot = await make_lot(db, qty=2)
    await advance_to(db, users, lot["lot_id"], "FT")
    await run_step(db, users, lot["lot_id"], "FT-01")
    return await lot_service.get_lot(db, lot["lot_id"])


async def test_build_outbound_production_report(factory, users):
    db = factory
    lot = await _finish_lot(db, users)
    assert lot["status"] == "COMPLETED"

    result = await erp_service.build_outbound(db, {"hours": 24, "doc_types": []}, "planner01")
    assert result["created"]["PRODUCTION_REPORT"] == 1

    row = await db.fetchrow(
        "SELECT * FROM erp_outbound WHERE doc_type = 'PRODUCTION_REPORT' AND reference = $1",
        lot["lot_id"],
    )
    assert row["status"] == "PENDING"
    assert row["payload"]["good_qty"] == lot["qty"]
    assert row["payload"]["device_id"] == "TEST-QFN48"


async def test_build_outbound_shipment_and_scrap(factory, users):
    db = factory
    lot = await _finish_lot(db, users)
    await lot_service.ship_lots(
        db,
        {"customer_code": "MTK", "lot_ids": [lot["lot_id"]], "customer_po": "PO-1", "remark": ""},
        users["planner01"],
    )
    result = await erp_service.build_outbound(
        db, {"hours": 24, "doc_types": ["SHIPMENT"]}, "planner01"
    )
    assert result["created"]["SHIPMENT"] == 1
    row = await db.fetchrow("SELECT * FROM erp_outbound WHERE doc_type = 'SHIPMENT'")
    assert row["payload"]["lot_ids"] == [lot["lot_id"]]


async def test_build_outbound_rejects_unknown_type(factory):
    with pytest.raises(ValidationError, match="不支援的上行單據類型"):
        await erp_service.build_outbound(factory, {"doc_types": ["NOPE"]}, "planner01")


async def test_build_outbound_is_idempotent(factory, users):
    db = factory
    await _finish_lot(db, users)
    first = await erp_service.build_outbound(db, {"hours": 24}, "planner01")
    second = await erp_service.build_outbound(db, {"hours": 24}, "planner01")
    assert first["created"]["PRODUCTION_REPORT"] == 1
    assert second["created"]["PRODUCTION_REPORT"] == 1  # 覆蓋既有的待送單，不會變兩張
    assert await db.fetchval(
        "SELECT count(*) FROM erp_outbound WHERE doc_type = 'PRODUCTION_REPORT'"
    ) == 1


async def test_fetch_ack_and_requeue(factory, users):
    db = factory
    await _finish_lot(db, users)
    await erp_service.build_outbound(db, {"hours": 24}, "planner01")

    fetched = await erp_service.fetch_outbound(db, "PRODUCTION_REPORT", 10, True)
    assert fetched["count"] == 1 and fetched["marked_sent"] is True
    doc_id = fetched["documents"][0]["id"]
    assert (await db.fetchrow("SELECT status FROM erp_outbound WHERE id = $1", doc_id))["status"] == "SENT"

    # 已送出的不會再被拉一次
    assert (await erp_service.fetch_outbound(db, "PRODUCTION_REPORT", 10, True))["count"] == 0

    acked = await erp_service.ack_outbound(db, [doc_id], "planner01", "ok")
    assert acked["acked"] == 1

    requeued = await erp_service.requeue_outbound(db, [doc_id], "planner01")
    assert requeued["requeued"] == 0  # 已 ACK 的不再重送


async def test_fail_then_requeue(factory, users):
    db = factory
    await _finish_lot(db, users)
    await erp_service.build_outbound(db, {"hours": 24}, "planner01")
    doc_id = (await erp_service.fetch_outbound(db, None, 10, True))["documents"][0]["id"]

    await erp_service.fail_outbound(db, [doc_id], "ERP 連線逾時", "planner01")
    row = await db.fetchrow("SELECT * FROM erp_outbound WHERE id = $1", doc_id)
    assert row["status"] == "FAILED" and row["error"] == "ERP 連線逾時"

    await erp_service.requeue_outbound(db, [doc_id], "planner01")
    row = await db.fetchrow("SELECT * FROM erp_outbound WHERE id = $1", doc_id)
    assert row["status"] == "PENDING" and row["error"] is None


async def test_export_csv(factory, users):
    db = factory
    lot = await _finish_lot(db, users)
    await erp_service.build_outbound(db, {"hours": 24}, "planner01")
    content = await erp_service.export_csv(db, "PRODUCTION_REPORT")
    lines = content.strip().splitlines()
    assert lines[0].startswith("erp_doc_id,lot_id,wo_no")
    assert lot["lot_id"] in lines[1]


async def test_export_csv_rejects_unknown_type(factory):
    with pytest.raises(ValidationError):
        await erp_service.export_csv(factory, "NOPE")


async def test_summary_counts(factory):
    db = factory
    await erp_service.receive(db, CUSTOMER_DOC, "planner01")
    summary = await erp_service.summary(db)
    assert summary["inbound_pending"] == 1
    assert summary["inbound"]["CUSTOMER"]["PENDING"] == 1


# ── API ─────────────────────────────────────────────────────
async def test_erp_api_flow(client, token, factory):
    headers = await token("planner01")

    res = await client.post("/api/erp/inbound", json=CUSTOMER_DOC, headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["duplicate"] is False

    processed = await client.post("/api/erp/inbound/process", json={"limit": 10}, headers=headers)
    assert processed.json()["processed"] == 1

    listing = await client.get("/api/erp/inbound?status=PROCESSED", headers=headers)
    assert len(listing.json()) == 1

    summary = await client.get("/api/erp/summary", headers=headers)
    assert summary.json()["inbound"]["CUSTOMER"]["PROCESSED"] == 1


async def test_erp_export_csv_api(client, token, factory, users):
    db = factory
    await _finish_lot(db, users)
    headers = await token("planner01")
    await client.post("/api/erp/outbound/build", json={"hours": 24}, headers=headers)

    res = await client.get("/api/erp/outbound/export.csv?doc_type=PRODUCTION_REPORT", headers=headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "erp_doc_id,lot_id" in res.text


async def test_erp_api_requires_planner(client, token, factory):
    headers = await token("op001")
    res = await client.post("/api/erp/inbound", json=CUSTOMER_DOC, headers=headers)
    assert res.status_code == 403
