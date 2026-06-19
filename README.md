# CM Daily Report — Render.com Deploy Guide

## โครงสร้างไฟล์
```
cm_render/
├── app.py                    ← Flask app หลัก
├── requirements.txt          ← Python packages
├── render.yaml               ← Render.com auto-config
├── Template_Daily_Report.xlsx
├── Template_Month_Report.xlsx
└── templates/
    └── index.html            ← Web App (UI)
```

## วิธี Deploy บน Render.com

### ขั้นตอนที่ 1 — GitHub
1. สร้าง GitHub repository ใหม่ (private ก็ได้)
2. Upload ไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้น GitHub

### ขั้นตอนที่ 2 — Render.com
1. ไปที่ https://render.com → Sign in
2. กด **"New +"** → **"Blueprint"**
3. เลือก GitHub repo ที่สร้างไว้
4. Render จะอ่าน `render.yaml` และสร้างทุกอย่างอัตโนมัติ:
   - Web Service (Flask app)
   - PostgreSQL database

### ขั้นตอนที่ 3 — รอ Deploy
- ใช้เวลาประมาณ 3-5 นาที
- เมื่อ deploy สำเร็จจะได้ URL เช่น `https://cm-daily-report.onrender.com`

## หมายเหตุสำคัญ

### Free tier ของ Render.com
- Web service จะ **sleep** หลังไม่มีคนใช้ 15 นาที
- ครั้งแรกที่เปิดอาจใช้เวลา 30-60 วินาที (wake up)
- PostgreSQL free tier มีพื้นที่ 1GB

### Template Excel
- ไฟล์ `Template_Daily_Report.xlsx` และ `Template_Month_Report.xlsx`
  ต้องอยู่ใน root ของ project (ระดับเดียวกับ `app.py`)
- หากอัปเดต Template ให้ push ไฟล์ใหม่ขึ้น GitHub แล้ว Render จะ redeploy

### Environment Variables (ตั้งใน Render Dashboard)
| Variable | ค่า |
|----------|-----|
| SECRET_KEY | (auto-generated) |
| DATABASE_URL | (auto จาก PostgreSQL) |

## ทดสอบ Local ก่อน Deploy
```bash
pip install -r requirements.txt
python app.py
# เปิด http://localhost:5000
```
