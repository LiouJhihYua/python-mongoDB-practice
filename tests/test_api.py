"""HTTP 層整合測試：走完一條真實的現場作業動線。"""


async def test_health(client):
    res = await client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


async def test_end_to_end_production_via_api(client, token):
    planner = await token("planner01")
    operator = await token("op001")
    qc = await token("qc01")

    # 1) 生管開工單並下達
    res = await client.post("/api/work-orders", headers=planner, json={
        "device_id": "TEST-QFN48", "plan_qty": 20, "unit_type": "WAFER",
        "due_date": "2030-01-01T00:00:00Z", "priority": 3, "customer_po": "PO-9",
    })
    assert res.status_code == 201
    wo_no = res.json()["wo_no"]
    assert (await client.post(f"/api/work-orders/{wo_no}/release", headers=planner)).status_code == 200

    # 2) 取晶圓開批
    wafers = (await client.get("/api/master/wafers?consumed=false&limit=3", headers=planner)).json()["items"]
    wafer_ids = [w["wafer_id"] for w in wafers[:3]]
    res = await client.post("/api/lots", headers=planner, json={
        "wo_no": wo_no, "qty": 3, "wafer_ids": wafer_ids, "carrier_id": "MAG1",
    })
    assert res.status_code == 201, res.text
    lot_id = res.json()["lot_id"]

    # 3) 作業員在免設備的進料站進出站
    assert (await client.post("/api/lots/track-in", headers=operator,
                       json={"lot_id": lot_id, "eq_id": ""})).status_code == 200
    res = await client.post("/api/lots/track-out", headers=operator,
                      json={"lot_id": lot_id, "good_qty": 3, "reject_qty": 0})
    assert res.status_code == 200
    assert res.json()["current_op"] == "WFR_SAW"

    # 4) 切割站：片 → 顆
    assert (await client.post("/api/lots/track-in", headers=operator,
                       json={"lot_id": lot_id, "eq_id": "DS-01"})).status_code == 200
    res = await client.post("/api/lots/track-out", headers=operator, json={
        "lot_id": lot_id, "good_qty": 2950, "reject_qty": 50,
        "defects": [{"defect_code": "SAW-CHIP", "qty": 50}],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["qty"] == 2950
    assert body["unit_type"] == "DIE"
    assert body["last_step"]["qty_expected"] == 3000

    # 5) 品保扣留後放行
    assert (await client.post("/api/lots/hold", headers=qc,
                       json={"lot_id": lot_id, "reason": "QUALITY", "remark": "抽檢"})).status_code == 200
    holds = (await client.get("/api/quality/holds?status=OPEN", headers=qc)).json()
    assert holds[0]["lot_id"] == lot_id
    assert (await client.post("/api/lots/release", headers=qc,
                       json={"lot_id": lot_id, "remark": "OK"})).status_code == 200

    # 6) 報表看得到資料
    dash = (await client.get("/api/reports/dashboard?hours=24", headers=planner)).json()
    assert dash["kpi"]["wip_lots"] == 1
    assert dash["kpi"]["moves"] == 2

    wip = (await client.get("/api/reports/wip", headers=planner)).json()
    assert wip["items"][0]["op_code"] == "WIRE_BOND"

    pareto = (await client.get("/api/quality/defects/pareto?hours=24", headers=qc)).json()
    assert pareto["items"][0]["defect_code"] == "SAW-CHIP"

    # 7) 追溯查得到來源晶圓與設備
    trace = (await client.get(f"/api/trace/lots/{lot_id}/backward", headers=planner)).json()
    assert [w["wafer_id"] for w in trace["source_wafers"]] == sorted(wafer_ids)
    assert "DS-01" in trace["equipments_used"]

    forward = (await client.get(f"/api/trace/wafers/{wafer_ids[0]}/forward", headers=planner)).json()
    assert forward["impacted_lot_count"] == 1

    # 8) 履歷完整
    history = (await client.get(f"/api/lots/{lot_id}/history", headers=planner)).json()
    assert [h["action"] for h in history][:3] == ["CREATE", "TRACK_IN", "TRACK_OUT"]


async def test_business_error_has_structured_body(client, token):
    operator = await token("op001")
    res = await client.post("/api/lots/track-in", headers=operator, json={"lot_id": "NO-SUCH-LOT"})
    assert res.status_code == 404
    body = res.json()
    assert body["error"] == "NOT_FOUND"
    assert "找不到批號" in body["message"]


async def test_state_error_maps_to_409(client, token):
    planner, operator = await token("planner01"), await token("op001")
    wo_no = (await client.post("/api/work-orders", headers=planner, json={
        "device_id": "TEST-QFN48", "plan_qty": 5, "unit_type": "WAFER",
        "due_date": "2030-01-01T00:00:00Z",
    })).json()["wo_no"]
    await client.post(f"/api/work-orders/{wo_no}/release", headers=planner)
    lot_id = (await client.post("/api/lots", headers=planner,
                         json={"wo_no": wo_no, "qty": 1})).json()["lot_id"]

    await client.post("/api/lots/track-in", headers=operator, json={"lot_id": lot_id, "eq_id": ""})
    res = await client.post("/api/lots/track-in", headers=operator, json={"lot_id": lot_id, "eq_id": ""})
    assert res.status_code == 409
    assert res.json()["error"] == "INVALID_STATE"


async def test_equipment_oee_endpoint(client, token):
    headers = await token("eng01")
    assert (await client.post("/api/equipments/WB-01/state", headers=headers,
                       json={"state": "PRODUCTIVE", "reason_code": "TEST"})).status_code == 200
    rows = (await client.get("/api/equipments/oee?hours=8", headers=headers)).json()
    assert {r["eq_id"] for r in rows} == {"DS-01", "WB-01", "FT-01"}
    assert all(0 <= r["oee"] <= 1 for r in rows)

    summary = (await client.get("/api/equipments/summary", headers=headers)).json()
    assert summary["total"] == 3
    assert summary["by_state"]["PRODUCTIVE"]["equipments"] == ["WB-01"]


async def test_openapi_schema_available(client):
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    for path in ("/api/lots/track-in", "/api/reports/dashboard",
                 "/api/trace/lots/{lot_id}/backward", "/api/equipments/{eq_id}/oee"):
        assert path in paths
