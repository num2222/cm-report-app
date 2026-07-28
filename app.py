# -*- coding: utf-8 -*-
"""
ระบบติดตามสต๊อกสินค้า (กลาง + ห้องช่าง) + PR (Purchase Requisition) + ติดตั้ง/คืนคลัง
สำหรับทีมซ่อมบำรุงระบบไฟฟ้า
"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import calendar
import re
import json
import io
import os
from dotenv import load_dotenv

load_dotenv()

import line_service

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

# --- ฐานข้อมูล ---
# ในเครื่อง (local): ใช้ SQLite ไฟล์เดียว ไม่ต้องตั้งค่าอะไร
# บน Render: ตั้งค่า environment variable DATABASE_URL ให้ชี้ไปที่ PostgreSQL
#   (ใช้ "Internal Database URL" ของฐานข้อมูลเดิมที่ CM app ใช้อยู่ได้เลย เพื่อไม่ต้องเปิด Postgres ใหม่)
_database_url = os.environ.get("DATABASE_URL", "")
_is_postgres = _database_url.startswith("postgres")
if _database_url:
    # Render (และผู้ให้บริการ Postgres หลายเจ้า) ส่ง URL แบบ "postgres://" มา
    # แต่ SQLAlchemy รุ่นใหม่ต้องการ "postgresql://" ต้องแปลงก่อนใช้งาน
    if _database_url.startswith("postgres://"):
        _database_url = _database_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _database_url
    if _is_postgres:
        # ถ้าใช้ฐานข้อมูลร่วมกับแอปอื่น (เช่น Corrective Maintenance Report) ให้แยกตารางของแอปนี้
        # ไปอยู่ใน schema ของตัวเอง ("stock_app") กันชื่อตารางชนกับของแอปอื่นโดยเด็ดขาด
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {'options': '-csearch_path=stock_app'}
        }
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(BASE_DIR, 'stock_pr.db')}"

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

db = SQLAlchemy(app)


def format_thai_date(d):
    """แปลง date เป็นรูปแบบไทย DD-MM-YYYY แบบ พ.ศ. เช่น 2026-07-01 -> 01-07-2569"""
    if not d:
        return "-"
    return f"{d.day:02d}-{d.month:02d}-{d.year + 543}"


app.jinja_env.filters["thai_date"] = format_thai_date

APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")


@app.before_request
def require_login():
    """บังคับล็อกอินด้วยรหัสผ่านเดียว (ตั้งค่าผ่าน APP_PASSWORD) ก่อนเข้าใช้งานทุกหน้า
    ยกเว้นหน้า login และ webhook ของ LINE (LINE ต้องยิงเข้ามาได้โดยไม่ล็อกอิน)"""
    exempt_endpoints = {"login", "static", "line_webhook"}
    if request.endpoint in exempt_endpoints or request.endpoint is None:
        return
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------

class Item(db.Model):
    """
    สินค้าแต่ละรายการมี 2 สต๊อกแยกกัน:
    - central_qty: สต๊อกสินค้ากลาง (คลังหลัก) เพิ่มจากการรับ PR, ลดจากการโอนไปห้องช่าง
    - tech_room_qty: สต๊อกสินค้าห้องช่าง เพิ่มจากการโอนมาจากสต๊อกกลาง, ลดจากการติดตั้งจริง
    - item_type: 'company' (สินค้าที่บริษัทซื้อ/สต๊อกเอง) หรือ 'aot' (อะไหล่ของ AOT/Owner ที่เราเข้าไปเปลี่ยนให้ แต่ไม่ได้เป็นสต๊อกของบริษัท)
    - seq: ลำดับที่ใช้อ้างอิงกับรายงาน Excel ประจำเดือน (ปกติ 1-62 สำหรับสินค้าบริษัท, 63+ สำหรับอะไหล่ AOT)
    """
    __tablename__ = "items"
    id = db.Column(db.Integer, primary_key=True)
    seq = db.Column(db.Integer)
    name = db.Column(db.String(200), nullable=False)
    unit = db.Column(db.String(30), default="ชิ้น")
    category = db.Column(db.String(100))
    item_type = db.Column(db.String(20), default="company")  # company / aot
    unit_price = db.Column(db.Float, default=0)
    central_qty = db.Column(db.Float, default=0)
    tech_room_qty = db.Column(db.Float, default=0)
    reorder_point = db.Column(db.Float, default=0)
    location = db.Column(db.String(100))
    note = db.Column(db.Text)

    def status(self):
        if self.central_qty <= 0:
            return "หมด"
        if self.central_qty < self.reorder_point:
            return "ใกล้หมด"
        return "ปกติ"

    def central_value(self):
        return self.central_qty * (self.unit_price or 0)

    def tech_room_value(self):
        return self.tech_room_qty * (self.unit_price or 0)

    def is_aot(self):
        return self.item_type == "aot"

    def display_name(self):
        return f"{self.name} (AOT)" if self.is_aot() else self.name


class LineUser(db.Model):
    __tablename__ = "line_users"
    id = db.Column(db.Integer, primary_key=True)
    line_user_id = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(150))
    role = db.Column(db.String(30), default="unassigned")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NotifyGroup(db.Model):
    __tablename__ = "notify_groups"
    id = db.Column(db.Integer, primary_key=True)
    line_group_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(150))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ApprovalChainStep(db.Model):
    __tablename__ = "approval_chain_steps"
    id = db.Column(db.Integer, primary_key=True)
    step_order = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    active = db.Column(db.Boolean, default=True)


class PRApprovalStep(db.Model):
    __tablename__ = "pr_approval_steps"
    id = db.Column(db.Integer, primary_key=True)
    pr_id = db.Column(db.Integer, db.ForeignKey("purchase_requisitions.id"), nullable=False)
    step_order = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="รอคิว")
    actor_name = db.Column(db.String(100))
    comment = db.Column(db.Text)
    acted_at = db.Column(db.DateTime)


class PR(db.Model):
    __tablename__ = "purchase_requisitions"
    id = db.Column(db.Integer, primary_key=True)
    pr_no = db.Column(db.String(50), unique=True, nullable=False)
    date_issued = db.Column(db.Date, default=date.today)
    requester = db.Column(db.String(100))
    requester_line_user_id = db.Column(db.Integer, db.ForeignKey("line_users.id"), nullable=True)
    status = db.Column(db.String(30), default="รออนุมัติ")
    reject_reason = db.Column(db.Text)
    expected_date = db.Column(db.Date)
    received_date = db.Column(db.Date)
    note = db.Column(db.Text)

    lines = db.relationship("PRLine", backref="pr", cascade="all, delete-orphan")
    requester_line_user = db.relationship("LineUser")
    approval_steps = db.relationship(
        "PRApprovalStep", backref="pr", cascade="all, delete-orphan",
        order_by="PRApprovalStep.step_order"
    )

    def current_step(self):
        for s in self.approval_steps:
            if s.status == "กำลังพิจารณา":
                return s
        return None

    def is_fully_approved(self):
        return bool(self.approval_steps) and all(s.status == "อนุมัติ" for s in self.approval_steps)

    def is_rejected(self):
        return any(s.status == "ไม่อนุมัติ" for s in self.approval_steps)

    def stage_summary(self):
        if not self.approval_steps:
            return self.status
        if self.is_rejected():
            rejected = next(s for s in self.approval_steps if s.status == "ไม่อนุมัติ")
            return f"ไม่อนุมัติ (ที่ขั้น {rejected.title})"
        if self.is_fully_approved():
            return "อนุมัติครบทุกขั้นตอน"
        cur = self.current_step()
        return f"รอ {cur.title} พิจารณา" if cur else "-"

    def receiving_status(self):
        if not self.lines:
            return "-"
        total_req = sum(l.qty_requested for l in self.lines)
        total_recv = sum(l.qty_received for l in self.lines)
        if total_recv <= 0:
            return "รอของ"
        if total_recv < total_req:
            return "ได้รับบางส่วน"
        return "ได้รับครบ"


class PRLine(db.Model):
    __tablename__ = "pr_lines"
    id = db.Column(db.Integer, primary_key=True)
    pr_id = db.Column(db.Integer, db.ForeignKey("purchase_requisitions.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    qty_requested = db.Column(db.Float, default=0)
    qty_received = db.Column(db.Float, default=0)
    line_status = db.Column(db.String(20), default="pending")  # pending / approved / rejected
    qty_approved = db.Column(db.Float, default=0)
    line_reject_reason = db.Column(db.Text)

    item = db.relationship("Item")

    def status_label(self):
        return {"pending": "รอพิจารณา", "approved": "อนุมัติ", "rejected": "ไม่อนุมัติ"}.get(self.line_status, self.line_status)


class StockTransfer(db.Model):
    """การโอนของจากสต๊อกกลาง ไปสต๊อกห้องช่าง (ช่างมาเบิกไปเตรียมไว้รอเปลี่ยน)"""
    __tablename__ = "stock_transfers"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    qty = db.Column(db.Float, default=0)
    technician = db.Column(db.String(100))
    date_transferred = db.Column(db.Date, default=date.today)
    note = db.Column(db.Text)

    item = db.relationship("Item")


class InstallRecord(db.Model):
    """
    บันทึกการติดตั้ง/เปลี่ยนอะไหล่จริงหน้างาน (ผู้ใช้กรอกเองจากที่เห็นในกลุ่ม LINE)
    ตัดสต๊อกห้องช่างทันที
    - install_type = 'replacement': เปลี่ยนของเก่าที่ชำรุด -> ของเก่าต้องคืนคลัง (คืนให้ Owner)
    - install_type = 'new': ติดตั้งเพิ่มใหม่ (ไม่ได้เปลี่ยนของเดิม) -> ไม่มีของเก่าคืนคลัง
    """
    __tablename__ = "install_records"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    qty = db.Column(db.Float, default=0)
    install_type = db.Column(db.String(20), default="replacement")
    technician = db.Column(db.String(100))
    area = db.Column(db.String(100))
    date_installed = db.Column(db.Date, default=date.today)
    note = db.Column(db.Text)

    item = db.relationship("Item")

    def returned_to_owner_qty(self):
        return self.qty if self.install_type == "replacement" else 0

    def type_label(self):
        return "เปลี่ยนของเก่า (คืนคลัง)" if self.install_type == "replacement" else "ติดตั้งเพิ่มใหม่ (ไม่มีคืน)"


class CmImportInstallLink(db.Model):
    """เชื่อมแถว CM 1 แถว กับ InstallRecord ได้หลายรายการ (กรณีต้องตัดสต๊อกอุปกรณ์เสริมเพิ่มในงานเดียวกัน)"""
    __tablename__ = "cm_import_install_links"
    id = db.Column(db.Integer, primary_key=True)
    cm_row_id = db.Column(db.Integer, db.ForeignKey("cm_import_rows.id"), nullable=False)
    install_record_id = db.Column(db.Integer, db.ForeignKey("install_records.id"), nullable=False)

    install_record = db.relationship("InstallRecord")


class CmImportRow(db.Model):
    """
    แถวที่นำเข้าจากไฟล์ CSV รายงาน CM รายเดือน (คอลัมน์ "การแก้ไข")
    ใช้เป็นคิวให้ผู้ใช้ตรวจสอบ + Confirm ก่อนตัดสต๊อกห้องช่างจริง
    """
    __tablename__ = "cm_import_rows"
    id = db.Column(db.Integer, primary_key=True)
    area = db.Column(db.String(50))          # พื้นที่
    date_text = db.Column(db.String(20))     # วันที่ (ตามที่อยู่ใน CSV เช่น 01/07/69)
    date_installed = db.Column(db.Date)       # วันที่แปลงเป็น ค.ศ. แล้ว (เดาไว้ให้ ปรับได้ตอน confirm)
    seq = db.Column(db.String(20))            # ลำดับ
    job_no = db.Column(db.String(50))
    sap_no = db.Column(db.String(50))
    location_detail = db.Column(db.String(200))  # บริเวณ
    problem = db.Column(db.Text)               # ปัญหา
    fix_text = db.Column(db.Text)              # การแก้ไข
    technician = db.Column(db.String(100))     # ช่าง

    item_guess = db.Column(db.String(200))     # ชื่ออะไหล่ที่เดาไว้จากข้อความ
    qty_guess = db.Column(db.Float)            # จำนวนที่เดาไว้จากข้อความ

    status = db.Column(db.String(20), default="pending")  # pending / confirmed / ignored

    import_batch = db.Column(db.String(200))   # ชื่อไฟล์ที่นำเข้า
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)

    install_links = db.relationship("CmImportInstallLink", backref="cm_row", cascade="all, delete-orphan")

    def confirmed_installs(self):
        return [l.install_record for l in self.install_links]


class StockCount(db.Model):
    """บันทึกผลนับสต๊อกจริง เทียบกับยอดตามระบบ (แยกนับได้ทั้งสต๊อกกลาง/ห้องช่าง)"""
    __tablename__ = "stock_counts"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    location = db.Column(db.String(20), default="central")
    count_date = db.Column(db.Date, default=date.today)
    book_qty = db.Column(db.Float)
    actual_qty = db.Column(db.Float)
    note = db.Column(db.Text)

    item = db.relationship("Item")

    def variance(self):
        return (self.actual_qty or 0) - (self.book_qty or 0)

    def location_label(self):
        return "สต๊อกกลาง" if self.location == "central" else "สต๊อกห้องช่าง"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def parse_date(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_thai_be_date(s):
    """
    แปลงวันที่แบบ พ.ศ. จากไฟล์ CSV รายงาน CM เป็น date แบบ ค.ศ. คืนค่า None ถ้าแปลงไม่ได้
    รองรับ 2 รูปแบบที่เจอจริง:
    - DD/MM/YY แบบ พ.ศ. 2 หลัก เช่น "01/07/69" -> พ.ศ. 2569 -> ค.ศ. 2026
    - DD/MM/YYYY ที่ระบบต้นทางแปลงปีผิดเป็น 4 หลักแบบ ค.ศ. เช่น "01/07/1969"
      (Excel มักตีความเลขปี 2 หลัก "69" เป็น "1969" โดยอัตโนมัติ) -> ให้ตัดเหลือ 2 หลักท้าย
      แล้วตีความเป็น พ.ศ. เหมือนเดิม เช่น 1969 -> 69 -> พ.ศ. 2569 -> ค.ศ. 2026
    """
    if not s:
        return None
    try:
        parts = s.strip().split("/")
        if len(parts) != 3:
            return None
        d, m, y = parts
        y = y.strip()
        yi = int(y)
        if len(y) == 4 and yi > 2400:
            # ปี พ.ศ. เต็ม 4 หลักอยู่แล้ว เช่น 2569
            ce_year = yi - 543
        else:
            # ปี 2 หลัก หรือปีที่ถูกแปลงผิดเป็น 4 หลักแบบ ค.ศ. (เช่น 1969) -> เอาแค่ 2 หลักท้าย
            yy = yi % 100
            ce_year = 2500 + yy - 543
        return date(ce_year, int(m), int(d))
    except Exception:
        return None


CM_KEYWORD_RE = re.compile(r"เปลี่ยน|ติดตั้ง")
CM_QTY_RE1 = re.compile(r"จำนวน\s*([\d.]+)")
CM_QTY_RE2 = re.compile(r"([\d.]+)\s*(?:หลอด|ตัว|ชิ้น|อัน|ดวง|จุด|ลูก)")


def guess_item_and_qty(fix_text):
    """เดาชื่ออะไหล่และจำนวนจากข้อความ 'การแก้ไข' แบบหยาบๆ (ผู้ใช้ต้องตรวจสอบ/แก้ไขก่อน Confirm เสมอ)"""
    if not fix_text:
        return None, None, None
    text = fix_text.strip()

    qty = None
    m = CM_QTY_RE1.search(text)
    if m:
        qty = float(m.group(1))
    else:
        m2 = CM_QTY_RE2.search(text)
        if m2:
            qty = float(m2.group(1))

    kw_match = CM_KEYWORD_RE.search(text)
    install_type_guess = "replacement" if (kw_match and "เปลี่ยน" in kw_match.group()) else "new"
    if "เปลี่ยน" in text:
        install_type_guess = "replacement"
    elif "ติดตั้ง" in text:
        install_type_guess = "new"

    item_guess = None
    if kw_match:
        start = kw_match.end()
        rest = text[start:]
        # ตัดที่คำว่า "จำนวน" หรือเลขตัวแรกที่เจอ ถือเป็นจุดสิ้นสุดชื่ออะไหล่ที่เดา
        cut_positions = []
        idx_qty_word = rest.find("จำนวน")
        if idx_qty_word != -1:
            cut_positions.append(idx_qty_word)
        num_match = re.search(r"\d", rest)
        if num_match:
            cut_positions.append(num_match.start())
        cut = min(cut_positions) if cut_positions else len(rest)
        item_guess = rest[:cut].strip(" :-")
        if not item_guess:
            item_guess = None

    return item_guess, qty, install_type_guess


def parse_cm_csv(file_stream, filename=""):
    """
    อ่านไฟล์ CSV รายงาน CM รายเดือน (จากระบบ Corrective Maintenance Report)
    คืนค่าจำนวนแถวที่นำเข้าใหม่ (ข้ามแถวที่ Job No. ซ้ำกับที่มีอยู่แล้ว)
    """
    import csv
    import io
    content = file_stream.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)

    # หาแถว header จริง (มีคำว่า "การแก้ไข" อยู่ในแถวนั้น)
    header_idx = None
    for i, r in enumerate(rows):
        if any("การแก้ไข" in cell for cell in r):
            header_idx = i
            break
    if header_idx is None:
        return 0

    data_rows = rows[header_idx + 1:]
    new_count = 0
    for r in data_rows:
        if len(r) < 20:
            continue
        area, date_text, seq, job_no, sap_no = r[0], r[1], r[2], r[3], r[4]
        location_detail, problem = r[8], r[9]
        fix_text, technician = r[18], r[19]

        if not fix_text or not CM_KEYWORD_RE.search(fix_text):
            continue

        # กันข้อมูลซ้ำ ถ้าเคยนำเข้า Job No. นี้แล้ว (หรือ area+seq+date ถ้าไม่มี Job No.)
        dup_query = CmImportRow.query
        if job_no:
            existing = dup_query.filter_by(job_no=job_no).first()
        else:
            existing = dup_query.filter_by(area=area, date_text=date_text, seq=seq).first()
        if existing:
            continue

        item_guess, qty_guess, install_type_guess = guess_item_and_qty(fix_text)

        row = CmImportRow(
            area=area,
            date_text=date_text,
            date_installed=parse_thai_be_date(date_text) or date.today(),
            seq=seq,
            job_no=job_no,
            sap_no=sap_no,
            location_detail=location_detail,
            problem=problem,
            fix_text=fix_text,
            technician=technician,
            item_guess=item_guess,
            qty_guess=qty_guess,
            status="pending",
            import_batch=filename,
        )
        db.session.add(row)
        new_count += 1
    db.session.commit()
    return new_count


def notify_group(text):
    groups = NotifyGroup.query.filter_by(active=True).all()
    if not groups or not line_service.is_configured():
        return
    for g in groups:
        try:
            line_service.push_message(g.line_group_id, text)
        except Exception:
            pass


def create_approval_steps_for_pr(pr):
    templates = ApprovalChainStep.query.filter_by(active=True).order_by(ApprovalChainStep.step_order).all()
    for i, t in enumerate(templates):
        step = PRApprovalStep(
            pr_id=pr.id,
            step_order=t.step_order,
            title=t.title,
            status="กำลังพิจารณา" if i == 0 else "รอคิว",
        )
        db.session.add(step)


def notify_new_pr(pr):
    lines = "\n".join(f"- {l.item.name} x {l.qty_requested} {l.item.unit}" for l in pr.lines)
    stage = pr.stage_summary()
    text = (
        f"📝 มี PR ใหม่เข้าสู่กระบวนการอนุมัติ\n"
        f"เลขที่: {pr.pr_no}\n"
        f"ผู้ขอ: {pr.requester or '-'}\n"
        f"วันที่ออก: {pr.date_issued}\n"
        f"รายการ:\n{lines or '-'}\n"
        f"สถานะปัจจุบัน: {stage}"
    )
    notify_group(text)


def notify_step_action(pr, step):
    if step.status == "อนุมัติ":
        icon = "✅"
        result_text = f"{icon} {step.title} อนุมัติแล้ว"
    else:
        icon = "❌"
        result_text = f"{icon} {step.title} ไม่อนุมัติ"
        if step.comment:
            result_text += f"\nเหตุผล: {step.comment}"

    stage = pr.stage_summary()
    text = (
        f"{result_text}\n"
        f"PR เลขที่: {pr.pr_no}\n"
        f"สถานะล่าสุด: {stage}"
    )
    notify_group(text)

    if pr.is_fully_approved() and pr.requester_line_user and pr.requester_line_user.active:
        try:
            line_service.push_message(
                pr.requester_line_user.line_user_id,
                f"🎉 PR เลขที่ {pr.pr_no} ของคุณได้รับการอนุมัติครบทุกขั้นตอนแล้ว\nคาดว่าจะได้รับของ: {pr.expected_date or 'ยังไม่ระบุ'}"
            )
        except Exception:
            pass
    elif pr.is_rejected() and pr.requester_line_user and pr.requester_line_user.active:
        try:
            line_service.push_message(
                pr.requester_line_user.line_user_id,
                f"❌ PR เลขที่ {pr.pr_no} ของคุณไม่ได้รับการอนุมัติ\nเหตุผล: {step.comment or 'ไม่ได้ระบุ'}"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ROUTES: LOGIN
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == APP_PASSWORD:
            session["logged_in"] = True
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        flash("รหัสผ่านไม่ถูกต้อง", "success")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# ROUTES: DASHBOARD
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    items = Item.query.all()
    low_stock = [i for i in items if i.item_type != "aot" and i.central_qty < i.reorder_point]

    prs = PR.query.all()
    pr_summary = {
        "รออนุมัติ": sum(1 for p in prs if p.status == "รออนุมัติ"),
        "อนุมัติ": sum(1 for p in prs if p.status == "อนุมัติ"),
        "ไม่อนุมัติ": sum(1 for p in prs if p.status == "ไม่อนุมัติ"),
        "อนุมัติบางส่วน": sum(1 for p in prs if p.status == "อนุมัติบางส่วน"),
    }

    pending_delivery = [p for p in prs if p.status in ("อนุมัติ", "อนุมัติบางส่วน") and p.receiving_status() != "ได้รับครบ"]
    recent_installs = InstallRecord.query.order_by(InstallRecord.date_installed.desc()).limit(8).all()
    central_total_value = sum(i.central_value() for i in items)
    tech_room_total_value = sum(i.tech_room_value() for i in items)

    return render_template(
        "dashboard.html",
        items=items,
        low_stock=low_stock,
        pr_summary=pr_summary,
        pending_delivery=pending_delivery,
        recent_installs=recent_installs,
        central_total_value=central_total_value,
        tech_room_total_value=tech_room_total_value,
    )


# ---------------------------------------------------------------------------
# ROUTES: ITEMS (STOCK)
# ---------------------------------------------------------------------------

@app.route("/items")
def items_list():
    q = request.args.get("q", "").strip()
    query = Item.query
    if q:
        query = query.filter(Item.name.contains(q))
    items = query.order_by(Item.seq, Item.category, Item.name).all()
    return render_template("items.html", items=items, q=q)


@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    if request.method == "POST":
        item = Item(
            seq=int(request.form.get("seq")) if request.form.get("seq") else None,
            name=request.form["name"],
            unit=request.form.get("unit") or "ชิ้น",
            category=request.form.get("category"),
            item_type=request.form.get("item_type") or "company",
            unit_price=float(request.form.get("unit_price") or 0),
            central_qty=float(request.form.get("central_qty") or 0),
            tech_room_qty=float(request.form.get("tech_room_qty") or 0),
            reorder_point=float(request.form.get("reorder_point") or 0),
            location=request.form.get("location"),
            note=request.form.get("note"),
        )
        db.session.add(item)
        db.session.commit()
        flash("เพิ่มรายการสินค้าเรียบร้อย", "success")
        return redirect(url_for("items_list"))
    return render_template("item_form.html", item=None)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == "POST":
        item.seq = int(request.form.get("seq")) if request.form.get("seq") else None
        item.name = request.form["name"]
        item.unit = request.form.get("unit") or "ชิ้น"
        item.category = request.form.get("category")
        item.item_type = request.form.get("item_type") or "company"
        item.unit_price = float(request.form.get("unit_price") or 0)
        item.central_qty = float(request.form.get("central_qty") or 0)
        item.tech_room_qty = float(request.form.get("tech_room_qty") or 0)
        item.reorder_point = float(request.form.get("reorder_point") or 0)
        item.location = request.form.get("location")
        item.note = request.form.get("note")
        db.session.commit()
        flash("แก้ไขรายการสินค้าเรียบร้อย", "success")
        return redirect(url_for("items_list"))
    return render_template("item_form.html", item=item)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash("ลบรายการสินค้าเรียบร้อย", "success")
    return redirect(url_for("items_list"))


# ---------------------------------------------------------------------------
# ROUTES: PR (Purchase Requisition)
# ---------------------------------------------------------------------------

@app.route("/pr")
def pr_list():
    status_filter = request.args.get("status", "")
    query = PR.query
    if status_filter:
        query = query.filter(PR.status == status_filter)
    prs = query.order_by(PR.date_issued.desc()).all()
    return render_template("pr_list.html", prs=prs, status_filter=status_filter)


@app.route("/pr/new", methods=["GET", "POST"])
def pr_new():
    items = Item.query.order_by(Item.name).all()
    if request.method == "POST":
        req_line_id = request.form.get("requester_line_user_id")
        pr = PR(
            pr_no=request.form["pr_no"],
            date_issued=parse_date(request.form.get("date_issued")) or date.today(),
            requester=request.form.get("requester"),
            requester_line_user_id=int(req_line_id) if req_line_id else None,
            status=request.form.get("status") or "รออนุมัติ",
            reject_reason=request.form.get("reject_reason"),
            expected_date=parse_date(request.form.get("expected_date")),
            note=request.form.get("note"),
        )
        db.session.add(pr)
        db.session.flush()

        item_ids = request.form.getlist("item_id")
        qtys = request.form.getlist("qty_requested")
        line_statuses = request.form.getlist("line_status")
        qty_approveds = request.form.getlist("qty_approved")
        line_reasons = request.form.getlist("line_reject_reason")
        for idx, (iid, qty) in enumerate(zip(item_ids, qtys)):
            if iid and qty:
                db.session.add(PRLine(
                    pr_id=pr.id, item_id=int(iid), qty_requested=float(qty),
                    line_status=(line_statuses[idx] if idx < len(line_statuses) else "pending") or "pending",
                    qty_approved=float(qty_approveds[idx]) if idx < len(qty_approveds) and qty_approveds[idx] else 0,
                    line_reject_reason=line_reasons[idx] if idx < len(line_reasons) else None,
                ))

        create_approval_steps_for_pr(pr)
        db.session.commit()
        notify_new_pr(pr)
        flash("สร้าง PR เรียบร้อย และแจ้งเตือนกลุ่ม LINE แล้ว (ถ้าตั้งค่าไว้)", "success")
        return redirect(url_for("pr_list"))
    line_users = LineUser.query.filter_by(active=True).all()
    return render_template("pr_form.html", pr=None, items=items, line_users=line_users)


@app.route("/pr/<int:pr_id>/edit", methods=["GET", "POST"])
def pr_edit(pr_id):
    pr = PR.query.get_or_404(pr_id)
    items = Item.query.order_by(Item.name).all()
    if request.method == "POST":
        req_line_id = request.form.get("requester_line_user_id")
        pr.pr_no = request.form["pr_no"]
        pr.date_issued = parse_date(request.form.get("date_issued")) or pr.date_issued
        pr.requester = request.form.get("requester")
        pr.requester_line_user_id = int(req_line_id) if req_line_id else None
        pr.status = request.form.get("status")
        pr.reject_reason = request.form.get("reject_reason")
        pr.expected_date = parse_date(request.form.get("expected_date"))
        pr.received_date = parse_date(request.form.get("received_date"))
        pr.note = request.form.get("note")

        PRLine.query.filter_by(pr_id=pr.id).delete()
        item_ids = request.form.getlist("item_id")
        qtys = request.form.getlist("qty_requested")
        recv_qtys = request.form.getlist("qty_received")
        line_statuses = request.form.getlist("line_status")
        qty_approveds = request.form.getlist("qty_approved")
        line_reasons = request.form.getlist("line_reject_reason")
        for idx, (iid, qty, rqty) in enumerate(zip(item_ids, qtys, recv_qtys)):
            if iid and qty:
                db.session.add(PRLine(
                    pr_id=pr.id, item_id=int(iid),
                    qty_requested=float(qty),
                    qty_received=float(rqty or 0),
                    line_status=(line_statuses[idx] if idx < len(line_statuses) else "pending") or "pending",
                    qty_approved=float(qty_approveds[idx]) if idx < len(qty_approveds) and qty_approveds[idx] else 0,
                    line_reject_reason=line_reasons[idx] if idx < len(line_reasons) else None,
                ))
        db.session.commit()
        flash("บันทึกการแก้ไข PR เรียบร้อย", "success")
        return redirect(url_for("pr_list"))
    line_users = LineUser.query.filter_by(active=True).all()
    return render_template("pr_form.html", pr=pr, items=items, line_users=line_users)


@app.route("/pr/<int:pr_id>/receive", methods=["POST"])
def pr_receive(pr_id):
    pr = PR.query.get_or_404(pr_id)
    for line in pr.lines:
        recv_key = f"recv_{line.id}"
        val = request.form.get(recv_key)
        if val:
            add_qty = float(val)
            if add_qty > 0:
                line.qty_received += add_qty
                line.item.central_qty += add_qty
    if pr.receiving_status() == "ได้รับครบ":
        pr.received_date = date.today()
    db.session.commit()
    flash("บันทึกรับของเรียบร้อย และอัปเดตสต๊อกกลางแล้ว", "success")
    return redirect(url_for("pr_edit", pr_id=pr.id))


@app.route("/pr/<int:pr_id>/delete", methods=["POST"])
def pr_delete(pr_id):
    pr = PR.query.get_or_404(pr_id)
    db.session.delete(pr)
    db.session.commit()
    flash("ลบ PR เรียบร้อย", "success")
    return redirect(url_for("pr_list"))


@app.route("/pr/<int:pr_id>/step/<int:step_id>/action", methods=["POST"])
def pr_step_action(pr_id, step_id):
    pr = PR.query.get_or_404(pr_id)
    step = PRApprovalStep.query.get_or_404(step_id)
    if step.pr_id != pr.id or step.status != "กำลังพิจารณา":
        flash("ขั้นตอนนี้ไม่ได้อยู่ในสถานะที่พิจารณาได้ในตอนนี้", "success")
        return redirect(url_for("pr_edit", pr_id=pr.id))

    action = request.form.get("action")
    step.actor_name = request.form.get("actor_name")
    step.comment = request.form.get("comment")
    step.acted_at = datetime.utcnow()

    if action == "approve":
        step.status = "อนุมัติ"
        next_step = next(
            (s for s in sorted(pr.approval_steps, key=lambda x: x.step_order) if s.step_order > step.step_order),
            None
        )
        if next_step:
            next_step.status = "กำลังพิจารณา"
        else:
            pr.status = "อนุมัติ"
    elif action == "reject":
        step.status = "ไม่อนุมัติ"
        pr.status = "ไม่อนุมัติ"
        pr.reject_reason = step.comment

    db.session.commit()
    notify_step_action(pr, step)
    flash("บันทึกผลการพิจารณาเรียบร้อย และแจ้งเตือนกลุ่ม LINE แล้ว (ถ้าตั้งค่าไว้)", "success")
    return redirect(url_for("pr_edit", pr_id=pr.id))


# ---------------------------------------------------------------------------
# ROUTES: STOCK TRANSFER (สต๊อกกลาง -> สต๊อกห้องช่าง)
# ---------------------------------------------------------------------------

@app.route("/transfers")
def transfers_list():
    transfers = StockTransfer.query.order_by(StockTransfer.date_transferred.desc()).all()
    return render_template("transfers.html", transfers=transfers)


@app.route("/transfers/new", methods=["GET", "POST"])
def transfer_new():
    items = Item.query.order_by(Item.name).all()
    if request.method == "POST":
        item = Item.query.get_or_404(int(request.form["item_id"]))
        qty = float(request.form["qty"])
        t = StockTransfer(
            item_id=item.id,
            qty=qty,
            technician=request.form.get("technician"),
            date_transferred=parse_date(request.form.get("date_transferred")) or date.today(),
            note=request.form.get("note"),
        )
        item.central_qty -= qty
        item.tech_room_qty += qty
        db.session.add(t)
        db.session.commit()
        flash("บันทึกการโอนสต๊อกไปห้องช่างเรียบร้อย", "success")
        return redirect(url_for("transfers_list"))
    return render_template("transfer_form.html", items=items)


@app.route("/transfers/<int:t_id>/delete", methods=["POST"])
def transfer_delete(t_id):
    t = StockTransfer.query.get_or_404(t_id)
    t.item.central_qty += t.qty
    t.item.tech_room_qty -= t.qty
    db.session.delete(t)
    db.session.commit()
    flash("ลบรายการโอนเรียบร้อย และคืนยอดสต๊อกกลับแล้ว", "success")
    return redirect(url_for("transfers_list"))


# ---------------------------------------------------------------------------
# ROUTES: INSTALL RECORD (ติดตั้ง/เปลี่ยนอะไหล่จริงหน้างาน)
# ---------------------------------------------------------------------------

@app.route("/installs")
def installs_list():
    installs = InstallRecord.query.order_by(InstallRecord.date_installed.desc()).all()
    return render_template("installs.html", installs=installs)


@app.route("/installs/new", methods=["GET", "POST"])
def install_new():
    items = Item.query.order_by(Item.name).all()
    if request.method == "POST":
        item = Item.query.get_or_404(int(request.form["item_id"]))
        qty = float(request.form["qty"])
        rec = InstallRecord(
            item_id=item.id,
            qty=qty,
            install_type=request.form.get("install_type") or "replacement",
            technician=request.form.get("technician"),
            area=request.form.get("area"),
            date_installed=parse_date(request.form.get("date_installed")) or date.today(),
            note=request.form.get("note"),
        )
        item.tech_room_qty -= qty
        db.session.add(rec)
        db.session.commit()
        flash("บันทึกการติดตั้ง/เปลี่ยนอะไหล่เรียบร้อย และตัดสต๊อกห้องช่างแล้ว", "success")
        return redirect(url_for("installs_list"))
    return render_template("install_form.html", items=items)


@app.route("/installs/<int:rec_id>/edit", methods=["GET", "POST"])
def install_edit(rec_id):
    rec = InstallRecord.query.get_or_404(rec_id)
    items = Item.query.order_by(Item.name).all()
    if request.method == "POST":
        new_item = Item.query.get_or_404(int(request.form["item_id"]))
        new_qty = float(request.form["qty"])

        # คืนยอดสต๊อกห้องช่างของสินค้าเดิมก่อน แล้วค่อยหักของสินค้า/จำนวนใหม่
        rec.item.tech_room_qty += rec.qty
        new_item.tech_room_qty -= new_qty

        rec.item_id = new_item.id
        rec.qty = new_qty
        rec.install_type = request.form.get("install_type") or rec.install_type
        rec.technician = request.form.get("technician")
        rec.area = request.form.get("area")
        rec.date_installed = parse_date(request.form.get("date_installed")) or rec.date_installed
        rec.note = request.form.get("note")
        db.session.commit()
        flash("แก้ไขรายการติดตั้งเรียบร้อย และปรับยอดสต๊อกห้องช่างให้ตรงแล้ว", "success")
        return redirect(url_for("installs_list"))
    return render_template("install_form.html", items=items, rec=rec)


@app.route("/installs/<int:rec_id>/delete", methods=["POST"])
def install_delete(rec_id):
    rec = InstallRecord.query.get_or_404(rec_id)
    rec.item.tech_room_qty += rec.qty
    db.session.delete(rec)
    db.session.commit()
    flash("ลบรายการติดตั้งเรียบร้อย และคืนยอดสต๊อกห้องช่างกลับแล้ว", "success")
    return redirect(url_for("installs_list"))


# ---------------------------------------------------------------------------
# ROUTES: CM CSV IMPORT QUEUE (นำเข้าจากรายงาน CM รายเดือน เพื่อคิว Confirm ตัดสต๊อก)
# ---------------------------------------------------------------------------

@app.route("/cm-import/sync", methods=["POST"])
def cm_import_sync():
    """
    ดึงเคสใหม่จากตาราง cases ของแอป Corrective Maintenance Report โดยตรง
    (ใช้ได้เมื่อสองแอปแชร์ฐานข้อมูล PostgreSQL เดียวกัน — ไม่ทำงานตอนรันด้วย SQLite ในเครื่อง)
    ดึงเฉพาะเคสที่วันที่ >= วันที่เริ่มต้นที่ระบุ (กันดึงประวัติเก่าทั้งหมดมาปนกับสต๊อกปัจจุบัน)
    """
    from sqlalchemy import text

    start_date = parse_date(request.form.get("sync_start_date"))
    if not start_date:
        flash("กรุณาระบุวันที่เริ่มต้นก่อนกดซิงก์", "success")
        return redirect(url_for("cm_import_queue"))

    try:
        rows = db.session.execute(text("""
            SELECT area, date, seq, job_no, sap_no, location, problem, solution, technician
            FROM public.cases
            WHERE (cancelled IS NULL OR cancelled = FALSE)
              AND (solution ILIKE :kw1 OR solution ILIKE :kw2)
        """), {"kw1": "%เปลี่ยน%", "kw2": "%ติดตั้ง%"}).fetchall()
    except Exception as e:
        flash(f"เชื่อมต่อฐานข้อมูล CM ไม่สำเร็จ (ใช้ได้เฉพาะตอนแชร์ PostgreSQL กับแอป CM เท่านั้น): {e}", "success")
        return redirect(url_for("cm_import_queue"))

    new_count = 0
    skipped_old = 0
    skipped_unparsed = 0
    for r in rows:
        area, date_text, seq, job_no, sap_no, location_detail, problem, fix_text, technician = r
        if not fix_text or not CM_KEYWORD_RE.search(fix_text):
            continue

        case_date = parse_thai_be_date(date_text)
        if case_date is None:
            skipped_unparsed += 1
            continue
        if case_date < start_date:
            skipped_old += 1
            continue

        if job_no:
            existing = CmImportRow.query.filter_by(job_no=job_no).first()
        else:
            existing = CmImportRow.query.filter_by(area=area, date_text=date_text, seq=seq).first()
        if existing:
            continue

        item_guess, qty_guess, install_type_guess = guess_item_and_qty(fix_text)
        row = CmImportRow(
            area=area,
            date_text=date_text,
            date_installed=case_date,
            seq=seq,
            job_no=job_no,
            sap_no=sap_no,
            location_detail=location_detail,
            problem=problem,
            fix_text=fix_text,
            technician=technician,
            item_guess=item_guess,
            qty_guess=qty_guess,
            status="pending",
            import_batch="sync:cm_database",
        )
        db.session.add(row)
        new_count += 1
    db.session.commit()

    msg = f"ซิงก์จากฐานข้อมูล CM สำเร็จ พบรายการใหม่ {new_count} รายการ (ตั้งแต่ {start_date})"
    if skipped_old:
        msg += f" — ข้ามเคสที่เก่ากว่าวันที่กำหนด {skipped_old} รายการ"
    if skipped_unparsed:
        msg += f" — มี {skipped_unparsed} รายการที่วันที่อ่านไม่ได้ ข้ามไป (ใช้วิธี import CSV แทนได้)"
    flash(msg, "success")
    return redirect(url_for("cm_import_queue"))


@app.route("/cm-import", methods=["GET", "POST"])
def cm_import():
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or not file.filename:
            flash("กรุณาเลือกไฟล์ CSV ก่อน", "success")
            return redirect(url_for("cm_import"))
        new_count = parse_cm_csv(file.stream, filename=file.filename)
        flash(f"นำเข้าไฟล์ {file.filename} สำเร็จ พบรายการที่เกี่ยวข้องกับสต๊อกใหม่ {new_count} รายการ (แถวที่มี Job No. ซ้ำจะถูกข้ามอัตโนมัติ)", "success")
        return redirect(url_for("cm_import_queue"))
    return render_template("cm_import.html")


@app.route("/cm-import/queue")
def cm_import_queue():
    rows = CmImportRow.query.filter_by(status="pending").order_by(CmImportRow.date_installed.desc()).all()
    items = Item.query.order_by(Item.name).all()
    from datetime import timedelta
    default_sync_date = (date.today() - timedelta(days=90)).isoformat()
    return render_template("cm_import_queue.html", rows=rows, items=items, default_sync_date=default_sync_date)


@app.route("/cm-import/<int:row_id>/confirm", methods=["POST"])
def cm_import_confirm(row_id):
    row = CmImportRow.query.get_or_404(row_id)
    item_ids = request.form.getlist("item_id")
    qtys = request.form.getlist("qty")
    install_types = request.form.getlist("install_type")

    created = 0
    for idx, (iid, qty) in enumerate(zip(item_ids, qtys)):
        if not iid or not qty:
            continue
        item = Item.query.get_or_404(int(iid))
        qty_f = float(qty)
        install_type = install_types[idx] if idx < len(install_types) else "replacement"

        rec = InstallRecord(
            item_id=item.id,
            qty=qty_f,
            install_type=install_type,
            technician=row.technician,
            area=f"{row.area} / {row.location_detail}" if row.location_detail else row.area,
            date_installed=row.date_installed or date.today(),
            note=f"นำเข้าจาก CM Job No. {row.job_no or '-'} SAP {row.sap_no or '-'} — {row.fix_text}",
        )
        item.tech_room_qty -= qty_f
        db.session.add(rec)
        db.session.flush()
        db.session.add(CmImportInstallLink(cm_row_id=row.id, install_record_id=rec.id))
        created += 1

    if created == 0:
        flash("กรุณาเลือกสินค้าและใส่จำนวนอย่างน้อย 1 รายการก่อน Confirm", "success")
        return redirect(url_for("cm_import_queue"))

    row.status = "confirmed"
    db.session.commit()
    flash(f"Confirm ตัดสต๊อกห้องช่างเรียบร้อย {created} รายการ", "success")
    return redirect(url_for("cm_import_queue"))


@app.route("/cm-import/<int:row_id>/ignore", methods=["POST"])
def cm_import_ignore(row_id):
    row = CmImportRow.query.get_or_404(row_id)
    row.status = "ignored"
    db.session.commit()
    flash("ข้ามรายการนี้เรียบร้อย (ไม่ตัดสต๊อก)", "success")
    return redirect(url_for("cm_import_queue"))


@app.route("/cm-import/history")
def cm_import_history():
    rows = CmImportRow.query.filter(CmImportRow.status != "pending").order_by(CmImportRow.imported_at.desc()).all()
    return render_template("cm_import_history.html", rows=rows)


# ---------------------------------------------------------------------------
# ROUTES: STOCK COUNT (แยกนับได้ทั้งสต๊อกกลาง/ห้องช่าง)
# ---------------------------------------------------------------------------

@app.route("/stock-count", methods=["GET", "POST"])
def stock_count():
    location = request.args.get("location", "central")
    if location not in ("central", "tech_room"):
        location = "central"
    items = Item.query.order_by(Item.category, Item.name).all()
    if request.method == "POST":
        location = request.form.get("location", "central")
        for item in items:
            actual_key = f"actual_{item.id}"
            val = request.form.get(actual_key)
            if val is not None and val != "":
                actual = float(val)
                book_qty = item.central_qty if location == "central" else item.tech_room_qty
                sc = StockCount(
                    item_id=item.id,
                    location=location,
                    count_date=date.today(),
                    book_qty=book_qty,
                    actual_qty=actual,
                    note=request.form.get(f"note_{item.id}"),
                )
                db.session.add(sc)
                if location == "central":
                    item.central_qty = actual
                else:
                    item.tech_room_qty = actual
        db.session.commit()
        flash("บันทึกผลนับสต๊อกเรียบร้อย ปรับยอดตามที่นับจริงแล้ว", "success")
        return redirect(url_for("stock_count_history"))
    return render_template("stock_count.html", items=items, location=location)


@app.route("/stock-count/history")
def stock_count_history():
    counts = StockCount.query.order_by(StockCount.count_date.desc()).all()
    return render_template("stock_count_history.html", counts=counts)


# ---------------------------------------------------------------------------
# ROUTES: REPORTS
# ---------------------------------------------------------------------------

@app.route("/reports/movement")
def report_movement():
    items = Item.query.order_by(Item.category, Item.name).all()
    rows = []
    for item in items:
        transfers = StockTransfer.query.filter_by(item_id=item.id).all()
        installs = InstallRecord.query.filter_by(item_id=item.id).all()
        total_transferred = sum(t.qty for t in transfers)
        total_installed = sum(r.qty for r in installs)
        total_returned_owner = sum(r.returned_to_owner_qty() for r in installs)
        total_new_install = sum(r.qty for r in installs if r.install_type == "new")

        last_central_count = (StockCount.query.filter_by(item_id=item.id, location="central")
                               .order_by(StockCount.count_date.desc()).first())
        last_tech_count = (StockCount.query.filter_by(item_id=item.id, location="tech_room")
                            .order_by(StockCount.count_date.desc()).first())
        rows.append({
            "item": item,
            "total_transferred": total_transferred,
            "total_installed": total_installed,
            "total_returned_owner": total_returned_owner,
            "total_new_install": total_new_install,
            "central_last_variance": last_central_count.variance() if last_central_count else None,
            "tech_last_variance": last_tech_count.variance() if last_tech_count else None,
        })
    return render_template("report_movement.html", rows=rows)


@app.route("/reports/pr-summary")
def report_pr_summary():
    prs = PR.query.order_by(PR.date_issued.desc()).all()
    total = len(prs)
    by_status = {}
    for p in prs:
        by_status.setdefault(p.status, []).append(p)
    reject_reasons = {}
    for p in prs:
        if p.status == "ไม่อนุมัติ" and p.reject_reason:
            reject_reasons[p.reject_reason] = reject_reasons.get(p.reject_reason, 0) + 1
    return render_template("report_pr_summary.html", prs=prs, total=total, by_status=by_status, reject_reasons=reject_reasons)


@app.route("/reports/monthly")
def report_monthly():
    """สรุปสต๊อกสินค้ากลางประจำเดือน + สรุปอะไหล่ที่ใช้ไป/คืนคลังประจำเดือน"""
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    days_in_month = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, days_in_month)

    items = Item.query.order_by(Item.category, Item.name).all()

    stock_rows = []
    usage_rows = []
    total_central_value = 0
    total_installed_all = 0
    total_returned_all = 0

    for item in items:
        transferred_this_month = sum(
            t.qty for t in StockTransfer.query.filter(
                StockTransfer.item_id == item.id,
                StockTransfer.date_transferred >= start_date,
                StockTransfer.date_transferred <= end_date,
            ).all()
        )
        received_this_month = 0
        prs_received = PR.query.filter(
            PR.received_date >= start_date, PR.received_date <= end_date
        ).all()
        for pr in prs_received:
            for line in pr.lines:
                if line.item_id == item.id:
                    received_this_month += line.qty_received

        installs_this_month = InstallRecord.query.filter(
            InstallRecord.item_id == item.id,
            InstallRecord.date_installed >= start_date,
            InstallRecord.date_installed <= end_date,
        ).all()
        installed_qty = sum(r.qty for r in installs_this_month)
        returned_qty = sum(r.returned_to_owner_qty() for r in installs_this_month)
        new_install_qty = installed_qty - returned_qty

        total_central_value += item.central_value()
        total_installed_all += installed_qty
        total_returned_all += returned_qty

        stock_rows.append({
            "item": item,
            "received": received_this_month,
            "transferred": transferred_this_month,
            "current_central": item.central_qty,
            "value": item.central_value(),
        })
        if installed_qty > 0:
            usage_rows.append({
                "item": item,
                "installed": installed_qty,
                "returned": returned_qty,
                "new_install": new_install_qty,
            })

    return render_template(
        "report_monthly.html",
        year=year, month=month,
        stock_rows=stock_rows, usage_rows=usage_rows,
        total_central_value=total_central_value,
        total_installed_all=total_installed_all,
        total_returned_all=total_returned_all,
    )


@app.route("/reports/monthly/export")
def report_monthly_export():
    """ส่งออกรายงานประจำเดือนเป็นไฟล์ Excel ตามแม่แบบ Template_SparePart_Month.xlsx"""
    import openpyxl

    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    days_in_month = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, days_in_month)

    template_path = os.path.join(BASE_DIR, "excel_templates", "Template_SparePart_Month.xlsx")
    wb = openpyxl.load_workbook(template_path)
    ws_stock = wb["Stock"]
    ws_sum = wb["Sum"]

    # --- Sheet "Stock": เฉพาะสินค้าบริษัท ลำดับ 1-62 ---
    company_items = Item.query.filter(
        Item.item_type == "company", Item.seq.isnot(None), Item.seq >= 1, Item.seq <= 62
    ).all()
    for item in company_items:
        r = item.seq + 7  # seq 1 -> row 8 ... seq 62 -> row 69
        ws_stock.cell(row=r, column=2, value=item.name)          # B รายการ
        ws_stock.cell(row=r, column=3, value=item.unit)           # C หน่วย
        ws_stock.cell(row=r, column=4, value=item.central_qty)    # D ปริมาณ (สต๊อกกลาง)
        ws_stock.cell(row=r, column=5, value=item.unit_price)     # E ราคาต่อหน่วย
        ws_stock.cell(row=r, column=6, value=f"=D{r}*E{r}")       # F เป็นเงิน
        ws_stock.cell(row=r, column=7, value=item.reorder_point)  # G Min (จุดสั่งซื้อขั้นต่ำ)
        # H (Max) และ I (หมายเหตุ) ปล่อยว่างไว้ตามที่ระบุ

    # --- Sheet "Sum": สินค้าบริษัท ลำดับ 1-62 ---
    for item in company_items:
        r = item.seq + 7
        installs = InstallRecord.query.filter(
            InstallRecord.item_id == item.id,
            InstallRecord.date_installed >= start_date,
            InstallRecord.date_installed <= end_date,
        ).all()
        installed_qty = sum(x.qty for x in installs)
        returned_qty = sum(x.returned_to_owner_qty() for x in installs)

        ws_sum.cell(row=r, column=2, value=item.name)             # B รายการ
        ws_sum.cell(row=r, column=3, value=installed_qty)         # C จำนวนที่ใช้
        ws_sum.cell(row=r, column=4, value=item.unit)              # D หน่วย
        ws_sum.cell(row=r, column=5, value=returned_qty)          # E จำนวนคืนคลัง
        ws_sum.cell(row=r, column=6, value=item.unit)              # F หน่วย
        ws_sum.cell(row=r, column=7, value="✓")                   # G Amplo
        ws_sum.cell(row=r, column=9, value=item.unit_price)        # I ราคาต่อหน่วย
        ws_sum.cell(row=r, column=10, value=f"=C{r}*I{r}")         # J รวมเป็นเงิน

    # --- Sheet "Sum": อะไหล่ AOT (Owner) ต่อจากลำดับ 62 ---
    aot_items = Item.query.filter(Item.item_type == "aot").order_by(
        db.case((Item.seq.is_(None), 1), else_=0), Item.seq, Item.name
    ).all()
    aot_row_start = 71
    aot_row_limit = 77  # ช่องว่างในเทมเพลตมีถึงแถวนี้เท่านั้น
    skipped_aot = 0
    next_seq = 63
    for i, item in enumerate(aot_items):
        r = aot_row_start + i
        if r > aot_row_limit:
            skipped_aot += 1
            continue
        display_seq = item.seq if item.seq else next_seq
        next_seq = display_seq + 1

        installs = InstallRecord.query.filter(
            InstallRecord.item_id == item.id,
            InstallRecord.date_installed >= start_date,
            InstallRecord.date_installed <= end_date,
        ).all()
        installed_qty = sum(x.qty for x in installs)
        returned_qty = sum(x.returned_to_owner_qty() for x in installs)

        ws_sum.cell(row=r, column=1, value=display_seq)            # A ลำดับ
        ws_sum.cell(row=r, column=2, value=item.name)              # B รายการ
        ws_sum.cell(row=r, column=3, value=installed_qty)          # C จำนวนที่ใช้
        ws_sum.cell(row=r, column=4, value=item.unit)               # D หน่วย
        ws_sum.cell(row=r, column=5, value=returned_qty)           # E จำนวนคืนคลัง
        ws_sum.cell(row=r, column=6, value=item.unit)               # F หน่วย
        ws_sum.cell(row=r, column=8, value="✓")                    # H AOT
        ws_sum.cell(row=r, column=9, value=item.unit_price)         # I ราคาต่อหน่วย
        ws_sum.cell(row=r, column=10, value=f"=C{r}*I{r}")          # J รวมเป็นเงิน

    if skipped_aot:
        flash(f"หมายเหตุ: มีอะไหล่ AOT เกินพื้นที่ในแม่แบบ {skipped_aot} รายการ ไม่ได้ใส่ในไฟล์นี้ (พื้นที่รองรับสูงสุด {aot_row_limit - aot_row_start + 1} รายการ)", "success")

    out_dir = os.path.join(BASE_DIR, "tmp_exports")
    os.makedirs(out_dir, exist_ok=True)
    out_filename = f"รายงานอะไหล่ประจำเดือน_{month:02d}-{year}.xlsx"
    out_path = os.path.join(out_dir, out_filename)
    wb.save(out_path)

    return send_file(out_path, as_attachment=True, download_name=out_filename)


# ---------------------------------------------------------------------------
# ROUTES: LINE WEBHOOK + USER MANAGEMENT
# ---------------------------------------------------------------------------

@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not line_service.verify_signature(body, signature):
        return jsonify({"error": "invalid signature"}), 400

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        source = event.get("source", {})
        event_type = event.get("type")
        group_id = source.get("groupId")
        user_id = source.get("userId")

        if group_id:
            existing_group = NotifyGroup.query.filter_by(line_group_id=group_id).first()
            summary = line_service.get_group_summary(group_id)
            group_name = summary.get("groupName") if summary else None
            if not existing_group:
                existing_group = NotifyGroup(line_group_id=group_id, name=group_name, active=False)
                db.session.add(existing_group)
                db.session.commit()
                if event_type == "join":
                    line_service.push_message(
                        group_id,
                        "สวัสดีครับ 🙏 บอทถูกเชิญเข้ากลุ่มนี้แล้ว\nรอผู้ดูแลระบบเปิดใช้งานการแจ้งเตือนให้กลุ่มนี้ที่หน้า \"ผู้รับแจ้งเตือน LINE\" ก่อนนะครับ"
                    )
            elif group_name and existing_group.name != group_name:
                existing_group.name = group_name
                db.session.commit()
            continue

        if user_id:
            existing = LineUser.query.filter_by(line_user_id=user_id).first()
            profile = line_service.get_profile(user_id)
            display_name = profile.get("displayName") if profile else None

            if not existing:
                existing = LineUser(line_user_id=user_id, display_name=display_name, role="unassigned")
                db.session.add(existing)
                db.session.commit()
                reply_token = event.get("replyToken")
                if reply_token:
                    line_service.reply_message(
                        reply_token,
                        "ลงทะเบียนรับการแจ้งเตือนเรียบร้อยครับ 🙏\nรอผู้ดูแลระบบกำหนดสิทธิ์การแจ้งเตือนให้ก่อนนะครับ"
                    )
            elif display_name and existing.display_name != display_name:
                existing.display_name = display_name
                db.session.commit()

    return jsonify({"status": "ok"})


@app.route("/line-users")
def line_users_list():
    users = LineUser.query.order_by(LineUser.created_at.desc()).all()
    groups = NotifyGroup.query.order_by(NotifyGroup.created_at.desc()).all()
    return render_template(
        "line_users.html", users=users, groups=groups,
        line_configured=line_service.is_configured()
    )


@app.route("/line-users/<int:user_id>/update", methods=["POST"])
def line_user_update(user_id):
    u = LineUser.query.get_or_404(user_id)
    u.role = request.form.get("role", "unassigned")
    u.active = request.form.get("active") == "on"
    db.session.commit()
    flash("อัปเดตสิทธิ์ผู้ใช้ LINE เรียบร้อย", "success")
    return redirect(url_for("line_users_list"))


@app.route("/line-users/<int:user_id>/delete", methods=["POST"])
def line_user_delete(user_id):
    u = LineUser.query.get_or_404(user_id)
    db.session.delete(u)
    db.session.commit()
    flash("ลบผู้ใช้ LINE เรียบร้อย", "success")
    return redirect(url_for("line_users_list"))


@app.route("/notify-groups/<int:group_id>/update", methods=["POST"])
def notify_group_update(group_id):
    g = NotifyGroup.query.get_or_404(group_id)
    g.active = request.form.get("active") == "on"
    db.session.commit()
    flash("อัปเดตสถานะกลุ่มแจ้งเตือนเรียบร้อย", "success")
    return redirect(url_for("line_users_list"))


@app.route("/notify-groups/<int:group_id>/delete", methods=["POST"])
def notify_group_delete(group_id):
    g = NotifyGroup.query.get_or_404(group_id)
    db.session.delete(g)
    db.session.commit()
    flash("ลบกลุ่มแจ้งเตือนเรียบร้อย", "success")
    return redirect(url_for("line_users_list"))


# ---------------------------------------------------------------------------
# ROUTES: APPROVAL CHAIN TEMPLATE
# ---------------------------------------------------------------------------

@app.route("/approval-chain")
def approval_chain_list():
    steps = ApprovalChainStep.query.order_by(ApprovalChainStep.step_order).all()
    return render_template("approval_chain.html", steps=steps)


@app.route("/approval-chain/new", methods=["POST"])
def approval_chain_new():
    max_order = db.session.query(db.func.max(ApprovalChainStep.step_order)).scalar() or 0
    step = ApprovalChainStep(
        step_order=max_order + 1,
        title=request.form.get("title"),
        active=True,
    )
    db.session.add(step)
    db.session.commit()
    flash("เพิ่มขั้นตอนอนุมัติเรียบร้อย", "success")
    return redirect(url_for("approval_chain_list"))


@app.route("/approval-chain/<int:step_id>/update", methods=["POST"])
def approval_chain_update(step_id):
    step = ApprovalChainStep.query.get_or_404(step_id)
    step.title = request.form.get("title")
    step.step_order = int(request.form.get("step_order") or step.step_order)
    step.active = request.form.get("active") == "on"
    db.session.commit()
    flash("บันทึกขั้นตอนอนุมัติเรียบร้อย", "success")
    return redirect(url_for("approval_chain_list"))


@app.route("/approval-chain/<int:step_id>/delete", methods=["POST"])
def approval_chain_delete(step_id):
    step = ApprovalChainStep.query.get_or_404(step_id)
    db.session.delete(step)
    db.session.commit()
    flash("ลบขั้นตอนอนุมัติเรียบร้อย", "success")
    return redirect(url_for("approval_chain_list"))


# ---------------------------------------------------------------------------
# ROUTES: EXPORT / IMPORT ข้อมูลทั้งหมด (สำรองข้อมูล / ย้ายข้อมูล)
# ---------------------------------------------------------------------------

# ลำดับสำคัญ: ต้องเรียงตาม dependency (ตารางที่ไม่มี FK ก่อน แล้วค่อยตารางที่อ้างอิงตารางอื่น)
# หมายเหตุ: ไม่รวมตาราง User (บัญชีล็อกอิน) ในไฟล์สำรองข้อมูลนี้โดยเจตนา เพื่อความปลอดภัย
EXPORT_MODELS = [
    (Item, "items"),
    (ApprovalChainStep, "approval_chain_steps"),
    (LineUser, "line_users"),
    (NotifyGroup, "notify_groups"),
    (PR, "purchase_requisitions"),
    (PRLine, "pr_lines"),
    (PRApprovalStep, "pr_approval_steps"),
    (StockTransfer, "stock_transfers"),
    (InstallRecord, "install_records"),
    (StockCount, "stock_counts"),
    (CmImportRow, "cm_import_rows"),
    (CmImportInstallLink, "cm_import_install_links"),
]


def _export_serialize(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _import_deserialize(col_type, v):
    if v is None:
        return None
    type_name = col_type.__class__.__name__
    try:
        if type_name == "Date":
            return date.fromisoformat(v) if isinstance(v, str) else v
        if type_name == "DateTime":
            return datetime.fromisoformat(v) if isinstance(v, str) else v
    except Exception:
        return None
    return v


@app.route("/admin/export")
def admin_export():
    data = {}
    for model, key in EXPORT_MODELS:
        rows = []
        for obj in model.query.all():
            row = {col.name: _export_serialize(getattr(obj, col.name)) for col in model.__table__.columns}
            rows.append(row)
        data[key] = rows

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    buf = io.BytesIO(payload.encode("utf-8"))
    filename = f"stock_app_backup_{date.today().isoformat()}.json"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/json")


@app.route("/admin/import", methods=["GET", "POST"])
def admin_import():
    if request.method == "POST":
        file = request.files.get("backup_file")
        confirm = request.form.get("confirm")
        if confirm != "yes":
            flash("กรุณาติ๊กยืนยันก่อน Import (การ Import จะลบข้อมูลเดิมทั้งหมดของแอปนี้แล้วแทนที่ด้วยไฟล์ backup)", "success")
            return redirect(url_for("admin_import"))
        if not file or not file.filename:
            flash("กรุณาเลือกไฟล์ backup ก่อน", "success")
            return redirect(url_for("admin_import"))

        try:
            data = json.loads(file.stream.read().decode("utf-8"))
        except Exception as e:
            flash(f"อ่านไฟล์ไม่สำเร็จ ตรวจสอบว่าเป็นไฟล์ backup ที่ export จากระบบนี้: {e}", "success")
            return redirect(url_for("admin_import"))

        try:
            # ลบข้อมูลเดิมทั้งหมด (เรียงย้อนกลับกันปัญหา FK)
            for model, key in reversed(EXPORT_MODELS):
                model.query.delete()
            db.session.commit()

            # เติมข้อมูลใหม่จากไฟล์ backup ตามลำดับ
            for model, key in EXPORT_MODELS:
                for row in data.get(key, []):
                    kwargs = {}
                    for col in model.__table__.columns:
                        if col.name in row:
                            kwargs[col.name] = _import_deserialize(col.type, row[col.name])
                    db.session.add(model(**kwargs))
                db.session.flush()
            db.session.commit()

            # sync ตัวนับ id ของ PostgreSQL ให้ไม่ชนกับ id ที่ import เข้ามา (SQLite ไม่ต้องทำ)
            if _is_postgres:
                from sqlalchemy import text
                for model, key in EXPORT_MODELS:
                    table = model.__table__.name
                    db.session.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('stock_app.{table}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM stock_app.{table}), 1))"
                    ))
                db.session.commit()

        except Exception as e:
            db.session.rollback()
            flash(f"Import ล้มเหลว ข้อมูลเดิมยังอยู่ครบ ไม่มีอะไรเสียหาย: {e}", "success")
            return redirect(url_for("admin_import"))

        flash("Import ข้อมูลสำเร็จ ข้อมูลเดิมถูกแทนที่ด้วยข้อมูลจากไฟล์ backup เรียบร้อยแล้ว", "success")
        return redirect(url_for("dashboard"))

    return render_template("admin_import.html")


# ---------------------------------------------------------------------------

with app.app_context():
    if _is_postgres:
        from sqlalchemy import text
        db.session.execute(text("CREATE SCHEMA IF NOT EXISTS stock_app"))
        db.session.commit()
    db.create_all()

if __name__ == "__main__":
    app.run(debug=not bool(_database_url), host="0.0.0.0", port=5050)
