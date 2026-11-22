from fastapi.testclient import TestClient
from app.main import SessionLocal, app, init_seed_data

client = TestClient(app)
db = SessionLocal()
init_seed_data(db)
db.close()

USER_HEADER = {"x-token": "user-token"}
ADMIN_HEADER = {"x-token": "admin-token"}


def test_health_and_rooms():
    assert client.get("/health").status_code == 200
    res = client.get("/api/rooms")
    assert res.status_code == 200
    assert len(res.json()) >= 3


def test_booking_lifecycle_and_conflict():
    payload = {
        "room_id": 1,
        "start_time": "2026-12-01T10:00:00",
        "end_time": "2026-12-01T12:00:00",
    }
    # Успешное бронирование
    res1 = client.post("/api/bookings", json=payload, headers=USER_HEADER)
    assert res1.status_code == 200
    booking_id = res1.json()["id"]

    # Попытка двойного бронирования (конфликт 409)
    res2 = client.post("/api/bookings", json=payload, headers=USER_HEADER)
    assert res2.status_code == 409

    # Отмена брони
    res_del = client.delete(f"/api/bookings/{booking_id}", headers=USER_HEADER)
    assert res_del.status_code == 200


def test_duration_validation():
    # Меньше 1 часа или больше 4 часов -> 400
    bad_payload = {
        "room_id": 1,
        "start_time": "2026-12-02T10:00:00",
        "end_time": "2026-12-02T16:00:00",
    }
    res = client.post("/api/bookings", json=bad_payload, headers=USER_HEADER)
    assert res.status_code == 400


def test_vip_room_and_admin_approve():
    vip_payload = {
        "room_id": 3,  # VIP Зал Совета
        "start_time": "2026-12-03T14:00:00",
        "end_time": "2026-12-03T16:00:00",
    }
    res = client.post("/api/bookings", json=vip_payload, headers=USER_HEADER)
    assert res.status_code == 200
    assert res.json()["status"] == "PENDING"

    booking_id = res.json()["id"]
    app_res = client.patch(f"/api/bookings/{booking_id}/approve", headers=ADMIN_HEADER)
    assert app_res.status_code == 200
    assert app_res.json()["status"] == "CONFIRMED"


def test_csv_export():
    res = client.get("/api/reports/export", headers=ADMIN_HEADER)
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
