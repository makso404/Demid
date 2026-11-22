import csv
import io
import logging
import os
from datetime import datetime
from enum import Enum
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

# Логирование всех действий и ошибок
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("booking_service")

# Подключение к БД (по умолчанию SQLite для локального запуска без Docker)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rooms.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --- МОДЕЛИ ДАННЫХ (Room, Booking, User, Role) ---
class RoleEnum(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class BookingStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"  # Для VIP-комнат
    CANCELLED = "CANCELLED"


class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    token = Column(String, unique=True, nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"))
    role = relationship("Role")


class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    capacity = Column(Integer, nullable=False)
    is_vip = Column(Boolean, default=False)


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, default=BookingStatus.CONFIRMED)
    room = relationship("Room")
    user = relationship("User")


Base.metadata.create_all(bind=engine)


# --- СХЕМЫ ВАЛИДАЦИИ (Pydantic) ---
class RoomCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    capacity: int = Field(..., ge=1, le=100)
    is_vip: bool = False


class RoomOut(RoomCreate):
    id: int

    class Config:
        from_attributes = True


class BookingCreate(BaseModel):
    room_id: int
    start_time: datetime
    end_time: datetime


class BookingOut(BaseModel):
    id: int
    room_id: int
    user_id: int
    start_time: datetime
    end_time: datetime
    status: str

    class Config:
        from_attributes = True


# --- ЗАВИСИМОСТИ И АВТОРИЗАЦИЯ ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_seed_data(db: Session):
    if not db.query(Role).first():
        r_user = Role(name=RoleEnum.USER)
        r_admin = Role(name=RoleEnum.ADMIN)
        db.add_all([r_user, r_admin])
        db.commit()
        db.add_all([
            User(username="employee", token="user-token", role_id=r_user.id),
            User(username="admin", token="admin-token", role_id=r_admin.id),
        ])
        db.add_all([
            Room(name="Переговорная Альфа", capacity=6, is_vip=False),
            Room(name="Переговорная Бета", capacity=10, is_vip=False),
            Room(name="VIP Зал Совета", capacity=15, is_vip=True),
        ])
        db.commit()


def get_current_user(x_token: str = Header(...), db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.token == x_token).first()
    if not user:
        logger.warning(f"Неудачная попытка входа с токеном: {x_token}")
        raise HTTPException(status_code=401, detail="Неверный токен авторизации")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role.name != RoleEnum.ADMIN:
        logger.warning(f"Отказ в доступе (не админ): {user.username}")
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


# --- ПРИЛОЖЕНИЕ И ЭНДПОИНТЫ ---
app = FastAPI(title="Онлайн-сервис бронирования переговорных", version="1.0.0")


@app.on_event("startup")
def on_startup():
    db = SessionLocal()
    init_seed_data(db)
    db.close()


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow()}


@app.get("/api/rooms", response_model=List[RoomOut])
def list_rooms(db: Session = Depends(get_db)):
    return db.query(Room).all()


@app.post("/api/rooms", response_model=RoomOut)
def create_room(
    payload: RoomCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.query(Room).filter(Room.name == payload.name).first():
        raise HTTPException(status_code=400, detail="Комната с таким названием уже существует")
    room = Room(**payload.model_dump())
    db.add(room)
    db.commit()
    db.refresh(room)
    logger.info(f"Админ {admin.username} создал комнату: {room.name}")
    return room


@app.get("/api/bookings", response_model=List[BookingOut])
def list_bookings(room_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Booking).filter(Booking.status != BookingStatus.CANCELLED)
    if room_id:
        query = query.filter(Booking.room_id == room_id)
    return query.order_by(Booking.start_time).all()


@app.post("/api/bookings", response_model=BookingOut)
def create_booking(
    payload: BookingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 1. Проверка длительности (1-4 часа по ТЗ)
    duration_hours = (payload.end_time - payload.start_time).total_seconds() / 3600
    if duration_hours < 1 or duration_hours > 4:
        raise HTTPException(
            status_code=400,
            detail="Длительность бронирования должна составлять от 1 до 4 часов",
        )

    room = db.query(Room).filter(Room.id == payload.room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Переговорная комната не найдена")

    # 2. Проверка конфликтов (двойное бронирование)
    conflict = (
        db.query(Booking)
        .filter(
            Booking.room_id == payload.room_id,
            Booking.status != BookingStatus.CANCELLED,
            Booking.start_time < payload.end_time,
            Booking.end_time > payload.start_time,
        )
        .first()
    )
    if conflict:
        logger.warning(f"Конфликт бронирования комнаты {room.id} пользователем {user.username}")
        raise HTTPException(
            status_code=409,
            detail="Конфликт бронирования: комната уже занята в выбранный интервал времени",
        )

    # 3. VIP-комнаты требуют подтверждения администратором
    status = BookingStatus.PENDING if room.is_vip else BookingStatus.CONFIRMED
    booking = Booking(
        room_id=payload.room_id,
        user_id=user.id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        status=status,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    logger.info(f"Пользователь {user.username} создал бронь #{booking.id} ({status})")
    return booking


@app.patch("/api/bookings/{booking_id}/approve", response_model=BookingOut)
def approve_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    booking.status = BookingStatus.CONFIRMED
    db.commit()
    db.refresh(booking)
    logger.info(f"Админ {admin.username} подтвердил VIP-бронь #{booking.id}")
    return booking


@app.delete("/api/bookings/{booking_id}")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    if booking.user_id != user.id and user.role.name != RoleEnum.ADMIN:
        raise HTTPException(status_code=403, detail="Нельзя отменить чужую бронь")
    booking.status = BookingStatus.CANCELLED
    db.commit()
    logger.info(f"Пользователь {user.username} отменил бронь #{booking.id}")
    return {"detail": "Бронирование отменено"}


@app.get("/api/reports/export")
def export_csv(
    room_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    query = db.query(Booking)
    if room_id:
        query = query.filter(Booking.room_id == room_id)
    if date_from:
        query = query.filter(Booking.start_time >= date_from)
    if date_to:
        query = query.filter(Booking.end_time <= date_to)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Room_ID", "User_ID", "Start", "End", "Status"])
    for b in query.all():
        writer.writerow([b.id, b.room_id, b.user_id, b.start_time, b.end_time, b.status])

    output.seek(0)
    logger.info(f"Админ {admin.username} выгрузил CSV-отчёт")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bookings_report.csv"},
    )



if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
def read_index():
    return FileResponse("app/static/index.html")
