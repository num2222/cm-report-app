"""
CM Daily Report — Flask Full-Stack
Deploy on Render.com with PostgreSQL
"""
import os, io
from flask import Flask, request, jsonify, send_file, render_template
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from pathlib import Path
import random, string, time

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///cm_local.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'cm-dev-key-change-in-prod')

db = SQLAlchemy(app)

class Case(db.Model):
    __tablename__ = 'cases'
    id            = db.Column(db.String(20), primary_key=True)
    area          = db.Column(db.String(10), nullable=False, index=True)
    date          = db.Column(db.String(10), nullable=False, index=True)
    seq           = db.Column(db.String(10))
    job_no        = db.Column(db.String(20))
    sap_no        = db.Column(db.String(20), nullable=True)
    notify_time   = db.Column(db.String(10))
    kpi_val       = db.Column(db.String(10))
    response_time = db.Column(db.String(20))
    std           = db.Column(db.String(5))
    approve_time  = db.Column(db.String(10))
    arrive_time   = db.Column(db.String(10))
    start_time    = db.Column(db.String(10))
    close_time    = db.Column(db.String(10))
    location      = db.Column(db.Text)
    problem       = db.Column(db.Text)
    solution      = db.Column(db.Text)
    reporter      = db.Column(db.String(100))
    receiver      = db.Column(db.String(100))
    technician    = db.Column(db.String(100))
    kpi1          = db.Column(db.String(10))
    kpi2          = db.Column(db.String(10))
    kpi3          = db.Column(db.String(10))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'area': self.area, 'date': self.date,
            'seq': self.seq, 'jobNo': self.job_no, 'sapNo': self.sap_no,
            'notifyTime': self.notify_time, 'kpiVal': self.kpi_val,
            'responseTime': self.response_time, 'std': self.std,
            'approveTime': self.approve_time, 'arriveTime': self.arrive_time,
            'startTime': self.start_time, 'closeTime': self.close_time,
            'location': self.location, 'problem': self.problem,
            'solution': self.solution, 'reporter': self.reporter,
            'receiver': self.receiver, 'technician': self.technician,
            'kpi1': self.kpi1, 'kpi2': self.kpi2, 'kpi3': self.kpi3,
        }

with app.app_context():
    db.create_all()

def gen_id():
    return ''.join(random.choices(string.ascii_lowercase+string.digits, k=8))+str(int(time.time()))[-4:]

def fmt_be(d):
    if not d: return ''
    p = d.split('-')
    return f"{p[2]}/{p[1]}/{str(int(p[0])+543)[-2:]}"

def kpi_sym(v):
    return 'P' if v=='pass' else ('O' if v=='fail' else '')

STD_LABELS = {'A':'5-30 นาที','B':'1-3 ชม.','C':'3 ชม.-1 วัน','D':'1-7 วัน','E':'7-14 วัน','F':'1 เดือน'}

def apply_dict(c, d):
    c.area         = d.get('area','')
    c.date         = d.get('date','')
    c.seq          = d.get('seq','')
    c.job_no       = d.get('jobNo','')
    c.sap_no       = d.get('sapNo','') or None
    c.notify_time  = d.get('notifyTime','')
    c.kpi_val      = d.get('kpiVal','')
    c.response_time= d.get('responseTime','')
    c.std          = d.get('std','')
    c.approve_time = d.get('approveTime','')
    c.arrive_time  = d.get('arriveTime','')
    c.start_time   = d.get('startTime','') or d.get('arriveTime','')
    c.close_time   = d.get('closeTime','')
    c.location     = d.get('location','')
    c.problem      = d.get('problem','')
    c.solution     = d.get('solution','')
    c.reporter     = d.get('reporter','')
    c.receiver     = d.get('receiver','')
    c.technician   = d.get('technician','')
    c.kpi1         = d.get('kpi1','')
    c.kpi2         = d.get('kpi2','')
    c.kpi3         = d.get('kpi3','')
    return c

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return jsonify(ok=True)

@app.route('/api/cases', methods=['GET'])
def get_cases():
    area  = request.args.get('area')
    date  = request.args.get('date')
    month = request.args.get('month')
    q = Case.query
    if area and area != 'ALL': q = q.filter_by(area=area)
    if date:  q = q.filter_by(date=date)
    if month: q = q.filter(Case.date.like(f'{month}%'))
    cases = q.order_by(Case.date.desc(), Case.notify_time).all()
    return jsonify([c.to_dict() for c in cases])

@app.route('/api/cases', methods=['POST'])
def add_case():
    d   = request.get_json()
    sap = (d.get('sapNo','') or '').strip()
    if sap and Case.query.filter_by(sap_no=sap).first():
        return jsonify(error=f'SAP No. {sap} มีในฐานข้อมูลแล้ว'), 409
    c   = apply_dict(Case(), d)
    c.id = d.get('id') or gen_id()
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201

@app.route('/api/cases/<cid>', methods=['PUT'])
def update_case(cid):
    c = Case.query.get_or_404(cid)
    d = request.get_json()
    sap = (d.get('sapNo','') or '').strip()
    if sap and sap != c.sap_no:
        dup = Case.query.filter_by(sap_no=sap).first()
        if dup and dup.id != cid:
            return jsonify(error=f'SAP No. {sap} มีในฐานข้อมูลแล้ว'), 409
    apply_dict(c, d)
    c.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(c.to_dict())

@app.route('/api/cases/<cid>', methods=['DELETE'])
def delete_case(cid):
    c = Case.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    return jsonify(ok=True)

def _write_rows(ws, cases, start_row=8):
    for i, c in enumerate(cases):
        r   = start_row + i
        std = c.get('std','')
        ws.cell(row=r, column=1).value  = c.get('seq') or (i+1)
        ws.cell(row=r, column=2).value  = c.get('jobNo','')
        ws.cell(row=r, column=3).value  = c.get('sapNo','')
        ws.cell(row=r, column=4).value  = c.get('notifyTime','')
        ws.cell(row=r, column=5).value  = c.get('location','')
        ws.cell(row=r, column=6).value  = c.get('problem','')
        ws.cell(row=r, column=7).value  = c.get('kpiVal','')
        ws.cell(row=r, column=8).value  = c.get('responseTime','')
        ws.cell(row=r, column=9).value  = c.get('approveTime','')
        ws.cell(row=r, column=10).value = c.get('arriveTime','')
        ws.cell(row=r, column=11).value = kpi_sym(c.get('kpi1'))
        ws.cell(row=r, column=12).value = c.get('solution','')
        ws.cell(row=r, column=13).value = std
        ws.cell(row=r, column=14).value = STD_LABELS.get(std,'')
        ws.cell(row=r, column=15).value = c.get('closeTime','')
        ws.cell(row=r, column=16).value = kpi_sym(c.get('kpi2'))

@app.route('/api/export/daily', methods=['POST'])
def export_daily():
    from openpyxl import load_workbook
    d        = request.get_json()
    date_iso = d.get('date','')
    cases_all= d.get('cases',[])
    tmpl = Path('Template_Daily_Report.xlsx')
    if not tmpl.exists():
        return jsonify(error='ไม่พบ Template_Daily_Report.xlsx'), 500
    wb = load_workbook(io.BytesIO(tmpl.read_bytes()))
    areas = ['MTB','Z2','FZ','SAT1']
    date_be = fmt_be(date_iso)
    area_cases = {}
    for area in areas:
        ws    = wb[area]
        cases = sorted([c for c in cases_all if c.get('area')==area], key=lambda c:c.get('notifyTime',''))
        area_cases[area] = cases
        done  = [c for c in cases if c.get('closeTime')]
        ws.cell(row=4,column=6).value  = date_be
        ws.cell(row=4,column=11).value = len(done)
        ws.cell(row=4,column=16).value = len(cases)-len(done)
        _write_rows(ws, cases, start_row=8)
    ws_cm = wb['Daily CM']
    all_c = [c for a in areas for c in area_cases[a]]
    all_d = [c for c in all_c if c.get('closeTime')]
    ws_cm.cell(row=7,column=5).value  = date_be
    ws_cm.cell(row=13,column=8).value = len(all_c)
    ws_cm.cell(row=14,column=8).value = len(all_d)
    ws_cm.cell(row=15,column=8).value = len(all_c)-len(all_d)
    for area,(r1,r2,r3) in {'MTB':(19,20,21),'Z2':(23,24,25),'FZ':(27,28,29),'SAT1':(31,32,33)}.items():
        ac=area_cases[area]; ad=[c for c in ac if c.get('closeTime')]
        ws_cm.cell(row=r1,column=10).value=len(ac)
        ws_cm.cell(row=r2,column=10).value=len(ad)
        ws_cm.cell(row=r3,column=10).value=len(ac)-len(ad)
    k1p=sum(1 for c in all_c if c.get('kpi1')=='pass')
    k1f=sum(1 for c in all_c if c.get('kpi1')=='fail')
    k2p=sum(1 for c in all_c if c.get('kpi2')=='pass')
    k2f=sum(1 for c in all_c if c.get('kpi2')=='fail')
    ws_cm.cell(row=38,column=4).value=k1p; ws_cm.cell(row=40,column=4).value=k1f
    ws_cm.cell(row=38,column=10).value=k2p; ws_cm.cell(row=40,column=10).value=k2f
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'CM_Daily_{date_iso}.xlsx')

@app.route('/api/export/monthly', methods=['POST'])
def export_monthly():
    from openpyxl import load_workbook
    d        = request.get_json()
    month    = d.get('month','')
    cases_all= d.get('cases',[])
    tmpl = Path('Template_Month_Report.xlsx')
    if not tmpl.exists():
        return jsonify(error='ไม่พบ Template_Month_Report.xlsx'), 500
    wb = load_workbook(io.BytesIO(tmpl.read_bytes()))
    yr,mo = month.split('-')
    month_be = f"{mo}/{int(yr)+543}"
    areas = ['MTB','Z2','FZ','SAT1']
    area_cases = {}

    def write_month_rows(ws, cases):
        for i,c in enumerate(cases):
            r=3+i; std=c.get('std','')
            ws.cell(row=r,column=1).value  = c.get('seq') or (i+1)
            ws.cell(row=r,column=2).value  = c.get('jobNo','')
            ws.cell(row=r,column=3).value  = c.get('sapNo','')
            ws.cell(row=r,column=4).value  = c.get('notifyTime','')
            ws.cell(row=r,column=5).value  = c.get('location','')
            ws.cell(row=r,column=6).value  = c.get('problem','')
            ws.cell(row=r,column=7).value  = c.get('kpiVal','')
            ws.cell(row=r,column=8).value  = c.get('responseTime','')
            ws.cell(row=r,column=9).value  = c.get('approveTime','')
            ws.cell(row=r,column=10).value = c.get('arriveTime','')
            ws.cell(row=r,column=11).value = kpi_sym(c.get('kpi1'))
            ws.cell(row=r,column=12).value = c.get('solution','')
            ws.cell(row=r,column=13).value = std
            ws.cell(row=r,column=14).value = STD_LABELS.get(std,'')
            ws.cell(row=r,column=15).value = c.get('closeTime','')
            ws.cell(row=r,column=16).value = kpi_sym(c.get('kpi2'))
            ws.cell(row=r,column=17).value = kpi_sym(c.get('kpi3'))

    for area in areas:
        cases = sorted([c for c in cases_all if c.get('area')==area],
                       key=lambda c:(c.get('date',''),c.get('notifyTime','')))
        area_cases[area] = cases
        write_month_rows(wb[area], cases)

    all_sorted = sorted([c for a in areas for c in area_cases[a]],
                        key=lambda c:(c.get('date',''),c.get('area',''),c.get('notifyTime','')))
    write_month_rows(wb['ALL_ZONE'], all_sorted)

    ws_mc = wb['Month CM']
    ws_mc.cell(row=7,column=5).value = month_be
    all_c = all_sorted; all_d=[c for c in all_c if c.get('closeTime')]
    ws_mc.cell(row=13,column=8).value=len(all_c)
    ws_mc.cell(row=14,column=8).value=len(all_d)
    ws_mc.cell(row=15,column=8).value=len(all_c)-len(all_d)
    for area,(r1,r2,r3) in {'MTB':(18,19,20),'Z2':(21,22,23),'FZ':(24,25,26),'SAT1':(27,28,29)}.items():
        ac=area_cases[area]; ad=[c for c in ac if c.get('closeTime')]
        ws_mc.cell(row=r1,column=13).value=len(ac)
        ws_mc.cell(row=r2,column=13).value=len(ad)
        ws_mc.cell(row=r3,column=13).value=len(ac)-len(ad)
    k1p=sum(1 for c in all_c if c.get('kpi1')=='pass'); k1f=sum(1 for c in all_c if c.get('kpi1')=='fail')
    k2p=sum(1 for c in all_c if c.get('kpi2')=='pass'); k2f=sum(1 for c in all_c if c.get('kpi2')=='fail')
    k3p=sum(1 for c in all_c if c.get('kpi3')=='pass'); k3f=sum(1 for c in all_c if c.get('kpi3')=='fail')
    k1t=(k1p+k1f) or 1; k2t=(k2p+k2f) or 1; k3t=(k3p+k3f) or 1
    ws_mc.cell(row=39,column=3).value=k1p;  ws_mc.cell(row=39,column=5).value=round(k1p/k1t*100,1)
    ws_mc.cell(row=41,column=3).value=k1f;  ws_mc.cell(row=41,column=5).value=round(k1f/k1t*100,1)
    ws_mc.cell(row=39,column=7).value=k2p;  ws_mc.cell(row=39,column=9).value=round(k2p/k2t*100,1)
    ws_mc.cell(row=41,column=7).value=k2f;  ws_mc.cell(row=41,column=9).value=round(k2f/k2t*100,1)
    ws_mc.cell(row=39,column=12).value=k3p; ws_mc.cell(row=39,column=14).value=round(k3p/k3t*100,1)
    ws_mc.cell(row=41,column=12).value=k3f; ws_mc.cell(row=41,column=14).value=round(k3f/k3t*100,1)
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'CM_Monthly_{month}.xlsx')

if __name__ == '__main__':
    app.run(debug=True, port=5000)
