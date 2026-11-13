import asyncio, logging, os, sqlite3, csv, io, urllib.request, json, html, tempfile, shutil
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN')
ADMIN_IDS={int(x) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()}
DB='nns_service.db'
logging.basicConfig(level=logging.INFO)
dp=Dispatcher(); bot=None

def conn():
    # SQLite is used from several async handlers. Give short concurrent writes
    # time to wait instead of immediately raising 'database is locked'.
    c=sqlite3.connect(DB, timeout=30.0)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    c=conn(); c.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT,tg_id INTEGER UNIQUE NOT NULL,username TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cars(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id INTEGER NOT NULL,brand TEXT NOT NULL,model TEXT NOT NULL,year INTEGER,vin TEXT,mileage INTEGER DEFAULT 0,engine TEXT,gearbox TEXT,photo_file_id TEXT,registration_number TEXT,FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS service_groups(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS works(id INTEGER PRIMARY KEY AUTOINCREMENT,group_id INTEGER NOT NULL,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1,FOREIGN KEY(group_id) REFERENCES service_groups(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT,car_id INTEGER NOT NULL,service_date TEXT NOT NULL,mileage INTEGER NOT NULL,work_name TEXT NOT NULL,description TEXT,price REAL DEFAULT 0,deleted INTEGER DEFAULT 0,FOREIGN KEY(car_id) REFERENCES cars(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS service_work_items(service_id INTEGER NOT NULL,work_id INTEGER NOT NULL,PRIMARY KEY(service_id,work_id),FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE,FOREIGN KEY(work_id) REFERENCES works(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS service_photos(id INTEGER PRIMARY KEY AUTOINCREMENT,service_id INTEGER NOT NULL,telegram_file_id TEXT NOT NULL,FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS service_history_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,car_id INTEGER NOT NULL,service_id INTEGER,photo_id INTEGER,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,FOREIGN KEY(car_id) REFERENCES cars(id) ON DELETE CASCADE,FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE,FOREIGN KEY(photo_id) REFERENCES service_photos(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS parts(id INTEGER PRIMARY KEY AUTOINCREMENT,car_id INTEGER NOT NULL,name TEXT NOT NULL,part_number TEXT NOT NULL,catalog_id INTEGER,sort_order INTEGER DEFAULT 0,FOREIGN KEY(car_id) REFERENCES cars(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS part_exclusions(car_id INTEGER NOT NULL,catalog_id INTEGER NOT NULL,PRIMARY KEY(car_id,catalog_id),FOREIGN KEY(car_id) REFERENCES cars(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS part_catalog(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS fluids(id INTEGER PRIMARY KEY AUTOINCREMENT,car_id INTEGER NOT NULL,system TEXT NOT NULL,oil_name TEXT,oil_type TEXT,quantity REAL,unit TEXT DEFAULT 'л',FOREIGN KEY(car_id) REFERENCES cars(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS vehicle_makes(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS vehicle_models(id INTEGER PRIMARY KEY AUTOINCREMENT,make_id INTEGER NOT NULL,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1,FOREIGN KEY(make_id) REFERENCES vehicle_makes(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS vehicle_engines(id INTEGER PRIMARY KEY AUTOINCREMENT,model_id INTEGER NOT NULL,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1,FOREIGN KEY(model_id) REFERENCES vehicle_models(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS vehicle_gearboxes(id INTEGER PRIMARY KEY AUTOINCREMENT,model_id INTEGER NOT NULL,name TEXT NOT NULL,sort_order INTEGER DEFAULT 0,active INTEGER DEFAULT 1,FOREIGN KEY(model_id) REFERENCES vehicle_models(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS vehicle_suppressions(kind TEXT NOT NULL,parent_key TEXT DEFAULT '',item_key TEXT NOT NULL,PRIMARY KEY(kind,parent_key,item_key));
    ''');
    part_cols={r['name'] for r in c.execute('PRAGMA table_info(parts)').fetchall()}
    if 'catalog_id' not in part_cols: c.execute('ALTER TABLE parts ADD COLUMN catalog_id INTEGER')
    if 'sort_order' not in part_cols: c.execute('ALTER TABLE parts ADD COLUMN sort_order INTEGER DEFAULT 0')
    service_cols={r['name'] for r in c.execute('PRAGMA table_info(services)').fetchall()}
    if 'deleted' not in service_cols: c.execute('ALTER TABLE services ADD COLUMN deleted INTEGER DEFAULT 0')
    cols={r['name'] for r in c.execute('PRAGMA table_info(cars)').fetchall()}
    if 'engine' not in cols: c.execute('ALTER TABLE cars ADD COLUMN engine TEXT')
    if 'gearbox' not in cols: c.execute('ALTER TABLE cars ADD COLUMN gearbox TEXT')
    if 'registration_number' not in cols: c.execute('ALTER TABLE cars ADD COLUMN registration_number TEXT')
    seed(c); migrate_service_catalog(c); normalize_vehicle_make_aliases(c); seed_part_catalog(c); migrate_part_order(c); seed_vehicle_catalog(c); sync_vehicle_database(c); normalize_vehicle_make_aliases(c); c.commit(); c.close()

def _vkey(value):
    return ' '.join((value or '').strip().casefold().split())

def _is_suppressed(c, kind, item_name, parent_name=''):
    return c.execute('SELECT 1 FROM vehicle_suppressions WHERE kind=? AND parent_key=? AND item_key=?',
                     (kind, _vkey(parent_name), _vkey(item_name))).fetchone() is not None

def _suppress(c, kind, item_name, parent_name=''):
    c.execute('INSERT OR IGNORE INTO vehicle_suppressions(kind,parent_key,item_key) VALUES(?,?,?)',
              (kind, _vkey(parent_name), _vkey(item_name)))


def simplify_engine_name(name):
    """Return a stable workshop label for an engine.
    The external catalogue contains many rows for the same engine family,
    differing only by power, gearbox, gear count, trim or marketing suffix.
    We keep the useful identity: displacement + engine family.
    """
    import re
    s=' '.join((name or '').strip().split())
    if not s:
        return s
    # Normalize decimal separator and strip bracketed technical specs.
    s=s.replace(',', '.')
    s=re.sub(r'\([^)]*(?:kW|hp|PS|CV|speed|gear|ступен)[^)]*\)', ' ', s, flags=re.I)
    # Remove power figures.
    s=re.sub(r'\b\d+(?:\.\d+)?\s*(?:kW|hp|PS|CV)\b', ' ', s, flags=re.I)
    # Remove gearbox names and gear counts. Gearbox is selected separately.
    s=re.sub(r'\b\d{1,2}\s*[- ]?(?:speed|ступен(?:ь|и|ей))\b', ' ', s, flags=re.I)
    s=re.sub(r'\b(?:manual|mechanical|automatic|automat|robotic|robot|dsg|dct|cvt|edc|powershift|s[ -]?tronic|tiptronic|multitronic|steptronic|geartronic|easytronic|quickshift|dual[ -]?clutch|at|mt|amt|m/t|a/t)\b', ' ', s, flags=re.I)
    # Common trailing horsepower number.
    s=re.sub(r'\s+\d{2,3}(?:\.0)?\s*$', '', s)
    s=' '.join(s.split()).strip(' -/,()')
    if not s:
        return name.strip()

    families=(
        'Skyactiv-X','BlueHDi','EcoBlue','EcoBoost','PureTech','MultiJet','Multijet',
        'JTDm','JTD','T-GDI','TFSI','TSI','TDI','HDi','HDI','dCi','CDTi','GDI',
        'TCe','TCE','THP','MPI','FSI','CGI','CDI','CRDi','CRDI','D-4D','D4D',
        'VVT-i','VVT','MIVEC','i-VTEC','i-DTEC','i-CTDi','SDI','PD TDI','eTSI',
        'M9R','OM642','OM651','OM646','OM640'
    )
    # Find displacement and family independently. This is deliberately stricter
    # than token matching so suffixes such as "quattro", "ultra", "S line",
    # power figures and transmission variants cannot create another engine.
    disp_matches=list(re.finditer(r'(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)(?:\s*[Ll])?(?![\d.])', s))
    for dm in disp_matches:
        disp=dm.group(1)
        tail=s[dm.end():]
        best=None
        best_pos=10**9
        for fam in families:
            fm=re.search(r'(?<![A-Za-z0-9])'+re.escape(fam)+r'(?![A-Za-z0-9])', tail, flags=re.I)
            if fm and fm.start()<best_pos:
                best=fam; best_pos=fm.start()
        if best:
            return f'{disp} {best}'

    # Engine codes without a family (e.g. M9R 780) are kept compactly.
    s=re.sub(r'\s+\d{2,3}(?:\.0)?\s*$', '', s)
    return s

PART_DEFAULTS=[
    'Фильтр масла',
    'Воздушный фильтр',
    'Салонный фильтр',
    'Топливный фильтр',
    'Ремень генератора',
    'Передние тормозные колодки',
    'Задние тормозные колодки',
    'Аккумулятор',
]

def seed_part_catalog(c):
    # Fixed universal list. It is created once and is not editable from the bot.
    # Old catalogue items from previous versions are disabled and their per-car
    # catalogue rows are removed; custom rows (catalog_id IS NULL) are preserved.
    keep_ids=[]
    for i,name in enumerate(PART_DEFAULTS):
        row=c.execute('SELECT id FROM part_catalog WHERE name=?',(name,)).fetchone()
        if row:
            cid=row['id']; c.execute('UPDATE part_catalog SET sort_order=?,active=1 WHERE id=?',(i,cid))
        else:
            c.execute('INSERT INTO part_catalog(name,sort_order,active) VALUES(?,?,1)',(name,i)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        keep_ids.append(cid)
    marks=','.join('?'*len(keep_ids))
    old=c.execute(f'SELECT id FROM part_catalog WHERE id NOT IN ({marks})',tuple(keep_ids)).fetchall()
    old_ids=[r['id'] for r in old]
    if old_ids:
        om=','.join('?'*len(old_ids))
        c.execute(f'DELETE FROM parts WHERE catalog_id IN ({om})',old_ids)
        c.execute(f'DELETE FROM part_exclusions WHERE catalog_id IN ({om})',old_ids)
        c.execute(f'UPDATE part_catalog SET active=0 WHERE id IN ({om})',old_ids)

def migrate_part_order(c):
    rows=c.execute('SELECT id,car_id,catalog_id,sort_order FROM parts ORDER BY car_id,id').fetchall()
    by_car={}
    for r in rows: by_car.setdefault(r['car_id'],[]).append(r)
    for car_id,items in by_car.items():
        for i,r in enumerate(items):
            if not r['sort_order']:
                c.execute('UPDATE parts SET sort_order=? WHERE id=?',(i,r['id']))

def sync_parts_to_all_cars(c):
    # Explicit admin action: add active catalogue items to every car, but respect
    # per-car exclusions so an item deleted inside a car never comes back.
    cars=c.execute('SELECT id FROM cars').fetchall()
    for car in cars: sync_parts_to_car(c,car['id'])

def sync_parts_to_car(c,car_id):
    cats=c.execute('SELECT id,name,sort_order FROM part_catalog WHERE active=1 ORDER BY sort_order,id').fetchall()
    max_order=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM parts WHERE car_id=?',(car_id,)).fetchone()[0]
    for cat in cats:
        excluded=c.execute('SELECT 1 FROM part_exclusions WHERE car_id=? AND catalog_id=?',(car_id,cat['id'])).fetchone()
        if excluded: continue
        exists=c.execute('SELECT id FROM parts WHERE car_id=? AND catalog_id=?',(car_id,cat['id'])).fetchone()
        if not exists:
            max_order += 1
            c.execute('INSERT INTO parts(car_id,name,part_number,catalog_id,sort_order) VALUES(?,?,?,?,?)',(car_id,cat['name'],'',cat['id'],max_order))

def seed_vehicle_catalog(c):
    # The catalog may already exist from an older bot version. In that case
    # we must still seed missing engines/gearboxes instead of returning early.
    data={
      'Alfa Romeo':['147','156','159','166','Giulia','Giulietta','Stelvio'],
      'Audi':['A1','A3','A4','A5','A6','A7','A8','Q2','Q3','Q5','Q7','Q8','TT'],
      'BMW':['1 Series','2 Series','3 Series','4 Series','5 Series','6 Series','7 Series','8 Series','X1','X2','X3','X4','X5','X6','X7','Z4'],
      'Citroën':['C1','C2','C3','C4','C4 Cactus','C5','C5 Aircross','C6','C8','Berlingo','Jumpy','Jumper','DS3','DS4','DS5','Xsara Picasso','C4 Picasso','C4 Grand Picasso'],
      'Dacia':['Logan','Sandero','Duster','Dokker','Lodgy','Jogger'],
      'Fiat':['500','500X','Panda','Punto','Bravo','Tipo','Doblo','Qubo','Ducato','Fiorino','Freemont'],
      'Ford':['Fiesta','Focus','Mondeo','Fusion','Kuga','Puma','EcoSport','Edge','Galaxy','S-Max','C-Max','Transit','Transit Connect','Ranger'],
      'Honda':['Jazz','Civic','Accord','CR-V','HR-V','FR-V','Stream','CR-Z'],
      'Hyundai':['i10','i20','i30','i40','Elantra','Sonata','Tucson','Santa Fe','Kona','ix20','ix35'],
      'Kia':['Picanto','Rio','Ceed','ProCeed','Optima','Stinger','Stonic','Sportage','Sorento','Niro','Carnival'],
      'Land Rover':['Defender','Discovery','Discovery Sport','Freelander','Range Rover','Range Rover Sport','Range Rover Evoque','Velar'],
      'Mazda':['2','3','5','6','CX-3','CX-5','CX-7','CX-9','MX-5'],
      'Mercedes-Benz':['A-Class','B-Class','C-Class','E-Class','S-Class','CLA','CLS','GLA','GLB','GLC','GLE','GLK','GLS','ML','Vito','Viano','Sprinter','Citan'],
      'Mitsubishi':['Colt','Lancer','ASX','Outlander','Pajero','L200','Eclipse Cross'],
      'Nissan':['Micra','Note','Juke','Qashqai','X-Trail','Murano','Pathfinder','Navara','NV200','NV300','NV400'],
      'Opel':['Adam','Corsa','Astra','Insignia','Meriva','Zafira','Mokka','Crossland','Grandland','Combo','Vivaro','Movano'],
      'Peugeot':['107','108','206','207','208','301','307','308','407','408','508','2008','3008','5008','Partner','Expert','Boxer','Rifter'],
      'Porsche':['718','911','Panamera','Cayenne','Macan'],
      'Renault':['Clio','Megane','Laguna','Scenic','Grand Scenic','Espace','Talisman','Captur','Kadjar','Koleos','Kangoo','Trafic','Master'],
      'SEAT':['Ibiza','Leon','Toledo','Altea','Ateca','Arona','Tarraco'],
      'Skoda':['Fabia','Scala','Rapid','Octavia','Superb','Kamiq','Karoq','Kodiaq','Yeti','Roomster'],
      'Suzuki':['Alto','Swift','Baleno','Ignis','SX4','Vitara','S-Cross','Jimny'],
      'Toyota':['Aygo','Yaris','Auris','Corolla','Avensis','Camry','C-HR','RAV4','Highlander','Land Cruiser','Hilux','Proace'],
      'Volkswagen':['Up!','Polo','Golf','Jetta','Passat','Arteon','T-Roc','T-Cross','Tiguan','Touareg','Touran','Sharan','Caddy','Transporter','Crafter'],
      'Volvo':['C30','S40','S60','S80','S90','V40','V50','V60','V70','V90','XC40','XC60','XC70','XC90'],
      'Lexus':['CT','IS','ES','GS','LS','UX','NX','RX','GX','LX'],
      'Subaru':['Impreza','Legacy','Forester','Outback','XV','WRX'],
      'Tesla':['Model 3','Model S','Model X','Model Y'],
    }
    existing_makes = {r['name']: r['id'] for r in c.execute('SELECT id,name FROM vehicle_makes').fetchall()}
    for make,models in data.items():
        mid = existing_makes.get(make)
        if mid is None:
            if _is_suppressed(c,'make',make):
                continue
            mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_makes').fetchone()[0]
            c.execute('INSERT INTO vehicle_makes(name,sort_order) VALUES(?,?)',(make,mx+1)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        existing_models = {r['name']: r['id'] for r in c.execute('SELECT id,name FROM vehicle_models WHERE make_id=?',(mid,)).fetchall()}
        for m in models:
            if m not in existing_models and not _is_suppressed(c,'model',m,make):
                mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_models WHERE make_id=?',(mid,)).fetchone()[0]
                c.execute('INSERT INTO vehicle_models(make_id,name,sort_order) VALUES(?,?,?)',(mid,m,mx+1))

    # Initial model-specific engine / gearbox catalog.  The catalog is deliberately
    # kept editable from the bot, so additional variants can be added later.
    details={
      'Peugeot 308':(['1.6 HDi','1.6 BlueHDi','1.5 BlueHDi','2.0 HDi','1.2 PureTech','1.6 THP'],['5МКПП','6МКПП','EAT6','EAT8','ETG6']),
      'Peugeot 3008':(['1.6 HDi','1.6 BlueHDi','1.5 BlueHDi','2.0 HDi','1.2 PureTech','1.6 THP'],['6МКПП','EAT6','EAT8','EAT6 EAT']),
      'Peugeot 5008':(['1.6 HDi','1.6 BlueHDi','1.5 BlueHDi','2.0 HDi','1.2 PureTech','1.6 THP'],['6МКПП','EAT6','EAT8']),
      'Peugeot 508':(['1.6 HDi','1.5 BlueHDi','2.0 HDi','1.6 PureTech','1.6 THP'],['6МКПП','EAT6','EAT8','EAT6']),
      'Citroën C4':(['1.6 HDi','1.6 BlueHDi','1.5 BlueHDi','1.2 PureTech','1.6 VTi'],['5МКПП','6МКПП','EAT6','EAT8','ETG6']),
      'Citroën C4 Cactus':(['1.2 PureTech','1.6 BlueHDi','1.5 BlueHDi','1.6 e-HDi'],['5МКПП','6МКПП','EAT6','ETG6']),
      'Citroën C4 Grand Picasso':(['1.6 HDi','1.6 BlueHDi','2.0 HDi','1.2 PureTech'],['6МКПП','EAT6','EAT8','ETG6']),
      'Renault Captur':(['0.9 TCe','1.2 TCe','1.3 TCe','1.5 dCi','1.6 dCi'],['5МКПП','6МКПП','EDC','CVT']),
      'Renault Megane':(['1.2 TCe','1.3 TCe','1.5 dCi','1.6 dCi','1.9 dCi','2.0 dCi'],['5МКПП','6МКПП','EDC','CVT']),
      'Renault Scenic':(['1.5 dCi','1.6 dCi','2.0 dCi','1.2 TCe','1.6 16V'],['6МКПП','EDC','CVT']),
      'Renault Grand Scenic':(['1.5 dCi','1.6 dCi','2.0 dCi','1.2 TCe'],['6МКПП','EDC','CVT']),
      'Renault Espace':(['1.6 dCi','2.0 dCi','2.0 TCe','2.0 Blue dCi'],['6МКПП','EDC','CVT']),
      'Renault Laguna':(['1.5 dCi','1.9 dCi','2.0 dCi','2.0 16V'],['6МКПП','AJ0','DP0','CVT']),
      'Renault Trafic':(['1.6 dCi','2.0 dCi','2.5 dCi'],['6МКПП','EDC','6АКПП']),
      'Opel Astra':(['1.4 Turbo','1.5 Diesel','1.6 CDTi','1.7 CDTi','2.0 CDTi','1.6 бензин'],['5МКПП','6МКПП','6T40','9T45','Easytronic']),
      'Opel Insignia':(['1.5 Turbo','1.6 CDTi','2.0 CDTi','2.0 Turbo'],['6МКПП','8АКПП','9АКПП','Aisin']),
      'Opel Vivaro':(['1.6 CDTi','2.0 CDTi','2.5 CDTi','2.0 Diesel'],['6МКПП','6АКПП','Easytronic']),
      'Volkswagen Polo':(['1.2 TSI','1.0 TSI','1.4 TDI','1.6 TDI','1.5 TSI'],['5МКПП','6МКПП','7DSG','6DSG']),
      'Volkswagen Golf':(['1.4 TSI','1.5 TSI','1.6 TDI','2.0 TDI','2.0 TSI','1.0 TSI'],['5МКПП','6МКПП','6DSG','7DSG']),
      'Volkswagen Passat':(['1.4 TSI','1.5 TSI','1.6 TDI','2.0 TDI','2.0 TSI'],['6МКПП','6DSG','7DSG','8АКПП']),
      'Volkswagen Tiguan':(['1.4 TSI','1.5 TSI','2.0 TSI','2.0 TDI'],['6МКПП','6DSG','7DSG','8АКПП']),
      'Volkswagen Touran':(['1.2 TSI','1.4 TSI','1.6 TDI','2.0 TDI'],['6МКПП','6DSG','7DSG']),
      'Volkswagen Caddy':(['1.6 TDI','2.0 TDI','1.4 TSI','1.5 TSI'],['5МКПП','6МКПП','6DSG','7DSG']),
      'Volkswagen Transporter':(['1.9 TDI','2.0 TDI','2.5 TDI','2.0 TSI'],['5МКПП','6МКПП','7DSG','8АКПП']),
      'Škoda Octavia':(['1.2 TSI','1.4 TSI','1.5 TSI','1.6 TDI','2.0 TDI'],['5МКПП','6МКПП','6DSG','7DSG']),
      'Škoda Superb':(['1.4 TSI','1.5 TSI','1.6 TDI','2.0 TDI','2.0 TSI'],['6МКПП','6DSG','7DSG']),
      'Škoda Kodiaq':(['1.5 TSI','2.0 TSI','2.0 TDI'],['6МКПП','6DSG','7DSG']),
      'Ford Focus':(['1.0 EcoBoost','1.5 EcoBoost','1.5 TDCi','1.6 TDCi','2.0 TDCi'],['5МКПП','6МКПП','6АКПП','8АКПП','PowerShift']),
      'Ford Mondeo':(['1.5 EcoBoost','2.0 EcoBoost','1.5 TDCi','2.0 TDCi','2.0 Hybrid'],['6МКПП','6АКПП','8АКПП','PowerShift']),
      'Ford Galaxy':(['1.6 TDCi','2.0 TDCi','2.0 EcoBlue'],['6МКПП','6АКПП','8АКПП']),
      'Ford Kuga':(['1.5 EcoBoost','1.5 TDCi','2.0 TDCi','2.0 EcoBlue'],['6МКПП','6АКПП','8АКПП','PowerShift']),
      'Ford Transit':(['2.0 TDCi','2.2 TDCi','2.4 TDCi','2.5 DI'],['5МКПП','6МКПП','6АКПП']),
      'Mercedes-Benz C-Class':(['C180','C200','C220 CDI','C250 CDI','C300','C320 CDI','C220d'],['6МКПП','7G-Tronic','9G-Tronic','5G-Tronic']),
      'Mercedes-Benz E-Class':(['E200','E220 CDI','E250 CDI','E270 CDI','E280 CDI','E300','E320 CDI','E350 CDI','E220d'],['6МКПП','7G-Tronic','9G-Tronic','5G-Tronic']),
      'Mercedes-Benz Vito':(['1.6 CDI','2.0 CDI','2.1 CDI','2.2 CDI','3.0 CDI'],['6МКПП','7G-Tronic','9G-Tronic','5G-Tronic']),
      'Mercedes-Benz Viano':(['2.0 CDI','2.2 CDI','3.0 CDI'],['6МКПП','7G-Tronic','5G-Tronic']),
      'Mercedes-Benz Sprinter':(['2.1 CDI','2.2 CDI','3.0 CDI'],['6МКПП','7G-Tronic','9G-Tronic']),
      'BMW 3 Series':(['316i','318i','320i','320d','325d','330d','335i'],['6МКПП','8HP','ZF 6HP','7DCT']),
      'BMW 5 Series':(['520i','520d','525d','530d','535d','540i'],['6МКПП','8HP','ZF 6HP']),
      'Audi A3':(['1.4 TFSI','1.5 TFSI','1.6 TDI','2.0 TDI','2.0 TFSI'],['6МКПП','6DSG','7DSG','S tronic']),
      'Audi A4':(['1.4 TFSI','1.8 TFSI','2.0 TFSI','2.0 TDI','3.0 TDI'],['6МКПП','Multitronic','S tronic','Tiptronic']),
      'Audi A6':(['2.0 TDI','2.0 TFSI','3.0 TDI','3.0 TFSI'],['6МКПП','Multitronic','S tronic','Tiptronic']),
      'Volvo XC60':(['D3','D4','D5','B4','T5','T6'],['6МКПП','8АКПП','Geartronic']),
      'Volvo XC90':(['D4','D5','B5','T5','T6','T8'],['6МКПП','8АКПП','Geartronic']),
      'Toyota Corolla':(['1.4 D-4D','1.6 Valvematic','1.8 Hybrid','2.0 Hybrid'],['6МКПП','CVT','e-CVT','4АКПП']),
      'Toyota RAV4':(['2.0 бензин','2.2 D-4D','2.5 Hybrid'],['6МКПП','CVT','e-CVT','6АКПП']),
      'Nissan Qashqai':(['1.2 DIG-T','1.3 DIG-T','1.5 dCi','1.6 dCi','2.0 бензин'],['6МКПП','CVT','Xtronic']),
      'Nissan X-Trail':(['1.6 dCi','1.7 dCi','2.0 dCi','2.0 бензин','2.5 бензин'],['6МКПП','CVT','Xtronic']),
      'Hyundai Tucson':(['1.6 GDI','1.6 T-GDI','1.6 CRDi','2.0 CRDi','2.0 бензин'],['6МКПП','7DCT','8АКПП']),
      'Kia Sportage':(['1.6 GDI','1.6 T-GDI','1.6 CRDi','2.0 CRDi','2.0 бензин'],['6МКПП','7DCT','8АКПП']),
      'Dacia Duster':(['1.0 TCe','1.2 TCe','1.3 TCe','1.5 dCi','1.5 Blue dCi','1.6 бензин'],['5МКПП','6МКПП','EDC','CVT']),
      'Dacia Logan':(['1.0 SCe','1.0 TCe','1.2 16V','1.5 dCi'],['5МКПП','6МКПП','Easy-R','CVT']),
    }
    for full_name,(engines,gearboxes) in details.items():
        if ' ' not in full_name: continue
        make,model=full_name.split(' ',1)
        # Mercedes-Benz and other multiword makes are handled by matching the longest existing make prefix.
        mm=None
        for mk in sorted(data.keys(), key=len, reverse=True):
            if full_name.startswith(mk+' '):
                make=mk; model=full_name[len(mk)+1:]; break
        rr=c.execute('SELECT id FROM vehicle_models WHERE make_id=(SELECT id FROM vehicle_makes WHERE name=?) AND name=?',(make,model)).fetchone()
        if not rr: continue
        mid=rr['id']
        existing_engines={r['name'] for r in c.execute('SELECT name FROM vehicle_engines WHERE model_id=?',(mid,)).fetchall()}
        next_engine=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_engines WHERE model_id=?',(mid,)).fetchone()[0]+1
        for name in engines:
            if name not in existing_engines:
                c.execute('INSERT INTO vehicle_engines(model_id,name,sort_order) VALUES(?,?,?)',(mid,name,next_engine)); next_engine+=1
        existing_gearboxes={r['name'] for r in c.execute('SELECT name FROM vehicle_gearboxes WHERE model_id=?',(mid,)).fetchall()}
        next_gearbox=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_gearboxes WHERE model_id=?',(mid,)).fetchone()[0]+1
        for name in gearboxes:
            if name not in existing_gearboxes:
                c.execute('INSERT INTO vehicle_gearboxes(model_id,name,sort_order) VALUES(?,?,?)',(mid,name,next_gearbox)); next_gearbox+=1


VEHICLE_DB_URL = "https://raw.githubusercontent.com/gor3a/vehicle-makes-models/main/data/csv/engines.csv"
VEHICLE_DB_CACHE = "vehicle_engines.csv"

def _normalize_transmission(value):
    v=(value or '').strip()
    if not v: return ''
    low=v.lower()
    replacements=[
        ('manual','МКПП'),('automatic','АКПП'),('cvt','CVT'),('dct','DCT'),
        ('dual clutch','DCT'),('powershift','PowerShift'),('s tronic','S tronic'),
        ('tiptronic','Tiptronic'),('multitronic','Multitronic'),('geartronic','Geartronic')
    ]
    for needle,repl in replacements:
        if needle in low:
            import re
            nums=re.findall(r'\\d+',v)
            if repl in ('МКПП','АКПП') and nums:
                return f"{nums[0]}{repl}"
            return repl
    return v

def sync_vehicle_database(c):
    """Merge a much larger public vehicle catalog into our editable local catalog.
    Source: gor3a/vehicle-makes-models, ODbL data license. Existing custom rows are kept.
    """
    try:
        if os.path.exists(VEHICLE_DB_CACHE) and os.path.getsize(VEHICLE_DB_CACHE)>1000:
            raw=open(VEHICLE_DB_CACHE,'rb').read()
        else:
            req=urllib.request.Request(VEHICLE_DB_URL,headers={'User-Agent':'NNS-Service-Bot/1.0'})
            raw=urllib.request.urlopen(req,timeout=45).read()
            with open(VEHICLE_DB_CACHE,'wb') as f: f.write(raw)
        text=raw.decode('utf-8-sig',errors='replace')
        reader=csv.DictReader(io.StringIO(text))
        makes={r['name']:r['id'] for r in c.execute('SELECT id,name FROM vehicle_makes').fetchall()}
        model_cache={(r['make_id'],r['name']):r['id'] for r in c.execute('SELECT id,make_id,name FROM vehicle_models').fetchall()}
        engine_cache={(r['model_id'],r['name']) for r in c.execute('SELECT model_id,name FROM vehicle_engines').fetchall()}
        gearbox_cache={(r['model_id'],r['name']) for r in c.execute('SELECT model_id,name FROM vehicle_gearboxes').fetchall()}
        make_order=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_makes').fetchone()[0]+1
        model_orders={}
        added_models=added_engines=added_gearboxes=0
        for row in reader:
            make=(row.get('make') or '').strip(); model=(row.get('model') or '').strip()
            if not make or not model: continue
            if make not in makes:
                if _is_suppressed(c,'make',make):
                    continue
                c.execute('INSERT INTO vehicle_makes(name,sort_order) VALUES(?,?)',(make,make_order)); makes[make]=c.execute('SELECT last_insert_rowid()').fetchone()[0]; make_order+=1
            mkid=makes[make]
            key=(mkid,model)
            if key not in model_cache:
                if _is_suppressed(c,'model',model,make):
                    continue
                if mkid not in model_orders:
                    model_orders[mkid]=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_models WHERE make_id=?',(mkid,)).fetchone()[0]+1
                c.execute('INSERT INTO vehicle_models(make_id,name,sort_order) VALUES(?,?,?)',(mkid,model,model_orders[mkid])); model_cache[key]=c.execute('SELECT last_insert_rowid()').fetchone()[0]; model_orders[mkid]+=1; added_models+=1
            mid=model_cache[key]
            eng=(row.get('engine_label') or '').strip()
            if eng and (mid,eng) not in engine_cache and not _is_suppressed(c,'engine',eng,model):
                mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_engines WHERE model_id=?',(mid,)).fetchone()[0]+1
                c.execute('INSERT INTO vehicle_engines(model_id,name,sort_order) VALUES(?,?,?)',(mid,eng,mx)); engine_cache.add((mid,eng)); added_engines+=1
            gb=_normalize_transmission(row.get('transmission'))
            if gb and (mid,gb) not in gearbox_cache and not _is_suppressed(c,'gearbox',gb,model):
                mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_gearboxes WHERE model_id=?',(mid,)).fetchone()[0]+1
                c.execute('INSERT INTO vehicle_gearboxes(model_id,name,sort_order) VALUES(?,?,?)',(mid,gb,mx)); gearbox_cache.add((mid,gb)); added_gearboxes+=1
        c.commit()
        logging.info('Vehicle DB synced: +%s models, +%s engines, +%s gearboxes',added_models,added_engines,added_gearboxes)
        return added_models,added_engines,added_gearboxes
    except Exception as e:
        logging.warning('Vehicle DB sync skipped: %s',e)
        return 0,0,0

def normalize_vehicle_make_aliases(c):
    rows=c.execute("SELECT id,name FROM vehicle_makes WHERE lower(name) IN ('skoda','škoda')").fetchall()
    if len(rows)<2:
        for r in rows:
            if r['name'].lower()=='skoda': c.execute("UPDATE vehicle_makes SET name='Škoda' WHERE id=?",(r['id'],))
        return
    canonical=next((r for r in rows if r['name']=='Škoda'), rows[0]); duplicate=next(r for r in rows if r['id']!=canonical['id'])
    c.execute("UPDATE cars SET brand='Škoda' WHERE lower(brand)='skoda'")
    dup_models=c.execute('SELECT id,name FROM vehicle_models WHERE make_id=?',(duplicate['id'],)).fetchall()
    for dm in dup_models:
        cm=c.execute('SELECT id FROM vehicle_models WHERE make_id=? AND lower(name)=lower(?)',(canonical['id'],dm['name'])).fetchone()
        if cm: c.execute('DELETE FROM vehicle_models WHERE id=?',(dm['id'],))
        else: c.execute('UPDATE vehicle_models SET make_id=? WHERE id=?',(canonical['id'],dm['id']))
    c.execute("UPDATE vehicle_makes SET name='Škoda' WHERE id=?",(canonical['id'],)); c.execute('DELETE FROM vehicle_makes WHERE id=?',(duplicate['id'],))

SERVICE_EXTRAS={
'Двигатель':[
'Замена вкладышей','Замена поршневых колец','Замена маслосъёмных колпачков','Замена сальника коленвала','Замена сальника распредвала',
'Замена датчика давления масла','Проверка компрессии','Проверка давления масла','Диагностика двигателя','Замена прокладок двигателя',
'Замена ДВС','Замена заднего сальника коленвала','Замена прокладок теплообменника ДВС','Снятие-установка поддона',
'Замена опорного кронштейна вискомуфты','Замена ремня генератора','Замена обводных роликов ремня генератора'
],
'ГРМ':[
'Замена ремня ГРМ','Замена цепи ГРМ','Замена цепи ГРМ с комплектом','Замена натяжителя цепи ГРМ','Замена роликов ГРМ','Замена цепи распредвалов'
],
'Охлаждение':[
'Замена радиатора охлаждения','Замена радиатора','Замена термостата','Замена патрубка','Замена расширительного бачка','Замена вентилятора охлаждения','Замена вентилятора','Замена помпы','Замена водяной помпы','Замена антифриза','Удаление воздуха из системы','Проверка системы охлаждения','Опрессовка системы охлаждения','Замена крышки расширительного бачка','Замена датчика температуры'
],
'Топливная система':[
'Диагностика топливной системы','Замена ТНВД','Замена форсунок','Снятие/установка форсунок','Чистка форсунок','Замена медных колец под форсунками','Замена прокладок форсунок'
],
'Система отработавших газов':[
'Замена клапана EGR','Замена EGR','Чистка EGR','Удаление EGR','Удаление DPF','Регенерация DPF','Замена датчика DPF','Чистка сажевого фильтра','Чистка DPF','Замена выпускного коллектора'
],
'Впуск и наддув':[
'Замена дроссельной заслонки','Чистка дроссельной заслонки','Чистка коллектора','Замена впускного коллектора','Замена интеркулера','Замена турбины','Снятие/установка турбины','Чистка впуска'
],
'Ходовая':['Замена втулок стабилизатора','Замена стоек стабилизатора','Замена опор амортизаторов','Замена подрамника','Замена наружного ШРУСа','Замена внутреннего ШРУСа','Замена полуоси','Замена сайлентблоков подрамника','Проверка ходовой','Диагностика подвески'],
'Рулевое управление':['Замена рулевой рейки','Замена рулевого карданчика','Замена рулевого наконечника левый','Замена рулевого наконечника правый','Замена рулевой тяги левая','Замена рулевой тяги правая'],
'Передняя ходовка':['Замена переднего рычага левый','Замена переднего рычага правый','Замена переднего амортизатора левый','Замена переднего амортизатора правый','Замена передней пружины левая','Замена передней пружины правая','Замена передней шаровой левая','Замена передней шаровой правая','Замена переднего ступичного подшипника левый','Замена переднего ступичного подшипника правый','Замена передней ступицы левая','Замена передней ступицы правая'],
'Задняя ходовка':['Замена заднего рычага левый','Замена заднего рычага правый','Замена заднего амортизатора левый','Замена заднего амортизатора правый','Замена задней пружины левая','Замена задней пружины правая','Замена задней ступичного подшипника левый','Замена задней ступичного подшипника правый','Замена задней ступицы левая','Замена задней ступицы правая'],
'Тормоза':['Замена передних тормозных колодок','Замена задних тормозных колодок','Замена передних тормозных дисков','Замена задних тормозных дисков','Замена направляющих суппорта','Замена ремкомплекта суппорта','Замена главного тормозного цилиндра','Замена вакуумного усилителя','Замена датчика ABS','Диагностика ABS/ESP'],
'КПП и сцепление':['Замена масла МКПП','Замена масла АКПП','Замена масла DSG/DCT','Замена масла вариатора','Замена фильтра АКПП','Замена сцепления','Замена комплекта сцепления','Замена двухмассового маховика','Замена выжимного подшипника','Замена главного цилиндра сцепления','Замена рабочего цилиндра сцепления','Ремонт мехатроника','Замена мехатроника','Адаптация сцепления','Адаптация КПП','Диагностика КПП'],
'Кондиционер':['Диагностика кондиционера','Заправка кондиционера','Проверка герметичности','Вакуумирование системы','Замена компрессора кондиционера','Замена муфты компрессора','Замена радиатора кондиционера','Замена конденсора','Замена испарителя','Замена осушителя','Чистка испарителя'],
'Кузов':['Замена лобового стекла','Замена бокового стекла','Замена фары','Замена фонаря','Замена зеркала','Ремонт стеклоподъёмника','Замена стеклоподъёмника','Ремонт центрального замка','Замена замка','Ремонт двери','Регулировка двери','Замена капота','Замена крыла','Замена бампера','Ремонт бампера','Полировка фар','Установка защиты']}


def migrate_service_catalog(c):
    """Keep the editable work catalog, but split the large engine bucket into
    practical systems. Existing works are moved instead of deleted, so custom
    work history and catalog entries remain intact."""
    for name in ('Шины','Электрика и диагностика','Другое'):
        c.execute('UPDATE service_groups SET active=0 WHERE name=?',(name,))
    requested=['Малый сервис','Двигатель','ГРМ','Охлаждение','Топливная система','Система отработавших газов','Впуск и наддув','Рулевое управление','Передняя ходовка','Задняя ходовка','Ходовая','Тормоза','КПП и сцепление','Кондиционер','Кузов']
    for idx,name in enumerate(requested):
        r=c.execute('SELECT id FROM service_groups WHERE name=?',(name,)).fetchone()
        if not r:
            c.execute('INSERT INTO service_groups(name,sort_order,active) VALUES(?,?,1)',(name,idx))
        else:
            c.execute('UPDATE service_groups SET active=1 WHERE id=?',(r['id'],))
    # Force the requested order, preserving the relative order of any other hidden groups.
    for idx,name in enumerate(requested):
        c.execute('UPDATE service_groups SET sort_order=? WHERE name=?',(idx,name))

    # Small service remains the first bucket.
    ms=c.execute('SELECT id FROM service_groups WHERE name=?',('Малый сервис',)).fetchone()
    if ms:
        small=['Замена масла','Замена масляного фильтра','Замена воздушного фильтра','Замена топливного фильтра','Замена фильтра салона']
        for name in small:
            r=c.execute('SELECT id FROM works WHERE group_id=? AND lower(name)=lower(?)',(ms['id'],name)).fetchone()
            if not r:
                mx=c.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM works WHERE group_id=?',(ms['id'],)).fetchone()[0]
                c.execute('INSERT INTO works(group_id,name,sort_order,active) VALUES(?,?,?,1)',(ms['id'],name,mx))

    # Move known works out of the old engine bucket. The first matching name is
    # sufficient because the catalog historically used unique work names.
    for target,items in SERVICE_EXTRAS.items():
        gid=c.execute('SELECT id FROM service_groups WHERE name=?',(target,)).fetchone()['id']
        for name in items:
            rows=c.execute('SELECT id,group_id FROM works WHERE lower(name)=lower(?)',(name,)).fetchall()
            if rows:
                # Prefer an existing row in the target; otherwise move the first row.
                target_row=next((r for r in rows if r['group_id']==gid),None)
                if target_row is None:
                    c.execute('UPDATE works SET group_id=? WHERE id=?',(gid,rows[0]['id']))
            else:
                mx=c.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM works WHERE group_id=?',(gid,)).fetchone()[0]
                c.execute('INSERT INTO works(group_id,name,sort_order,active) VALUES(?,?,?,1)',(gid,name,mx))

    # Remove engine duplicates that belong clearly to other buckets.
    other_names={n.casefold() for g,items in SERVICE_EXTRAS.items() if g!='Двигатель' for n in items}
    eng=c.execute('SELECT id FROM service_groups WHERE name=?',('Двигатель',)).fetchone()
    if eng:
        for n in other_names:
            c.execute('UPDATE works SET group_id=(SELECT id FROM service_groups WHERE name=? LIMIT 1) WHERE 0',('Двигатель',))
        # The moves above already relocated matching rows; no deletion is needed.


def seed(c):
    defaults={
    'Двигатель':['Замена масла','Замена масляного фильтра','Замена воздушного фильтра','Замена топливного фильтра','Замена свечей зажигания','Замена свечей накала','Замена ремня ГРМ','Замена цепи ГРМ','Замена роликов ГРМ','Замена водяной помпы','Замена антифриза','Замена термостата','Замена прокладки клапанной крышки','Замена прокладки ГБЦ','Регулировка клапанов','Замена форсунок','Снятие/установка форсунок','Чистка форсунок','Замена турбины','Снятие/установка турбины','Замена EGR','Чистка EGR','Чистка впуска','Чистка DPF','Диагностика двигателя'],
    'КПП и сцепление':['Замена масла МКПП','Замена масла АКПП','Замена масла DSG/DCT','Замена фильтра АКПП','Замена сцепления','Замена двухмассового маховика','Замена выжимного подшипника','Ремонт/замена мехатроника','Диагностика КПП'],
    'Тормоза':['Замена передних колодок','Замена задних колодок','Замена передних дисков','Замена задних дисков','Замена тормозной жидкости','Прокачка тормозов','Ремонт суппорта','Замена тормозного шланга','Диагностика тормозной системы'],
    'Ходовая':['Замена стойки амортизатора','Замена амортизаторов','Замена пружин','Замена шаровой','Замена сайлентблоков','Замена рычага','Замена рулевого наконечника','Замена рулевой тяги','Замена ступичного подшипника','Замена ступицы','Замена ШРУСа','Замена пыльника ШРУСа','Развал-схождение'],
    'Электрика и диагностика':['Компьютерная диагностика','Сброс ошибок','Поиск неисправности','Ремонт проводки','Замена аккумулятора','Замена генератора','Ремонт генератора','Замена стартера','Ремонт стартера','Установка камеры','Установка магнитолы','Установка парктроников'],
    'Кондиционер':['Диагностика кондиционера','Заправка кондиционера','Проверка герметичности','Замена компрессора','Замена радиатора кондиционера','Замена осушителя','Чистка системы кондиционирования'],
    'Охлаждение':['Замена радиатора','Замена термостата','Замена патрубка','Замена расширительного бачка','Замена вентилятора','Замена помпы','Замена антифриза','Удаление воздуха из системы'],
    'Кузов':['Замена фары','Замена фонаря','Замена зеркала','Ремонт стеклоподъёмника','Замена стеклоподъёмника','Ремонт центрального замка','Замена замка','Полировка фар','Установка защиты'],
    'Шины':['Снятие/установка колёс','Шиномонтаж','Балансировка','Ремонт прокола','Ремонт шины','Сезонная замена колёс'],
    'Другое':['Технический осмотр','Подготовка к техосмотру','Предпродажная диагностика','Проверка автомобиля перед покупкой','Долив технических жидкостей','Мойка двигателя','Другое']}
    if c.execute('SELECT COUNT(*) FROM service_groups').fetchone()[0]: return
    for gi,(g,ws) in enumerate(defaults.items()):
        c.execute('INSERT INTO service_groups(name,sort_order) VALUES(?,?)',(g,gi)); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        for wi,w in enumerate(ws): c.execute('INSERT INTO works(group_id,name,sort_order) VALUES(?,?,?)',(gid,w,wi))

def is_admin(uid): return uid in ADMIN_IDS

def upsert_client(message, start_payload=None):
    uid=message.from_user.id; username=message.from_user.username; c=conn()
    # Безопасная привязка по персональной ссылке:
    # ссылка НИКОГДА не перепривязывает уже существующий Telegram-аккаунт к другому клиенту.
    # Если аккаунт ещё не связан ни с одним клиентом, он может привязаться только к
    # существующему клиенту с временным (отрицательным) tg_id.
    row=c.execute('SELECT id FROM clients WHERE tg_id=?',(uid,)).fetchone()
    if row:
        c.execute('UPDATE clients SET username=? WHERE id=?',(username,row['id'])); cid=row['id']
        c.commit(); c.close(); return cid

    if start_payload and start_payload.startswith('client_'):
        try: cid=int(start_payload.split('_',1)[1])
        except: cid=None
        if cid:
            target=c.execute('SELECT id,tg_id FROM clients WHERE id=?',(cid,)).fetchone()
            if target:
                # Только новый/ещё не привязанный аккаунт может занять клиентскую ссылку.
                if int(target['tg_id']) < 0:
                    c.execute('UPDATE clients SET tg_id=?,username=? WHERE id=?',(uid,username,cid)); c.commit(); c.close(); return cid
                # Клиент уже привязан к другому Telegram-аккаунту — не трогаем его.
                c.close()
                return None

    manual=None
    if username: manual=c.execute('SELECT id FROM clients WHERE lower(username)=lower(?) AND tg_id<0',(username,)).fetchone()
    if manual:
        c.execute('UPDATE clients SET tg_id=?,username=? WHERE id=?',(uid,username,manual['id'])); cid=manual['id']
    else:
        c.execute('INSERT INTO clients(tg_id,username,created_at) VALUES(?,?,?)',(uid,username,datetime.now().isoformat())); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.commit(); c.close(); return cid
    if row:
        c.execute('UPDATE clients SET username=? WHERE id=?',(username,row['id'])); cid=row['id']
    else:
        manual=None
        if username: manual=c.execute('SELECT id FROM clients WHERE lower(username)=lower(?) AND tg_id<0',(username,)).fetchone()
        if manual:
            c.execute('UPDATE clients SET tg_id=?,username=? WHERE id=?',(uid,username,manual['id'])); cid=manual['id']
        else:
            c.execute('INSERT INTO clients(tg_id,username,created_at) VALUES(?,?,?)',(uid,username,datetime.now().isoformat())); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.commit(); c.close(); return cid

def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data=='sync_vehicle_catalog')
async def sync_vehicle_catalog_cb(call:CallbackQuery):
    c=conn(); a,b,d=sync_vehicle_database(c); c.close()
    await call.answer(f'Добавлено: {b} моторов, {d} КПП, {a} моделей',show_alert=True)

def admin_menu(): return ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='👥 Клиенты'),KeyboardButton(text='🚗 Автомобили')],
    [KeyboardButton(text='➕ Добавить клиента'),KeyboardButton(text='🚗 Справочник автомобилей')],
    [KeyboardButton(text='📋 Последние работы'),KeyboardButton(text='⚙️ Справочник работ')],
    [KeyboardButton(text='🛠 Система')],
],resize_keyboard=True)

async def system_menu(message):
    await message.answer(
        '🛠 <b>Система</b>\n\nЗдесь находятся резервное копирование и восстановление базы данных.\n\n🔗 Перепривязка клиента — отдельный пункт для исправления Telegram-привязки клиента.',
        parse_mode='HTML',
        reply_markup=kb([
            [InlineKeyboardButton(text='💾 Создать резервную копию', callback_data='sys_backup')],
            [InlineKeyboardButton(text='♻️ Восстановить резервную копию', callback_data='sys_restore')],
            [InlineKeyboardButton(text='🔗 Перепривязать клиента', callback_data='sys_rebind')],
            [InlineKeyboardButton(text='← Назад', callback_data='sys_back')],
        ])
    )
def client_menu(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='🚗 Мои автомобили')],[KeyboardButton(text='➕ Добавить автомобиль'),KeyboardButton(text='📖 Сервисная книжка')]],resize_keyboard=True)
def back_row(data='noop'): return [InlineKeyboardButton(text='← Назад',callback_data=data)]
def cancel_markup(callback_data='fsm_back'): return kb([[InlineKeyboardButton(text='← Назад',callback_data=callback_data)]])
def skip_markup(callback_data): return kb([[InlineKeyboardButton(text='⏭️ Пропустить',callback_data=callback_data)],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]])

class AddCar(StatesGroup): brand=State(); model=State(); year=State(); engine=State(); gearbox=State(); vin=State(); registration=State(); mileage=State(); photo=State()
class AdminAddCar(StatesGroup): brand=State(); model=State(); year=State(); engine=State(); gearbox=State(); vin=State(); registration=State(); mileage=State(); photo=State()
class AddClient(StatesGroup): username=State()
class ClientSearch(StatesGroup): query=State()
class AddService(StatesGroup): car_id=State(); group_id=State(); selected=State(); work_search=State(); mileage=State(); description=State(); price=State(); photos=State()
class Catalog(StatesGroup): group_name=State(); work_name=State(); edit_name=State(); work_order_num=State()
class PartCatalog(StatesGroup): name=State()
class AddPart(StatesGroup): name=State(); number=State()
class AddFluid(StatesGroup): system=State(); oil_name=State(); oil_type=State(); quantity=State()
class VehicleCatalogEdit(StatesGroup): make=State(); model=State(); engine=State(); gearbox=State()
class CarPhoto(StatesGroup): photo=State()
class EditCar(StatesGroup): brand=State(); model=State(); year=State(); engine=State(); gearbox=State(); vin=State(); registration=State(); mileage=State()
class HistorySearch(StatesGroup): query=State(); car_id=State()
class RebindClient(StatesGroup): client_id=State(); telegram_id=State()
class RestoreBackup(StatesGroup): waiting_file=State(); confirm=State()

# Main-menu buttons must always interrupt an unfinished FSM input step.
# Without this guard, a stale state such as AddClient.username can consume
# subsequent menu clicks as plain text (e.g. «🚗 Автомобили» is treated as
# a username), leaving the user stuck until another flow happens to clear FSM.
MAIN_MENU_TEXTS = {
    '👥 Клиенты',
    '🚗 Автомобили',
    '➕ Добавить клиента',
    '🚗 Справочник автомобилей',
    '📋 Последние работы',
    '⚙️ Справочник работ',
    '🚗 Мои автомобили',
    '➕ Добавить автомобиль',
    '📖 Сервисная книжка',
    '➕ Добавить работу',
    '🛠 Система',
}

async def create_db_backup(path):
    src=sqlite3.connect(DB, timeout=30.0)
    try:
        dst=sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

def validate_backup(path):
    c=sqlite3.connect(path, timeout=10.0)
    try:
        ok=c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required={'clients','cars','services','works','service_groups'}
        return ok and required.issubset(tables)
    finally:
        c.close()

@dp.callback_query(F.data=='sys_back')
async def sys_back(call:CallbackQuery,state:FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer('Главное меню NNS Service 👇',reply_markup=admin_menu())
    await call.answer()

@dp.callback_query(F.data=='sys_backup')
async def sys_backup(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    fd,path=tempfile.mkstemp(prefix='nns_backup_',suffix='.db'); os.close(fd)
    try:
        await create_db_backup(path)
        stamp=datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        await call.message.answer_document(__import__('aiogram').types.FSInputFile(path),caption=f'💾 Резервная копия NNS Service\n{stamp}')
        await call.answer('Резервная копия создана')
    except Exception as e:
        logging.exception('backup failed')
        await call.answer('Не удалось создать резервную копию',show_alert=True)
    finally:
        try: os.remove(path)
        except OSError: pass

@dp.callback_query(F.data=='sys_restore')
async def sys_restore(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(RestoreBackup.waiting_file)
    await call.message.answer('♻️ Отправь сюда файл резервной копии <b>.db</b>.\n\nПеред восстановлением бот автоматически сохранит текущую базу.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Отмена',callback_data='sys_restore_cancel')]]))
    await call.answer()

@dp.callback_query(F.data=='sys_restore_cancel')
async def sys_restore_cancel(call:CallbackQuery,state:FSMContext):
    await state.clear(); await call.message.answer('Отменено.',reply_markup=admin_menu()); await call.answer()

@dp.message(RestoreBackup.waiting_file,F.document)
async def restore_file(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    name=(message.document.file_name or '').lower()
    if not name.endswith('.db'):
        await message.answer('❌ Нужен файл базы данных .db'); return
    path=tempfile.mktemp(prefix='nns_restore_',suffix='.db')
    try:
        f=await bot.get_file(message.document.file_id)
        await bot.download_file(f.file_path,path)
        if not validate_backup(path):
            await message.answer('❌ Файл не прошёл проверку целостности или это не база NNS Service.'); return
        await state.update_data(restore_path=path)
        await state.set_state(RestoreBackup.confirm)
        await message.answer('⚠️ <b>ВНИМАНИЕ</b>\n\nТекущая база будет заменена данными из этого файла. Перед заменой будет создана автоматическая аварийная копия текущей базы.\n\nПродолжить?',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='❌ Да, восстановить',callback_data='sys_restore_confirm')],[InlineKeyboardButton(text='← Отмена',callback_data='sys_restore_cancel')]]))
    except Exception:
        logging.exception('restore upload failed')
        try: os.remove(path)
        except OSError: pass
        await message.answer('❌ Не удалось прочитать файл резервной копии.')

@dp.callback_query(F.data=='sys_restore_confirm')
async def sys_restore_confirm(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    d=await state.get_data(); path=d.get('restore_path')
    if not path or not os.path.exists(path):
        await state.clear(); await call.answer('Файл резервной копии не найден',show_alert=True); return
    safety=tempfile.mktemp(prefix='nns_before_restore_',suffix='.db')
    try:
        await create_db_backup(safety)
        # Also send the pre-restore database to the admin, so the rollback copy
        # is available even if the bot host is later replaced or lost.
        stamp=datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        await call.message.answer_document(__import__('aiogram').types.FSInputFile(safety),caption=f'🛡 Аварийная копия базы перед восстановлением\n{stamp}')
        # Replace the live DB atomically after validation and safety backup.
        os.replace(path,DB)
        await state.clear()
        await call.message.answer('✅ Резервная копия восстановлена.\n\nПредыдущая база отправлена тебе отдельным файлом перед восстановлением.',reply_markup=admin_menu())
        await call.answer('Восстановлено')
    except Exception:
        logging.exception('restore failed')
        if os.path.exists(safety):
            try: shutil.copy2(safety,DB)
            except Exception: logging.exception('failed to restore safety DB')
        await state.clear()
        await call.answer('❌ Не удалось восстановить базу',show_alert=True)
    finally:
        for p in (path,safety):
            try: os.remove(p)
            except OSError: pass

@dp.message(RestoreBackup.waiting_file)
async def restore_file_wrong(message:Message):
    await message.answer('Отправь именно файл резервной копии .db.')

@dp.callback_query(F.data=='sys_rebind')
async def sys_rebind(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    c=conn(); rows=c.execute('SELECT id,username,tg_id FROM clients ORDER BY username COLLATE NOCASE,id').fetchall(); c.close()
    buttons=[]
    for r in rows:
        label=f'#{r["id"]} @{r["username"]}' if r['username'] else f'#{r["id"]} Telegram ID {r["tg_id"]}'
        buttons.append([InlineKeyboardButton(text=label,callback_data=f'sys_rebind_client:{r["id"]}')])
    buttons.append([InlineKeyboardButton(text='← Назад',callback_data='sys_back')])
    await call.message.edit_text('🔗 <b>Перепривязка клиента</b>\n\nВыбери клиента, которому нужно назначить правильный Telegram ID.',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()

@dp.callback_query(F.data.startswith('sys_rebind_client:'))
async def sys_rebind_client(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    cid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT id,username,tg_id FROM clients WHERE id=?',(cid,)).fetchone(); c.close()
    if not r: await call.answer('Клиент не найден',show_alert=True); return
    await state.set_state(RebindClient.telegram_id); await state.update_data(client_id=cid)
    current=f'@{r["username"]}' if r['username'] else str(r['tg_id'])
    await call.message.edit_text(f'🔗 Клиент #{cid} {current}\n\nВведи правильный <b>Telegram ID</b> этого клиента.\n\nНапример: <code>123456789</code>',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Отмена',callback_data='sys_rebind')]])); await call.answer()

@dp.message(RebindClient.telegram_id)
async def sys_rebind_save(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    raw=(message.text or '').strip().replace(' ','')
    if raw.startswith('@'): raw=raw[1:]
    if not raw.isdigit() or int(raw)<=0:
        await message.answer('❌ Нужен числовой Telegram ID. Например: 123456789'); return
    new_tg=int(raw); d=await state.get_data(); cid=int(d['client_id'])
    c=conn(); existing=c.execute('SELECT id FROM clients WHERE tg_id=? AND id<>?',(new_tg,cid)).fetchone()
    if existing:
        c.close(); await message.answer(f'❌ Этот Telegram ID уже привязан к клиенту #{existing["id"]}. Сначала освободи эту привязку.'); return
    c.execute('UPDATE clients SET tg_id=? WHERE id=?',(new_tg,cid)); c.commit(); c.close(); await state.clear()
    await message.answer(f'✅ Клиент #{cid} перепривязан к Telegram ID {new_tg}.',reply_markup=admin_menu())

@dp.message(F.text.in_(MAIN_MENU_TEXTS))
async def main_menu_interrupt(message:Message,state:FSMContext):
    # Do not let an unfinished FSM state swallow a normal menu command.
    await state.clear()
    text=message.text
    if text=='👥 Клиенты':
        await clients(message); return
    if text=='🚗 Автомобили':
        await all_cars(message); return
    if text=='➕ Добавить клиента':
        await add_client(message,state); return
    if text=='🚗 Справочник автомобилей':
        await vehicle_catalog_menu(message); return
    if text=='📋 Последние работы':
        await recent(message); return
    if text=='⚙️ Справочник работ':
        await catalog(message); return
    if text=='🚗 Мои автомобили':
        await my_cars(message); return
    if text=='➕ Добавить автомобиль':
        await add_car_start(message,state); return
    if text=='📖 Сервисная книжка':
        await client_book(message); return
    if text=='➕ Добавить работу':
        await admin_service_global(message,state); return
    if text=='🛠 Система':
        await system_menu(message); return

@dp.callback_query(F.data.in_({'fsm_cancel','fsm_back'}))
async def fsm_back(call:CallbackQuery,state:FSMContext):
    # Внутри добавления обслуживания кнопка «Назад» должна возвращать
    # на предыдущий шаг, а не сбрасывать весь FSM и отправлять в главное меню.
    current=await state.get_state()
    d=await state.get_data()
    if current==AddService.work_search.state:
        await state.set_state(AddService.group_id)
        await state.update_data(search_query='')
        await render_service_categories(call,state)
        await call.answer()
        return
    if current==AddService.mileage.state:
        await state.set_state(AddService.group_id)
        if d.get('group_id'):
            await render_work_picker(call,state)
        else:
            await render_service_categories(call,state)
        await call.answer()
        return
    if current==AddService.description.state:
        await state.set_state(AddService.mileage)
        await call.message.edit_text('💾 <b>Пробег на момент обслуживания, км?</b>\n\nЕсли пробег не знаете — нажмите «Пропустить».',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='mileage_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]))
        await call.answer(); return
    if current==AddService.price.state:
        await state.set_state(AddService.description)
        await call.message.edit_text('📝 <b>Описание</b>\nЕсли не нужно — нажмите «Пропустить».',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='description_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]))
        await call.answer(); return
    if current==AddService.photos.state:
        await state.set_state(AddService.price)
        await call.message.edit_text('💶 <b>Общая стоимость</b>, €\nЕсли не указывать — нажмите «Пропустить».',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='price_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]))
        await call.answer(); return
    await state.clear()
    try:
        await call.message.edit_text('Главное меню NNS Service 👇',reply_markup=admin_menu() if is_admin(call.from_user.id) else client_menu())
    except Exception:
        try: await call.message.edit_caption(caption='Главное меню NNS Service 👇',reply_markup=kb([]))
        except Exception: pass
    await call.answer()

@dp.message(CommandStart())
async def start(message:Message,state:FSMContext):
    await state.clear()
    payload=(message.text or '').split(maxsplit=1)[1] if ' ' in (message.text or '') else None
    cid=upsert_client(message,payload)
    if payload and payload.startswith('client_') and not is_admin(message.from_user.id):
        if cid is None:
            await message.answer('⚠️ Этот Telegram-аккаунт уже привязан к другому клиенту. Персональная ссылка другого клиента привязку не меняет.',reply_markup=client_menu()); return
        await message.answer('👋 Ваша сервисная книжка NNS Service подключена. Откройте «🚗 Мои автомобили».',reply_markup=client_menu()); return
    await message.answer('Добро пожаловать в NNS Service 🚗\nЭлектронная сервисная книжка.',reply_markup=admin_menu() if is_admin(message.from_user.id) else client_menu())

@dp.message(F.text=='🚗 Мои автомобили')
async def my_cars(message:Message):
    # При возврате в список машин полностью закрываем старую сервисную книжку,
    # чтобы её старые callback-кнопки не могли срабатывать поверх нового экрана.
    await _clear_history_messages_for_chat(message.chat.id)
    cid=upsert_client(message); c=conn(); rows=c.execute('SELECT * FROM cars WHERE client_id=? ORDER BY brand COLLATE NOCASE, model COLLATE NOCASE, id',(cid,)).fetchall(); c.close()
    if not rows: await message.answer('Автомобилей пока нет.'); return
    await message.answer('🚗 Выберите автомобиль:',reply_markup=kb([[InlineKeyboardButton(text=f"🚗 {r['brand']} {r['model']}",callback_data=f"car:{r['id']}")] for r in rows]))

@dp.message(F.text=='📖 Сервисная книжка')
async def client_book(message:Message): await my_cars(message)

async def render_car_card_message(call:CallbackQuery, car_id:int):
    c=conn(); r=c.execute('SELECT c.*,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r: return False
    text=f"🚗 {r['brand']} {r['model']}\n"+(f"Год: {r['year']}\n" if r['year'] else '')+(f"Двигатель: {r['engine']}\n" if r['engine'] else '')+(f"КПП: {r['gearbox']}\n" if r['gearbox'] else '')+(f"VIN: {r['vin']}\n" if r['vin'] else '')+(f"Гос. номер: {r['registration_number']}\n" if r['registration_number'] else '')+f"Пробег: {r['mileage']:,} км".replace(',',' ')
    if is_admin(call.from_user.id):
        buttons=[[InlineKeyboardButton(text='📖 Сервисная книжка',callback_data=f'history:{car_id}')],[InlineKeyboardButton(text='🔧 Добавить работы',callback_data=f'servicecar:{car_id}')],[InlineKeyboardButton(text='🧰 Запчасти',callback_data=f'parts:{car_id}')],[InlineKeyboardButton(text='🛢️ Масла',callback_data=f'fluids:{car_id}')],[InlineKeyboardButton(text='✏️ Изменить данные',callback_data=f'editcar:{car_id}')],[InlineKeyboardButton(text='📷 Добавить / изменить фото',callback_data=f'carphoto:{car_id}')],[InlineKeyboardButton(text='🗑 Удалить машину',callback_data=f'confirm_delete_car:{car_id}')]]
    else:
        buttons=[[InlineKeyboardButton(text='📖 Сервисная книжка',callback_data=f'history:{car_id}')],[InlineKeyboardButton(text='🛢️ Масла',callback_data=f'fluids:{car_id}')]]
    buttons.append([InlineKeyboardButton(text='← Назад', callback_data='back_cars' if is_admin(call.from_user.id) else 'back_my_cars')])
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=text,reply_markup=kb(buttons))
        else:
            await call.message.edit_text(text,reply_markup=kb(buttons))
        return True
    except Exception:
        return False

@dp.callback_query(F.data.startswith('car:'))
async def car_card(call:CallbackQuery):
    car_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT c.*,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id): await call.answer('Нет доступа',show_alert=True); return
    text=f"🚗 {r['brand']} {r['model']}\n"+(f"Год: {r['year']}\n" if r['year'] else '')+(f"Двигатель: {r['engine']}\n" if r['engine'] else '')+(f"КПП: {r['gearbox']}\n" if r['gearbox'] else '')+(f"VIN: {r['vin']}\n" if r['vin'] else '')+(f"Гос. номер: {r['registration_number']}\n" if r['registration_number'] else '')+f"Пробег: {r['mileage']:,} км".replace(',',' ')
    if is_admin(call.from_user.id):
        buttons=[[InlineKeyboardButton(text='📖 Сервисная книжка',callback_data=f'history:{car_id}')],[InlineKeyboardButton(text='🔧 Добавить работы',callback_data=f'servicecar:{car_id}')],[InlineKeyboardButton(text='🧰 Запчасти',callback_data=f'parts:{car_id}')],[InlineKeyboardButton(text='🛢️ Масла',callback_data=f'fluids:{car_id}')],[InlineKeyboardButton(text='✏️ Изменить данные',callback_data=f'editcar:{car_id}')],[InlineKeyboardButton(text='📷 Добавить / изменить фото',callback_data=f'carphoto:{car_id}')],[InlineKeyboardButton(text='🗑 Удалить машину',callback_data=f'confirm_delete_car:{car_id}')]]
    else:
        buttons=[[InlineKeyboardButton(text='📖 Сервисная книжка',callback_data=f'history:{car_id}')],[InlineKeyboardButton(text='🛢️ Масла',callback_data=f'fluids:{car_id}')]]
    buttons.append([InlineKeyboardButton(text='← Назад', callback_data='back_cars' if is_admin(call.from_user.id) else 'back_my_cars')])
    if r['photo_file_id']: await call.message.answer_photo(r['photo_file_id'],caption=text,reply_markup=kb(buttons))
    else: await call.message.edit_text(text,reply_markup=kb(buttons)) if not call.message.photo else await call.message.answer(text,reply_markup=kb(buttons))
    await call.answer()

@dp.callback_query(F.data.startswith('editcar:'))
async def editcar_start(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer('Доступно только сервису',show_alert=True); return
    car_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT c.*,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id):
        await call.answer('Нет доступа',show_alert=True); return
    await state.clear(); await state.update_data(car_id=car_id)
    buttons=[[InlineKeyboardButton(text='🚗 Марка / модель',callback_data='editcar_field_model')],
              [InlineKeyboardButton(text='📅 Год',callback_data='editcar_field_year')],
              [InlineKeyboardButton(text='🔧 Двигатель',callback_data='editcar_field_engine')],
              [InlineKeyboardButton(text='⚙️ КПП',callback_data='editcar_field_gearbox')],
              [InlineKeyboardButton(text='🔤 VIN',callback_data='editcar_field_vin')],
              [InlineKeyboardButton(text='🔢 Гос. номер',callback_data='editcar_field_registration')],
              [InlineKeyboardButton(text='📏 Пробег',callback_data='editcar_field_mileage')],
              [InlineKeyboardButton(text='← Назад',callback_data=f'car:{car_id}')]]
    await call.message.edit_text(f'✏️ <b>Изменить данные автомобиля</b>\n\n{r["brand"]} {r["model"]}\n\nВыберите, что изменить:',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()

@dp.callback_query(F.data=='editcar_field_year')
async def editcar_field_year(call:CallbackQuery,state:FSMContext):
    await state.update_data(edit_section='year'); await state.set_state(EditCar.year)
    await call.message.edit_text('📅 Выберите новый год выпуска:',reply_markup=year_markup('editcar_year_only:')); await call.answer()

@dp.callback_query(F.data.startswith('editcar_year_only:'))
async def editcar_year_only(call:CallbackQuery,state:FSMContext):
    v=call.data.split(':',1)[1]; d=await state.get_data(); car_id=d.get('car_id')
    if v!='skip' and car_id:
        c=conn(); c.execute('UPDATE cars SET year=? WHERE id=?',(int(v),car_id)); c.commit(); c.close()
    await state.clear(); await call.message.edit_text('✅ Данные автомобиля обновлены.',reply_markup=kb([[InlineKeyboardButton(text='← Назад к машине',callback_data=f'car:{car_id}')]])); await call.answer('Сохранено')

@dp.callback_query(F.data=='editcar_field_engine')
async def editcar_field_engine(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id'); c=conn(); r=c.execute('SELECT model_id FROM (SELECT m.id model_id FROM cars c JOIN vehicle_makes vm ON vm.name=c.brand JOIN vehicle_models m ON m.make_id=vm.id AND m.name=c.model WHERE c.id=?)',(car_id,)).fetchone(); c.close()
    if not r: await call.answer('Модель не найдена в справочнике',show_alert=True); return
    await state.update_data(edit_section='engine',model_id=r['model_id']); await state.set_state(EditCar.engine); await show_vehicle_engines(call,r['model_id'],'editcar_engine:'); await call.answer()

@dp.callback_query(F.data=='editcar_field_gearbox')
async def editcar_field_gearbox(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id'); c=conn(); r=c.execute('SELECT model_id FROM (SELECT m.id model_id FROM cars c JOIN vehicle_makes vm ON vm.name=c.brand JOIN vehicle_models m ON m.make_id=vm.id AND m.name=c.model WHERE c.id=?)',(car_id,)).fetchone(); c.close()
    if not r: await call.answer('Модель не найдена в справочнике',show_alert=True); return
    await state.update_data(edit_section='gearbox',model_id=r['model_id']); await state.set_state(EditCar.gearbox); await show_vehicle_gearboxes(call,r['model_id'],'editcar_gearbox:'); await call.answer()

@dp.callback_query(F.data=='editcar_field_vin')
async def editcar_field_vin(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id')
    c=conn(); r=c.execute('SELECT vin FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    current=(r['vin'] or 'не указан') if r else 'не указан'
    await state.update_data(edit_section='vin'); await state.set_state(EditCar.vin)
    await call.message.edit_text(f'🔤 <b>Изменение VIN</b>\n\nТекущий VIN: <code>{html.escape(current)}</code>\n\nВведите новый VIN. Можно изменить одну цифру или букву, остальное оставить как есть.',parse_mode='HTML',reply_markup=skip_markup('editcar_skip_vin')); await call.answer()

@dp.callback_query(F.data=='editcar_field_registration')
async def editcar_field_registration(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id')
    c=conn(); r=c.execute('SELECT registration_number FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    current=(r['registration_number'] or 'не указан') if r else 'не указан'
    await state.update_data(edit_section='registration')
    await state.set_state(EditCar.registration)
    await call.message.edit_text(f'🔢 <b>Изменение гос. номера</b>\n\nТекущий номер: <code>{html.escape(current)}</code>\n\nВведите новый регистрационный номер.',parse_mode='HTML',reply_markup=skip_markup('editcar_skip_registration'))
    await call.answer()

@dp.callback_query(F.data=='editcar_field_mileage')
async def editcar_field_mileage(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id')
    c=conn(); r=c.execute('SELECT mileage FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    current=(r['mileage'] or 0) if r else 0
    await state.update_data(edit_section='mileage'); await state.set_state(EditCar.mileage)
    await call.message.edit_text(f'📏 <b>Изменение пробега</b>\n\nТекущий пробег: <code>{current:,} км</code>'.replace(',',' ')+'\n\nВведите новый пробег или нажмите «⏭️ Пропустить».',parse_mode='HTML',reply_markup=skip_markup('editcar_skip_mileage')); await call.answer()

@dp.callback_query(F.data=='editcar_field_model')
async def editcar_model_start(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id')
    if not car_id: await call.answer('Сессия истекла',show_alert=True); return
    await state.set_state(EditCar.brand); await show_vehicle_makes(call,'editcar_make:'); await call.answer()

@dp.callback_query(F.data.startswith('editcar_make:letter:'))
async def editcar_make_letter(call:CallbackQuery,state:FSMContext):
    rest=call.data.split(':',2)[2]
    if ':page:' in rest:
        letter,pg=rest.split(':page:',1); page=int(pg)
    else: letter,page=rest,0
    await show_vehicle_makes(call,'editcar_make:',page,letter); await call.answer()

@dp.callback_query(F.data.startswith('editcar_make:'))
async def editcar_make(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=? AND active=1',(mid,)).fetchone(); c.close()
    if not r: await call.answer('Марка не найдена',show_alert=True); return
    await state.update_data(make_id=mid,brand=r['name']); await state.set_state(EditCar.model); await show_vehicle_models(call,mid,'editcar_model:'); await call.answer()

@dp.callback_query(F.data.startswith('editcar_model:'))
async def editcar_model(call:CallbackQuery,state:FSMContext):
    model_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT m.name,vm.name make_name FROM vehicle_models m JOIN vehicle_makes vm ON vm.id=m.make_id WHERE m.id=? AND m.active=1',(model_id,)).fetchone(); c.close()
    if not r: await call.answer('Модель не найдена',show_alert=True); return
    await state.update_data(model=r['name'],brand=r['make_name'],model_id=model_id); await state.set_state(EditCar.year)
    await call.message.edit_text(f'🚗 <b>{r["make_name"]} {r["name"]}</b>\n\n📅 Выберите год выпуска:',parse_mode='HTML',reply_markup=year_markup('editcar_year:')); await call.answer()

@dp.callback_query(F.data.startswith('editcar_year:'))
async def editcar_year(call:CallbackQuery,state:FSMContext):
    v=call.data.split(':',1)[1]; d=await state.get_data()
    if v!='skip': await state.update_data(year=int(v))
    await state.set_state(EditCar.engine); d=await state.get_data(); await show_vehicle_engines(call,d.get('model_id'),'editcar_engine:'); await call.answer()

@dp.callback_query(F.data.startswith('editcar_engine:'))
async def editcar_engine(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]; d=await state.get_data()
    if val=='manual': await call.message.edit_text('🔧 Введите двигатель / мотор:',reply_markup=skip_markup('editcar_skip_engine')); await call.answer(); return
    if val=='skip':
        if d.get('edit_section')=='engine': await editcar_done_inline(call,state); return
        await state.set_state(EditCar.gearbox); await show_vehicle_gearboxes(call,d.get('model_id'),'editcar_gearbox:'); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_engines WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('Двигатель не найден',show_alert=True); return
    await state.update_data(engine=r['name'])
    if d.get('edit_section')=='engine': await editcar_done_inline(call,state); return
    await state.set_state(EditCar.gearbox); await show_vehicle_gearboxes(call,d.get('model_id'),'editcar_gearbox:'); await call.answer()

@dp.callback_query(F.data.startswith('editcar_gearbox:'))
async def editcar_gearbox(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]
    if val=='manual': await call.message.edit_text('⚙️ Введите КПП:',reply_markup=skip_markup('editcar_skip_gearbox')); await call.answer(); return
    if val=='skip':
        if (await state.get_data()).get('edit_section')=='gearbox': await editcar_done_inline(call,state); return
        await state.set_state(EditCar.vin); await call.message.edit_text('VIN?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_vin')); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_gearboxes WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('КПП не найдена',show_alert=True); return
    await state.update_data(gearbox=r['name'])
    if (await state.get_data()).get('edit_section')=='gearbox': await editcar_done_inline(call,state); return
    await state.set_state(EditCar.vin); await call.message.edit_text('VIN?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_vin')); await call.answer()

@dp.callback_query(F.data=='editcar_skip_engine')
async def editcar_skip_engine(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.set_state(EditCar.gearbox); await show_vehicle_gearboxes(call,d.get('model_id'),'editcar_gearbox:'); await call.answer()

@dp.callback_query(F.data=='editcar_skip_gearbox')
async def editcar_skip_gearbox(call:CallbackQuery,state:FSMContext):
    await state.set_state(EditCar.vin); await call.message.edit_text('VIN?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_vin')); await call.answer()

@dp.message(EditCar.engine)
async def editcar_engine_text(message:Message,state:FSMContext):
    if message.text=='← Назад': await editcar_cancel(message,state); return
    if message.text and message.text.startswith('⏭️'): return
    await state.update_data(engine=message.text.strip()); d=await state.get_data(); await state.set_state(EditCar.gearbox); await show_vehicle_gearboxes(message,d.get('model_id'),'editcar_gearbox:')

@dp.message(EditCar.gearbox)
async def editcar_gearbox_text(message:Message,state:FSMContext):
    if message.text=='← Назад': await editcar_cancel(message,state); return
    await state.update_data(gearbox=message.text.strip()); await state.set_state(EditCar.vin); await message.answer('VIN?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_vin'))

@dp.callback_query(F.data=='editcar_skip_vin')
async def editcar_skip_vin(call:CallbackQuery,state:FSMContext):
    if (await state.get_data()).get('edit_section')=='vin':
        await editcar_done_inline(call,state); return
    await state.set_state(EditCar.registration); await call.message.edit_text('🔢 Регистрационный номер автомобиля?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_registration')); await call.answer()

@dp.message(EditCar.vin)
async def editcar_vin(message:Message,state:FSMContext):
    if message.text=='← Назад': await editcar_cancel(message,state); return
    if message.text and 'пропуст' not in message.text.lower(): await state.update_data(vin=message.text.strip().upper())
    d=await state.get_data()
    if d.get('edit_section')=='vin': await editcar_done_inline(message,state); return
    await state.set_state(EditCar.registration); await message.answer('🔢 Регистрационный номер автомобиля?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_registration'))

@dp.callback_query(F.data=='editcar_skip_registration')
async def editcar_skip_registration(call:CallbackQuery,state:FSMContext):
    if (await state.get_data()).get('edit_section')=='registration':
        await editcar_done_inline(call,state); return
    await state.set_state(EditCar.mileage)
    await call.message.edit_text('📏 Пробег?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_mileage'))
    await call.answer()

@dp.message(EditCar.registration)
async def editcar_registration(message:Message,state:FSMContext):
    if message.text=='← Назад': await editcar_cancel(message,state); return
    if message.text and 'пропуст' not in message.text.lower(): await state.update_data(registration_number=message.text.strip().upper())
    if (await state.get_data()).get('edit_section')=='registration': await editcar_done_inline(message,state); return
    await state.set_state(EditCar.mileage)
    await message.answer('📏 Пробег?\n\nЕсли менять не нужно — нажмите «⏭️ Пропустить».',reply_markup=skip_markup('editcar_skip_mileage'))

@dp.callback_query(F.data=='editcar_skip_mileage')
async def editcar_skip_mileage(call:CallbackQuery,state:FSMContext):
    await finish_editcar(call.message,state); await call.answer('Сохранено')

@dp.message(EditCar.mileage)
async def editcar_mileage(message:Message,state:FSMContext):
    if message.text=='← Назад': await editcar_cancel(message,state); return
    if message.text and 'пропуст' in message.text.lower(): await finish_editcar(message,state); return
    try: m=int(message.text.replace(' ',''))
    except: await message.answer('Введите пробег числом или нажмите «⏭️ Пропустить».'); return
    await state.update_data(mileage=m); await finish_editcar(message,state)


@dp.callback_query(F.data.startswith('editcar_cancel:'))
async def editcar_cancel_callback(call:CallbackQuery,state:FSMContext):
    car_id=int(call.data.split(':')[1]); await state.clear(); await call.message.edit_text('Изменение отменено.',reply_markup=kb([[InlineKeyboardButton(text='← Назад к машине',callback_data=f'car:{car_id}')]])); await call.answer()

async def editcar_cancel(message,state):
    d=await state.get_data(); car_id=d.get('car_id'); await state.clear(); await message.answer('Изменение отменено.',reply_markup=kb([[InlineKeyboardButton(text='← Назад к машине',callback_data=f'car:{car_id}')]]) if car_id else kb([]))

async def editcar_done_inline(call,state):
    d=await state.get_data(); car_id=d.get('car_id'); field=d.get('edit_section')
    if car_id and field in ('engine','gearbox','vin','registration'):
        db_field='registration_number' if field=='registration' else field
        value=d.get(field if field!='registration' else 'registration_number',''); c=conn(); c.execute(f'UPDATE cars SET {db_field}=? WHERE id=?',(value,car_id)); c.commit(); c.close()
    await state.clear(); await call.message.edit_text('✅ Данные автомобиля обновлены.',reply_markup=kb([[InlineKeyboardButton(text='← Назад к машине',callback_data=f'car:{car_id}')]])); await call.answer('Сохранено')

async def finish_editcar(message,state):
    d=await state.get_data(); car_id=d.get('car_id')
    if not car_id: await state.clear(); await message.answer('Ошибка: автомобиль не найден.'); return
    c=conn(); r=c.execute('SELECT * FROM cars WHERE id=?',(car_id,)).fetchone()
    if not r: c.close(); await state.clear(); await message.answer('Автомобиль не найден.'); return
    vals={k:d[k] for k in ('brand','model','year','engine','gearbox','vin','registration_number','mileage') if k in d}
    if vals:
        fields=', '.join(f'{k}=?' for k in vals); params=list(vals.values())+[car_id]; c.execute(f'UPDATE cars SET {fields} WHERE id=?',params); c.commit()
    c.close(); await state.clear();
    c=conn(); r=c.execute('SELECT * FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    text=f'🚗 <b>{r["brand"]} {r["model"]}</b>\n'+(f'Год: {r["year"]}\n' if r['year'] else '')+(f'Двигатель: {r["engine"]}\n' if r['engine'] else '')+(f'КПП: {r["gearbox"]}\n' if r['gearbox'] else '')+(f'VIN: {r["vin"]}\n' if r['vin'] else '')+(f'Гос. номер: {r["registration_number"]}\n' if r['registration_number'] else '')+f'Пробег: {r["mileage"]:,} км'.replace(',',' ')
    buttons=[[InlineKeyboardButton(text='📖 Сервисная книжка',callback_data=f'history:{car_id}')],[InlineKeyboardButton(text='🔧 Добавить работы',callback_data=f'servicecar:{car_id}')],[InlineKeyboardButton(text='🧰 Запчасти',callback_data=f'parts:{car_id}')],[InlineKeyboardButton(text='🛢️ Масла',callback_data=f'fluids:{car_id}')],[InlineKeyboardButton(text='✏️ Изменить данные',callback_data=f'editcar:{car_id}')],[InlineKeyboardButton(text='📷 Добавить / изменить фото',callback_data=f'carphoto:{car_id}')],[InlineKeyboardButton(text='← Назад',callback_data='back_my_cars' if not is_admin(message.from_user.id) else 'back_cars')]]
    if is_admin(message.from_user.id): buttons.insert(-1,[InlineKeyboardButton(text='🗑 Удалить машину',callback_data=f'confirm_delete_car:{car_id}')])
    if r['photo_file_id']:
        await message.answer_photo(r['photo_file_id'],caption=text,parse_mode='HTML',reply_markup=kb(buttons))
    else:
        await message.answer(text,parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith('carphoto:'))
async def car_photo_start(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer('Доступно только сервису',show_alert=True); return
    car_id=int(call.data.split(':')[1])
    c=conn(); r=c.execute('SELECT c.id,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id):
        await call.answer('Нет доступа',show_alert=True); return
    await state.update_data(car_id=car_id)
    await state.set_state(CarPhoto.photo)
    await call.message.edit_text('📷 Отправьте фотографию автомобиля.',reply_markup=cancel_markup(f'carphoto_back:{car_id}'))
    await call.answer()

@dp.message(CarPhoto.photo,F.photo)
async def car_photo_save(message:Message,state:FSMContext):
    d=await state.get_data(); car_id=d.get('car_id')
    if not car_id:
        await state.clear(); await message.answer('Ошибка: автомобиль не найден.',reply_markup=admin_menu() if is_admin(message.from_user.id) else client_menu()); return
    c=conn(); c.execute('UPDATE cars SET photo_file_id=? WHERE id=?',(message.photo[-1].file_id,car_id)); c.commit(); c.close(); await state.clear()
    await message.answer('✅ Фото автомобиля сохранено.',reply_markup=admin_menu() if is_admin(message.from_user.id) else client_menu())

@dp.callback_query(F.data.startswith('carphoto_back:'))
async def car_photo_back(call:CallbackQuery,state:FSMContext):
    car_id=int(call.data.split(':')[1])
    await state.clear()
    ok=await render_car_card_message(call,car_id)
    await call.answer('Назад' if ok else 'Автомобиль не найден', show_alert=not ok)

@dp.message(CarPhoto.photo)
async def car_photo_wrong(message:Message,state:FSMContext):
    if message.text=='← Назад':
        await state.clear(); await message.answer('Операция отменена.',reply_markup=admin_menu() if is_admin(message.from_user.id) else client_menu()); return
    await message.answer('Пожалуйста, отправьте именно фотографию автомобиля или нажмите «← Назад».')

async def _clear_history_messages_for_chat(chat_id):
    c=conn(); rows=c.execute('SELECT DISTINCT car_id FROM service_history_messages WHERE chat_id=?',(chat_id,)).fetchall(); c.close()
    if rows:
        # Удаляем старые сообщения истории параллельно, чтобы переходы
        # «Автомобили»/«Мои автомобили» не зависали на десятки секунд.
        await asyncio.gather(*( _clear_history_messages(int(r['car_id'])) for r in rows ), return_exceptions=True)

@dp.callback_query(F.data=='back_my_cars')
async def back_my_cars(call:CallbackQuery,state:FSMContext):
    await state.clear(); await call.answer()
    await _clear_history_messages_for_chat(call.from_user.id)
    await my_cars_from_call(call)
async def my_cars_from_call(call):
    c=conn(); rows=c.execute('SELECT * FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE cl.tg_id=? ORDER BY c.brand COLLATE NOCASE, c.model COLLATE NOCASE, c.id',(call.from_user.id,)).fetchall(); c.close()
    markup=kb([[InlineKeyboardButton(text=f"🚗 {r['brand']} {r['model']}",callback_data=f"car:{r['id']}")] for r in rows])
    try:
        if call.message.photo:
            await call.message.delete(); await call.message.answer('🚗 Выберите автомобиль:',reply_markup=markup)
        else: await call.message.edit_text('🚗 Выберите автомобиль:',reply_markup=markup)
    except Exception: await call.message.answer('🚗 Выберите автомобиль:',reply_markup=markup)

@dp.callback_query(F.data=='back_cars')
async def back_cars(call:CallbackQuery,state:FSMContext):
    await state.clear(); await call.answer(); await _clear_history_messages_for_chat(call.message.chat.id)
    c=conn(); rows=c.execute('SELECT c.*,cl.username,cl.id client_id FROM cars c JOIN clients cl ON cl.id=c.client_id ORDER BY c.brand COLLATE NOCASE, c.model COLLATE NOCASE, c.id').fetchall(); c.close()
    buttons=[[InlineKeyboardButton(text=(f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — @{r['username']}" if r['username'] else f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — клиент #{r['client_id']}"),callback_data=f"car:{r['id']}")] for r in rows]
    markup=kb(buttons)
    try:
        if call.message.photo:
            await call.message.delete(); await call.message.answer('🚗 Все автомобили:',reply_markup=markup)
        else: await call.message.edit_text('🚗 Все автомобили:',reply_markup=markup)
    except Exception: await call.message.answer('🚗 Все автомобили:',reply_markup=markup)

def work_lines(value):
    import re
    raw=(value or '').replace('\r','\n')
    raw=re.sub(r'\s*;\s*','\n',raw)
    parts=[x.strip(' •-') for x in raw.split('\n') if x.strip(' •-')]
    return parts or ([raw.strip()] if raw.strip() else [])


def _service_work_ids(c, service_id, work_name):
    existing=c.execute('SELECT work_id FROM service_work_items WHERE service_id=?',(service_id,)).fetchall()
    if existing: return [int(r['work_id']) for r in existing]
    names=work_lines(work_name); ids=[]
    for name in names:
        r=c.execute('SELECT w.id FROM works w WHERE lower(w.name)=lower(?) ORDER BY w.active DESC,w.id LIMIT 1',(name,)).fetchone()
        if r: ids.append(int(r['id']))
    for wid in ids:
        c.execute('INSERT OR IGNORE INTO service_work_items(service_id,work_id) VALUES(?,?)',(service_id,wid))
    return ids

def _grouped_work_lines(c, service_id, work_name):
    ids=_service_work_ids(c,service_id,work_name)
    if not ids:
        return [('Работы',work_lines(work_name))] if work_lines(work_name) else []
    placeholders=','.join('?'*len(ids))
    rows=c.execute(f'SELECT w.id,w.name,g.name AS group_name,g.sort_order FROM works w JOIN service_groups g ON g.id=w.group_id WHERE w.id IN ({placeholders})',(ids)).fetchall()
    by={}
    order={}
    for r in rows:
        by.setdefault(r['group_name'],[]).append(r['name']); order[r['group_name']]=r['sort_order']
    return [(name,by[name]) for name in sorted(by,key=lambda n:(order[n],n.casefold()))]

async def _clear_history_messages(car_id, exclude_message_id=None):
    c=conn(); rows=c.execute('SELECT chat_id,message_id FROM service_history_messages WHERE car_id=?',(car_id,)).fetchall(); c.close()
    tasks=[]
    for r in rows:
        if exclude_message_id is not None and int(r['message_id']) == int(exclude_message_id):
            continue
        tasks.append(bot.delete_message(chat_id=r['chat_id'],message_id=r['message_id']))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    c=conn()
    if exclude_message_id is None:
        c.execute('DELETE FROM service_history_messages WHERE car_id=?',(car_id,))
    else:
        c.execute('DELETE FROM service_history_messages WHERE car_id=? AND message_id<>?',(car_id,int(exclude_message_id)))
    c.commit(); c.close()

async def _remember_history_message(car_id,service_id,photo_id,chat_id,message_id):
    c=conn(); c.execute('INSERT INTO service_history_messages(car_id,service_id,photo_id,chat_id,message_id) VALUES(?,?,?,?,?)',(car_id,service_id,photo_id,chat_id,message_id)); c.commit(); c.close()

_history_locks = {}

async def _render_history(car_id, chat_id, fallback_message=None):
    """Render service history as separate visit messages, with photos below each visit.

    Important: all SQLite bookkeeping for this render is done through one connection.
    Telegram API calls are kept out of the DB transaction so a slow Telegram request
    cannot hold a SQLite write lock.  Old Telegram messages are cleaned up in the
    background after the new view is ready.
    """
    lock = _history_locks.setdefault((chat_id, car_id), asyncio.Lock())
    if lock.locked():
        return False

    async with lock:
        c = conn()
        try:
            rows = c.execute(
                'SELECT * FROM services WHERE car_id=? AND deleted=0 ORDER BY service_date ASC,id ASC',
                (car_id,)
            ).fetchall()
            old = c.execute(
                'SELECT id,service_id,photo_id,message_id,chat_id FROM service_history_messages '
                'WHERE car_id=? AND chat_id=? ORDER BY id',
                (car_id, chat_id)
            ).fetchall()
        finally:
            c.close()

        # Existing Telegram messages grouped by service.
        service_text_old = {}
        photo_old = {}
        search_old = None
        old_message_ids = set()
        for r in old:
            mid = int(r['message_id'])
            old_message_ids.add(mid)
            if r['service_id'] is not None and r['photo_id'] is None:
                service_text_old.setdefault(int(r['service_id']), []).append(mid)
            elif r['photo_id'] is not None:
                photo_old[int(r['photo_id'])] = mid
            elif r['service_id'] is None and r['photo_id'] is None:
                search_old = mid

        active_ids = set()
        new_db_rows = []

        if not rows:
            body = '📖 <b>Сервисная книжка</b>\n\nИстория пока пустая.'
            markup = kb([[InlineKeyboardButton(text='← К автомобилю', callback_data=f'history_back:{car_id}')]])
            try:
                if fallback_message and fallback_message.chat.id == chat_id:
                    await fallback_message.edit_text(body, reply_markup=markup, parse_mode='HTML')
                    history_mid = fallback_message.message_id
                else:
                    m = await bot.send_message(chat_id, body, reply_markup=markup, parse_mode='HTML')
                    history_mid = m.message_id
            except Exception:
                m = await bot.send_message(chat_id, body, reply_markup=markup, parse_mode='HTML')
                history_mid = m.message_id
            active_ids.add(history_mid)
            new_db_rows.append((None, None, chat_id, history_mid))
        else:
            for idx, r in enumerate(rows):
                lines = [f'📅 <b>{html.escape(str(r["service_date"]))}</b>']
                grouped = None
                # Grouping uses a short-lived read-only connection and never holds a
                # transaction while Telegram API requests are running.
                c = conn()
                try:
                    grouped = _grouped_work_lines(c, r['id'], r['work_name'])
                    phcount = c.execute('SELECT COUNT(*) AS n FROM service_photos WHERE service_id=?', (r['id'],)).fetchone()['n']
                finally:
                    c.close()

                if grouped:
                    lines.append('🔧 <b>Работы:</b>')
                    for group_name, works in grouped:
                        lines.append(f'\n<b>▸ {html.escape(group_name)}</b>')
                        lines.extend(f'• {html.escape(wn)}' for wn in works)
                lines.append(f"Пробег: {int(r['mileage']):,} км".replace(',', ' '))
                if r['description']:
                    lines.append(f"📝 {html.escape(str(r['description']))}")
                if r['price']:
                    lines.append(f"💶 Стоимость: {float(r['price']):.2f} €")
                if phcount:
                    lines.append(f'📷 Фотографий: {phcount}')

                markup = kb([[InlineKeyboardButton(text=f'✏️ Изменить #{idx+1}', callback_data=f'editservice:{r["id"]}'),
                              InlineKeyboardButton(text=f'🗑 Удалить #{idx+1}', callback_data=f'delservice:{r["id"]}')]])
                sid = int(r['id'])
                mid = service_text_old.get(sid, [None])[0]

                try:
                    if mid:
                        try:
                            await bot.edit_message_text(chat_id=chat_id, message_id=mid,
                                                        text='\n'.join(lines), reply_markup=markup, parse_mode='HTML')
                        except Exception:
                            m = await bot.send_message(chat_id, '\n'.join(lines), reply_markup=markup, parse_mode='HTML')
                            mid = m.message_id
                    else:
                        m = await bot.send_message(chat_id, '\n'.join(lines), reply_markup=markup, parse_mode='HTML')
                        mid = m.message_id
                except Exception:
                    logging.exception('failed to render service id=%s', sid)
                    continue

                active_ids.add(int(mid))
                new_db_rows.append((sid, None, chat_id, int(mid)))

                # Photos are sent immediately after their corresponding service,
                # not all the way at the bottom of the book.
                c = conn()
                try:
                    photos = c.execute('SELECT id,telegram_file_id FROM service_photos WHERE service_id=? ORDER BY id', (sid,)).fetchall()
                finally:
                    c.close()
                for ph in photos:
                    pid = int(ph['id'])
                    pmid = photo_old.get(pid)
                    if pmid:
                        active_ids.add(pmid)
                        new_db_rows.append((sid, pid, chat_id, pmid))
                        continue
                    try:
                        pm = await bot.send_photo(
                            chat_id,
                            ph['telegram_file_id'],
                            reply_markup=kb([[InlineKeyboardButton(text='🗑 Удалить фото', callback_data=f'delphoto:{pid}')]])
                        )
                        active_ids.add(int(pm.message_id))
                        new_db_rows.append((sid, pid, chat_id, int(pm.message_id)))
                    except Exception:
                        logging.exception('failed to render photo id=%s for service id=%s', pid, sid)

        # Search is deliberately the last message in the book.
        search_markup = kb([
            [InlineKeyboardButton(text='🔎 Найти работу', callback_data=f'history_search:{car_id}')],
            [InlineKeyboardButton(text='← К автомобилю', callback_data=f'history_back:{car_id}')]
        ])
        search_id = search_old
        try:
            if search_id:
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=search_id,
                                                text='🔎 <b>Поиск по работам</b>',
                                                reply_markup=search_markup, parse_mode='HTML')
                except Exception:
                    sm = await bot.send_message(chat_id, '🔎 <b>Поиск по работам</b>',
                                                reply_markup=search_markup, parse_mode='HTML')
                    search_id = sm.message_id
            else:
                sm = await bot.send_message(chat_id, '🔎 <b>Поиск по работам</b>',
                                            reply_markup=search_markup, parse_mode='HTML')
                search_id = sm.message_id
            active_ids.add(int(search_id))
            new_db_rows.append((None, None, chat_id, int(search_id)))
        except Exception:
            logging.exception('failed to render history search for car_id=%s', car_id)

        # Book is already visible. Database cleanup is best-effort and must never
        # turn a successfully rendered book into a false "Не удалось открыть" error.
        try:
            stale = old_message_ids - active_ids
            if stale:
                async def cleanup(ids):
                    await asyncio.gather(*(bot.delete_message(chat_id=chat_id, message_id=i) for i in ids), return_exceptions=True)
                asyncio.create_task(cleanup(stale))

            c = conn()
            c.execute('DELETE FROM service_history_messages WHERE car_id=? AND chat_id=?', (car_id, chat_id))
            for sid, pid, cid, mid in new_db_rows:
                c.execute('INSERT INTO service_history_messages(car_id,service_id,photo_id,chat_id,message_id) VALUES(?,?,?,?,?)',
                          (car_id, sid, pid, cid, mid))
            c.commit()
            c.close()
        except Exception:
            logging.exception('history DB bookkeeping failed for car_id=%s', car_id)

        return True


@dp.callback_query(F.data.startswith('history_back:'))
async def history_back(call:CallbackQuery):
    car_id=int(call.data.split(':')[1])
    c=conn(); r=c.execute('SELECT c.id,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id):
        await call.answer('Нет доступа',show_alert=True); return
    # Не удаляем сообщение, на котором нажата «← К автомобилю»: именно его
    # переиспользуем для карточки машины. Старый код удалял его первым, после
    # чего пытался редактировать уже удалённое сообщение.
    current_id=call.message.message_id
    await _clear_history_messages(car_id, exclude_message_id=current_id)
    ok=await render_car_card_message(call,car_id)
    if ok:
        c=conn(); c.execute('DELETE FROM service_history_messages WHERE car_id=? AND message_id=?',(car_id,current_id)); c.commit(); c.close()
        await call.answer()
    else:
        await call.answer('Не удалось открыть автомобиль',show_alert=True)

@dp.callback_query(F.data.startswith('history_from_search:'))
async def history_from_search(call:CallbackQuery):
    car_id = int(call.data.split(':')[1])
    c = conn()
    r = c.execute('SELECT c.id,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?', (car_id,)).fetchone()
    c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id'] != call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return

    # Search results are a temporary screen. Remove both the results message
    # and the old search prompt/footer that was left near the top of the chat.
    # The footer must be recreated as a NEW message after the history, otherwise
    # _render_history would reuse the old message in its original position.
    try:
        await call.message.delete()
    except Exception:
        pass
    try:
        c = conn()
        old_search = c.execute(
            'SELECT message_id FROM service_history_messages '
            'WHERE car_id=? AND chat_id=? AND service_id IS NULL AND photo_id IS NULL '
            'ORDER BY id DESC LIMIT 1',
            (car_id, call.message.chat.id)
        ).fetchone()
        if old_search:
            old_search_id = int(old_search['message_id'])
            try:
                await bot.delete_message(call.message.chat.id, old_search_id)
            except Exception:
                pass
            c.execute('DELETE FROM service_history_messages WHERE car_id=? AND chat_id=? AND message_id=?',
                      (car_id, call.message.chat.id, old_search_id))
            c.commit()
        c.close()
    except Exception:
        logging.exception('failed to remove old history search footer for car_id=%s', car_id)
    await call.answer()
    try:
        await _render_history(car_id, call.message.chat.id)
    except Exception:
        logging.exception('failed to return from history search for car_id=%s', car_id)
        try:
            await bot.send_message(call.message.chat.id, '⚠️ Не удалось открыть сервисную книжку. Попробуйте ещё раз.')
        except Exception:
            pass


@dp.callback_query(F.data.startswith('history:'))
async def history(call:CallbackQuery):
    car_id=int(call.data.split(':')[1])
    # Проверяем, что машина существует и принадлежит текущему клиенту
    # либо пользователь является администратором.
    c=conn(); r=c.execute('SELECT c.id,cl.tg_id FROM cars c JOIN clients cl ON cl.id=c.client_id WHERE c.id=?',(car_id,)).fetchone(); c.close()
    if not r or (not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id):
        await call.answer('Нет доступа',show_alert=True); return
    await call.answer()
    try:
        rendered=await _render_history(car_id,call.message.chat.id,call.message)
        if rendered is False:
            # Второй быстрый клик по той же кнопке не запускает второй рендер
            # и не удаляет сообщения, которые уже рисуются.
            return
    except Exception:
        logging.exception('history render failed for car_id=%s', car_id)
        try:
            await bot.send_message(call.message.chat.id,'⚠️ Не удалось открыть сервисную книжку. Попробуйте ещё раз.')
        except Exception:
            pass

@dp.callback_query(F.data.startswith('history_search:'))
async def history_search_start(call:CallbackQuery,state:FSMContext):
    car_id=int(call.data.split(':')[1])
    await state.set_state(HistorySearch.query)
    await state.update_data(car_id=car_id)
    await call.message.edit_text("🔎 <b>Поиск по работам</b>\n\nВведите часть названия работы.\nНапример: <b>масл</b>, <b>торм</b>, <b>стой</b>.",parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Сервисная книжка',callback_data=f'history:{car_id}')]]))
    await call.answer()

@dp.message(HistorySearch.query)
async def history_search_query(message:Message,state:FSMContext):
    d=await state.get_data(); car_id=int(d.get('car_id',0)); q=(message.text or '').strip()
    if not q:
        await message.answer('Введите название или хотя бы несколько букв работы.')
        return
    c=conn(); rows=c.execute("SELECT id,service_date,mileage,work_name,description,price FROM services WHERE car_id=? AND deleted=0 AND work_name LIKE ? COLLATE NOCASE ORDER BY service_date DESC,id DESC",(car_id,f'%{q}%')).fetchall(); c.close()
    if not rows:
        text=f'🔎 <b>Поиск по работам</b>\n\nПо запросу «{html.escape(q)}» ничего не найдено.'
    else:
        lines=[f'🔎 <b>Поиск по работам</b>',f'Запрос: <b>{html.escape(q)}</b>',f'Найдено: <b>{len(rows)}</b>','']
        for r in rows[:30]:
            works=work_lines(r['work_name'])
            matched=[w for w in works if q.casefold() in w.casefold()]
            shown=matched or works
            lines.append(f"📅 <b>{html.escape(r['service_date'])}</b>")
            lines.append('🔧 '+', '.join(html.escape(w) for w in shown))
            lines.append(f"🚗 Пробег: {r['mileage']:,} км".replace(',',' '))
            if r['description']: lines.append(f"📝 {html.escape(r['description'])}")
            if r['price']: lines.append(f"💶 {r['price']:.2f} €")
            lines.append('')
        if len(rows)>30: lines.append(f'Показаны первые 30 из {len(rows)} результатов.')
        text='\n'.join(lines)
    await message.answer(text,parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Сервисная книжка',callback_data=f'history_from_search:{car_id}')],[InlineKeyboardButton(text='🚗 К автомобилю',callback_data=f'history_back:{car_id}')]]))
    await state.clear()

@dp.callback_query(F.data.startswith('delphoto:'))
async def delete_service_photo(call:CallbackQuery):
    photo_id=int(call.data.split(':')[1])
    c=conn()
    ph=c.execute('SELECT id FROM service_photos WHERE id=?',(photo_id,)).fetchone()
    if not ph:
        c.close(); await call.answer('Фото не найдено',show_alert=True); return

    # ВАЖНО: при удалении фотографии НЕ меняем services и НЕ удаляем
    # service_history_messages. Запись обслуживания и выполненные работы
    # должны остаться полностью нетронутыми. Удаляем только строку фото.
    c.execute('DELETE FROM service_photos WHERE id=?',(photo_id,))
    c.commit(); c.close()

    # Удаляем только Telegram-сообщение с этой фотографией.
    try:
        await call.message.delete()
    except Exception:
        pass

    await call.answer('Фото удалено')

@dp.callback_query(F.data.startswith('delservice:'))
async def delete_service(call:CallbackQuery):
    sid=int(call.data.split(':')[1])
    try:
        c=conn(); r=c.execute('SELECT s.car_id,cl.tg_id FROM services s JOIN cars car ON car.id=s.car_id JOIN clients cl ON cl.id=car.client_id WHERE s.id=? AND s.deleted=0',(sid,)).fetchone()
        if not r: c.close(); await call.answer('Запись не найдена',show_alert=True); return
        if not is_admin(call.from_user.id) and r['tg_id']!=call.from_user.id: c.close(); await call.answer('Нет доступа',show_alert=True); return
        msgs=c.execute('SELECT chat_id,message_id FROM service_history_messages WHERE service_id=? AND photo_id IS NULL',(sid,)).fetchall()
        c.execute('UPDATE services SET deleted=1 WHERE id=?',(sid,)); c.execute('DELETE FROM service_history_messages WHERE service_id=? AND photo_id IS NULL',(sid,)); c.commit(); c.close()
        for m in msgs:
            try: await bot.delete_message(chat_id=int(m['chat_id']),message_id=int(m['message_id']))
            except Exception: logging.exception('failed to delete Telegram service message sid=%s',sid)
        await call.answer('Обслуживание удалено')
    except Exception:
        logging.exception('delete_service failed sid=%s',sid)
        try: await call.answer('Не удалось удалить запись',show_alert=True)
        except Exception: pass

@dp.callback_query(F.data.startswith('editservice:'))
async def edit_service(call:CallbackQuery,state:FSMContext):
    sid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT * FROM services WHERE id=? AND deleted=0',(sid,)).fetchone(); c.close()
    if not r: await call.answer('Запись не найдена',show_alert=True); return
    c=conn(); works=c.execute('SELECT id,name FROM works WHERE active=1 ORDER BY group_id,sort_order,id').fetchall(); photos=c.execute('SELECT telegram_file_id FROM service_photos WHERE service_id=? ORDER BY id',(sid,)).fetchall(); c.close()
    selected=[]; old=set(work_lines(r['work_name']))
    for w in works:
        if w['name'] in old: selected.append(w['id'])
    existing_photos=[p['telegram_file_id'] for p in photos]
    await state.clear(); await state.update_data(edit_service_id=sid,car_id=r['car_id'],selected=selected,mileage=r['mileage'],description=r['description'] or '',price=r['price'] or 0,photos=existing_photos,edit_service=True)
    await state.set_state(AddService.group_id)
    c=conn(); g=c.execute('SELECT id FROM service_groups WHERE active=1 ORDER BY sort_order,id LIMIT 1').fetchone(); c.close()
    if g: await state.update_data(group_id=g['id']); await render_work_picker(call,state)
    else: await call.answer('Нет категорий работ',show_alert=True); return
    await call.answer()

@dp.callback_query(F.data.startswith('photos:'))
async def photos(call:CallbackQuery):
    sid=int(call.data.split(':')[1]); c=conn(); rows=c.execute('SELECT id,telegram_file_id FROM service_photos WHERE service_id=?',(sid,)).fetchall(); c.close()
    for r in rows:
        await call.message.answer_photo(r['telegram_file_id'],reply_markup=kb([[InlineKeyboardButton(text='🗑 Удалить фото',callback_data=f'delphoto:{r["id"]}')]]))
    await call.answer()

# Client add car
# Global admin menu actions. These are registered before FSM input handlers,
# so a menu button always works even if an unfinished FSM state is still active.
@dp.message(F.text=='➕ Добавить работу')
async def admin_service_global(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    await state.clear()
    c=conn(); rows=c.execute('SELECT c.id,c.brand,c.model,c.engine,c.gearbox,cl.username,cl.id client_id FROM cars c JOIN clients cl ON cl.id=c.client_id ORDER BY c.brand COLLATE NOCASE, c.model COLLATE NOCASE, c.id').fetchall(); c.close()
    buttons=[[InlineKeyboardButton(text=f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — @{r['username']}" if r['username'] else f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — клиент #{r['client_id']}",callback_data=f"newsvc:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text='← Назад',callback_data='service_abort')])
    await message.answer('🧾 <b>NNS SERVICE • Добавление обслуживания</b>\n\nВыберите автомобиль:',reply_markup=kb(buttons),parse_mode='HTML')

@dp.message(F.text=='➕ Добавить автомобиль')
async def add_car_start(message:Message,state:FSMContext):
    cid=upsert_client(message); await state.update_data(client_id=cid); await state.set_state(AddCar.brand); await show_vehicle_makes(message, 'addcar_make:')

@dp.callback_query(F.data.startswith('addcar_make:letter:'))
async def addcar_make_letter(call:CallbackQuery,state:FSMContext):
    rest=call.data.split(':',2)[2]
    if ':page:' in rest:
        letter,pg=rest.split(':page:',1); page=int(pg)
    else:
        letter=rest; page=0
    await show_vehicle_makes(call,'addcar_make:',page,letter); await call.answer()

@dp.callback_query(F.data.startswith('addcar_make:'))
async def ac_make(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=? AND active=1',(mid,)).fetchone(); c.close()
    if not r: await call.answer('Марка не найдена',show_alert=True); return
    await state.update_data(brand=r['name'],make_id=mid); await state.set_state(AddCar.model); await show_vehicle_models(call,mid,'addcar_model:'); await call.answer()

@dp.callback_query(F.data.startswith('addcar_model:'))
async def ac_model_choice(call:CallbackQuery,state:FSMContext):
    model_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT m.name,vm.name make_name FROM vehicle_models m JOIN vehicle_makes vm ON vm.id=m.make_id WHERE m.id=? AND m.active=1',(model_id,)).fetchone(); c.close()
    if not r: await call.answer('Модель не найдена',show_alert=True); return
    await state.update_data(model=r['name'],brand=r['make_name'],model_id=model_id)
    await state.set_state(AddCar.year)
    await call.message.edit_text(f'🚗 <b>{r["make_name"]} {r["name"]}</b>\n\n📅 Выберите год выпуска:',parse_mode='HTML',reply_markup=year_markup('addcar_year:'))
    await call.answer()

async def show_vehicle_engines(target, model_id, prefix='veh_engine:'):
    c=conn(); model=c.execute('SELECT m.name,vm.name make_name FROM vehicle_models m JOIN vehicle_makes vm ON vm.id=m.make_id WHERE m.id=? AND m.active=1',(model_id,)).fetchone(); rows=c.execute('SELECT * FROM vehicle_engines WHERE model_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(model_id,)).fetchall(); c.close()
    if not model: return
    # The external database contains many entries that differ only by power
    # output. For the mechanic's selection screen, collapse those variants
    # into one practical label (2.0 TDI instead of 2.0 TDI 110 kW / 140 kW / 150 hp).
    grouped=[]; seen=set()
    for r in rows:
        label=simplify_engine_name(r['name'])
        key=_vkey(label)
        if key in seen: continue
        seen.add(key)
        grouped.append((label,r['id']))
    buttons=[[InlineKeyboardButton(text=f'🔧 {label}',callback_data=f'{prefix}{eid}')] for label,eid in grouped]
    buttons.append([InlineKeyboardButton(text='✍️ Ввести вручную',callback_data=f'{prefix}manual')])
    buttons.append([InlineKeyboardButton(text='⏭️ Пропустить',callback_data=f'{prefix}skip')])
    buttons.append([InlineKeyboardButton(text='← К моделям',callback_data=f'addcar_back_models:{model_id}')])
    if isinstance(target, Message): await target.answer(f'🚗 <b>{model["make_name"]} {model["name"]}</b>\n\n🔧 Выберите двигатель:',parse_mode='HTML',reply_markup=kb(buttons))
    else: await target.message.edit_text(f'🚗 <b>{model["make_name"]} {model["name"]}</b>\n\n🔧 Выберите двигатель:',parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith('addcar_engine:'))
async def ac_engine_choice(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]; d=await state.get_data()
    if val=='skip': await state.update_data(engine=''); await state.set_state(AddCar.gearbox); await show_vehicle_gearboxes(call,d['model_id'],'addcar_gearbox:'); await call.answer(); return
    if val=='manual': await call.message.edit_text('🔧 Введите двигатель / мотор:',reply_markup=skip_markup('addcar_skip_engine')); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_engines WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('Двигатель не найден',show_alert=True); return
    await state.update_data(engine=r['name']); await state.set_state(AddCar.gearbox); await show_vehicle_gearboxes(call,d['model_id'],'addcar_gearbox:'); await call.answer()

async def show_vehicle_gearboxes(target, model_id, prefix='veh_gearbox:'):
    c=conn(); model=c.execute('SELECT m.name,vm.name make_name FROM vehicle_models m JOIN vehicle_makes vm ON vm.id=m.make_id WHERE m.id=? AND m.active=1',(model_id,)).fetchone(); rows=c.execute('SELECT * FROM vehicle_gearboxes WHERE model_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(model_id,)).fetchall(); c.close()
    if not model: return
    buttons=[[InlineKeyboardButton(text=f'⚙️ {r["name"]}',callback_data=f'{prefix}{r["id"]}')] for r in rows]
    buttons.append([InlineKeyboardButton(text='✍️ Ввести вручную',callback_data=f'{prefix}manual')])
    buttons.append([InlineKeyboardButton(text='⏭️ Пропустить',callback_data=f'{prefix}skip')])
    buttons.append([InlineKeyboardButton(text='← К двигателям',callback_data=f'addcar_back_engine:{model_id}')])
    if isinstance(target, Message): await target.answer(f'🚗 <b>{model["make_name"]} {model["name"]}</b>\n\n⚙️ Выберите КПП:',parse_mode='HTML',reply_markup=kb(buttons))
    else: await target.message.edit_text(f'🚗 <b>{model["make_name"]} {model["name"]}</b>\n\n⚙️ Выберите КПП:',parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith('addcar_gearbox:'))
async def ac_gearbox_choice(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]; d=await state.get_data()
    if val=='skip': await state.update_data(gearbox=''); await state.set_state(AddCar.vin); await call.message.edit_text('VIN?',reply_markup=skip_markup('addcar_skip_vin')); await call.answer(); return
    if val=='manual': await call.message.edit_text('⚙️ Введите КПП:',reply_markup=skip_markup('addcar_skip_gearbox')); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_gearboxes WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('КПП не найдена',show_alert=True); return
    await state.update_data(gearbox=r['name']); await state.set_state(AddCar.vin); await call.message.edit_text('VIN?',reply_markup=skip_markup('addcar_skip_vin')); await call.answer()

@dp.callback_query(F.data.startswith('addcar_back_engine:'))
async def ac_back_engine(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); await state.set_state(AddCar.engine); await show_vehicle_engines(call,mid,'addcar_engine:'); await call.answer()

@dp.callback_query(F.data=='addcar_back_makes')
async def ac_back_makes(call:CallbackQuery,state:FSMContext):
    await state.set_state(AddCar.brand)
    await show_vehicle_makes(call,'addcar_make:')
    await call.answer()

@dp.callback_query(F.data.startswith('addcar_back_models:'))
async def ac_back_models(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1])
    await state.set_state(AddCar.model)
    await show_vehicle_models(call,mid,'addcar_model:')
    await call.answer()

@dp.message(AddCar.brand)
async def ac_brand(message:Message,state:FSMContext):
    # Оставлено для совместимости со старыми состояниями. Новая форма выбирает марку кнопкой.
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(brand=message.text); await state.set_state(AddCar.model); await message.answer('Модель?',reply_markup=cancel_markup())
@dp.message(AddCar.model)
async def ac_model(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(model=message.text); await state.set_state(AddCar.year); await message.answer('Год выпуска? Выберите год:',reply_markup=year_markup('addcar_year:'))
@dp.callback_query(F.data.startswith('addcar_year:'))
async def ac_year_choice(call:CallbackQuery,state:FSMContext):
    v=call.data.split(':',1)[1]
    if v=='skip': await state.update_data(year=None)
    else: await state.update_data(year=int(v))
    await state.set_state(AddCar.engine); d=await state.get_data(); await show_vehicle_engines(call,d.get('model_id'),'addcar_engine:'); await call.answer()

@dp.message(AddCar.year)
async def ac_year(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    try: y=int(message.text)
    except: await message.answer('Выберите год кнопкой или введите число.'); return
    if y<2000 or y>2026: await message.answer('Год должен быть от 2000 до 2024.'); return
    await state.update_data(year=y); await state.set_state(AddCar.engine); d=await state.get_data(); await show_vehicle_engines(message,d.get('model_id'),'addcar_engine:')

@dp.callback_query(F.data=='addcar_skip_engine')
async def ac_skip_engine(call:CallbackQuery,state:FSMContext):
    await state.update_data(engine=''); d=await state.get_data(); await state.set_state(AddCar.gearbox); await show_vehicle_gearboxes(call,d.get('model_id'),'addcar_gearbox:'); await call.answer()

@dp.callback_query(F.data=='addcar_skip_gearbox')
async def ac_skip_gearbox(call:CallbackQuery,state:FSMContext):
    await state.update_data(gearbox=''); await state.set_state(AddCar.vin); await call.message.edit_text('VIN',reply_markup=skip_markup('addcar_skip_vin')); await call.answer()
@dp.message(AddCar.engine)
async def ac_engine(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(engine=message.text); await state.set_state(AddCar.gearbox); await message.answer('КПП?\nНапример: 6МКПП, EDC, AL4',reply_markup=skip_markup('addcar_skip_gearbox'))
@dp.message(AddCar.gearbox)
async def ac_gearbox(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(gearbox=message.text); await state.set_state(AddCar.vin); await message.answer('VIN',reply_markup=skip_markup('addcar_skip_vin'))
@dp.callback_query(F.data=='addcar_skip_vin')
async def ac_skip_vin(call:CallbackQuery,state:FSMContext):
    await state.update_data(vin='')
    await state.set_state(AddCar.registration)
    await call.message.edit_text('🔢 Регистрационный номер автомобиля?',reply_markup=skip_markup('addcar_skip_registration'))
    await call.answer()

@dp.message(AddCar.vin)
async def ac_vin(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(vin='' if message.text=='-' else message.text.upper()); await state.set_state(AddCar.registration); await message.answer('🔢 Регистрационный номер автомобиля?',reply_markup=skip_markup('addcar_skip_registration'))
@dp.callback_query(F.data=='addcar_skip_registration')
async def ac_skip_registration(call:CallbackQuery,state:FSMContext):
    await state.update_data(registration_number='')
    await state.set_state(AddCar.mileage)
    await call.message.edit_text('Текущий пробег, км?',reply_markup=skip_markup('addcar_skip_mileage'))
    await call.answer()

@dp.message(AddCar.registration)
async def ac_registration(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    await state.update_data(registration_number='' if message.text=='-' else message.text.strip().upper())
    await state.set_state(AddCar.mileage)
    await message.answer('Текущий пробег, км?',reply_markup=skip_markup('addcar_skip_mileage'))

@dp.message(AddCar.mileage)
async def ac_mileage(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    try: m=int(message.text.replace(' ',''))
    except: await message.answer('Введите пробег числом.'); return
    await state.update_data(mileage=m); await state.set_state(AddCar.photo); await message.answer('Фото автомобиля?',reply_markup=skip_markup('addcar_skip_photo'))
@dp.callback_query(F.data=='addcar_skip_mileage')
async def ac_skip_mileage(call:CallbackQuery,state:FSMContext):
    await state.update_data(mileage=0)
    await state.set_state(AddCar.photo)
    await call.message.edit_text('Фото автомобиля?',reply_markup=skip_markup('addcar_skip_photo'))
    await call.answer()

@dp.callback_query(F.data=='addcar_skip_photo')
async def ac_skip_photo(call:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if not d.get('brand') or not d.get('model'):
        await call.answer('Данные автомобиля потеряны. Начните добавление заново.',show_alert=True); return
    try:
        await _insert_car(state,call.from_user.id,None)
        await call.message.edit_text('✅ Автомобиль добавлен без фото.')
        await call.message.answer('Главное меню NNS Service 👇',reply_markup=client_menu())
        await call.answer('Готово')
    except Exception:
        logging.exception('addcar skip photo failed')
        await call.answer('Не удалось сохранить автомобиль.',show_alert=True)

async def _insert_car(state, tg_id, photo=None):
    d=await state.get_data()
    cid=d.get('client_id')
    if not cid:
        raise RuntimeError('client_id missing')
    c=conn(); c.execute('INSERT INTO cars(client_id,brand,model,year,vin,mileage,engine,gearbox,photo_file_id,registration_number) VALUES(?,?,?,?,?,?,?,?,?,?)',(cid,d.get('brand',''),d.get('model',''),d.get('year'),d.get('vin',''),d.get('mileage',0),d.get('engine',''),d.get('gearbox',''),photo,d.get('registration_number',''))); car_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]; sync_parts_to_car(c,car_id); c.commit(); c.close(); await state.clear(); return car_id

async def save_client_car(message,state,photo=None):
    try:
        await _insert_car(state,message.from_user.id,photo)
        await message.answer('✅ Автомобиль добавлен.',reply_markup=client_menu())
    except Exception as e:
        logging.exception('save_client_car failed')
        await message.answer(f'❌ Не удалось сохранить автомобиль: {e}')

@dp.message(AddCar.photo,F.photo)
async def ac_photo(message:Message,state:FSMContext):
    await save_client_car(message,state,message.photo[-1].file_id)

@dp.message(AddCar.photo)
async def ac_no_photo(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=client_menu()); return
    if message.text and 'пропуст' in message.text.lower():
        await save_client_car(message,state)
    else:
        await message.answer('Отправьте фото автомобиля или нажмите «⏭️ Пропустить».',reply_markup=skip_markup('addcar_skip_photo'))

# Admin clients
@dp.message(F.text=='➕ Добавить клиента')
async def add_client(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AddClient.username); await message.answer('Введите Telegram username клиента, например @ivan_ivanov',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='addclient_back')]]))
@dp.callback_query(F.data=='addclient_back')
async def addclient_back(call:CallbackQuery,state:FSMContext):
    await state.clear()
    await call.message.edit_text('Главное меню NNS Service 👇',reply_markup=kb([]))
    await call.message.answer('Выберите действие:',reply_markup=admin_menu())
    await call.answer()

@dp.message(AddClient.username)
async def add_client_username(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    u=(message.text or '').strip().lstrip('@').lower()
    if not u or ' ' in u: await message.answer('Введите только username, например @ivan_ivanov.'); return
    c=conn(); ex=c.execute('SELECT id FROM clients WHERE lower(username)=lower(?)',(u,)).fetchone()
    if ex: c.close(); await state.clear(); await message.answer('⚠️ Такой клиент уже есть.',reply_markup=admin_menu()); return
    temp=-int(datetime.now().timestamp()*1000000); c.execute('INSERT INTO clients(tg_id,username,created_at) VALUES(?,?,?)',(temp,u,datetime.now().isoformat())); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close(); await state.clear()
    link=f'https://t.me/NNSauto_bot?start=client_{cid}'
    await message.answer(f'✅ Клиент @{u} добавлен.\n\n🔗 Персональная ссылка:\n{link}\n\nЭту ссылку можно сохранить в контакте клиента. Когда он откроет её и нажмёт Start, аккаунт автоматически привяжется.',reply_markup=admin_menu())
@dp.message(F.text=='👥 Клиенты')
async def clients(message:Message):
    if not is_admin(message.from_user.id): return
    c=conn(); rows=c.execute('SELECT * FROM clients ORDER BY username COLLATE NOCASE, id').fetchall(); c.close(); buttons=[]
    for r in rows:
        label=f"#{r['id']} @{r['username']}" if r['username'] else f"#{r['id']} Telegram ID {r['tg_id']}"
        buttons.append([InlineKeyboardButton(text=label,callback_data=f'client:{r["id"]}')])
    buttons.append([InlineKeyboardButton(text='🔎 Найти клиента или автомобиль',callback_data='client_search')])
    await message.answer('👥 <b>Клиенты</b>\n\nВыберите клиента или воспользуйтесь поиском.',parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data=='client_search')
async def client_search_start(call:CallbackQuery,state:FSMContext):
    await state.set_state(ClientSearch.query)
    await call.message.edit_text('🔎 <b>Поиск</b>\n\nВведите имя пользователя, марку, модель, VIN или гос. номер автомобиля.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='client_search_back')]]))
    await call.answer()

@dp.callback_query(F.data=='client_search_back')
async def client_search_back(call:CallbackQuery,state:FSMContext):
    await state.clear()
    c=conn(); rows=c.execute('SELECT * FROM clients ORDER BY username COLLATE NOCASE, id').fetchall(); c.close()
    buttons=[[InlineKeyboardButton(text=(f"#{r['id']} @{r['username']}" if r['username'] else f"#{r['id']} Telegram ID {r['tg_id']}"),callback_data=f'client:{r["id"]}')] for r in rows]
    buttons.append([InlineKeyboardButton(text='🔎 Найти клиента или автомобиль',callback_data='client_search')])
    await call.message.edit_text('👥 <b>Клиенты</b>',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()

@dp.message(ClientSearch.query)
async def client_search_query(message:Message,state:FSMContext):
    q=(message.text or '').strip()
    if q=='← Назад':
        await state.clear(); await message.answer('👥 Клиенты:',reply_markup=admin_menu()); return
    if not q: await message.answer('Введите текст для поиска.'); return
    like=f'%{q}%'; c=conn()
    clients=c.execute("SELECT id,username,tg_id FROM clients WHERE lower(COALESCE(username,'')) LIKE lower(?) OR CAST(tg_id AS TEXT) LIKE ? ORDER BY username COLLATE NOCASE,id",(like,like)).fetchall()
    cars=c.execute("SELECT cars.id,cars.brand,cars.model,cars.vin,cars.registration_number,cars.client_id,clients.username FROM cars JOIN clients ON clients.id=cars.client_id WHERE lower(cars.brand) LIKE lower(?) OR lower(cars.model) LIKE lower(?) OR lower(COALESCE(cars.vin,'')) LIKE lower(?) OR lower(COALESCE(cars.registration_number,'')) LIKE lower(?) ORDER BY cars.brand COLLATE NOCASE,cars.model COLLATE NOCASE,cars.id",(like,like,like,like)).fetchall(); c.close()
    out=[]
    for r in clients:
        out.append([InlineKeyboardButton(text=(f"👤 @{r['username']}" if r['username'] else f"👤 Клиент #{r['id']}"),callback_data=f'client:{r["id"]}')])
    for r in cars:
        label=f"🚗 {r['brand']} {r['model']}"+(f" — {r['registration_number']}" if r['registration_number'] else '')+(f" — @{r['username']}" if r['username'] else f" — клиент #{r['client_id']}")
        out.append([InlineKeyboardButton(text=label,callback_data=f'car:{r["id"]}')])
    out.append([InlineKeyboardButton(text='🔎 Новый поиск',callback_data='client_search')])
    out.append([InlineKeyboardButton(text='← Назад',callback_data='client_search_back')])
    await state.clear()
    await message.answer(f'🔎 <b>Результаты поиска: {q}</b>\n\nНайдено: {len(clients)} клиентов, {len(cars)} автомобилей.',parse_mode='HTML',reply_markup=kb(out))

@dp.callback_query(F.data.startswith('client:'))
async def client_card(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    cid=int(call.data.split(':')[1]); c=conn(); cl=c.execute('SELECT * FROM clients WHERE id=?',(cid,)).fetchone(); cars=c.execute('SELECT * FROM cars WHERE client_id=? ORDER BY brand COLLATE NOCASE, model COLLATE NOCASE, id',(cid,)).fetchall(); c.close()
    if not cl: await call.answer('Клиент не найден',show_alert=True); return
    label=f"@{cl['username']}" if cl['username'] else f"ID {cl['tg_id']}"; link=f'https://t.me/NNSauto_bot?start=client_{cid}'
    buttons=[[InlineKeyboardButton(text=f"🚗 {r['brand']} {r['model']}",callback_data=f"car:{r['id']}")] for r in cars]
    buttons += [[InlineKeyboardButton(text='➕ Добавить автомобиль',callback_data=f'adminaddcar:{cid}')],[InlineKeyboardButton(text='🔗 Персональная ссылка',callback_data=f'clientlink:{cid}')],[InlineKeyboardButton(text='🗑 Удалить клиента',callback_data=f'confirm_delete_client:{cid}')],[InlineKeyboardButton(text='← Назад',callback_data='back_clients')]]
    await call.message.edit_text(f'👤 {label}\nID клиента: {cid}\nАвтомобилей: {len(cars)}\n\n🔗 {link}',reply_markup=kb(buttons)); await call.answer()
@dp.callback_query(F.data.startswith('confirm_delete_car:'))
async def confirm_delete_car(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    car_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT brand,model FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    if not r: await call.answer('Машина не найдена',show_alert=True); return
    await call.message.edit_text(f'⚠️ Вы точно хотите удалить машину {r["brand"]} {r["model"]}?\n\nБудет удалена вся история обслуживания этой машины.',reply_markup=kb([[InlineKeyboardButton(text='❌ Да, удалить',callback_data=f'delete_car:{car_id}')],[InlineKeyboardButton(text='← Отмена',callback_data=f'car:{car_id}')]])); await call.answer()

@dp.callback_query(F.data.startswith('delete_car:'))
async def delete_car(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    car_id=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT brand,model,client_id FROM cars WHERE id=?',(car_id,)).fetchone(); c.close()
    if not r: await call.answer('Машина уже удалена',show_alert=True); return
    await _clear_history_messages(car_id)
    c=conn(); c.execute('DELETE FROM cars WHERE id=?',(car_id,)); c.commit(); c.close()
    await call.answer('Машина удалена')
    await call.message.edit_text('🗑 Машина удалена.',reply_markup=kb([[InlineKeyboardButton(text='← К клиенту',callback_data=f'client:{r["client_id"]}')],[InlineKeyboardButton(text='👥 Клиенты',callback_data='back_clients')]]))

@dp.callback_query(F.data.startswith('confirm_delete_client:'))
async def confirm_delete_client(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    cid=int(call.data.split(':')[1]); c=conn(); cl=c.execute('SELECT username,tg_id FROM clients WHERE id=?',(cid,)).fetchone(); cars=c.execute('SELECT id FROM cars WHERE client_id=?',(cid,)).fetchall(); c.close()
    if not cl: await call.answer('Клиент не найден',show_alert=True); return
    label=f'@{cl["username"]}' if cl['username'] else f'клиента #{cid}'
    await call.message.edit_text(f'⚠️ Вы точно хотите удалить {label}?\n\nБудут удалены все автомобили клиента и вся история обслуживания.',reply_markup=kb([[InlineKeyboardButton(text='❌ Да, удалить клиента',callback_data=f'delete_client:{cid}')],[InlineKeyboardButton(text='← Отмена',callback_data=f'client:{cid}')]])); await call.answer()

@dp.callback_query(F.data.startswith('delete_client:'))
async def delete_client(call:CallbackQuery):
    if not is_admin(call.from_user.id): return
    cid=int(call.data.split(':')[1]); c=conn(); cl=c.execute('SELECT username,tg_id FROM clients WHERE id=?',(cid,)).fetchone(); cars=c.execute('SELECT id FROM cars WHERE client_id=?',(cid,)).fetchall(); c.close()
    if not cl: await call.answer('Клиент уже удалён',show_alert=True); return
    for car in cars: await _clear_history_messages(car['id'])
    c=conn(); c.execute('DELETE FROM clients WHERE id=?',(cid,)); c.commit(); c.close()
    await call.answer('Клиент удалён')
    await back_clients(call)

@dp.callback_query(F.data=='back_clients')
async def back_clients(call:CallbackQuery):
    c=conn(); rows=c.execute('SELECT * FROM clients ORDER BY username COLLATE NOCASE, id').fetchall(); c.close()
    buttons=[[InlineKeyboardButton(text=(f"#{r['id']} @{r['username']}" if r['username'] else f"#{r['id']} Telegram ID {r['tg_id']}"),callback_data=f'client:{r["id"]}')] for r in rows]
    buttons.append([InlineKeyboardButton(text='🔎 Найти клиента или автомобиль',callback_data='client_search')])
    await call.message.edit_text('👥 <b>Клиенты</b>',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()
@dp.callback_query(F.data.startswith('clientlink:'))
async def client_link(call:CallbackQuery):
    cid=int(call.data.split(':')[1]); await call.message.edit_text(f'🔗 Персональная ссылка\n\nhttps://t.me/NNSauto_bot?start=client_{cid}',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data=f'client:{cid}')]])); await call.answer()

# Admin add car
@dp.callback_query(F.data.startswith('adminaddcar:'))
async def admin_car_start(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    cid=int(call.data.split(':')[1])
    await state.update_data(client_id=cid)
    await state.set_state(AdminAddCar.brand)
    await show_vehicle_makes(call, 'adminaddcar_make:')
    await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_make:letter:'))
async def adminaddcar_make_letter(call:CallbackQuery,state:FSMContext):
    rest=call.data.split(':',2)[2]
    if ':page:' in rest:
        letter,pg=rest.split(':page:',1); page=int(pg)
    else:
        letter=rest; page=0
    await show_vehicle_makes(call,'adminaddcar_make:',page,letter); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_make:'))
async def aac_make_choice(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1])
    c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=? AND active=1',(mid,)).fetchone(); c.close()
    if not r:
        await call.answer('Марка не найдена',show_alert=True); return
    await state.update_data(brand=r['name'],make_id=mid)
    await state.set_state(AdminAddCar.model)
    await show_vehicle_models(call,mid,'adminaddcar_model:')
    await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_model:'))
async def aac_model_choice(call:CallbackQuery,state:FSMContext):
    model_id=int(call.data.split(':')[1])
    c=conn(); r=c.execute('SELECT m.name,vm.name make_name FROM vehicle_models m JOIN vehicle_makes vm ON vm.id=m.make_id WHERE m.id=? AND m.active=1 AND vm.active=1',(model_id,)).fetchone(); c.close()
    if not r:
        await call.answer('Модель не найдена',show_alert=True); return
    await state.update_data(model=r['name'],brand=r['make_name'],model_id=model_id)
    await state.set_state(AdminAddCar.year)
    await call.message.edit_text(f'🚗 <b>{r["make_name"]} {r["name"]}</b>\n\n📅 Выберите год выпуска:',parse_mode='HTML',reply_markup=year_markup('adminaddcar_year:'))
    await call.answer()

@dp.callback_query(F.data=='adminaddcar_back_makes')
async def aac_back_makes(call:CallbackQuery,state:FSMContext):
    await state.set_state(AdminAddCar.brand)
    await show_vehicle_makes(call,'adminaddcar_make:')
    await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_back_models:'))
async def aac_back_models(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1])
    await state.set_state(AdminAddCar.model)
    await show_vehicle_models(call,mid,'adminaddcar_model:')
    await call.answer()

@dp.message(AdminAddCar.brand)
async def aac_brand(message:Message,state:FSMContext):
    # Legacy text input compatibility; normal flow now uses the catalog buttons.
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(brand=message.text); await state.set_state(AdminAddCar.model); await message.answer('Модель?',reply_markup=cancel_markup())
@dp.message(AdminAddCar.model)
async def aac_model(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(model=message.text); await state.set_state(AdminAddCar.year); await message.answer('Год?',reply_markup=cancel_markup())
@dp.callback_query(F.data.startswith('adminaddcar_year:'))
async def aac_year_choice(call:CallbackQuery,state:FSMContext):
    v=call.data.split(':',1)[1]
    if v=='skip': await state.update_data(year=None)
    else: await state.update_data(year=int(v))
    await state.set_state(AdminAddCar.engine); d=await state.get_data(); await show_vehicle_engines(call,d.get('model_id'),'adminaddcar_engine:'); await call.answer()

@dp.message(AdminAddCar.year)
async def aac_year(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    try: y=int(message.text)
    except: await message.answer('Выберите год кнопкой или введите число.'); return
    if y<2000 or y>2026: await message.answer('Год должен быть от 2000 до 2024.'); return
    await state.update_data(year=y); await state.set_state(AdminAddCar.engine); d=await state.get_data(); await show_vehicle_engines(message,d.get('model_id'),'adminaddcar_engine:')

@dp.callback_query(F.data.startswith('adminaddcar_engine:'))
async def aac_engine_choice(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]; d=await state.get_data()
    if val=='skip': await state.update_data(engine=''); await state.set_state(AdminAddCar.gearbox); await show_vehicle_gearboxes(call,d['model_id'],'adminaddcar_gearbox:'); await call.answer(); return
    if val=='manual': await call.message.edit_text('🔧 Введите двигатель / мотор:',reply_markup=skip_markup('adminaddcar_skip_engine')); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_engines WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('Двигатель не найден',show_alert=True); return
    await state.update_data(engine=r['name']); await state.set_state(AdminAddCar.gearbox); await show_vehicle_gearboxes(call,d['model_id'],'adminaddcar_gearbox:'); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_gearbox:'))
async def aac_gearbox_choice(call:CallbackQuery,state:FSMContext):
    val=call.data.split(':',1)[1]; d=await state.get_data()
    if val=='skip': await state.update_data(gearbox=''); await state.set_state(AdminAddCar.vin); await call.message.edit_text('VIN? Или -',reply_markup=skip_markup('adminaddcar_skip_vin')); await call.answer(); return
    if val=='manual': await call.message.edit_text('⚙️ Введите КПП:',reply_markup=skip_markup('adminaddcar_skip_gearbox')); await call.answer(); return
    c=conn(); r=c.execute('SELECT name FROM vehicle_gearboxes WHERE id=? AND active=1',(int(val),)).fetchone(); c.close()
    if not r: await call.answer('КПП не найдена',show_alert=True); return
    await state.update_data(gearbox=r['name']); await state.set_state(AdminAddCar.vin); await call.message.edit_text('VIN? Или -',reply_markup=skip_markup('adminaddcar_skip_vin')); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_back_engine:'))
async def aac_back_engine(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); await state.set_state(AdminAddCar.engine); await show_vehicle_engines(call,mid,'adminaddcar_engine:'); await call.answer()

@dp.callback_query(F.data=='adminaddcar_skip_engine')
async def aac_skip_engine(call:CallbackQuery,state:FSMContext):
    await state.update_data(engine=''); d=await state.get_data(); await state.set_state(AdminAddCar.gearbox); await show_vehicle_gearboxes(call,d.get('model_id'),'adminaddcar_gearbox:'); await call.answer()

@dp.callback_query(F.data=='adminaddcar_skip_gearbox')
async def aac_skip_gearbox(call:CallbackQuery,state:FSMContext):
    await state.update_data(gearbox=''); await state.set_state(AdminAddCar.vin); await call.message.edit_text('VIN',reply_markup=skip_markup('addcar_skip_vin')); await call.answer()
@dp.message(AdminAddCar.engine)
async def aac_engine(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(engine=message.text); await state.set_state(AdminAddCar.gearbox); await message.answer('КПП?\nНапример: 6МКПП, EDC, AL4',reply_markup=skip_markup('adminaddcar_skip_gearbox'))
@dp.message(AdminAddCar.gearbox)
async def aac_gearbox(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(gearbox=message.text); await state.set_state(AdminAddCar.vin); await message.answer('VIN',reply_markup=skip_markup('addcar_skip_vin'))
@dp.callback_query(F.data=='adminaddcar_skip_vin')
async def aac_skip_vin(call:CallbackQuery,state:FSMContext):
    await state.update_data(vin='')
    await state.set_state(AdminAddCar.registration)
    await call.message.edit_text('🔢 Регистрационный номер автомобиля?',reply_markup=skip_markup('adminaddcar_skip_registration'))
    await call.answer()

@dp.message(AdminAddCar.vin)
async def aac_vin(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(vin='' if message.text=='-' else message.text.upper()); await state.set_state(AdminAddCar.registration); await message.answer('🔢 Регистрационный номер автомобиля?',reply_markup=skip_markup('adminaddcar_skip_registration'))
@dp.callback_query(F.data=='adminaddcar_skip_registration')
async def aac_skip_registration(call:CallbackQuery,state:FSMContext):
    await state.update_data(registration_number='')
    await state.set_state(AdminAddCar.mileage)
    await call.message.edit_text('Пробег, км?',reply_markup=skip_markup('adminaddcar_skip_mileage'))
    await call.answer()

@dp.message(AdminAddCar.registration)
async def aac_registration(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(registration_number='' if message.text=='-' else message.text.strip().upper())
    await state.set_state(AdminAddCar.mileage)
    await message.answer('Пробег, км?',reply_markup=skip_markup('adminaddcar_skip_mileage'))

@dp.message(AdminAddCar.mileage)
async def aac_mileage(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    try: m=int(message.text.replace(' ',''))
    except: await message.answer('Введите пробег числом.'); return
    await state.update_data(mileage=m); await state.set_state(AdminAddCar.photo); await message.answer('Фото автомобиля?',reply_markup=skip_markup('adminaddcar_skip_photo'))
@dp.callback_query(F.data=='adminaddcar_skip_mileage')
async def aac_skip_mileage(call:CallbackQuery,state:FSMContext):
    await state.update_data(mileage=0)
    await state.set_state(AdminAddCar.photo)
    await call.message.edit_text('Фото автомобиля?',reply_markup=skip_markup('adminaddcar_skip_photo'))
    await call.answer()

@dp.callback_query(F.data=='adminaddcar_skip_photo')
async def aac_skip_photo(call:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if not d.get('brand') or not d.get('model'):
        await call.answer('Данные автомобиля потеряны. Начните добавление заново.',show_alert=True); return
    try:
        await _insert_car(state,call.from_user.id,None)
        await call.message.edit_text('✅ Автомобиль добавлен без фото.')
        await call.message.answer('Главное меню NNS Service 👇',reply_markup=admin_menu())
        await call.answer('Готово')
    except Exception:
        logging.exception('admin addcar skip photo failed')
        await call.answer('Не удалось сохранить автомобиль.',show_alert=True)

async def save_admin_car(message,state,photo=None):
    try:
        await _insert_car(state,message.from_user.id,photo)
        await message.answer('✅ Автомобиль добавлен.',reply_markup=admin_menu())
    except Exception as e:
        logging.exception('save_admin_car failed')
        await message.answer(f'❌ Не удалось сохранить автомобиль: {e}')
@dp.message(AdminAddCar.photo,F.photo)
async def aac_photo(message:Message,state:FSMContext): await save_admin_car(message,state,message.photo[-1].file_id)
@dp.message(AdminAddCar.photo)
async def aac_no_photo(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    if message.text and 'пропуст' in message.text.lower(): await save_admin_car(message,state)
    else: await message.answer('Отправьте фото автомобиля или нажмите «⏭️ Пропустить».',reply_markup=skip_markup('adminaddcar_skip_photo'))

def year_markup(prefix):
    rows=[]
    years=list(range(2000,2027))
    for i in range(0,len(years),5):
        rows.append([InlineKeyboardButton(text=str(y),callback_data=f'{prefix}{y}') for y in years[i:i+5]])
    rows.append([InlineKeyboardButton(text='⏭️ Пропустить',callback_data=prefix+'skip')])
    rows.append([InlineKeyboardButton(text='← Назад',callback_data='fsm_back')])
    return kb(rows)

async def show_vehicle_makes(target, prefix='veh_make:', page=0, letter=None):
    c=conn(); rows=c.execute('SELECT * FROM vehicle_makes WHERE active=1 ORDER BY name COLLATE NOCASE,id').fetchall(); c.close()
    # При добавлении автомобиля сначала показываем только буквы, чтобы быстро найти марку.
    if prefix in ('addcar_make:', 'adminaddcar_make:') and letter is None:
        letters=[]
        for r in rows:
            name=str(r['name']).strip()
            if name:
                ch=name[0].upper()
                if ch not in letters: letters.append(ch)
        letters=sorted(letters)
        buttons=[]
        for i in range(0,len(letters),7):
            buttons.append([InlineKeyboardButton(text=x,callback_data=f'{prefix}letter:{x}') for x in letters[i:i+7]])
        buttons.append([InlineKeyboardButton(text='← Назад',callback_data='fsm_back')])
        title='🚗 <b>Выберите первую букву марки</b>'
        if isinstance(target, Message): await target.answer(title,parse_mode='HTML',reply_markup=kb(buttons))
        else: await target.message.edit_text(title,parse_mode='HTML',reply_markup=kb(buttons))
        return
    if letter is not None:
        letter=str(letter).upper()
        rows=[r for r in rows if str(r['name']).strip().upper().startswith(letter)]
        page_size=50
        total_pages=max(1,(len(rows)+page_size-1)//page_size); page=max(0,min(int(page),total_pages-1)); chunk=rows[page*page_size:(page+1)*page_size]
        buttons=[[InlineKeyboardButton(text=f'🚗 {r["name"]}',callback_data=f'{prefix}{r["id"]}')] for r in chunk]
        nav=[]
        if page>0: nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'{prefix}letter:{letter}:page:{page-1}'))
        if page<total_pages-1: nav.append(InlineKeyboardButton(text='➡️',callback_data=f'{prefix}letter:{letter}:page:{page+1}'))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text='← К буквам',callback_data=f'{prefix}letters')])
        title=f'🚗 <b>Марки на букву {letter}</b>' + (f'\nСтраница {page+1} из {total_pages}' if total_pages>1 else '')
        if isinstance(target, Message): await target.answer(title,parse_mode='HTML',reply_markup=kb(buttons))
        else: await target.message.edit_text(title,parse_mode='HTML',reply_markup=kb(buttons))
        return
    page_size=50; total_pages=max(1,(len(rows)+page_size-1)//page_size); page=max(0,min(int(page),total_pages-1)); chunk=rows[page*page_size:(page+1)*page_size]
    buttons=[[InlineKeyboardButton(text=f'🚗 {r["name"]}',callback_data=f'{prefix}{r["id"]}')] for r in chunk]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='⬅️ Предыдущая',callback_data=f'{prefix}page:{page-1}'))
    if page<total_pages-1: nav.append(InlineKeyboardButton(text='Следующая ➡️',callback_data=f'{prefix}page:{page+1}'))
    if nav: buttons.append(nav)
    if total_pages>1: buttons.append([InlineKeyboardButton(text=f'📄 {page+1}/{total_pages}',callback_data='noop')])
    buttons.append([InlineKeyboardButton(text='← Назад',callback_data='fsm_back')])
    title=f'🚗 <b>Выберите марку</b>\n\nСтраница {page+1} из {total_pages}' if total_pages>1 else '🚗 <b>Выберите марку</b>'
    if isinstance(target, Message): await target.answer(title,parse_mode='HTML',reply_markup=kb(buttons))
    else: await target.message.edit_text(title,parse_mode='HTML',reply_markup=kb(buttons))

async def show_vehicle_models(target, make_id, prefix='veh_model:', page=0):
    # Модели показываем одним списком. Переходов по страницам здесь нет —
    # после выбора марки пользователь сразу видит весь доступный список моделей.
    c=conn(); make=c.execute('SELECT name FROM vehicle_makes WHERE id=? AND active=1',(make_id,)).fetchone(); rows=c.execute('SELECT * FROM vehicle_models WHERE make_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(make_id,)).fetchall(); c.close()
    if not make: return
    buttons=[[InlineKeyboardButton(text=f'🚘 {r["name"]}',callback_data=f'{prefix}{r["id"]}')] for r in rows]
    back_data='veh_back_makes' if prefix=='veh_model:' else ('adminaddcar_back_makes' if prefix=='adminaddcar_model:' else 'addcar_back_makes')
    buttons.append([InlineKeyboardButton(text='← К маркам',callback_data=back_data)])
    title=f'🚗 <b>{make["name"]}</b>\n\nВыберите модель:'
    if not rows: title += '\n\nМоделей пока нет.'
    if isinstance(target, Message): await target.answer(title,parse_mode='HTML',reply_markup=kb(buttons))
    else: await target.message.edit_text(title,parse_mode='HTML',reply_markup=kb(buttons))

async def render_vehicle_catalog(call, page='makes'):
    c=conn()
    if str(page).startswith('makes'):
        raw_page=str(page).split(':',1)[1] if ':' in str(page) else '0'
        try: pg=int(raw_page)
        except: pg=0
        rows=c.execute('SELECT * FROM vehicle_makes WHERE active=1 ORDER BY name COLLATE NOCASE,id').fetchall(); c.close()
        page_size=50; total_pages=max(1,(len(rows)+page_size-1)//page_size); pg=max(0,min(pg,total_pages-1)); chunk=rows[pg*page_size:(pg+1)*page_size]
        buttons=[[InlineKeyboardButton(text=f'🚗 {r["name"]}',callback_data=f'vehcatmake:{r["id"]}')] for r in chunk]
        nav=[]
        if pg>0: nav.append(InlineKeyboardButton(text='⬅️ Предыдущая',callback_data=f'vehcat_page:{pg-1}'))
        if pg<total_pages-1: nav.append(InlineKeyboardButton(text='Следующая ➡️',callback_data=f'vehcat_page:{pg+1}'))
        if nav: buttons.append(nav)
        if total_pages>1: buttons.append([InlineKeyboardButton(text=f'📄 {pg+1}/{total_pages}',callback_data='noop')])
        buttons += [[InlineKeyboardButton(text='✏️ Редактировать марки',callback_data='veh_edit_makes')],[InlineKeyboardButton(text='❌ Закрыть',callback_data='veh_close')]]
        title='🚗 <b>Справочник автомобилей</b>\n\nВыберите марку.' + (f'\nСтраница {pg+1} из {total_pages}' if total_pages>1 else '')
        await call.message.edit_text(title,parse_mode='HTML',reply_markup=kb(buttons)); return
    mid=int(page); make=c.execute('SELECT * FROM vehicle_makes WHERE id=? AND active=1',(mid,)).fetchone(); models=c.execute('SELECT * FROM vehicle_models WHERE make_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(mid,)).fetchall(); c.close()
    if not make: return
    buttons=[[InlineKeyboardButton(text=f'🚘 {m["name"]}',callback_data=f'vehcatmodel:{m["id"]}')] for m in models]
    buttons += [[InlineKeyboardButton(text='➕ Добавить модель',callback_data=f'veh_new_model:{mid}')],[InlineKeyboardButton(text='✏️ Редактировать модели',callback_data=f'veh_edit_models:{mid}')],[InlineKeyboardButton(text='🗑 Скрыть марку',callback_data=f'veh_hide_make:{mid}')],[InlineKeyboardButton(text='← К маркам',callback_data='veh_back_makes')]]
    await call.message.edit_text(f'🚗 <b>{make["name"]}</b>\n\nВыберите модель или действие.',parse_mode='HTML',reply_markup=kb(buttons))

@dp.message(F.text=='🚗 Справочник автомобилей')
async def vehicle_catalog_menu(message:Message):
    if not is_admin(message.from_user.id): return
    c=conn(); rows=c.execute('SELECT * FROM vehicle_makes WHERE active=1 ORDER BY name COLLATE NOCASE,id').fetchall(); c.close()
    page_size=50; total_pages=max(1,(len(rows)+page_size-1)//page_size); pg=0; chunk=rows[:page_size]
    buttons=[[InlineKeyboardButton(text=f'🚗 {r["name"]}',callback_data=f'vehcatmake:{r["id"]}')] for r in chunk]
    if total_pages>1: buttons.append([InlineKeyboardButton(text='Следующая ➡️',callback_data='vehcat_page:1')])
    if total_pages>1: buttons.append([InlineKeyboardButton(text=f'📄 1/{total_pages}',callback_data='noop')])
    buttons += [[InlineKeyboardButton(text='✏️ Редактировать марки',callback_data='veh_edit_makes')],[InlineKeyboardButton(text='❌ Закрыть',callback_data='veh_close')]]
    await message.answer('🚗 <b>Справочник автомобилей</b>\n\nВыберите марку.' + (f'\nСтраница 1 из {total_pages}' if total_pages>1 else ''),parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith('vehcat_page:'))
async def vehcat_page(call:CallbackQuery):
    pg=int(call.data.split(':')[1]); await render_vehicle_catalog(call,f'makes:{pg}'); await call.answer()

@dp.callback_query(F.data.startswith('addcar_make:letter:'))
async def ac_make_letter(call:CallbackQuery,state:FSMContext):
    parts=call.data.split(':')
    letter=parts[2]
    page=int(parts[4]) if len(parts)>4 and parts[3]=='page' else 0
    await show_vehicle_makes(call,'addcar_make:',page,letter); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_make:letter:'))
async def aac_make_letter(call:CallbackQuery,state:FSMContext):
    parts=call.data.split(':')
    letter=parts[2]
    page=int(parts[4]) if len(parts)>4 and parts[3]=='page' else 0
    await show_vehicle_makes(call,'adminaddcar_make:',page,letter); await call.answer()

@dp.callback_query(F.data.startswith('addcar_make:page:'))
async def ac_make_page(call:CallbackQuery,state:FSMContext):
    pg=int(call.data.split(':')[2]); await show_vehicle_makes(call,'addcar_make:',pg); await call.answer()

@dp.callback_query(F.data=='addcar_make:letters')
async def ac_make_letters(call:CallbackQuery,state:FSMContext): await show_vehicle_makes(call,'addcar_make:',0,None); await call.answer()

@dp.callback_query(F.data=='adminaddcar_make:letters')
async def aac_make_letters(call:CallbackQuery,state:FSMContext): await show_vehicle_makes(call,'adminaddcar_make:',0,None); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_make:page:'))
async def aac_make_page(call:CallbackQuery,state:FSMContext):
    pg=int(call.data.split(':')[2]); await show_vehicle_makes(call,'adminaddcar_make:',pg); await call.answer()

@dp.callback_query(F.data.startswith('addcar_model_page:'))
async def ac_model_page(call:CallbackQuery,state:FSMContext):
    _,make_id,pg=call.data.split(':'); await show_vehicle_models(call,int(make_id),'addcar_model:',int(pg)); await call.answer()

@dp.callback_query(F.data.startswith('adminaddcar_model_page:'))
async def aac_model_page(call:CallbackQuery,state:FSMContext):
    _,make_id,pg=call.data.split(':'); await show_vehicle_models(call,int(make_id),'adminaddcar_model:',int(pg)); await call.answer()

@dp.callback_query(F.data.startswith('veh_model_page:'))
async def veh_model_page(call:CallbackQuery):
    _,make_id,pg=call.data.split(':'); await show_vehicle_models(call,int(make_id),'veh_model:',int(pg)); await call.answer()

@dp.callback_query(F.data=='veh_back_makes')
async def veh_back_makes(call:CallbackQuery): await render_vehicle_catalog(call,'makes'); await call.answer()

@dp.callback_query(F.data.startswith('vehcatmake:'))
async def vehcat_make(call:CallbackQuery): await render_vehicle_catalog(call,call.data.split(':')[1]); await call.answer()

@dp.callback_query(F.data.startswith('vehcatmodel:'))
async def vehcat_model(call:CallbackQuery):
    mid=int(call.data.split(':')[1]); c=conn(); m=c.execute('SELECT vm.name make_name,v.name,v.make_id FROM vehicle_models v JOIN vehicle_makes vm ON vm.id=v.make_id WHERE v.id=?',(mid,)).fetchone()
    erows=c.execute('SELECT name FROM vehicle_engines WHERE model_id=? AND active=1 ORDER BY sort_order,id',(mid,)).fetchall()
    engines=len({_vkey(simplify_engine_name(r['name'])) for r in erows if simplify_engine_name(r['name'])})
    gears=c.execute('SELECT COUNT(*) FROM vehicle_gearboxes WHERE model_id=? AND active=1',(mid,)).fetchone()[0]; c.close()
    if not m: await call.answer('Модель не найдена',show_alert=True); return
    await call.message.edit_text(f'🚘 <b>{m["make_name"]} {m["name"]}</b>\n\n🔧 Двигатели: {engines}\n⚙️ КПП: {gears}\n\nВыберите раздел.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text=f'🔧 Двигатели ({engines})',callback_data=f'veh_engines:{mid}')],[InlineKeyboardButton(text=f'⚙️ КПП ({gears})',callback_data=f'veh_gearboxes:{mid}')],[InlineKeyboardButton(text='✏️ Переименовать модель',callback_data=f'veh_rename_model:{mid}')],[InlineKeyboardButton(text='🗑 Скрыть модель',callback_data=f'veh_hide_model:{mid}')],[InlineKeyboardButton(text='← Назад',callback_data=f'vehcatmake:{m["make_id"]}')]])); await call.answer()

# Replace make-name back button with numeric make id by resolving it before rendering.
async def render_vehicle_subcatalog(call, model_id, kind, page=0):
    PAGE_SIZE=50
    c=conn(); m=c.execute('SELECT vm.name make_name,v.name,v.make_id FROM vehicle_models v JOIN vehicle_makes vm ON vm.id=v.make_id WHERE v.id=? AND v.active=1 AND vm.active=1',(model_id,)).fetchone()
    table='vehicle_engines' if kind=='engine' else 'vehicle_gearboxes'; label='Двигатели' if kind=='engine' else 'КПП'; icon='🔧' if kind=='engine' else '⚙️'
    all_rows=c.execute(f'SELECT * FROM {table} WHERE model_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(model_id,)).fetchall()
    if kind=='engine':
        grouped=[]; seen=set()
        for r in all_rows:
            label_name=simplify_engine_name(r['name']); key=_vkey(label_name)
            if not label_name or key in seen: continue
            seen.add(key); grouped.append((r,label_name))
        total=len(grouped)
        page=max(0,min(page,max(0,(total-1)//PAGE_SIZE)))
        page_rows=grouped[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    else:
        total=len(all_rows); page=max(0,min(page,max(0,(total-1)//PAGE_SIZE)))
        page_rows=[(r,r['name']) for r in all_rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]
    c.close()
    if not m: return
    pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE); page=max(0,min(page,pages-1)); out=[]
    for r,display_name in page_rows:
        out.append([InlineKeyboardButton(text=f'{icon} {display_name}',callback_data=f'veh_rename_{kind}:{r["id"]}'),InlineKeyboardButton(text='🗑',callback_data=f'veh_hide_{kind}:{r["id"]}:{page}')])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='◀️',callback_data=f'veh_{kind}_page:{model_id}:{page-1}'))
    nav.append(InlineKeyboardButton(text=f'{page+1}/{pages}',callback_data='veh_noop'))
    if page<pages-1: nav.append(InlineKeyboardButton(text='▶️',callback_data=f'veh_{kind}_page:{model_id}:{page+1}'))
    if nav: out.append(nav)
    out.append([InlineKeyboardButton(text='➕ Добавить',callback_data=f'veh_new_{kind}:{model_id}')])
    out.append([InlineKeyboardButton(text='← Назад к модели',callback_data=f'vehcatmodel:{model_id}')])
    await call.message.edit_text(f'{icon} <b>{label}: {m["make_name"]} {m["name"]}</b>\n\nПоказано {page*PAGE_SIZE+1 if total else 0}–{min((page+1)*PAGE_SIZE,total)} из {total}',parse_mode='HTML',reply_markup=kb(out))

@dp.callback_query(F.data=='veh_noop')
async def veh_noop(call:CallbackQuery): await call.answer()

@dp.callback_query(F.data.startswith('veh_engines:'))
async def veh_engines(call:CallbackQuery): await render_vehicle_subcatalog(call,int(call.data.split(':')[1]),'engine'); await call.answer()
@dp.callback_query(F.data.startswith('veh_gearboxes:'))
async def veh_gearboxes(call:CallbackQuery): await render_vehicle_subcatalog(call,int(call.data.split(':')[1]),'gearbox'); await call.answer()

@dp.callback_query(F.data.startswith('veh_engine_page:'))
async def veh_engine_page(call:CallbackQuery):
    _,mid,page=call.data.split(':'); await render_vehicle_subcatalog(call,int(mid),'engine',int(page)); await call.answer()

@dp.callback_query(F.data.startswith('veh_gearbox_page:'))
async def veh_gearbox_page(call:CallbackQuery):
    _,mid,page=call.data.split(':'); await render_vehicle_subcatalog(call,int(mid),'gearbox',int(page)); await call.answer()

@dp.callback_query(F.data.startswith('veh_new_engine:'))
async def veh_new_engine(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); await state.update_data(veh_kind='new_engine',veh_parent=mid,veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.engine); await call.message.edit_text('➕ <b>Новый двигатель</b>\n\nВведите название двигателя.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()
@dp.callback_query(F.data.startswith('veh_new_gearbox:'))
async def veh_new_gearbox(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); await state.update_data(veh_kind='new_gearbox',veh_parent=mid,veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.gearbox); await call.message.edit_text('➕ <b>Новая КПП</b>\n\nВведите название КПП.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('veh_rename_engine:'))
async def veh_rename_engine(call:CallbackQuery,state:FSMContext):
    eid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name,model_id FROM vehicle_engines WHERE id=?',(eid,)).fetchone(); c.close(); await state.update_data(veh_id=eid,veh_kind='engine',veh_parent=r['model_id'],veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.engine); await call.message.edit_text(f'✏️ <b>Переименовать двигатель</b>\n\nСейчас: <code>{r["name"]}</code>\n\nВведите новое название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()
@dp.callback_query(F.data.startswith('veh_rename_gearbox:'))
async def veh_rename_gearbox(call:CallbackQuery,state:FSMContext):
    gid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name,model_id FROM vehicle_gearboxes WHERE id=?',(gid,)).fetchone(); c.close(); await state.update_data(veh_id=gid,veh_kind='gearbox',veh_parent=r['model_id'],veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.gearbox); await call.message.edit_text(f'✏️ <b>Переименовать КПП</b>\n\nСейчас: <code>{r["name"]}</code>\n\nВведите новое название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('veh_rename_make:'))
async def veh_rename_make(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=?',(mid,)).fetchone(); c.close(); await state.update_data(veh_id=mid,veh_kind='make',veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.make); await call.message.edit_text(f'✏️ <b>Переименовать марку</b>\n\nСейчас: <code>{r["name"]}</code>\n\nВведите новое название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('veh_rename_model:'))
async def veh_rename_model(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name,make_id FROM vehicle_models WHERE id=?',(mid,)).fetchone(); c.close(); await state.update_data(veh_id=mid,veh_kind='model',veh_parent=r['make_id'],veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.model); await call.message.edit_text(f'✏️ <b>Переименовать модель</b>\n\nСейчас: <code>{r["name"]}</code>\n\nВведите новое название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()


async def _render_visibility_makes(call:CallbackQuery, page:int=0):
    c=conn(); rows=c.execute('SELECT id,name,active FROM vehicle_makes ORDER BY name COLLATE NOCASE,id').fetchall(); c.close()
    page_size=50
    total_pages=max(1,(len(rows)+page_size-1)//page_size)
    page=max(0,min(int(page),total_pages-1))
    chunk=rows[page*page_size:(page+1)*page_size]
    out=[]
    for r in chunk:
        icon='☑️' if r['active'] else '⬜'
        out.append([InlineKeyboardButton(text=f'{icon} {r["name"]}',callback_data=f'veh_toggle_make:{r["id"]}:{page}')])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='⬅️ Предыдущая',callback_data=f'veh_edit_makes_page:{page-1}'))
    if page<total_pages-1: nav.append(InlineKeyboardButton(text='Следующая ➡️',callback_data=f'veh_edit_makes_page:{page+1}'))
    if nav: out.append(nav)
    if total_pages>1: out.append([InlineKeyboardButton(text=f'📄 {page+1}/{total_pages}',callback_data='noop')])
    out.append([InlineKeyboardButton(text='➕ Добавить новую марку',callback_data='veh_new_make')])
    out.append([InlineKeyboardButton(text='← Назад',callback_data='veh_back_makes')])
    await call.message.edit_text(f'✏️ <b>Видимость марок</b>\n\n☑️ — показывается\n⬜ — скрыта\n\nСтраница {page+1} из {total_pages}. Нажмите на марку, чтобы переключить.',parse_mode='HTML',reply_markup=kb(out))

@dp.callback_query(F.data=='veh_edit_makes')
async def veh_edit_makes(call:CallbackQuery):
    await _render_visibility_makes(call,0); await call.answer()

@dp.callback_query(F.data.startswith('veh_edit_makes_page:'))
async def veh_edit_makes_page(call:CallbackQuery):
    await _render_visibility_makes(call,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data.startswith('veh_toggle_make:'))
async def veh_toggle_make(call:CallbackQuery):
    parts=call.data.split(':'); mid=int(parts[1]); page=int(parts[2]) if len(parts)>2 else 0
    c=conn(); r=c.execute('SELECT active FROM vehicle_makes WHERE id=?',(mid,)).fetchone()
    if not r: c.close(); await call.answer('Марка не найдена',show_alert=True); return
    c.execute('UPDATE vehicle_makes SET active=? WHERE id=?',(0 if r['active'] else 1,mid)); c.commit(); c.close()
    await _render_visibility_makes(call,page); await call.answer('Изменено')

async def _render_visibility_models(call:CallbackQuery, make_id:int):
    c=conn(); make=c.execute('SELECT name FROM vehicle_makes WHERE id=?',(make_id,)).fetchone(); rows=c.execute('SELECT id,name,active FROM vehicle_models WHERE make_id=? ORDER BY sort_order,id',(make_id,)).fetchall(); c.close()
    if not make: await call.answer('Марка не найдена',show_alert=True); return
    out=[]
    for r in rows:
        icon='☑️' if r['active'] else '⬜'
        out.append([InlineKeyboardButton(text=f'{icon} {r["name"]}',callback_data=f'veh_toggle_model:{r["id"]}:{make_id}')])
    out.append([InlineKeyboardButton(text='← Назад к марке',callback_data=f'vehcatmake:{make_id}')])
    await call.message.edit_text(f'✏️ <b>Видимость моделей: {make["name"]}</b>\n\n☑️ — показывается\n⬜ — скрыта\n\nНажмите на модель, чтобы переключить.',parse_mode='HTML',reply_markup=kb(out))

@dp.callback_query(F.data.startswith('veh_edit_models:'))
async def veh_edit_models(call:CallbackQuery):
    await _render_visibility_models(call,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data.startswith('veh_toggle_model:'))
async def veh_toggle_model(call:CallbackQuery):
    parts=call.data.split(':'); model_id=int(parts[1]); make_id=int(parts[2]); c=conn(); r=c.execute('SELECT active FROM vehicle_models WHERE id=? AND make_id=?',(model_id,make_id)).fetchone()
    if not r: c.close(); await call.answer('Модель не найдена',show_alert=True); return
    c.execute('UPDATE vehicle_models SET active=? WHERE id=?',(0 if r['active'] else 1,model_id)); c.commit(); c.close()
    await _render_visibility_models(call,make_id); await call.answer('Изменено')

@dp.callback_query(F.data.startswith('veh_hide_make:'))
async def veh_hide_make(call:CallbackQuery):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=?',(mid,)).fetchone();
    if r: _suppress(c,'make',r['name'])
    c.execute('UPDATE vehicle_makes SET active=0 WHERE id=?',(mid,)); c.commit(); c.close(); await render_vehicle_catalog(call,'makes'); await call.answer('Марка скрыта.')

@dp.callback_query(F.data.startswith('veh_hide_model:'))
async def veh_hide_model(call:CallbackQuery):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT make_id,name FROM vehicle_models WHERE id=?',(mid,)).fetchone(); parent=c.execute('SELECT name FROM vehicle_makes WHERE id=?',(r['make_id'],)).fetchone() if r else None;
    if r and parent: _suppress(c,'model',r['name'],parent['name'])
    c.execute('UPDATE vehicle_models SET active=0 WHERE id=?',(mid,)); c.commit(); c.close(); await render_vehicle_catalog(call,str(r['make_id'])); await call.answer('Модель скрыта.')

@dp.callback_query(F.data=='veh_hidden_makes')
async def veh_hidden_makes(call:CallbackQuery):
    c=conn(); rows=c.execute('SELECT id,name FROM vehicle_makes WHERE active=0 ORDER BY sort_order,id').fetchall(); c.close()
    out=[]
    for r in rows:
        out.append([InlineKeyboardButton(text=f'👁 {r["name"]}',callback_data=f'veh_restore_make:{r["id"]}')])
    out.append([InlineKeyboardButton(text='← Назад',callback_data='veh_back_makes')])
    text='👁 <b>Скрытые марки</b>\n\n'
    text += 'Нажмите на марку, чтобы вернуть её.' if rows else 'Скрытых марок нет.'
    await call.message.edit_text(text,parse_mode='HTML',reply_markup=kb(out)); await call.answer()

@dp.callback_query(F.data.startswith('veh_restore_make:'))
async def veh_restore_make(call:CallbackQuery):
    mid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM vehicle_makes WHERE id=?',(mid,)).fetchone()
    if not r: c.close(); await call.answer('Марка не найдена',show_alert=True); return
    c.execute("DELETE FROM vehicle_suppressions WHERE kind='make' AND parent_key='' AND item_key=?",(_vkey(r['name']),))
    c.execute('UPDATE vehicle_makes SET active=1 WHERE id=?',(mid,))
    c.commit(); c.close(); await render_vehicle_catalog(call,'makes'); await call.answer('Марка показана.')

@dp.callback_query(F.data=='veh_new_make')
async def veh_new_make(call:CallbackQuery,state:FSMContext):
    await state.update_data(veh_kind='new_make',veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.make); await call.message.edit_text('➕ <b>Новая марка</b>\n\nВведите название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('veh_new_model:'))
async def veh_new_model(call:CallbackQuery,state:FSMContext):
    mid=int(call.data.split(':')[1]); await state.update_data(veh_kind='new_model',veh_parent=mid,veh_chat_id=call.message.chat.id,veh_message_id=call.message.message_id); await state.set_state(VehicleCatalogEdit.model); await call.message.edit_text('➕ <b>Новая модель</b>\n\nВведите название.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='veh_input_cancel')]])); await call.answer()

@dp.callback_query(F.data=='veh_sort_makes')
async def veh_sort_makes(call:CallbackQuery):
    c=conn(); rows=c.execute('SELECT id,name FROM vehicle_makes WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close(); out=[]
    for i,r in enumerate(rows):
        nav=[]
        if i: nav.append(InlineKeyboardButton(text='⬆️',callback_data=f'vcm_up:{r["id"]}'))
        if i<len(rows)-1: nav.append(InlineKeyboardButton(text='⬇️',callback_data=f'vcm_down:{r["id"]}'))
        nav.append(InlineKeyboardButton(text=r['name'],callback_data=f'vehcatmake:{r["id"]}')); out.append(nav)
    out.append([InlineKeyboardButton(text='← Назад',callback_data='veh_back_makes')]); await call.message.edit_text('↕️ <b>Порядок марок</b>',parse_mode='HTML',reply_markup=kb(out)); await call.answer()

@dp.callback_query(F.data=='veh_sort_models:')
async def unused_model_sort(call:CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith('veh_sort_models:'))
async def veh_sort_models(call:CallbackQuery):
    pid=int(call.data.split(':',1)[1]); await render_vehicle_model_sort(call,pid); await call.answer()

async def render_vehicle_model_sort(call,pid):
    c=conn(); make=c.execute('SELECT name FROM vehicle_makes WHERE id=? AND active=1',(pid,)).fetchone(); rows=c.execute('SELECT id,name FROM vehicle_models WHERE make_id=? AND active=1 ORDER BY sort_order,id',(pid,)).fetchall(); c.close()
    if not make: return
    out=[]
    for i,r in enumerate(rows):
        nav=[]
        if i: nav.append(InlineKeyboardButton(text='⬆️',callback_data=f'vmod_up:{r["id"]}:{pid}'))
        if i<len(rows)-1: nav.append(InlineKeyboardButton(text='⬇️',callback_data=f'vmod_down:{r["id"]}:{pid}'))
        nav.append(InlineKeyboardButton(text=r['name'],callback_data=f'vehcatmodel:{r["id"]}')); out.append(nav)
    out.append([InlineKeyboardButton(text='← Назад',callback_data=f'vehcatmake:{pid}')])
    await call.message.edit_text(f'↕️ <b>Порядок моделей: {make["name"]}</b>',parse_mode='HTML',reply_markup=kb(out))

@dp.callback_query(F.data.startswith('vcm_up:') | F.data.startswith('vcm_down:'))
async def veh_move_make_fixed(call:CallbackQuery):
    direction='up' if call.data.startswith('vcm_up:') else 'down'; mid=int(call.data.split(':')[1]); c=conn(); ids=[r['id'] for r in c.execute('SELECT id FROM vehicle_makes WHERE active=1 ORDER BY sort_order,id').fetchall()]
    if mid not in ids: c.close(); await call.answer('Марка не найдена',show_alert=True); return
    i=ids.index(mid); j=i-1 if direction=='up' else i+1
    if 0<=j<len(ids): ids[i],ids[j]=ids[j],ids[i]
    for n,x in enumerate(ids): c.execute('UPDATE vehicle_makes SET sort_order=? WHERE id=?',(n,x))
    c.commit(); c.close(); await veh_sort_makes(call); await call.answer()

@dp.callback_query(F.data.startswith('vmod_up:') | F.data.startswith('vmod_down:'))
async def veh_move_model_fixed(call:CallbackQuery):
    parts=call.data.split(':'); direction='up' if parts[0]=='vmod_up' else 'down'; mid=int(parts[1]); pid=int(parts[2]); c=conn(); ids=[r['id'] for r in c.execute('SELECT id FROM vehicle_models WHERE make_id=? AND active=1 ORDER BY sort_order,id',(pid,)).fetchall()]
    if mid not in ids: c.close(); await call.answer('Модель не найдена',show_alert=True); return
    i=ids.index(mid); j=i-1 if direction=='up' else i+1
    if 0<=j<len(ids): ids[i],ids[j]=ids[j],ids[i]
    for n,x in enumerate(ids): c.execute('UPDATE vehicle_models SET sort_order=? WHERE id=?',(n,x))
    c.commit(); c.close(); await render_vehicle_model_sort(call,pid); await call.answer()

@dp.callback_query(F.data=='veh_close')
async def veh_close(call:CallbackQuery): await call.message.edit_text('🚗 Справочник автомобилей закрыт.'); await call.answer()

@dp.callback_query(F.data.startswith('veh_hide_engine:'))
async def veh_hide_engine(call:CallbackQuery):
    parts=call.data.split(':'); eid=int(parts[1]); page=int(parts[2]) if len(parts)>2 else 0; c=conn(); r=c.execute('SELECT e.model_id,e.name,m.name model_name FROM vehicle_engines e JOIN vehicle_models m ON m.id=e.model_id WHERE e.id=?',(eid,)).fetchone();
    if not r: await call.answer('Двигатель не найден',show_alert=True); return
    _suppress(c,'engine',r['name'],r['model_name']); c.execute('UPDATE vehicle_engines SET active=0 WHERE id=?',(eid,)); c.commit(); c.close(); await render_vehicle_subcatalog(call,int(r['model_id']),'engine',page); await call.answer('Двигатель удалён из справочника.')
@dp.callback_query(F.data.startswith('veh_hide_gearbox:'))
async def veh_hide_gearbox(call:CallbackQuery):
    parts=call.data.split(':'); gid=int(parts[1]); page=int(parts[2]) if len(parts)>2 else 0; c=conn(); r=c.execute('SELECT g.model_id,g.name,m.name model_name FROM vehicle_gearboxes g JOIN vehicle_models m ON m.id=g.model_id WHERE g.id=?',(gid,)).fetchone();
    if not r: await call.answer('КПП не найдена',show_alert=True); return
    _suppress(c,'gearbox',r['name'],r['model_name']); c.execute('UPDATE vehicle_gearboxes SET active=0 WHERE id=?',(gid,)); c.commit(); c.close(); await render_vehicle_subcatalog(call,int(r['model_id']),'gearbox',page); await call.answer('КПП удалена из справочника.')

@dp.callback_query(F.data.startswith('veh_sort_engine:'))
async def veh_sort_engine(call:CallbackQuery): await render_vehicle_subsort(call,int(call.data.split(':')[1]),'engine'); await call.answer()
@dp.callback_query(F.data.startswith('veh_sort_gearbox:'))
async def veh_sort_gearbox(call:CallbackQuery): await render_vehicle_subsort(call,int(call.data.split(':')[1]),'gearbox'); await call.answer()
async def render_vehicle_subsort(call,model_id,kind):
    table='vehicle_engines' if kind=='engine' else 'vehicle_gearboxes'; label='Двигатели' if kind=='engine' else 'КПП'; icon='🔧' if kind=='engine' else '⚙️'; c=conn(); rows=c.execute(f'SELECT id,name FROM {table} WHERE model_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(model_id,)).fetchall(); c.close(); out=[]
    for i,r in enumerate(rows):
        nav=[]
        if i: nav.append(InlineKeyboardButton(text='⬆️',callback_data=f'v{kind}_up:{r["id"]}:{model_id}'))
        if i<len(rows)-1: nav.append(InlineKeyboardButton(text='⬇️',callback_data=f'v{kind}_down:{r["id"]}:{model_id}'))
        nav.append(InlineKeyboardButton(text=r['name'],callback_data=f'veh_rename_{kind}:{r["id"]}')); out.append(nav)
    out.append([InlineKeyboardButton(text='← Назад',callback_data=f'veh_{kind}s:{model_id}')]); await call.message.edit_text(f'↕️ <b>Порядок: {label}</b>',parse_mode='HTML',reply_markup=kb(out))
@dp.callback_query(F.data.startswith('vengine_up:') | F.data.startswith('vengine_down:'))
async def vengine_move(call:CallbackQuery): await move_vehicle_subitem(call,'engine')
@dp.callback_query(F.data.startswith('vgearbox_up:') | F.data.startswith('vgearbox_down:'))
async def vgearbox_move(call:CallbackQuery): await move_vehicle_subitem(call,'gearbox')
async def move_vehicle_subitem(call,kind):
    parts=call.data.split(':'); direction='up' if '_up:' in call.data else 'down'; iid=int(parts[1]); model_id=int(parts[2]); table='vehicle_engines' if kind=='engine' else 'vehicle_gearboxes'; c=conn(); ids=[r['id'] for r in c.execute(f'SELECT id FROM {table} WHERE model_id=? AND active=1 ORDER BY name COLLATE NOCASE,id',(model_id,)).fetchall()];
    if iid not in ids: c.close(); await call.answer('Элемент не найден',show_alert=True); return
    i=ids.index(iid); j=i-1 if direction=='up' else i+1
    if 0<=j<len(ids): ids[i],ids[j]=ids[j],ids[i]
    for n,x in enumerate(ids): c.execute(f'UPDATE {table} SET sort_order=? WHERE id=?',(n,x))
    c.commit(); c.close(); await render_vehicle_subsort(call,model_id,kind); await call.answer()

@dp.callback_query(F.data=='veh_input_cancel')
async def veh_input_cancel(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.clear(); kind=d.get('veh_kind')
    if kind in ('new_model','model'): await render_vehicle_catalog(call,str(d.get('veh_parent')))
    elif kind in ('new_engine','engine'): await render_vehicle_subcatalog(call,int(d.get('veh_parent')),'engine')
    elif kind in ('new_gearbox','gearbox'): await render_vehicle_subcatalog(call,int(d.get('veh_parent')),'gearbox')
    else: await render_vehicle_catalog(call,'makes')
    await call.answer()

@dp.message(VehicleCatalogEdit.make)
async def vehicle_make_input(message:Message,state:FSMContext):
    d=await state.get_data()
    if not d.get('veh_kind'): return
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); kind=d['veh_kind']
    if kind=='new_make':
        mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_makes').fetchone()[0]; c.execute('INSERT INTO vehicle_makes(name,sort_order) VALUES(?,?)',(name,mx+1)); c.commit(); c.close(); await state.clear(); await render_vehicle_catalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),'makes')
    elif kind=='make':
        c.execute('UPDATE vehicle_makes SET name=? WHERE id=?',(name,d['veh_id'])); c.commit(); c.close(); await state.clear(); await render_vehicle_catalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),'makes')

@dp.message(VehicleCatalogEdit.model)
async def vehicle_model_input(message:Message,state:FSMContext):
    d=await state.get_data()
    if not d.get('veh_kind'): return
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); kind=d['veh_kind']
    if kind=='new_model':
        mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_models WHERE make_id=?',(d['veh_parent'],)).fetchone()[0]; c.execute('INSERT INTO vehicle_models(make_id,name,sort_order) VALUES(?,?,?)',(d['veh_parent'],name,mx+1))
    elif kind=='model': c.execute('UPDATE vehicle_models SET name=? WHERE id=?',(name,d['veh_id']))
    c.commit(); c.close(); parent=d.get('veh_parent') if kind=='new_model' else c.execute if False else None
    await state.clear()
    # For edited model, recover parent from DB; for new model it is already known.
    parent=d.get('veh_parent')
    if not parent and kind=='model':
        c2=conn(); rr=c2.execute('SELECT make_id FROM vehicle_models WHERE id=?',(d['veh_id'],)).fetchone(); c2.close(); parent=rr['make_id'] if rr else None
    await render_vehicle_catalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),str(parent) if parent else 'makes')

@dp.message(VehicleCatalogEdit.engine)
async def vehicle_engine_input(message:Message,state:FSMContext):
    d=await state.get_data(); name=(message.text or '').strip()
    if message.text=='← Назад': await state.clear(); await render_vehicle_subcatalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),int(d['veh_parent']),'engine'); return
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); kind=d['veh_kind']
    if kind=='new_engine': mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_engines WHERE model_id=?',(d['veh_parent'],)).fetchone()[0]; c.execute('INSERT INTO vehicle_engines(model_id,name,sort_order) VALUES(?,?,?)',(d['veh_parent'],name,mx+1))
    elif kind=='engine': c.execute('UPDATE vehicle_engines SET name=? WHERE id=?',(name,d['veh_id']))
    c.commit(); c.close(); parent=d['veh_parent']; await state.clear(); await render_vehicle_subcatalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),int(parent),'engine')

@dp.message(VehicleCatalogEdit.gearbox)
async def vehicle_gearbox_input(message:Message,state:FSMContext):
    d=await state.get_data(); name=(message.text or '').strip()
    if message.text=='← Назад': await state.clear(); await render_vehicle_subcatalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),int(d['veh_parent']),'gearbox'); return
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); kind=d['veh_kind']
    if kind=='new_gearbox': mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM vehicle_gearboxes WHERE model_id=?',(d['veh_parent'],)).fetchone()[0]; c.execute('INSERT INTO vehicle_gearboxes(model_id,name,sort_order) VALUES(?,?,?)',(d['veh_parent'],name,mx+1))
    elif kind=='gearbox': c.execute('UPDATE vehicle_gearboxes SET name=? WHERE id=?',(name,d['veh_id']))
    c.commit(); c.close(); parent=d['veh_parent']; await state.clear(); await render_vehicle_subcatalog(SimpleCall(bot,d['veh_chat_id'],d['veh_message_id']),int(parent),'gearbox')

@dp.message(F.text=='🚗 Автомобили')
async def all_cars(message:Message):
    if not is_admin(message.from_user.id): return
    # Переход в общий список машин всегда закрывает открытую сервисную книжку.
    # Иначе старые inline-кнопки истории остаются в чате и могут срабатывать
    # одновременно с новым экраном.
    await _clear_history_messages_for_chat(message.chat.id)
    c=conn(); rows=c.execute('SELECT c.*,cl.username,cl.id client_id FROM cars c JOIN clients cl ON cl.id=c.client_id ORDER BY c.brand COLLATE NOCASE, c.model COLLATE NOCASE, c.id').fetchall(); c.close()
    if not rows: await message.answer('Автомобилей пока нет.'); return
    buttons=[[InlineKeyboardButton(text=f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — @{r['username']}" if r['username'] else f"🚗 {r['brand']} {r['model']} — {r['engine'] or 'мотор не указан'} / {r['gearbox'] or 'КПП не указана'} — клиент #{r['client_id']}",callback_data=f"car:{r['id']}")] for r in rows]
    await message.answer('🚗 Все автомобили:',reply_markup=kb(buttons))

# Service multi-select — fixed inline picker
async def render_service_categories(call:CallbackQuery,state:FSMContext):
    c=conn(); gs=c.execute('SELECT * FROM service_groups WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close()
    d=await state.get_data(); selected=d.get('selected',[])
    rows=[[InlineKeyboardButton(text=f"🔧 {g['name']}",callback_data=f"workgroup:{g['id']}")] for g in gs]
    rows += [[InlineKeyboardButton(text='🔎 Поиск работ',callback_data='work_search_start')]]
    rows += [[InlineKeyboardButton(text=f"💾 Выбрано: {len(selected)} — Сохранить",callback_data='works_done')],
             [InlineKeyboardButton(text='← Назад',callback_data='service_abort')]]
    await call.message.edit_text(
        '🧾 <b>NNS SERVICE • Добавление работ</b>\n\nВыберите категорию. Выбранные работы сохраняются, пока вы не нажмёте «Сохранить».\n\n<b>Выбрано:</b> '+str(len(selected)),
        reply_markup=kb(rows),parse_mode='HTML')

async def start_service_picker(message:Message,state:FSMContext,car_id:int):
    await state.clear(); await state.update_data(car_id=car_id,selected=[])
    c=conn(); gs=c.execute('SELECT * FROM service_groups WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close()
    rows=[[InlineKeyboardButton(text=f"🔧 {g['name']}",callback_data=f"workgroup:{g['id']}")] for g in gs]
    rows += [[InlineKeyboardButton(text='🔎 Поиск работ',callback_data='work_search_start')]]
    rows += [[InlineKeyboardButton(text='💾 Выбрано: 0 — Сохранить',callback_data='works_done')],
             [InlineKeyboardButton(text='← Назад',callback_data='service_abort')]]
    msg=await message.answer(
        '🧾 <b>NNS SERVICE • Добавление обслуживания</b>\n\nВыберите категорию. Выбранные работы сохраняются при переходе между категориями.\n\n<b>Выбрано:</b> 0',
        reply_markup=kb(rows),parse_mode='HTML')
    await state.update_data(picker_chat_id=msg.chat.id,picker_message_id=msg.message_id)

async def render_work_picker(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); gid=d['group_id']; selected=set(d.get('selected',[]))
    c=conn(); g=c.execute('SELECT name FROM service_groups WHERE id=?',(gid,)).fetchone(); ws=c.execute('SELECT * FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id',(gid,)).fetchall(); c.close()
    rows=[[InlineKeyboardButton(text=f"{'☑' if w['id'] in selected else '☐'} {w['name']}",callback_data=f"togglework:{w['id']}")] for w in ws]
    rows += [[InlineKeyboardButton(text='🔎 Поиск работ',callback_data='work_search_start')],
             [InlineKeyboardButton(text='➕ Добавить работу',callback_data=f'inline_addwork:{gid}'),
              InlineKeyboardButton(text='✏️ Изменить',callback_data=f'inline_editworks:{gid}')],
             [InlineKeyboardButton(text=f'💾 Сохранить ({len(selected)})',callback_data='works_done')],
             [InlineKeyboardButton(text='← Категории',callback_data='work_categories'),
              InlineKeyboardButton(text='← Назад',callback_data='service_abort')]]
    await call.message.edit_text(
        f'🔧 <b>{g["name"]}</b>\n\n☑ — работа выбрана\n☐ — не выбрана\n\n<b>Выбрано:</b> {len(selected)}',
        reply_markup=kb(rows),parse_mode='HTML')


@dp.callback_query(F.data.startswith('newsvc:'))
async def service_car_select(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    await start_service_picker(call.message,state,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data.startswith('servicecar:'))
async def service_from_car(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id): return
    await start_service_picker(call.message,state,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data.startswith('workgroup:'))
async def work_group(call:CallbackQuery,state:FSMContext):
    await state.update_data(group_id=int(call.data.split(':')[1]), search_query=''); await state.set_state(AddService.group_id); await render_work_picker(call,state); await call.answer()

async def render_work_search(call:CallbackQuery,state:FSMContext,query:str):
    q=(query or '').strip()
    d=await state.get_data(); selected=set(d.get('selected',[]))
    c=conn(); rows_db=c.execute('SELECT w.id,w.name,w.group_id,g.name AS group_name FROM works w JOIN service_groups g ON g.id=w.group_id WHERE w.active=1 AND g.active=1 AND w.name LIKE ? COLLATE NOCASE ORDER BY g.sort_order,g.id,w.sort_order,w.id',('%'+q+'%',)).fetchall(); c.close()
    rows=[]
    for w in rows_db[:100]:
        rows.append([InlineKeyboardButton(text=f"{'☑' if w['id'] in selected else '☐'} {w['name']} — {w['group_name']}",callback_data=f"togglework:{w['id']}")])
    if len(rows_db)>100:
        rows.append([InlineKeyboardButton(text=f'Показаны первые 100 из {len(rows_db)}',callback_data='noop')])
    rows += [[InlineKeyboardButton(text='🔎 Новый поиск',callback_data='work_search_start')],
             [InlineKeyboardButton(text=f'💾 Сохранить ({len(selected)})',callback_data='works_done')],
             [InlineKeyboardButton(text='← Категории',callback_data='work_categories')]]
    if not rows_db:
        text=f'🔎 <b>Поиск работ</b>\n\nПо запросу «{q}» ничего не найдено.'
    else:
        text=f'🔎 <b>Поиск работ</b>\n\nЗапрос: <b>{q}</b>\nНайдено: {len(rows_db)}\n\nНажимайте на работы, чтобы выбрать или снять выбор.'
    await call.message.edit_text(text,parse_mode='HTML',reply_markup=kb(rows))

@dp.callback_query(F.data=='work_search_start')
async def work_search_start(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.update_data(search_query=''); await state.set_state(AddService.work_search)
    await call.message.edit_text('🔎 <b>Поиск работ</b>\n\nВведите несколько букв или слово из названия работы.\nНапример: <b>масл</b>, <b>торм</b>, <b>стой</b>.',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Категории',callback_data='work_categories')]]))
    await call.answer()

@dp.message(AddService.work_search)
async def work_search_message(message:Message,state:FSMContext):
    q=(message.text or '').strip()
    if not q:
        await message.answer('Введите хотя бы одну букву для поиска.')
        return
    d=await state.get_data(); await state.update_data(search_query=q)
    chat_id=d.get('picker_chat_id'); msg_id=d.get('picker_message_id')
    if chat_id and msg_id:
        await render_work_search(SimpleCall(bot,chat_id,msg_id),state,q)
    else:
        await message.answer(f'Поиск: {q}')

@dp.callback_query(F.data.startswith('togglework:'))
async def toggle_work(call:CallbackQuery,state:FSMContext):
    wid=int(call.data.split(':')[1]); d=await state.get_data(); selected=set(d.get('selected',[]))
    selected.remove(wid) if wid in selected else selected.add(wid)
    await state.update_data(selected=list(selected))
    q=d.get('search_query','')
    if q:
        await render_work_search(call,state,q)
    else:
        await render_work_picker(call,state)
    await call.answer()

@dp.callback_query(F.data=='work_categories')
async def work_categories(call:CallbackQuery,state:FSMContext):
    await state.update_data(search_query=''); await state.set_state(AddService.group_id)
    await render_service_categories(call,state); await call.answer()

@dp.callback_query(F.data=='works_done')
async def works_done(call:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if not d.get('selected'): await call.answer('Сначала выберите хотя бы одну работу.',show_alert=True); return
    c=conn(); names=[r['name'] for r in c.execute('SELECT name FROM works WHERE id IN (%s) ORDER BY id'%(','.join('?'*len(d['selected']))),d['selected']).fetchall()]; c.close()
    await state.update_data(service_panel_chat_id=call.message.chat.id,service_panel_message_id=call.message.message_id)
    await state.set_state(AddService.mileage)
    await call.message.edit_text('💾 <b>Работы выбраны</b>\n\n'+'\n'.join('• '+n for n in names)+'\n\n<b>Пробег на момент обслуживания, км?</b>\n\nЕсли пробег не знаете — нажмите «Пропустить».',
                                 parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='mileage_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]))
    await call.answer()

@dp.callback_query(F.data.startswith('inline_addwork:'))
async def inline_addwork(call:CallbackQuery,state:FSMContext):
    gid=int(call.data.split(':')[1]); d=await state.get_data()
    await state.update_data(group_id=gid,picker_chat_id=call.message.chat.id,picker_message_id=call.message.message_id)
    await state.set_state(Catalog.work_name)
    await call.message.edit_text('➕ <b>Новая работа</b>\n\nВведите название работы — после сохранения вернётесь в этот же список.\n\n← Назад',
                                 parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='inline_catalog_cancel')]]))
    await call.answer()

class SimpleMessage:
    def __init__(self,bot,chat_id,message_id):
        self.bot=bot; self.chat=type('Chat',(),{'id':chat_id})(); self.message_id=message_id
    async def edit_text(self,*args,**kwargs):
        return await self.bot.edit_message_text(chat_id=self.chat.id,message_id=self.message_id,*args,**kwargs)
class SimpleCall:
    def __init__(self,bot,chat_id,message_id): self.message=SimpleMessage(bot,chat_id,message_id)

@dp.message(Catalog.work_name)
async def work_name(message:Message,state:FSMContext):
    if message.text=='← Назад':
        d=await state.get_data()
        await state.clear()
        if d.get('catalog_message_id'):
            await render_catalog_group(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id']),d['group_id']); return
        if d.get('picker_message_id'):
            await state.set_state(AddService.group_id); await render_work_picker(SimpleCall(bot,d['picker_chat_id'],d['picker_message_id']),state); return
        await message.answer('Отменено.',reply_markup=admin_menu()); return
    d=await state.get_data(); name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM works WHERE group_id=?',(d['group_id'],)).fetchone()[0]; c.execute('INSERT INTO works(group_id,name,sort_order,active) VALUES(?,?,?,1)',(d['group_id'],name,mx+1)); c.commit(); c.close()
    if d.get('catalog_message_id'):
        await state.clear()
        await render_catalog_group(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id']),d['group_id']); return
    # Return to the service work picker without losing ANY AddService state.
    # Do not clear the FSM before the picker has been restored: if Telegram
    # rejects editing the old picker message, we can send a fresh picker instead.
    gid=d['group_id']; picker_chat=d.get('picker_chat_id'); picker_message=d.get('picker_message_id')
    service_context = {k:v for k,v in d.items() if k not in ('work_id','group_id')}
    service_context['group_id'] = gid
    await state.clear()
    await state.update_data(**service_context)
    await state.set_state(AddService.group_id)
    if picker_chat and picker_message:
        try:
            await render_work_picker(SimpleCall(bot,picker_chat,picker_message),state)
            return
        except Exception:
            pass
    # Fallback: the original picker message may have become uneditable.
    # Send a new picker message and keep the service state intact.
    msg=await message.answer('🧾 <b>Выбор работ</b>\n\nПродолжаем добавление обслуживания. Выбранные работы сохранены.',parse_mode='HTML')
    await state.update_data(picker_chat_id=msg.chat.id,picker_message_id=msg.message_id)
    await render_work_picker(SimpleCall(bot,msg.chat.id,msg.message_id),state)

@dp.callback_query(F.data.startswith('inline_editworks:'))
async def inline_editworks(call:CallbackQuery,state:FSMContext):
    gid=int(call.data.split(':')[1]); c=conn(); ws=c.execute('SELECT * FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id',(gid,)).fetchall(); c.close()
    rows=[[InlineKeyboardButton(text='✏️ '+w['name'],callback_data=f'inline_rename:{w["id"]}')] for w in ws]
    rows.append([InlineKeyboardButton(text='← Назад',callback_data=f'workgroup:{gid}')])
    await call.message.edit_text('✏️ <b>Изменить работу</b>\n\nВыберите работу:',parse_mode='HTML',reply_markup=kb(rows)); await call.answer()

@dp.callback_query(F.data.startswith('inline_rename:'))
async def inline_rename(call:CallbackQuery,state:FSMContext):
    wid=int(call.data.split(':')[1]); c=conn(); w=c.execute('SELECT * FROM works WHERE id=?',(wid,)).fetchone(); c.close()
    d=await state.get_data()
    await state.update_data(work_id=wid,group_id=w['group_id'],picker_chat_id=d.get('picker_chat_id',call.message.chat.id),picker_message_id=d.get('picker_message_id',call.message.message_id))
    await state.set_state(Catalog.edit_name)
    await call.message.edit_text(f'✏️ <b>Изменение работы</b>\n\nСейчас: {w["name"]}\n\nВведите новое название.\n\n← Назад',
                                 parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='inline_catalog_cancel')]])); await call.answer()

@dp.callback_query(F.data=='inline_catalog_cancel')
async def inline_catalog_cancel(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.set_state(AddService.group_id)
    await render_work_picker(call,state); await call.answer()

@dp.message(Catalog.edit_name)
async def do_rename(message:Message,state:FSMContext):
    if message.text=='← Назад':
        d=await state.get_data(); await state.clear()
        if d.get('catalog_message_id'):
            await render_catalog_group(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id']),d['group_id']); return
        await state.set_state(AddService.group_id); await render_work_picker(SimpleCall(bot,d['picker_chat_id'],d['picker_message_id']),state); return
    d=await state.get_data(); name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    c=conn(); c.execute('UPDATE works SET name=? WHERE id=?',(name,d['work_id'])); c.commit(); c.close()
    if d.get('catalog_message_id'):
        sc=SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id'])
        if d.get('visibility_mode'):
            await state.clear(); await render_catalog_visibility_works(sc,d['group_id'],int(d.get('catalog_page',0))); return
        await state.clear(); await render_catalog_group(sc,d['group_id']); return
    # Restore the complete AddService context after editing a work.
    # The previous implementation could lose the picker/message state and leave
    # the user on a dead screen. Keep all fields except the temporary edit ids.
    if d.get('picker_message_id') and d.get('car_id'):
        service_context = {k:v for k,v in d.items() if k not in ('work_id','group_id')}
        service_context['group_id'] = d['group_id']
        await state.clear()
        await state.update_data(**service_context)
        await state.set_state(AddService.group_id)
        try:
            await render_work_picker(SimpleCall(bot,d['picker_chat_id'],d['picker_message_id']),state)
            return
        except Exception:
            pass
        # Fallback if the old picker message cannot be edited.
        msg=await message.answer('🧾 <b>Выбор работ</b>\n\nИзменение сохранено. Выбранные работы сохранены.',parse_mode='HTML')
        await state.update_data(picker_chat_id=msg.chat.id,picker_message_id=msg.message_id)
        await render_work_picker(SimpleCall(bot,msg.chat.id,msg.message_id),state)
        return
    await state.set_state(AddService.group_id)
    await render_work_picker(SimpleCall(bot,d['picker_chat_id'],d['picker_message_id']),state)

@dp.message(AddService.mileage)
async def service_mileage(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('❌ Добавление работ отменено.',reply_markup=admin_menu()); return
    try: m=int(message.text.replace(' ',''))
    except: await message.answer('Введите пробег числом или нажмите «Пропустить».'); return
    d=await state.get_data(); c=conn(); car=c.execute('SELECT mileage FROM cars WHERE id=?',(d['car_id'],)).fetchone(); c.close()
    if car and m<car['mileage']:
        await state.update_data(mileage=m); await message.answer(f"⚠️ Сейчас в карточке: {car['mileage']:,} км\nВы указали: {m:,} км\n\nСохранить?".replace(',',' '),reply_markup=kb([[InlineKeyboardButton(text='✅ Сохранить',callback_data='mileage_ok')],[InlineKeyboardButton(text='← Назад',callback_data='service_abort')]])); return
    await state.update_data(mileage=m); await service_after_mileage(message,state)

@dp.callback_query(F.data=='mileage_skip')
async def mileage_skip(call:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    c=conn(); car=c.execute('SELECT mileage FROM cars WHERE id=?',(d['car_id'],)).fetchone(); c.close()
    if not car:
        await call.answer('Автомобиль не найден',show_alert=True); return
    # If the mileage is unknown, keep the mileage already stored on the car.
    # The service record receives that same value, so the car mileage is not changed.
    await state.update_data(mileage=car['mileage'])
    await service_after_mileage(call.message,state)
    await call.answer('Пробег оставлен без изменений')

async def edit_service_panel(state, text, reply_markup=None, parse_mode=None):
    d=await state.get_data(); chat_id=d.get('service_panel_chat_id'); message_id=d.get('service_panel_message_id')
    if not chat_id or not message_id: return False
    kwargs={'chat_id':chat_id,'message_id':message_id,'text':text}
    if reply_markup is not None: kwargs['reply_markup']=reply_markup
    if parse_mode is not None: kwargs['parse_mode']=parse_mode
    try:
        await bot.edit_message_text(**kwargs); return True
    except Exception: return False

async def service_after_mileage(message,state):
    await state.set_state(AddService.description)
    text='📝 <b>Описание</b>\nЕсли не нужно — нажмите «Пропустить».'
    markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='description_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]])
    if not await edit_service_panel(state,text,markup,'HTML'):
        await message.answer(text,parse_mode='HTML',reply_markup=markup)

@dp.callback_query(F.data=='description_skip')
async def description_skip(call:CallbackQuery,state:FSMContext):
    await state.update_data(description='')
    await state.set_state(AddService.price)
    await edit_service_panel(state,'💶 <b>Общая стоимость</b>, €\nЕсли не указывать — нажмите «Пропустить».',kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='price_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]),'HTML')
    await call.answer()

@dp.callback_query(F.data=='mileage_ok')
async def mileage_ok(call:CallbackQuery,state:FSMContext):
    await service_after_mileage(call.message,state); await call.answer()

@dp.message(AddService.description)
async def service_desc(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(description='' if (message.text or '').lower()=='нет' else (message.text or '').strip())
    await state.set_state(AddService.price)
    text='💶 <b>Общая стоимость</b>, €\nЕсли не указывать — нажмите «Пропустить».'
    markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='price_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]])
    if not await edit_service_panel(state,text,markup,'HTML'):
        await message.answer(text,parse_mode='HTML',reply_markup=markup)

@dp.callback_query(F.data=='price_skip')
async def price_skip(call:CallbackQuery,state:FSMContext):
    await state.update_data(price=0,photos=(await state.get_data()).get('photos',[]))
    await state.set_state(AddService.photos)
    await edit_service_panel(state,'📷 <b>Фото работ</b>\nЕсли фото не нужны — нажмите «Пропустить».',kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='photos_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]]),'HTML')
    await call.answer()

@dp.message(AddService.price)
async def service_price(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    try: p=float((message.text or '').replace(',','.'))
    except: await message.answer('Введите число, например 180, или нажмите «Пропустить».'); return
    await state.update_data(price=p,photos=(await state.get_data()).get('photos',[])); await state.set_state(AddService.photos)
    text='📷 <b>Фото работ</b>\nОтправляйте сколько нужно. Когда закончите — «готово».\nЕсли фото не нужны — нажмите «Пропустить».'
    markup=kb([[InlineKeyboardButton(text='⏭ Пропустить',callback_data='photos_skip')],[InlineKeyboardButton(text='← Назад',callback_data='fsm_back')]])
    if not await edit_service_panel(state,text,markup,'HTML'):
        await message.answer(text,parse_mode='HTML',reply_markup=markup)

@dp.callback_query(F.data=='photos_skip')
async def photos_skip(call:CallbackQuery,state:FSMContext):
    # Сразу подтверждаем нажатие, чтобы Telegram не оставлял кнопку
    # в состоянии ожидания, пока выполняется сохранение обслуживания.
    await call.answer('Сохраняю обслуживание…')
    d=await state.get_data()
    await state.update_data(photos=d.get('photos',[]))
    try:
        await finish_service(call.message,state)
    except Exception as e:
        # Не теряем FSM при ошибке: пользователь сможет повторить сохранение.
        try:
            await call.message.answer(f'⚠️ Не удалось сохранить обслуживание. Попробуйте ещё раз.\nОшибка: {type(e).__name__}')
        except Exception:
            pass

@dp.message(AddService.photos,F.photo)
async def service_photo(message:Message,state:FSMContext):
    d=await state.get_data()
    ph=list(d.get('photos',[]))
    ph.append(message.photo[-1].file_id)
    await state.update_data(photos=ph)

    # После каждой фотографии отправляем НОВОЕ сообщение с кнопкой «Готово».
    # Поэтому кнопка не остаётся вверху и не теряется после следующего фото.
    await message.answer(
        f'📷 Фото добавлено: <b>{len(ph)}</b>\nНажмите «Готово», когда все фотографии отправлены.',
        parse_mode='HTML',
        reply_markup=kb([[InlineKeyboardButton(text='✅ Готово',callback_data='photos_done')]])
    )

@dp.callback_query(F.data=='photos_done')
async def photos_done(call:CallbackQuery,state:FSMContext):
    await call.answer('Сохраняю обслуживание…')
    try:
        await finish_service(call.message,state)
    except Exception as e:
        try:
            await call.message.answer(f'⚠️ Не удалось сохранить обслуживание. Попробуйте ещё раз.\nОшибка: {type(e).__name__}')
        except Exception:
            pass

@dp.message(AddService.photos)
async def service_finish(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    if (message.text or '').lower()!='готово': await message.answer('Отправьте фото или нажмите «Пропустить».'); return
    await finish_service(message,state)

async def finish_service(message:Message,state:FSMContext):
    d=await state.get_data()
    last_error=None
    sid=None
    client=None
    for attempt in range(5):
        c=None
        try:
            c=conn()
            names=[r['name'] for r in c.execute('SELECT name FROM works WHERE id IN (%s) ORDER BY id'%(','.join('?'*len(d['selected']))),d['selected']).fetchall()]
            work_name='\n'.join('• '+n for n in names)
            today=datetime.now().strftime('%Y-%m-%d')
            if d.get('edit_service'):
                sid=d['edit_service_id']
                c.execute('UPDATE services SET mileage=?,work_name=?,description=?,price=? WHERE id=?',(d['mileage'],work_name,d.get('description',''),d.get('price',0),sid))
                c.execute('DELETE FROM service_work_items WHERE service_id=?',(sid,))
                for wid in d.get('selected',[]): c.execute('INSERT OR IGNORE INTO service_work_items(service_id,work_id) VALUES(?,?)',(sid,wid))
                c.execute('DELETE FROM service_photos WHERE service_id=?',(sid,))
                for fid in d.get('photos',[]): c.execute('INSERT INTO service_photos(service_id,telegram_file_id) VALUES(?,?)',(sid,fid))
            else:
                c.execute('INSERT INTO services(car_id,service_date,mileage,work_name,description,price) VALUES(?,?,?,?,?,?)',(d['car_id'],today,d['mileage'],work_name,d.get('description',''),d.get('price',0)))
                sid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                for wid in d.get('selected',[]): c.execute('INSERT OR IGNORE INTO service_work_items(service_id,work_id) VALUES(?,?)',(sid,wid))
                for fid in d.get('photos',[]): c.execute('INSERT INTO service_photos(service_id,telegram_file_id) VALUES(?,?)',(sid,fid))
            car=c.execute('SELECT client_id FROM cars WHERE id=?',(d['car_id'],)).fetchone()
            if not car: raise RuntimeError('Автомобиль не найден')
            client=c.execute('SELECT tg_id FROM clients WHERE id=?',(car['client_id'],)).fetchone()
            c.execute('UPDATE cars SET mileage=? WHERE id=?',(d['mileage'],d['car_id']))
            c.commit()
            c.close(); c=None
            break
        except sqlite3.OperationalError as e:
            last_error=e
            if c:
                try: c.rollback(); c.close()
                except Exception: pass
            if attempt < 4 and 'locked' in str(e).lower():
                await asyncio.sleep(0.25*(attempt+1))
                continue
            raise
        except Exception:
            if c:
                try: c.rollback(); c.close()
                except Exception: pass
            raise
    panel_chat=d.get('service_panel_chat_id'); panel_msg=d.get('service_panel_message_id')
    text=f"🔧 В сервисную книжку добавлено обслуживание\n\n{work_name}\nПробег: {d['mileage']:,} км\n".replace(',',' ')
    if d.get('price',0): text+=f"Стоимость: {d['price']:.2f} €\n"
    if d.get('description'): text+=f"{d['description']}\n"
    try:
        if client and client['tg_id']>0: await bot.send_message(client['tg_id'],text+'\nNNS Service 🚗')
    except Exception:
        pass
    await state.clear()
    final_text=('✅ <b>Обслуживание изменено</b>' if d.get('edit_service') else '✅ <b>Обслуживание сохранено</b>')+'\nПробег автомобиля обновлён.'
    if panel_chat and panel_msg:
        try:
            await bot.edit_message_text(chat_id=panel_chat,message_id=panel_msg,text=final_text,parse_mode='HTML')
            await bot.send_message(panel_chat,'Главное меню NNS Service 👇',reply_markup=admin_menu())
        except Exception:
            await message.answer(final_text,reply_markup=admin_menu(),parse_mode='HTML')
    else:
        await message.answer(final_text,reply_markup=admin_menu(),parse_mode='HTML')

@dp.callback_query(F.data=='service_abort')
async def service_abort(call:CallbackQuery,state:FSMContext):
    await state.clear(); await call.message.edit_text('❌ Добавление обслуживания отменено.')
    await call.message.edit_text('Главное меню NNS Service 👇',reply_markup=admin_menu()); await call.answer()

# Parts / fluids — open inside the current Telegram message when possible.
def _panel_text(call,text,markup):
    async def _run():
        try:
            if call.message.photo: await call.message.edit_caption(caption=text,parse_mode='HTML',reply_markup=markup)
            else: await call.message.edit_text(text,parse_mode='HTML',reply_markup=markup)
            return True
        except Exception: return False
    return _run()

async def render_parts(call,car_id):
    c=conn(); sync_parts_to_car(c,car_id); c.commit(); rows=c.execute('SELECT * FROM parts WHERE car_id=? ORDER BY sort_order,id',(car_id,)).fetchall(); c.close()
    out='🧰 <b>Запчасти и номера</b>\n\n'
    buttons=[]
    for r in rows:
        number=r['part_number'] or 'номер не указан'
        out+=f"• {html.escape(r['name'] or 'Без названия')}\n<pre>{html.escape(number)}</pre>\n"
        # Fixed universal list: only the part number is editable.
        buttons.append([InlineKeyboardButton(text=f'🔧 {r["name"] or "Без названия"}',callback_data=f'partnum:{r["id"]}')])
    buttons.append([InlineKeyboardButton(text='← К автомобилю',callback_data=f'car:{car_id}')])
    if not rows: out+='Пока ничего не добавлено.'
    if not await _panel_text(call,out,kb(buttons)): await call.message.answer(out,parse_mode='HTML',reply_markup=kb(buttons))

@dp.callback_query(F.data.startswith('parts:'))
async def parts(call:CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer('Доступно только сервису',show_alert=True); return
    await render_parts(call,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data.startswith('partnum:'))
async def partnum(call:CallbackQuery,state:FSMContext):
    pid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT car_id,name,part_number FROM parts WHERE id=?',(pid,)).fetchone(); c.close()
    if not r: await call.answer('Запчасть не найдена',show_alert=True); return
    await state.update_data(part_number_id=pid,car_id=r['car_id'],old_part_number=r['part_number'] or '')
    current=r['part_number'] or 'не указан'
    await state.set_state(AddPart.number)
    await call.message.edit_text(f'🔧 <b>{r["name"]}</b>\n\nТекущий номер: <b>{current}</b>\n\nВведите новый номер запчасти.',parse_mode='HTML',reply_markup=skip_markup('partnum_skip'))
    await call.answer()

@dp.callback_query(F.data=='partnum_skip')
async def partnum_skip(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.clear()
    await call.message.edit_text('🧰 <b>Запчасти и номера</b>\n\nНомер оставлен без изменений.',parse_mode='HTML')
    # Re-render the same panel after a short direct transition.
    await render_parts(call,int(d['car_id'])); await call.answer()

@dp.callback_query(F.data.startswith('partdel:'))
async def partdel(call:CallbackQuery):
    pid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT car_id,catalog_id FROM parts WHERE id=?',(pid,)).fetchone()
    if not r: await call.answer('Не найдено',show_alert=True); return
    if r['catalog_id']:
        c.execute('INSERT OR IGNORE INTO part_exclusions(car_id,catalog_id) VALUES(?,?)',(r['car_id'],r['catalog_id']))
    c.execute('DELETE FROM parts WHERE id=?',(pid,)); c.commit(); c.close(); await render_parts(call,r['car_id']); await call.answer('Запчасть убрана из этой машины')

async def _move_part(pid,direction):
    c=conn(); r=c.execute('SELECT car_id,sort_order FROM parts WHERE id=?',(pid,)).fetchone()
    if not r: c.close(); return None
    if direction=='up': other=c.execute('SELECT id,sort_order FROM parts WHERE car_id=? AND sort_order<? ORDER BY sort_order DESC,id DESC LIMIT 1',(r['car_id'],r['sort_order'])).fetchone()
    else: other=c.execute('SELECT id,sort_order FROM parts WHERE car_id=? AND sort_order>? ORDER BY sort_order,id LIMIT 1',(r['car_id'],r['sort_order'])).fetchone()
    if other:
        c.execute('UPDATE parts SET sort_order=? WHERE id=?',(other['sort_order'],pid)); c.execute('UPDATE parts SET sort_order=? WHERE id=?',(r['sort_order'],other['id'])); c.commit()
    c.close(); return r['car_id']

@dp.callback_query(F.data.startswith('partup:'))
async def partup(call:CallbackQuery):
    car_id=await _move_part(int(call.data.split(':')[1]),'up')
    if car_id: await render_parts(call,car_id)
    await call.answer()

@dp.callback_query(F.data.startswith('partdown:'))
async def partdown(call:CallbackQuery):
    car_id=await _move_part(int(call.data.split(':')[1]),'down')
    if car_id: await render_parts(call,car_id)
    await call.answer()

# Legacy edit callback: old messages may still contain it. From now on it only
# changes the number, never the part name.
@dp.callback_query(F.data.startswith('partedit:'))
async def partedit_legacy(call:CallbackQuery,state:FSMContext):
    await partnum(CallbackQuery.model_validate(call.model_dump(update={'data':'partnum:'+call.data.split(':')[1]})),state)

@dp.callback_query(F.data.startswith('addpart:'))
async def addpart_legacy(call:CallbackQuery,state:FSMContext):
    await call.answer('Новые произвольные запчасти отключены. Используйте пункты из стандартного списка.',show_alert=True)

@dp.message(AddPart.name)
async def part_name_legacy(message:Message,state:FSMContext):
    await message.answer('Выберите запчасть из стандартного списка.')

@dp.message(AddPart.number)
async def part_num(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    d=await state.get_data()
    pid=d.get('part_number_id')
    if pid:
        c=conn(); c.execute('UPDATE parts SET part_number=? WHERE id=?',(message.text.strip(),pid)); car_id=d['car_id']; c.commit(); c.close(); await state.clear(); await message.answer('✅ Номер сохранён.');
        # Return to the car parts panel through a fresh message because this is a Message event.
        c=conn(); rows=c.execute('SELECT * FROM parts WHERE car_id=? ORDER BY sort_order,id',(car_id,)).fetchall(); c.close()
        out='🧰 <b>Запчасти и номера</b>\n\n'+''.join(f"• {html.escape(r['name'] or 'Без названия')}\n<pre>{html.escape(r['part_number'] or 'номер не указан')}</pre>\n" for r in rows)
        buttons=[]
        for r in rows:
            buttons.append([InlineKeyboardButton(text=f'🔧 {r["name"]}',callback_data=f'partnum:{r["id"]}')])
        buttons.append([InlineKeyboardButton(text=f'← К автомобилю',callback_data=f'car:{car_id}')])
        await message.answer(out,parse_mode='HTML',reply_markup=kb(buttons)); return
    # Legacy add flow kept only for compatibility with an old in-progress FSM.
    await message.answer('Сначала выберите стандартную запчасть.')

@dp.callback_query(F.data.startswith('fluids:'))
async def fluids(call:CallbackQuery):
    car_id=int(call.data.split(':')[1]); c=conn(); rows=c.execute('SELECT * FROM fluids WHERE car_id=? ORDER BY id',(car_id,)).fetchall(); c.close(); out='🛢️ Масла\n\n'+('Пока ничего не добавлено.' if not rows else '')
    for r in rows: out+=f"{r['system'] or 'Система не указана'}\nМасло: {r['oil_name'] or 'не указано'}\nТип/вязкость: {r['oil_type'] or 'не указано'}\nКоличество: {r['quantity'] if r['quantity'] is not None else 'не указано'} {r['unit'] or ''}\n\n"
    buttons=[]
    if is_admin(call.from_user.id): buttons.append([InlineKeyboardButton(text='➕ Добавить',callback_data=f'addfluid:{car_id}')])
    buttons.append([InlineKeyboardButton(text='← К автомобилю',callback_data=f'car:{car_id}')])
    if not await _panel_text(call,out,kb(buttons)): await call.message.answer(out,reply_markup=kb(buttons))
    await call.answer()

@dp.callback_query(F.data.startswith('addfluid:'))
async def addfluid(call:CallbackQuery,state:FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer('Доступно только сервису',show_alert=True); return
    await state.update_data(car_id=int(call.data.split(':')[1])); await state.set_state(AddFluid.system); await call.message.edit_text('🛢️ <b>Масло</b>\n\nКакая система?',parse_mode='HTML',reply_markup=skip_markup('fluid_skip_system')); await call.answer()

@dp.callback_query(F.data=='fluid_skip_system')
async def fluid_skip_system(call:CallbackQuery,state:FSMContext):
    await state.update_data(system=''); await state.set_state(AddFluid.oil_name); await call.message.edit_text('Название масла?',reply_markup=skip_markup('fluid_skip_name')); await call.answer()

@dp.message(AddFluid.system)
async def fluid_system(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(system=(message.text or '').strip()); await state.set_state(AddFluid.oil_name); await message.answer('Название масла?',reply_markup=skip_markup('fluid_skip_name'))

@dp.callback_query(F.data=='fluid_skip_name')
async def fluid_skip_name(call:CallbackQuery,state:FSMContext):
    await state.update_data(oil_name=''); await state.set_state(AddFluid.oil_type); await call.message.edit_text('Тип / вязкость?',reply_markup=skip_markup('fluid_skip_type')); await call.answer()

@dp.message(AddFluid.oil_name)
async def fluid_name(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(oil_name=(message.text or '').strip()); await state.set_state(AddFluid.oil_type); await message.answer('Тип / вязкость?',reply_markup=skip_markup('fluid_skip_type'))

@dp.callback_query(F.data=='fluid_skip_type')
async def fluid_skip_type(call:CallbackQuery,state:FSMContext):
    await state.update_data(oil_type=''); await state.set_state(AddFluid.quantity); await call.message.edit_text('Количество, л?',reply_markup=skip_markup('fluid_skip_qty')); await call.answer()

@dp.message(AddFluid.oil_type)
async def fluid_type(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    await state.update_data(oil_type=(message.text or '').strip()); await state.set_state(AddFluid.quantity); await message.answer('Количество, л?',reply_markup=skip_markup('fluid_skip_qty'))

@dp.callback_query(F.data=='fluid_skip_qty')
async def fluid_skip_qty(call:CallbackQuery,state:FSMContext):
    await state.update_data(quantity=None); await save_fluid(call.message,state); await call.answer()

@dp.message(AddFluid.quantity)
async def fluid_qty(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    try: q=float(message.text.replace(',','.'))
    except: await message.answer('Введите число, например 7.5 или нажмите «Пропустить».'); return
    await state.update_data(quantity=q); await save_fluid(message,state)

async def save_fluid(message,state):
    d=await state.get_data(); c=conn(); c.execute('INSERT INTO fluids(car_id,system,oil_name,oil_type,quantity) VALUES(?,?,?,?,?)',(d['car_id'],d.get('system',''),d.get('oil_name',''),d.get('oil_type',''),d.get('quantity'))); c.commit(); c.close(); await state.clear(); await message.answer('✅ Данные сохранены.',reply_markup=admin_menu())

# Catalog add/edit/hide — one-message UI, matching the service picker
@dp.message(F.text=='🧰 Справочник запчастей')
async def part_catalog_open(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id): return
    c=conn(); rows=c.execute('SELECT * FROM part_catalog WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close()
    text='🧰 <b>Стандартные запчасти</b>\n\nЭтот список одинаковый для всех новых автомобилей. Номер запчасти указывается отдельно внутри каждой машины.\n\n'
    text += ''.join(f'{i+1}. {r["name"]}\n' for i,r in enumerate(rows))
    await message.answer(text,parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='❌ Закрыть',callback_data='pcat_close')]]))

@dp.callback_query(F.data=='pcat_add')
async def pcat_add(call:CallbackQuery,state:FSMContext):
    await state.set_state(PartCatalog.name); await state.update_data(pcat_message_id=call.message.message_id,pcat_chat_id=call.message.chat.id); await call.message.edit_text('➕ Введите название запчасти.\n\n← Назад',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='pcat_cancel')]])); await call.answer()

@dp.message(PartCatalog.name)
async def pcat_name(message:Message,state:FSMContext):
    if message.text=='← Назад': await state.clear(); await message.answer('Отменено.',reply_markup=admin_menu()); return
    name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    d=await state.get_data(); c=conn();
    if d.get('pcat_id'):
        cid=d['pcat_id']; old=c.execute('SELECT name FROM part_catalog WHERE id=?',(cid,)).fetchone(); c.execute('UPDATE part_catalog SET name=? WHERE id=?',(name,cid)); c.execute('UPDATE parts SET name=? WHERE catalog_id=?',(name,cid)); c.commit(); c.close(); await state.clear(); await message.answer('✅ Пункт переименован во всех машинах.',reply_markup=admin_menu()); return
    c.execute('INSERT OR IGNORE INTO part_catalog(name,sort_order,active) VALUES(?,?,1)',(name,c.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM part_catalog').fetchone()[0])); c.commit(); c.close(); await state.clear(); await message.answer('✅ Пункт добавлен в шаблон. Он будет добавляться в новые машины. Для старых машин используйте «🔄 Добавить новые пункты во все машины».',reply_markup=admin_menu())

@dp.callback_query(F.data.startswith('pcat_edit:'))
async def pcat_edit(call:CallbackQuery,state:FSMContext):
    cid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT * FROM part_catalog WHERE id=?',(cid,)).fetchone(); c.close()
    if not r: await call.answer('Не найдено',show_alert=True); return
    await call.message.edit_text(f'🧰 <b>{r["name"]}</b>',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='✏️ Переименовать',callback_data=f'pcat_rename:{cid}')],[InlineKeyboardButton(text='🗑 Скрыть',callback_data=f'pcat_hide:{cid}')],[InlineKeyboardButton(text='← Назад',callback_data='pcat_back')]])); await call.answer()

@dp.callback_query(F.data.startswith('pcat_rename:'))
async def pcat_rename(call:CallbackQuery,state:FSMContext):
    cid=int(call.data.split(':')[1]); c=conn(); r=c.execute('SELECT name FROM part_catalog WHERE id=?',(cid,)).fetchone(); c.close(); await state.update_data(pcat_id=cid); await state.set_state(PartCatalog.name); await call.message.edit_text(f'✏️ Сейчас: {r["name"]}\n\nВведите новое название.',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='pcat_cancel')]])); await call.answer()

@dp.callback_query(F.data=='pcat_hidden')
async def pcat_hidden(call:CallbackQuery):
    c=conn(); rows=c.execute('SELECT * FROM part_catalog WHERE active=0 ORDER BY sort_order,id').fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f'👁 {r["name"]}',callback_data=f'pcat_show:{r["id"]}')] for r in rows] or [[InlineKeyboardButton(text='Пусто',callback_data='pcat_back')]]; buttons.append([InlineKeyboardButton(text='← Назад',callback_data='pcat_back')]); await call.message.edit_text('👁 <b>Скрытые запчасти</b>',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()

@dp.callback_query(F.data.startswith('pcat_show:'))
async def pcat_show(call:CallbackQuery):
    cid=int(call.data.split(':')[1]); c=conn(); c.execute('UPDATE part_catalog SET active=1 WHERE id=?',(cid,)); sync_parts_to_all_cars(c); c.commit(); c.close(); await call.answer('Пункт возвращён.'); await pcat_back(call)

@dp.callback_query(F.data.startswith('pcat_hide:'))
async def pcat_hide(call:CallbackQuery):
    cid=int(call.data.split(':')[1]); c=conn(); c.execute('UPDATE part_catalog SET active=0 WHERE id=?',(cid,)); c.commit(); c.close(); await call.answer('Пункт скрыт.'); await pcat_back(call)

@dp.callback_query(F.data=='pcat_sync')
async def pcat_sync(call:CallbackQuery):
    c=conn(); sync_parts_to_all_cars(c); c.commit(); c.close(); await call.answer('Новые пункты добавлены во все машины.',show_alert=True); await pcat_back(call)

@dp.callback_query(F.data=='pcat_cancel')
async def pcat_cancel(call:CallbackQuery,state:FSMContext):
    await state.clear(); await pcat_back(call)

@dp.callback_query(F.data=='pcat_back')
async def pcat_back(call:CallbackQuery):
    c=conn(); rows=c.execute('SELECT * FROM part_catalog WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close(); buttons=[[InlineKeyboardButton(text=f'🧰 {r["name"]}',callback_data=f'pcat_edit:{r["id"]}')] for r in rows]; buttons += [[InlineKeyboardButton(text='➕ Добавить',callback_data='pcat_add')],[InlineKeyboardButton(text='🔄 Добавить новые пункты во все машины',callback_data='pcat_sync')],[InlineKeyboardButton(text='👁 Скрытые пункты',callback_data='pcat_hidden')],[InlineKeyboardButton(text='❌ Закрыть',callback_data='pcat_close')]]; await call.message.edit_text('🧰 <b>Справочник запчастей</b>\n\nЭти пункты автоматически добавляются во все автомобили.',parse_mode='HTML',reply_markup=kb(buttons)); await call.answer()

@dp.callback_query(F.data=='pcat_close')
async def pcat_close(call:CallbackQuery): await call.message.edit_text('🧰 Справочник запчастей закрыт.'); await call.answer()

async def render_catalog_groups(call):
    c=conn(); gs=c.execute("SELECT * FROM service_groups WHERE active=1 ORDER BY sort_order,id").fetchall(); c.close()
    rows=[[InlineKeyboardButton(text=f"🔧 {g['name']}",callback_data=f"cataloggroup:{g['id']}")] for g in gs]
    rows += [[InlineKeyboardButton(text='✏️ Редактировать',callback_data='catalog_visibility')],
             [InlineKeyboardButton(text='❌ Закрыть',callback_data='catalog_close')]]
    await call.message.edit_text('⚙️ <b>NNS SERVICE • Справочник работ</b>\n\nВыберите категорию.',parse_mode='HTML',reply_markup=kb(rows))

async def render_catalog_group(call,gid):
    c=conn(); g=c.execute("SELECT name FROM service_groups WHERE id=? AND active=1",(gid,)).fetchone(); ws=c.execute("SELECT * FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id",(gid,)).fetchall(); c.close()
    if not g: return
    rows=[[InlineKeyboardButton(text=f"🛠 {w['name']}",callback_data=f"catalog_work_noop:{w['id']}")] for w in ws]
    # Все настройки категории находятся внутри одного пункта «Видимость работ».
    # Это специально оставляет экран категории компактным.
    rows += [[InlineKeyboardButton(text='✏️ Редактировать',callback_data=f'cataloggroup_visibility:{gid}')],
             [InlineKeyboardButton(text='← Категории',callback_data='catalog_back')]]
    await call.message.edit_text(f"🔧 <b>{g['name']}</b>\n\nВыберите работу или откройте «Видимость работ» для управления всей категорией.",parse_mode='HTML',reply_markup=kb(rows))

@dp.message(F.text=='⚙️ Справочник работ')
async def catalog(message:Message):
    if not is_admin(message.from_user.id): return
    c=conn(); gs=c.execute("SELECT * FROM service_groups WHERE active=1 ORDER BY sort_order,id").fetchall(); c.close()
    rows=[[InlineKeyboardButton(text=f"🔧 {g['name']}",callback_data=f"cataloggroup:{g['id']}")] for g in gs]
    rows += [[InlineKeyboardButton(text='✏️ Редактировать',callback_data='catalog_visibility')],
             [InlineKeyboardButton(text='❌ Закрыть',callback_data='catalog_close')]]
    await message.answer('⚙️ <b>NNS SERVICE • Справочник работ</b>\n\nВыберите категорию.',parse_mode='HTML',reply_markup=kb(rows))

@dp.callback_query(F.data.startswith('cataloggroup:'))
async def catalog_group(call:CallbackQuery):
    await render_catalog_group(call,int(call.data.split(':')[1])); await call.answer()

@dp.callback_query(F.data=='catalog_back')
async def catalog_back(call:CallbackQuery):
    await render_catalog_groups(call); await call.answer()

@dp.callback_query(F.data=='catalog_visibility')
async def catalog_visibility(call:CallbackQuery):
    # Управление категориями. Никаких страниц для обычного количества
    # категорий: показываем каждую категорию отдельной кнопкой.
    await render_catalog_visibility_groups(call)
    await call.answer()

async def render_catalog_visibility_groups(call:CallbackQuery):
    c=conn()
    gs=c.execute('SELECT id,name,active FROM service_groups ORDER BY sort_order,id').fetchall()
    # На случай старой БД, где каталог не был мигрирован, восстанавливаем
    # стандартные категории прямо перед показом экрана.
    if not gs:
        migrate_service_catalog(c)
        c.commit()
        gs=c.execute('SELECT id,name,active FROM service_groups ORDER BY sort_order,id').fetchall()
    c.close()

    rows=[]
    for g in gs:
        status='☑️' if int(g['active']) else '⬜'
        rows.append([InlineKeyboardButton(
            text=f'{status} {g["name"]}',
            callback_data=f'catalog_toggle_group:{g["id"]}'
        )])
    rows.append([InlineKeyboardButton(text='➕ Добавить категорию',callback_data='newgroup')])
    rows.append([InlineKeyboardButton(text='↕️ Порядок категорий',callback_data='catalog_sort_groups')])
    rows.append([InlineKeyboardButton(text='← Назад',callback_data='catalog_back')])
    text='⚙️ <b>Редактирование категорий</b>\n\nНажмите на категорию, чтобы показать или скрыть её.\n☑️ — показывается\n⬜ — скрыта'
    if not gs:
        text='⚙️ <b>Редактирование категорий</b>\n\nКатегорий пока нет.'
    await call.message.edit_text(text,parse_mode='HTML',reply_markup=kb(rows))

@dp.callback_query(F.data.startswith('catalog_toggle_group:'))
async def catalog_toggle_group(call:CallbackQuery):
    gid=int(call.data.split(':')[1])
    c=conn()
    r=c.execute('SELECT active FROM service_groups WHERE id=?',(gid,)).fetchone()
    if not r:
        c.close()
        await call.answer('Категория не найдена',show_alert=True)
        return
    c.execute('UPDATE service_groups SET active=? WHERE id=?',(0 if int(r['active']) else 1,gid))
    c.commit(); c.close()
    await render_catalog_visibility_groups(call)
    await call.answer('Изменено')

@dp.callback_query(F.data.startswith('cataloggroup_visibility:'))
async def cataloggroup_visibility(call:CallbackQuery):
    gid=int(call.data.split(':')[1])
    await render_catalog_visibility_works(call,gid)
    await call.answer()

async def render_catalog_visibility_works(call:CallbackQuery,gid:int,page:int=0):
    """Compact category editor. Keep the keyboard under Telegram's 100-button limit.
    45 works/page gives 90 work buttons + controls, so the bottom controls always render.
    """
    PAGE_SIZE=45
    c=conn()
    g=c.execute('SELECT id,name,active FROM service_groups WHERE id=?',(gid,)).fetchone()
    total=int(c.execute('SELECT COUNT(*) FROM works WHERE group_id=?',(gid,)).fetchone()[0]) if g else 0
    max_page=max(0,(total-1)//PAGE_SIZE) if total else 0
    page=max(0,min(int(page),max_page))
    ws=c.execute('SELECT id,name,active FROM works WHERE group_id=? ORDER BY sort_order,id LIMIT ? OFFSET ?',(gid,PAGE_SIZE,page*PAGE_SIZE)).fetchall()
    c.close()
    if not g:
        await call.answer('Категория не найдена',show_alert=True); return

    rows=[]
    for w in ws:
        status='☑️' if int(w['active']) else '⬜'
        rows.append([
            InlineKeyboardButton(text=f'{status} {w["name"]}', callback_data=f'catalog_toggle_work:{w["id"]}:{gid}:{page}'),
            InlineKeyboardButton(text='✏️', callback_data=f'catalog_rename_work:{w["id"]}:{gid}:{page}')
        ])

    nav=[]
    if page>0:
        nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'catalog_vispage:{gid}:{page-1}'))
    if max_page>0:
        nav.append(InlineKeyboardButton(text=f'{page+1}/{max_page+1}',callback_data=f'catalog_vispage:{gid}:{page}'))
    if page<max_page:
        nav.append(InlineKeyboardButton(text='➡️',callback_data=f'catalog_vispage:{gid}:{page+1}'))
    if nav:
        rows.append(nav)

    rows += [
        [InlineKeyboardButton(text='➕ Добавить работу',callback_data=f'newwork:{gid}')],
        [InlineKeyboardButton(text='↕️ Порядок работ',callback_data=f'catalog_sort_works:{gid}')],
        [InlineKeyboardButton(text='✏️ Изменить категорию',callback_data=f'renamegroup:{gid}')],
        [InlineKeyboardButton(text='← Назад',callback_data=f'cataloggroup:{gid}')]
    ]
    text=f'⚙️ <b>Редактирование: {g["name"]}</b>\n\n☑️ — показывается\n⬜ — скрыта'
    if total:
        text += f'\n\nРаботы: {page*PAGE_SIZE+1}–{min((page+1)*PAGE_SIZE,total)} из {total}'
    else:
        text += '\n\nРабот в этой категории пока нет.'
    await call.message.edit_text(text,parse_mode='HTML',reply_markup=kb(rows))

@dp.callback_query(F.data.startswith('catalog_vispage:'))
async def catalog_vispage(call:CallbackQuery):
    _,gid,page=call.data.split(':')
    await render_catalog_visibility_works(call,int(gid),int(page))
    await call.answer()

@dp.callback_query(F.data.startswith('catalog_work_noop:'))
async def catalog_work_noop(call:CallbackQuery):
    # Work names in the normal catalog are intentionally not actionable.
    # Editing is available only through the category's «Редактировать» screen.
    await call.answer()

@dp.callback_query(F.data.startswith('catalog_rename_work:'))
async def catalog_rename_work(call:CallbackQuery,state:FSMContext):
    parts=call.data.split(':')
    wid=int(parts[1]); gid=int(parts[2]); page=int(parts[3]) if len(parts)>3 else 0
    c=conn(); w=c.execute('SELECT * FROM works WHERE id=? AND group_id=?',(wid,gid)).fetchone(); c.close()
    if not w:
        await call.answer('Работа не найдена',show_alert=True); return
    await state.update_data(work_id=wid,group_id=gid,catalog_chat_id=call.message.chat.id,
                           catalog_message_id=call.message.message_id,visibility_mode=True,catalog_page=page)
    await state.set_state(Catalog.edit_name)
    await call.message.edit_text(
        f'✏️ <b>Переименовать работу</b>\n\nСейчас: <code>{w["name"]}</code>\n\nВведите новое название.',
        parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='catalog_input_cancel')]]))
    await call.answer()

@dp.callback_query(F.data.startswith('catalog_toggle_work:'))
async def catalog_toggle_work(call:CallbackQuery):
    parts=call.data.split(':')
    wid=int(parts[1]); gid=int(parts[2]); page=int(parts[3]) if len(parts)>3 else 0
    c=conn()
    r=c.execute('SELECT active,group_id FROM works WHERE id=?',(wid,)).fetchone()
    if not r or int(r['group_id'])!=gid:
        c.close()
        await call.answer('Работа не найдена',show_alert=True)
        return
    c.execute('UPDATE works SET active=? WHERE id=?',(0 if int(r['active']) else 1,wid))
    c.commit(); c.close()
    await render_catalog_visibility_works(call,gid,page)
    await call.answer('Изменено')

@dp.callback_query(F.data=='catalog_close')
async def catalog_close(call:CallbackQuery):
    await call.message.edit_text('⚙️ Справочник закрыт.'); await call.answer()

@dp.callback_query(F.data=='newgroup')
async def newgroup(call:CallbackQuery,state:FSMContext):
    await state.update_data(catalog_chat_id=call.message.chat.id,catalog_message_id=call.message.message_id)
    await state.set_state(Catalog.group_name)
    await call.message.edit_text('➕ <b>Новая категория</b>\n\nВведите название.\n\n← Назад',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='catalog_input_cancel')]])); await call.answer()

@dp.message(Catalog.group_name)
async def group_name(message:Message,state:FSMContext):
    if message.text=='← Назад':
        d=await state.get_data(); await state.clear()
        if d.get('catalog_message_id'):
            if d.get('group_id'): await render_catalog_group(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id']),d['group_id'])
            else: await render_catalog_groups(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id']))
            return
        await message.answer('Отменено.',reply_markup=admin_menu()); return
    name=(message.text or '').strip()
    if not name: await message.answer('Название не может быть пустым.'); return
    d=await state.get_data(); c=conn()
    if d.get('group_id') and d.get('catalog_message_id'):
        c.execute('UPDATE service_groups SET name=? WHERE id=?',(name,d['group_id'])); c.commit(); c.close()
        sc=SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id'])
        await state.clear()
        if d.get('visibility_mode'):
            await render_catalog_visibility_works(sc,d['group_id'],int(d.get('catalog_page',0))); return
        await render_catalog_group(sc,d['group_id']); return
    mx=c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM service_groups').fetchone()[0]; c.execute('INSERT INTO service_groups(name,sort_order) VALUES(?,?)',(name,mx+1)); c.commit(); c.close(); await state.clear()
    if d.get('catalog_message_id'):
        await render_catalog_groups(SimpleCall(bot,d['catalog_chat_id'],d['catalog_message_id'])); return
    await message.answer('✅ Категория добавлена.',reply_markup=admin_menu())

@dp.callback_query(F.data.startswith('newwork:'))
async def newwork(call:CallbackQuery,state:FSMContext):
    gid=int(call.data.split(':')[1]); await state.update_data(group_id=gid,catalog_chat_id=call.message.chat.id,catalog_message_id=call.message.message_id); await state.set_state(Catalog.work_name)
    await call.message.edit_text('➕ <b>Новая работа</b>\n\nВведите название. После сохранения вернётесь сюда.\n\n← Назад',parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='catalog_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('editwork:'))
async def editwork(call:CallbackQuery,state:FSMContext):
    wid=int(call.data.split(':')[1]); c=conn(); w=c.execute('SELECT * FROM works WHERE id=?',(wid,)).fetchone(); c.close()
    if not w: await call.answer('Работа не найдена',show_alert=True); return
    await state.update_data(work_id=wid,group_id=w['group_id'],catalog_chat_id=call.message.chat.id,catalog_message_id=call.message.message_id)
    await call.message.edit_text(f"🛠 <b>{w['name']}</b>\n\nВыберите действие.",parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='✏️ Переименовать',callback_data=f'renamework:{wid}')],[InlineKeyboardButton(text='🗑 Скрыть из списка',callback_data=f'hidework:{wid}')],[InlineKeyboardButton(text='← Назад',callback_data=f"cataloggroup:{w['group_id']}")]])); await call.answer()

@dp.callback_query(F.data.startswith('renamework:'))
async def renamework(call:CallbackQuery,state:FSMContext):
    wid=int(call.data.split(':')[1]); c=conn(); w=c.execute('SELECT * FROM works WHERE id=?',(wid,)).fetchone(); c.close()
    await state.update_data(work_id=wid,group_id=w['group_id'],catalog_chat_id=call.message.chat.id,catalog_message_id=call.message.message_id,visibility_mode=False); await state.set_state(Catalog.edit_name)
    await call.message.edit_text(f"✏️ <b>Переименовать работу</b>\n\nСейчас: <code>{w['name']}</code>\n\nВведите новое название одним сообщением.\n\n← Назад",parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='catalog_input_cancel')]])); await call.answer()

@dp.callback_query(F.data.startswith('renamegroup:'))
async def renamegroup(call:CallbackQuery,state:FSMContext):
    gid=int(call.data.split(':')[1]); c=conn(); g=c.execute('SELECT name FROM service_groups WHERE id=?',(gid,)).fetchone(); c.close()
    await state.update_data(group_id=gid,catalog_chat_id=call.message.chat.id,catalog_message_id=call.message.message_id,visibility_mode=True,catalog_page=0); await state.set_state(Catalog.group_name)
    await call.message.edit_text(f"✏️ <b>Переименовать категорию</b>\n\nСейчас: <code>{g['name']}</code>\n\nВведите новое название.\n\n← Назад",parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='catalog_input_cancel')]])); await call.answer()

@dp.callback_query(F.data=='catalog_input_cancel')
async def catalog_input_cancel(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); gid=d.get('group_id'); await state.clear()
    if gid: await render_catalog_group(call,gid)
    else: await render_catalog_groups(call)
    await call.answer()

@dp.callback_query(F.data.startswith('hidework:'))
async def hidework(call:CallbackQuery):
    wid=int(call.data.split(':')[1]); c=conn(); w=c.execute('SELECT group_id FROM works WHERE id=?',(wid,)).fetchone(); c.execute('UPDATE works SET active=0 WHERE id=?',(wid,)); c.commit(); c.close()
    if w: await render_catalog_group(call,w['group_id'])
    await call.answer('Работа скрыта. Старые записи сохраняются.')

@dp.callback_query(F.data.startswith('delgroup:'))
async def delgroup(call:CallbackQuery):
    gid=int(call.data.split(':')[1]); c=conn(); c.execute('UPDATE service_groups SET active=0 WHERE id=?',(gid,)); c.execute('UPDATE works SET active=0 WHERE group_id=?',(gid,)); c.commit(); c.close(); await render_catalog_groups(call); await call.answer('Категория скрыта. Старые записи сохраняются.')

@dp.callback_query(F.data=='catalog_sort_groups')
async def catalog_sort_groups(call:CallbackQuery):
    c=conn(); gs=c.execute('SELECT * FROM service_groups WHERE active=1 ORDER BY sort_order,id').fetchall(); c.close(); rows=[]
    for i,g in enumerate(gs):
        nav=[]
        if i>0: nav.append(InlineKeyboardButton(text='⬆️',callback_data=f"movegroup:{g['id']}:up"))
        if i<len(gs)-1: nav.append(InlineKeyboardButton(text='⬇️',callback_data=f"movegroup:{g['id']}:down"))
        nav.append(InlineKeyboardButton(text=g['name'],callback_data=f"cataloggroup:{g['id']}")); rows.append(nav)
    rows.append([InlineKeyboardButton(text='← Назад',callback_data='catalog_back')])
    await call.message.edit_text('↕️ <b>Порядок категорий</b>\n\n⬆️/⬇️ меняют порядок.',parse_mode='HTML',reply_markup=kb(rows)); await call.answer()

@dp.callback_query(F.data.startswith('movegroup:'))
async def move_group(call:CallbackQuery):
    _,gid,dirn=call.data.split(':'); gid=int(gid); c=conn(); ids=[r['id'] for r in c.execute('SELECT id FROM service_groups WHERE active=1 ORDER BY sort_order,id').fetchall()]; i=ids.index(gid); j=i-1 if dirn=='up' else i+1
    if 0<=j<len(ids): ids[i],ids[j]=ids[j],ids[i]
    for n,x in enumerate(ids): c.execute('UPDATE service_groups SET sort_order=? WHERE id=?',(n,x))
    c.commit(); c.close(); await catalog_sort_groups(call); await call.answer()

@dp.callback_query(F.data.startswith('catalog_sort_works:'))
async def catalog_sort_works_page(call:CallbackQuery):
    parts=call.data.split(':')
    gid=int(parts[1]); page=int(parts[2]) if len(parts)>2 else 0
    await render_catalog_sort_works(call,gid,page)
    await call.answer()

async def render_catalog_sort_works(call:CallbackQuery, gid:int, page:int=0):
    """Number-based work ordering. Each work is one button; clicking it asks for
    the desired position. Moving 20 -> 3 automatically shifts the old 3..19 down,
    so duplicate positions never occur. One button per work lets the full list
    fit in a single Telegram keyboard for normal-sized categories.
    """
    PAGE_SIZE=97
    c=conn()
    g=c.execute('SELECT name FROM service_groups WHERE id=? AND active=1',(gid,)).fetchone()
    total=int(c.execute('SELECT COUNT(*) FROM works WHERE group_id=? AND active=1',(gid,)).fetchone()[0]) if g else 0
    max_page=max(0,(total-1)//PAGE_SIZE) if total else 0
    page=max(0,min(int(page),max_page))
    ws=c.execute('SELECT id,name FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id LIMIT ? OFFSET ?',(gid,PAGE_SIZE,page*PAGE_SIZE)).fetchall()
    c.close()
    if not g:
        await call.answer('Категория не найдена',show_alert=True); return

    rows=[]
    for idx,w in enumerate(ws):
        absolute=page*PAGE_SIZE+idx
        rows.append([InlineKeyboardButton(
            text=f'{absolute+1}. {w["name"]}',
            callback_data=f'work_order_num:{w["id"]}:{gid}:{page}'
        )])

    if max_page>0:
        nav=[]
        if page>0: nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'catalog_sort_works:{gid}:{page-1}'))
        nav.append(InlineKeyboardButton(text=f'{page+1}/{max_page+1}',callback_data=f'catalog_sort_works:{gid}:{page}'))
        if page<max_page: nav.append(InlineKeyboardButton(text='➡️',callback_data=f'catalog_sort_works:{gid}:{page+1}'))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text='← Назад',callback_data=f'cataloggroup_visibility:{gid}')])
    text=f'↕️ <b>Порядок работ: {g["name"]}</b>\n\nНажмите на работу и введите её новый порядковый номер.\nНапример: <b>20 → 3</b>. Старая №3 автоматически станет №4, и так далее.'
    if total: text += f'\n\nРаботы: {page*PAGE_SIZE+1}–{min((page+1)*PAGE_SIZE,total)} из {total}'
    else: text += '\n\nРабот в этой категории пока нет.'
    await call.message.edit_text(text,parse_mode='HTML',reply_markup=kb(rows))


@dp.callback_query(F.data.startswith('work_order_num:'))
async def work_order_num_start(call:CallbackQuery,state:FSMContext):
    _,wid,gid,page=call.data.split(':')
    wid=int(wid); gid=int(gid); page=int(page)
    c=conn(); w=c.execute('SELECT id,name,sort_order,group_id,active FROM works WHERE id=? AND group_id=?',(wid,gid)).fetchone()
    if not w or not int(w['active']):
        await call.answer('Работа не найдена',show_alert=True); return
    current=int(w['sort_order'])+1
    await state.update_data(work_order_id=wid,group_id=gid,work_order_page=page,
                            order_chat_id=call.message.chat.id,order_message_id=call.message.message_id)
    await state.set_state(Catalog.work_order_num)
    total_count=c.execute("SELECT COUNT(*) FROM works WHERE group_id=? AND active=1",(gid,)).fetchone()[0]
    c.close()
    await call.message.edit_text(
        f'🔢 <b>Новый номер работы</b>\n\nРабота: <b>{w["name"]}</b>\nСейчас: <b>{current}</b>\n\nВведите новый номер от <b>1</b> до <b>{total_count}</b>.\n\nНапример: <b>20 → 3</b> — старая №3 автоматически станет №4.',
        parse_mode='HTML',reply_markup=kb([[InlineKeyboardButton(text='← Назад',callback_data='work_order_num_cancel')]]))
    # Основная панель остаётся одной и той же, а введённое сообщение удаляется после сохранения.
    await call.answer()


@dp.callback_query(F.data=='work_order_num_cancel')
async def work_order_num_cancel(call:CallbackQuery,state:FSMContext):
    d=await state.get_data(); gid=d.get('group_id'); page=int(d.get('work_order_page',0)); await state.clear()
    if gid: await render_catalog_sort_works(call,int(gid),page)
    else: await render_catalog_groups(call)
    await call.answer()


@dp.message(Catalog.work_order_num)
async def work_order_num_save(message:Message,state:FSMContext):
    if message.text=='← Назад':
        d=await state.get_data(); gid=d.get('group_id'); page=int(d.get('work_order_page',0)); await state.clear()
        if gid:
            await render_catalog_sort_works(SimpleCall(bot,d['order_chat_id'],d['order_message_id']),int(gid),page)
        else:
            await message.answer('Отменено.',reply_markup=admin_menu())
        return
    d=await state.get_data(); raw=(message.text or '').strip()
    try: target=int(raw)
    except:
        await message.answer('Введите целое число — новый порядковый номер.'); return
    gid=int(d['group_id']); wid=int(d['work_order_id']); page=int(d.get('work_order_page',0))
    c=conn()
    rows=c.execute('SELECT id FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id',(gid,)).fetchall()
    ids=[int(r['id']) for r in rows]; n=len(ids)
    if wid not in ids:
        c.close(); await state.clear(); await message.answer('Работа не найдена.'); return
    if target<1 or target>n:
        c.close(); await message.answer(f'Номер должен быть от 1 до {n}.'); return
    old=ids.index(wid)+1
    if old != target:
        item=ids.pop(old-1); ids.insert(target-1,item)
        for pos,x in enumerate(ids):
            c.execute('UPDATE works SET sort_order=? WHERE id=?',(pos,x))
        c.commit()
    c.close()
    # Show the page containing the newly assigned position.
    new_page=(target-1)//97
    chat_id=d['order_chat_id']; message_id=d['order_message_id']
    await state.clear()
    await render_catalog_sort_works(SimpleCall(bot,chat_id,message_id),gid,new_page)
    await message.delete()


@dp.callback_query(F.data.startswith('work_order_select:'))
async def work_order_select(call:CallbackQuery):
    _,wid,gid,page=call.data.split(':')
    wid=int(wid); gid=int(gid); page=int(page)
    c=conn(); w=c.execute('SELECT id,name,group_id,active FROM works WHERE id=? AND group_id=?',(wid,gid)).fetchone(); c.close()
    if not w or not int(w['active']):
        await call.answer('Работа не найдена',show_alert=True); return
    rows=[
        [InlineKeyboardButton(text='⬆️ Поднять',callback_data=f'movework:{wid}:up:{gid}:{page}'),
         InlineKeyboardButton(text='⬇️ Опустить',callback_data=f'movework:{wid}:down:{gid}:{page}')],
        [InlineKeyboardButton(text='← К списку',callback_data=f'catalog_sort_works:{gid}:{page}')]
    ]
    await call.message.edit_text(f'↕️ <b>{w["name"]}</b>\n\nВыберите действие:',parse_mode='HTML',reply_markup=kb(rows))
    await call.answer()

@dp.callback_query(F.data.startswith('noop_order:'))
async def noop_order(call:CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith('movework:'))
async def move_work(call:CallbackQuery):
    parts=call.data.split(':')
    wid=int(parts[1]); dirn=parts[2]; gid_from_cb=int(parts[3]); page=int(parts[4]) if len(parts)>4 else 0
    c=conn()
    w=c.execute('SELECT group_id FROM works WHERE id=? AND active=1',(wid,)).fetchone()
    if not w or int(w['group_id'])!=gid_from_cb:
        c.close(); await call.answer('Работа не найдена в этой категории',show_alert=True); return
    gid=int(w['group_id'])
    ids=[r['id'] for r in c.execute('SELECT id FROM works WHERE group_id=? AND active=1 ORDER BY sort_order,id',(gid,)).fetchall()]
    i=ids.index(wid); j=i-1 if dirn=='up' else i+1
    if 0<=j<len(ids):
        ids[i],ids[j]=ids[j],ids[i]
        for n,x in enumerate(ids): c.execute('UPDATE works SET sort_order=? WHERE id=?',(n,x))
        c.commit()
    c.close()
    await render_catalog_sort_works(call,gid,page)
    await call.answer()

@dp.message(F.text=='📋 Последние работы')
async def recent(message:Message):
    if not is_admin(message.from_user.id): return
    c=conn(); rows=c.execute('SELECT s.*,c.brand,c.model,cl.username FROM services s JOIN cars c ON c.id=s.car_id JOIN clients cl ON cl.id=c.client_id WHERE s.deleted=0 ORDER BY s.id DESC LIMIT 20').fetchall(); c.close()
    if not rows: await message.answer('Работ пока нет.'); return
    out='📋 Последние работы\n\n';
    for r in rows: out+=f"{r['service_date']} — {r['brand']} {r['model']} — @{r['username'] if r['username'] else 'без username'}\n{r['work_name']} — {r['mileage']:,} км\n\n".replace(',',' ')
    await message.answer(out)

@dp.message()
async def fallback(message:Message): await message.answer('Выберите действие в меню 👇',reply_markup=admin_menu() if is_admin(message.from_user.id) else client_menu())

async def main():
    global bot
    if not BOT_TOKEN: raise RuntimeError('Не задан BOT_TOKEN. Создайте .env по примеру.')
    init_db(); bot=Bot(BOT_TOKEN); await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
