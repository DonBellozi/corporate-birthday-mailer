from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)

class LocalUser(Base):
    __tablename__ = "local_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(50), default="manual")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_valid: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

class EmployeeSnapshot(Base):
    __tablename__ = "employee_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"))
    employee_key: Mapped[str] = mapped_column(String(255), index=True)
    fio: Mapped[str] = mapped_column(String(500))
    birthday_day: Mapped[int] = mapped_column(Integer)
    birthday_month: Mapped[int] = mapped_column(Integer)
    gender: Mapped[str] = mapped_column(String(20), default="unknown")
    employee_state: Mapped[str] = mapped_column(String(200), default="")
    work_email: Mapped[str] = mapped_column(String(500), default="")
    hide_birthday: Mapped[bool] = mapped_column(Boolean, default=False)
    source_position: Mapped[str | None] = mapped_column(Text, nullable=True)

class PositionMapping(Base):
    __tablename__ = "position_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_position: Mapped[str] = mapped_column(Text, unique=True)
    display_position: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    congratulate: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class EmployeePositionChoice(Base):
    __tablename__ = "employee_position_choices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_position: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class IntroTemplate(Base):
    __tablename__ = "intro_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class WishTemplate(Base):
    __tablename__ = "wish_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[str] = mapped_column(String(20), default="universal")
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[str] = mapped_column(String(20), default="universal")
    filename: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class MailLog(Base):
    __tablename__ = "mail_logs"
    __table_args__ = (
        UniqueConstraint("employee_key", "birthday_year", name="uq_employee_birthday_year"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_key: Mapped[str] = mapped_column(String(255), index=True)
    fio: Mapped[str] = mapped_column(String(500))
    birthday_year: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    recipient: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    rendered_text: Mapped[str] = mapped_column(Text, default="")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(Text, default="")
