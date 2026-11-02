import hashlib
import json
import secrets
import time
import uuid
from contextlib import contextmanager
from sqlalchemy import create_engine, event, ForeignKey, String, Text, Integer, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str]
    role: Mapped[str]
    password_hash: Mapped[str]

class LoginSession(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    csrf: Mapped[str]
    expires: Mapped[int]

class Membership(Base):
    __tablename__ = 'project_members'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    project_id: Mapped[str]

class Record(Base):
    """Versioned MVP business records, keyed by project and record type."""
    __tablename__ = 'business_records'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str]
    kind: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict] = mapped_column(JSON)

class Task(Base):
    __tablename__ = 'workflow_runs'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str]
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    scenario: Mapped[str]
    title: Mapped[str]
    message: Mapped[str] = mapped_column(Text)
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(default='queued')
    resume: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created: Mapped[int] = mapped_column(default=lambda: int(time.time()))
    updated: Mapped[int] = mapped_column(default=lambda: int(time.time()))

class Proposal(Base):
    __tablename__ = 'proposals'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('workflow_runs.id'), unique=True)
    version: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str]
    decision: Mapped[str | None] = mapped_column(nullable=True)
    decision_by: Mapped[str | None] = mapped_column(nullable=True)
    comment: Mapped[str] = mapped_column(Text, default='')
    selected: Mapped[int | None] = mapped_column(nullable=True)

class Execution(Base):
    __tablename__ = 'tool_executions'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('workflow_runs.id'), unique=True)
    result: Mapped[dict] = mapped_column(JSON)

class Event(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    kind: Mapped[str]
    role: Mapped[str]
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[int] = mapped_column(default=lambda: int(time.time()))

class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str]
    name: Mapped[str]
    path: Mapped[str]
    text: Mapped[str] = mapped_column(Text)
    digest: Mapped[str]

engine = create_engine(f'sqlite:///{settings().data_dir / "app.db"}', connect_args={'check_same_thread': False, 'timeout': 10})
@event.listens_for(engine, 'connect')
def pragmas(conn, _):
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
Session = sessionmaker(engine, expire_on_commit=False)

@contextmanager
def db():
    with Session() as s:
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise

def uid(prefix=''):
    return prefix + uuid.uuid4().hex[:16]

def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + value

def verify_password(password, stored):
    return secrets.compare_digest(password_hash(password, stored.split(':')[0]), stored)

def log(task_id, kind, message, role='system', data=None):
    with db() as s:
        s.add(Event(task_id=task_id, kind=kind, role=role, message=message, data=data or {}))

def init_db():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from .config import ROOT
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'migrations'))
    tables = set(inspect(engine).get_table_names())
    # Adopt the exact pre-migration pilot schema only; future changes use revisions.
    if tables and 'alembic_version' not in tables:
        inspector = inspect(engine)
        expected = set(Base.metadata.tables)
        compatible = expected <= tables and all(
            {c['name'] for c in inspector.get_columns(name)} == set(Base.metadata.tables[name].columns.keys())
            for name in expected
        )
        if not compatible:
            raise RuntimeError('现有 SQLite 结构与初始版本不一致，请先备份并检查迁移。')
        command.stamp(config, '0001')
    command.upgrade(config, 'head')
    cfg = settings()
    with db() as s:
        for account, name, role, password in [('pm','林远','project_manager',cfg.pm_password), ('buyer','陈敏','procurement',cfg.buyer_password), ('finance','周宁','finance',cfg.finance_password)]:
            if not s.get(User, account):
                s.add(User(id=account,name=name,role=role,password_hash=password_hash(password)))
                s.flush()
                s.add(Membership(id=account+'-P001',user_id=account,project_id='P001'))
        rows = [
            ('P001','project',{'name':'滨江科创中心 · 机电安装工程','manager':'林远','progress':68}),
            ('PO001','order',{'supplier':'华东电缆','material':'低压电缆 YJV 4×35','quantity':200,'unit':'米','amount_cents':1600000,'due_date':'2026-11-04','delivery_plan':[],'status':'履约中'}),
            ('SCHEDULE001','schedule',{'activity':'三层电缆敷设','start':'2026-11-04','first_three_days_quantity':80,'remaining_required_date':'2026-11-09','required_spec':'YJV 4×35'}),
            ('BUDGET001','budget',{'category':'电气材料','available_cents':3000000}),
            ('Q001','quote',{'pricing_mode':'inclusive_delta','supplier':'华东电缆','material':'低压电缆 YJV 4×35','unit_price_cents':8000,'additional_cents':0,'quantity':200,'fulfillment_type':'split','delivery_commitments':[{'date':'2026-11-04','quantity':100},{'date':'2026-11-09','quantity':100}],'terms':'允许分批交货，首批100米，余量11月9日到场，不增加运费','valid_until':'2026-11-10'}),
            ('Q002','quote',{'pricing_mode':'inclusive_delta','supplier':'江南电气','material':'低压电缆 YJV 4×35','unit_price_cents':10400,'additional_cents':240000,'quantity':100,'fulfillment_type':'transfer','terms':'补充100米，其余100米仍由原供应商交付，增量成本含运费','valid_until':'2026-11-10'}),
            ('ACCEPT001','acceptance',{'order_id':'PO001','accepted_quantity':100,'confirmed_by':'林远'}),
            ('INV001','invoice',{'order_id':'PO001','invoice_number':'DEMO-001','amount_cents':800000,'duplicate':False}),
        ]
        for id, kind, data in rows:
            if not s.get(Record,id):
                s.add(Record(id=id,project_id='P001',kind=kind,data=data))
