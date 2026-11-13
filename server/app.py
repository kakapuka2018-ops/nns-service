import os, sqlite3, secrets
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

DB=os.getenv('DB_PATH','/data/nns_service.db')
app=FastAPI(title='NNS Service Multi-Master API',version='1.1.0')

def db():
    c=sqlite3.connect(DB, timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    return c

def init():
    c=db()
    c.execute('CREATE TABLE IF NOT EXISTS masters(id INTEGER PRIMARY KEY AUTOINCREMENT,tg_id INTEGER UNIQUE NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL DEFAULT "master",active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    cols={r['name'] for r in c.execute('PRAGMA table_info(clients)').fetchall()}
    if 'master_id' not in cols:
        c.execute('ALTER TABLE clients ADD COLUMN master_id INTEGER')
    c.execute('CREATE INDEX IF NOT EXISTS idx_clients_master_id ON clients(master_id)')
    c.execute('CREATE TABLE IF NOT EXISTS api_tokens(token TEXT PRIMARY KEY,master_id INTEGER NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    admins=[int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()]
    masters=[x.strip() for x in os.getenv('MASTER_IDS','').split(',') if x.strip()]
    for tg in admins:
        c.execute('INSERT OR IGNORE INTO masters(tg_id,name,role) VALUES(?,?,"admin")',(tg,f'Администратор {tg}'))
    for item in masters:
        try: tg_s,name=item.split(':',1); tg=int(tg_s)
        except ValueError: continue
        c.execute('INSERT OR IGNORE INTO masters(tg_id,name,role) VALUES(?,?,"master")',(tg,name))
    first=c.execute('SELECT id FROM masters ORDER BY CASE role WHEN "admin" THEN 0 ELSE 1 END,id LIMIT 1').fetchone()
    if first: c.execute('UPDATE clients SET master_id=? WHERE master_id IS NULL',(first['id'],))
    c.commit(); c.close()

@app.on_event('startup')
def startup(): init()

class Login(BaseModel): tg_id:int
class Transfer(BaseModel): master_id:int
class MasterIn(BaseModel): tg_id:int; name:str

def auth(x_tg_id: Optional[int], token: Optional[str]):
    c=db(); r=None
    if token:
        r=c.execute('SELECT m.* FROM api_tokens t JOIN masters m ON m.id=t.master_id WHERE t.token=? AND m.active=1',(token,)).fetchone()
    if not r and x_tg_id:
        r=c.execute('SELECT * FROM masters WHERE tg_id=? AND active=1',(x_tg_id,)).fetchone()
    c.close()
    if not r: raise HTTPException(401,'Нет доступа')
    return r

def scope(master,cid):
    if master['role']=='admin': return True
    c=db(); r=c.execute('SELECT master_id FROM clients WHERE id=?',(cid,)).fetchone(); c.close()
    return bool(r and r['master_id']==master['id'])

def require_admin(m):
    if m['role']!='admin': raise HTTPException(403,'Только администратор')

@app.get('/health')
def health(): return {'ok':True}

@app.post('/auth')
def login(data:Login):
    c=db(); m=c.execute('SELECT * FROM masters WHERE tg_id=? AND active=1',(data.tg_id,)).fetchone()
    if not m: c.close(); raise HTTPException(403,'Мастер не зарегистрирован')
    token=secrets.token_urlsafe(32); c.execute('INSERT INTO api_tokens(token,master_id) VALUES(?,?)',(token,m['id'])); c.commit(); c.close()
    return {'token':token,'master':dict(m)}

@app.get('/masters')
def masters(x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); require_admin(m)
    c=db(); rows=c.execute('SELECT id,tg_id,name,role,active FROM masters ORDER BY CASE role WHEN "admin" THEN 0 ELSE 1 END,name COLLATE NOCASE').fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/masters')
def add_master(data:MasterIn,x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); require_admin(m)
    c=db()
    try: c.execute('INSERT INTO masters(tg_id,name,role) VALUES(?,?,'"'"'master'"'"')',(data.tg_id,data.name)); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,'Такой Telegram ID уже зарегистрирован')
    c.close(); return {'ok':True}

@app.patch('/masters/{mid}/active')
def set_master_active(mid:int,x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); require_admin(m)
    c=db(); row=c.execute('SELECT role,active FROM masters WHERE id=?',(mid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Мастер не найден')
    if row['role']=='admin': c.close(); raise HTTPException(400,'Нельзя отключить администратора')
    c.execute('UPDATE masters SET active=? WHERE id=?',(0 if row['active'] else 1,mid)); c.commit(); c.close(); return {'ok':True}

@app.get('/clients')
def clients(x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); c=db()
    if m['role']=='admin': rows=c.execute('SELECT c.*,m.name master_name FROM clients c LEFT JOIN masters m ON m.id=c.master_id ORDER BY c.id').fetchall()
    else: rows=c.execute('SELECT c.*,m.name master_name FROM clients c LEFT JOIN masters m ON m.id=c.master_id WHERE c.master_id=? ORDER BY c.id',(m['id'],)).fetchall()
    c.close(); return [dict(r) for r in rows]

@app.get('/clients/{cid}')
def client(cid:int,x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token)
    if not scope(m,cid): raise HTTPException(403,'Нет доступа')
    c=db(); r=c.execute('SELECT c.*,m.name master_name FROM clients c LEFT JOIN masters m ON m.id=c.master_id WHERE c.id=?',(cid,)).fetchone()
    if not r: c.close(); raise HTTPException(404,'Клиент не найден')
    d=dict(r); d['cars']=[dict(x) for x in c.execute('SELECT * FROM cars WHERE client_id=? ORDER BY id',(cid,)).fetchall()]; c.close(); return d

@app.get('/cars/{carid}')
def car(carid:int,x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); c=db(); r=c.execute('SELECT car.*,cl.id client_id,cl.username,cl.tg_id,cl.master_id FROM cars car JOIN clients cl ON cl.id=car.client_id WHERE car.id=?',(carid,)).fetchone()
    if not r: c.close(); raise HTTPException(404,'Автомобиль не найден')
    if m['role']!='admin' and r['master_id']!=m['id']: c.close(); raise HTTPException(403,'Нет доступа')
    d=dict(r)
    d['services']=[dict(x) for x in c.execute('SELECT id,service_date,mileage,work_name,description,price FROM services WHERE car_id=? AND deleted=0 ORDER BY service_date ASC,id ASC',(carid,)).fetchall()] if table_exists(c,'services') else []
    d['parts']=[dict(x) for x in c.execute('SELECT id,name,part_number FROM parts WHERE car_id=? ORDER BY sort_order,id',(carid,)).fetchall()] if table_exists(c,'parts') else []
    d['fluids']=[dict(x) for x in c.execute('SELECT id,system,oil_name,oil_type,quantity,unit FROM fluids WHERE car_id=? ORDER BY id',(carid,)).fetchall()] if table_exists(c,'fluids') else []
    c.close(); return d

def table_exists(c,name):
    return bool(c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(name,)).fetchone())

@app.post('/clients/{cid}/transfer')
def transfer(cid:int,data:Transfer,x_tg_id:int=0,x_token:Optional[str]=Header(None)):
    m=auth(x_tg_id,x_token); require_admin(m)
    c=db(); target=c.execute('SELECT id FROM masters WHERE id=? AND active=1',(data.master_id,)).fetchone()
    if not target: c.close(); raise HTTPException(404,'Мастер не найден')
    if not c.execute('SELECT id FROM clients WHERE id=?',(cid,)).fetchone(): c.close(); raise HTTPException(404,'Клиент не найден')
    c.execute('UPDATE clients SET master_id=? WHERE id=?',(data.master_id,cid)); c.commit(); c.close(); return {'ok':True}
