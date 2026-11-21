from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import secrets
import psycopg2
import requests
import json
import re
import hmac
import hashlib
import time
import threading
from collections import deque

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024   # nadie necesita enviar mas de 64 KB

USUARIO_VALIDO = re.compile(r'^[\w.\-]{1,60}$')
MAX_LINKS = 1500
SESION_DIAS = 90

PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '')
GOOGLE_CLIENT_ID = '443136299093-nf63uj2112a5h9hiknmksot6veldl5na.apps.googleusercontent.com'

# ===== APIFY (Instagram) =====
APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '')
APIFY_ACTOR = 'apify~instagram-reel-scraper'
INSTAGRAM_MAX_REELS = 99999

# ===== SUSCRIPCIONES (valores por defecto; se cambian desde el panel) =====
# links = links al mes (None = ilimitados). instagram = si incluye Instagram
PLANES_DEFECTO = {
    'basico':    {'nombre': 'Básico',    'precio': 9,  'links': 300,  'instagram': False},
    'pro':       {'nombre': 'Pro',       'precio': 19, 'links': 5000, 'instagram': False},
    'ilimitado': {'nombre': 'Ilimitado', 'precio': 56, 'links': None, 'instagram': True},
}
ORDEN_PLANES = ['basico', 'pro', 'ilimitado']
DIAS_DEFECTO = 30       # dias de acceso por cada pago
PROMO_DEFECTO = 50      # % de descuento en el primer pago de cada cuenta (0 = sin promocion)

# ===== PAGOS CON CRIPTO (NOWPayments) =====
NOWPAY_API_KEY = os.environ.get('NOWPAY_API_KEY', '')
NOWPAY_IPN_SECRET = os.environ.get('NOWPAY_IPN_SECRET', '')
NOWPAY_API = 'https://api.nowpayments.io/v1'

# ===== PAGOS CON TARJETA / CRIPTO (PaymeGate) — si hay clave, se usa esta =====
PAYMEGATE_API_KEY = os.environ.get('PAYMEGATE_API_KEY', '')
PAYMEGATE_API = 'https://api.paymegate.com'


def get_db():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))


def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS searches (
            id SERIAL PRIMARY KEY, ip TEXT, platform TEXT, username TEXT,
            count INTEGER, user_email TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS count INTEGER")
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS user_email TEXT")
        cur.execute('''CREATE TABLE IF NOT EXISTS logins (
            id SERIAL PRIMARY KEY, email TEXT, name TEXT, picture TEXT, ip TEXT,
            created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY, email TEXT, message TEXT, ip TEXT,
            replied BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, name TEXT, picture TEXT,
            created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS debe_pagar BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_hasta TIMESTAMP")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_plan TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS links_usados INTEGER DEFAULT 0")
        cur.execute('''CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY, email TEXT, plan TEXT, creditos INTEGER,
            importe NUMERIC, metodo TEXT, estado TEXT DEFAULT 'pendiente', ref TEXT,
            created_at TIMESTAMP DEFAULT NOW())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS forced_ips (
            ip TEXT PRIMARY KEY, nota TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        # Sesiones de login verificadas con Google
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, email TEXT, created_at TIMESTAMP DEFAULT NOW())''')
        # Perfiles (@) bloqueados: quien los busque tendra que entrar y pagar
        cur.execute('''CREATE TABLE IF NOT EXISTS blocked_profiles (
            username TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT NOW())''')
        # Chat de soporte
        cur.execute('''CREATE TABLE IF NOT EXISTS chat (
            id SERIAL PRIMARY KEY, email TEXT, de TEXT, texto TEXT,
            leido BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())''')
        # Ajustes editables desde el panel
        cur.execute('''CREATE TABLE IF NOT EXISTS ajustes (
            clave TEXT PRIMARY KEY, valor TEXT)''')
        cur.execute("DELETE FROM sessions WHERE created_at < NOW() - INTERVAL '120 days'")
        cur.execute('CREATE INDEX IF NOT EXISTS idx_searches_email ON searches (user_email)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_searches_fecha ON searches (created_at)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_email ON chat (email)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_ref ON payments (ref)')
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('init_db error:', e)


init_db()


# ===================== UTILIDADES =====================

def client_ip():
    """IP real del visitante (Railway pone la suya delante, la real viene en estas cabeceras)."""
    ip = (request.headers.get('X-Real-IP') or '').strip()
    if not ip:
        ip = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return (ip or request.remote_addr or '')[:60]


# ----- Limite de peticiones (en memoria) -----
_rl = {}
_rl_lock = threading.Lock()


def limitado(clave, maximo, segundos):
    """True si 'clave' ya ha hecho 'maximo' peticiones en los ultimos 'segundos'."""
    ahora = time.time()
    with _rl_lock:
        if len(_rl) > 20000:
            _rl.clear()
        cola = _rl.setdefault(clave, deque())
        while cola and cola[0] < ahora - segundos:
            cola.popleft()
        if len(cola) >= maximo:
            return True
        cola.append(ahora)
        return False


def demasiadas():
    return jsonify({'error': 'demasiadas_peticiones'}), 429


def fallo(e):
    """Error inesperado: se guarda en el registro y al visitante no se le dan detalles internos."""
    app.logger.exception(e)
    return jsonify({'error': 'error_interno'}), 500


def check_password():
    """Clave del panel. Con 10 intentos fallidos desde una IP se bloquea 15 minutos."""
    if not PANEL_PASSWORD:
        return False
    ip = client_ip()
    with _rl_lock:
        fallos = _rl.get('pwfail:' + ip)
        if fallos:
            while fallos and fallos[0] < time.time() - 900:
                fallos.popleft()
            if len(fallos) >= 10:
                return False
    pw = request.headers.get('X-Panel-Password') or request.args.get('pw', '')
    if hmac.compare_digest(pw.encode('utf-8'), PANEL_PASSWORD.encode('utf-8')):
        return True
    with _rl_lock:
        _rl.setdefault('pwfail:' + ip, deque()).append(time.time())
    return False


@app.after_request
def cabeceras_seguridad(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.path not in ('/', '/panel'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


def get_ajuste(clave, defecto):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT valor FROM ajustes WHERE clave = %s', (clave,))
        f = cur.fetchone()
        cur.close()
        conn.close()
        return f[0] if f else defecto
    except:
        return defecto


def get_planes():
    """Planes con los precios/links guardados desde el panel."""
    planes = {k: dict(v) for k, v in PLANES_DEFECTO.items()}
    try:
        guardado = json.loads(get_ajuste('planes', '{}') or '{}')
        for k, v in guardado.items():
            if k in planes:
                if 'precio' in v:
                    planes[k]['precio'] = round(float(v['precio']), 2)
                if 'links' in v:
                    planes[k]['links'] = None if v['links'] in (None, '', 0) else int(v['links'])
    except:
        pass
    return planes


def dias_sub():
    try:
        return int(get_ajuste('dias', DIAS_DEFECTO))
    except:
        return DIAS_DEFECTO


def promo_pct():
    """% de descuento del primer pago (0 = desactivado)."""
    try:
        return max(0, min(90, int(float(get_ajuste('promo', PROMO_DEFECTO)))))
    except:
        return PROMO_DEFECTO


def promo_usuario(email):
    """Descuento que le toca a esta cuenta: solo si nunca ha pagado nada."""
    pct = promo_pct()
    if not pct or not email:
        return 0
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM payments WHERE email = %s AND estado = 'pagado' LIMIT 1", (email,))
        ya_pago = cur.fetchone() is not None
        cur.close()
        conn.close()
        return 0 if ya_pago else pct
    except:
        return 0


def precio_final(precio, pct):
    return round(float(precio) * (100 - pct) / 100.0, 2) if pct else float(precio)


def leer_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    t = request.args.get('token', '')
    if not t:
        data = request.get_json(force=True, silent=True) or {}
        t = data.get('token', '') or ''
    return (t or '').strip()


def email_de_sesion():
    """Email del usuario segun su sesion verificada. '' si no hay sesion valida."""
    token = leer_token()
    if not token:
        return ''
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""SELECT email FROM sessions
                       WHERE token = %s AND created_at > NOW() - (%s || ' days')::interval""",
                    (token, str(SESION_DIAS)))
        f = cur.fetchone()
        cur.close()
        conn.close()
        return f[0] if f else ''
    except:
        return ''


def get_user(email):
    """Datos del usuario. Lo crea si no existe."""
    if not email:
        return None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO users (email) VALUES (%s) ON CONFLICT (email) DO NOTHING', (email,))
        conn.commit()
        cur.execute('''SELECT email, name, picture, debe_pagar, sub_hasta,
                              (sub_hasta IS NOT NULL AND sub_hasta > NOW()),
                              COALESCE(sub_plan, 'ilimitado'), COALESCE(links_usados, 0)
                       FROM users WHERE email = %s''', (email,))
        f = cur.fetchone()
        cur.close()
        conn.close()
        if not f:
            return None
        planes = get_planes()
        plan = f[6] if f[6] in planes else 'ilimitado'
        limite = planes[plan]['links']
        usados = int(f[7] or 0)
        return {'email': f[0], 'name': f[1] or '', 'picture': f[2] or '',
                'debe_pagar': bool(f[3]), 'sub_hasta': str(f[4]) if f[4] else '',
                'activo': bool(f[5]), 'plan': plan, 'plan_nombre': planes[plan]['nombre'],
                'instagram': bool(planes[plan]['instagram']),
                'limite': limite, 'usados': usados,
                'restantes': None if limite is None else max(0, limite - usados)}
    except:
        return None


def perfil_bloqueado(username):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM blocked_profiles WHERE username = %s', (username.lower(),))
        b = cur.fetchone() is not None
        cur.close()
        conn.close()
        return b
    except:
        return False


def ip_forzada(ip):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM forced_ips WHERE ip = %s LIMIT 1', (ip,))
        b = cur.fetchone() is not None
        cur.close()
        conn.close()
        return b
    except:
        return False


# ===================== PAGINAS =====================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/panel')
def panel():
    return send_from_directory('.', 'panel.html')


# ===================== LOGIN Y CUENTA =====================

@app.route('/track-login', methods=['POST'])
def track_login():
    """Recibe el login de Google, lo comprueba con Google y crea una sesion."""
    if limitado('login:' + client_ip(), 15, 300):
        return demasiadas()
    try:
        data = request.get_json(force=True, silent=True) or {}
        credential = str(data.get('credential', '') or '')
        if not credential or len(credential) > 5000:
            return jsonify({'error': 'sin_credencial'}), 400
        r = requests.get('https://oauth2.googleapis.com/tokeninfo',
                         params={'id_token': credential}, timeout=15)
        if r.status_code != 200:
            return jsonify({'error': 'login_invalido'}), 401
        info = r.json()
        if (info.get('aud') != GOOGLE_CLIENT_ID
                or info.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
                or str(info.get('email_verified')).lower() != 'true'):
            return jsonify({'error': 'login_invalido'}), 401
        email = (info.get('email') or '').strip().lower()
        name = (info.get('name', '') or '')[:120]
        picture = (info.get('picture', '') or '')[:500]
        if not picture.startswith('https://'):
            picture = ''
        if not email:
            return jsonify({'error': 'login_invalido'}), 401

        token = secrets.token_urlsafe(32)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO logins (email, name, picture, ip) VALUES (%s, %s, %s, %s)',
                    (email, name, picture, client_ip()))
        cur.execute('''INSERT INTO users (email, name, picture) VALUES (%s, %s, %s)
                       ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, picture = EXCLUDED.picture''',
                    (email, name, picture))
        cur.execute('INSERT INTO sessions (token, email) VALUES (%s, %s)', (token, email))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True, 'token': token, 'email': email, 'name': name, 'picture': picture})
    except Exception as e:
        return fallo(e)


@app.route('/logout', methods=['POST'])
def logout():
    token = leer_token()
    if token:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('DELETE FROM sessions WHERE token = %s', (token,))
            conn.commit()
            cur.close()
            conn.close()
        except:
            pass
    return jsonify({'ok': True})


@app.route('/mi-cuenta')
def mi_cuenta():
    email = email_de_sesion()
    if not email:
        return jsonify({'logueado': False})
    comprobar_pendientes(email)
    u = get_user(email)
    if not u:
        return jsonify({'logueado': False})
    no_leidos = 0
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM chat WHERE email = %s AND de = 'admin' AND leido = FALSE", (email,))
        no_leidos = cur.fetchone()[0]
        cur.close()
        conn.close()
    except:
        pass
    return jsonify({'logueado': True, 'email': u['email'], 'activo': u['activo'],
                    'sub_hasta': u['sub_hasta'], 'debe_pagar': u['debe_pagar'],
                    'plan': u['plan'] if u['activo'] else '', 'plan_nombre': u['plan_nombre'] if u['activo'] else '',
                    'instagram': u['activo'] and u['instagram'],
                    'limite': u['limite'], 'usados': u['usados'], 'restantes': u['restantes'],
                    'promo': promo_usuario(email), 'chat_no_leidos': no_leidos})


def lista_planes():
    planes = get_planes()
    return [dict(planes[k], id=k) for k in ORDEN_PLANES]


@app.route('/config')
def config():
    return jsonify({'planes': lista_planes(), 'dias': dias_sub(), 'promo': promo_pct(), 'v': 3})


# ===================== PAGOS CON CRIPTO =====================

@app.route('/crear-pago', methods=['POST'])
def crear_pago():
    if limitado('pago:' + client_ip(), 8, 600):
        return demasiadas()
    if PAYMEGATE_API_KEY:
        return crear_pago_paymegate()
    if not NOWPAY_API_KEY:
        return jsonify({'error': 'pagos_no_configurados'}), 503
    email = email_de_sesion()
    if not email:
        return jsonify({'error': 'login_requerido'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        plan = data.get('plan', 'pro')
        planes = get_planes()
        if plan not in planes:
            return jsonify({'error': 'plan_invalido'}), 400
        importe = precio_final(planes[plan]['precio'], promo_usuario(email))
        dias = dias_sub()
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''INSERT INTO payments (email, plan, creditos, importe, metodo, estado)
                       VALUES (%s, %s, %s, %s, 'cripto', 'pendiente') RETURNING id''',
                    (email, plan, dias, importe))
        pago_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        base = request.host_url.rstrip('/')
        r = requests.post(
            NOWPAY_API + '/invoice',
            headers={'x-api-key': NOWPAY_API_KEY, 'Content-Type': 'application/json'},
            json={'price_amount': importe, 'price_currency': 'usd',
                  'order_id': str(pago_id),
                  'order_description': f"ClipLinks {planes[plan]['nombre']} - {dias} dias",
                  'ipn_callback_url': base + '/webhook-pago',
                  'success_url': base + '/?pago=ok',
                  'cancel_url': base + '/?pago=cancelado'},
            timeout=30)
        if r.status_code not in (200, 201):
            return jsonify({'error': 'error_pasarela', 'detalle': r.text[:200]}), 502
        inv = r.json()
        url_pago = inv.get('invoice_url')
        if not url_pago:
            return jsonify({'error': 'sin_url'}), 502
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('UPDATE payments SET ref = %s WHERE id = %s', (str(inv.get('id', '')), pago_id))
            conn.commit()
            cur.close()
            conn.close()
        except:
            pass
        return jsonify({'url': url_pago, 'importe': importe, 'dias': dias})
    except Exception as e:
        return fallo(e)


def marcar_pagado(pago_id):
    """Marca un pago como pagado y activa el plan. Solo la primera vez (no duplica dias)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""UPDATE payments SET estado = 'pagado'
                   WHERE id = %s AND estado <> 'pagado'
                   RETURNING email, plan, creditos""", (int(pago_id),))
    fila = cur.fetchone()
    if fila:
        email, plan, dias = fila
        activar_plan(cur, email, plan if plan in PLANES_DEFECTO else 'ilimitado', int(dias or DIAS_DEFECTO))
    conn.commit()
    cur.close()
    conn.close()
    return bool(fila)


def url_web():
    """Direccion de la web desde la que paga el usuario (Vercel o Railway)."""
    origen = (request.headers.get('Origin') or '').rstrip('/')
    if origen.startswith('https://'):
        return origen
    return request.host_url.rstrip('/').replace('http://', 'https://')


def crear_pago_paymegate():
    email = email_de_sesion()
    if not email:
        return jsonify({'error': 'login_requerido'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        plan = data.get('plan', 'pro')
        planes = get_planes()
        if plan not in planes:
            return jsonify({'error': 'plan_invalido'}), 400
        importe = precio_final(planes[plan]['precio'], promo_usuario(email))
        dias = dias_sub()
        u = get_user(email) or {}
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''INSERT INTO payments (email, plan, creditos, importe, metodo, estado)
                       VALUES (%s, %s, %s, %s, 'paymegate', 'pendiente') RETURNING id''',
                    (email, plan, dias, importe))
        pago_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        r = requests.post(
            PAYMEGATE_API + '/v1/orders',
            headers={'X-API-Key': PAYMEGATE_API_KEY, 'Content-Type': 'application/json'},
            json={'amount': '%.2f' % float(importe), 'currency': 'USD',
                  'externalId': str(pago_id),
                  'paymentMethodsKeys': ['*'],
                  'backUrl': url_web() + '/?pago=ok',
                  'customer': {'email': email, 'fullName': u.get('name') or email.split('@')[0]},
                  'metadata': {'pago_id': pago_id, 'plan': plan}},
            timeout=30)
        if r.status_code not in (200, 201):
            return jsonify({'error': 'error_pasarela', 'detalle': r.text[:300]}), 502
        resp = r.json()
        orden = resp.get('data', resp) if isinstance(resp, dict) else {}
        url_pago = orden.get('checkoutUrl')
        if not url_pago:
            return jsonify({'error': 'sin_url', 'detalle': str(resp)[:300]}), 502
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE payments SET ref = %s WHERE id = %s', (str(orden.get('orderUUID', '')), pago_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'url': url_pago, 'importe': importe, 'dias': dias})
    except Exception as e:
        return fallo(e)


def orden_pagada(order_uuid, pago_id):
    """Pregunta a PaymeGate si la orden esta pagada (no nos fiamos de lo que llega al webhook)."""
    if not PAYMEGATE_API_KEY or not order_uuid:
        return False
    try:
        r = requests.get(PAYMEGATE_API + '/v1/orders/' + str(order_uuid),
                         headers={'X-API-Key': PAYMEGATE_API_KEY}, timeout=20)
        if r.status_code != 200:
            return False
        resp = r.json()
        orden = resp.get('data', resp) if isinstance(resp, dict) else {}
        return (str(orden.get('status', '')).upper() == 'PAID'
                and str(orden.get('externalId', '')) == str(pago_id))
    except:
        return False


def comprobar_pendientes(email):
    """Por si el aviso de PaymeGate no llega: revisa los pagos pendientes recientes del usuario."""
    if not PAYMEGATE_API_KEY:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""SELECT id, ref FROM payments
                       WHERE email = %s AND metodo = 'paymegate' AND estado = 'pendiente'
                         AND ref IS NOT NULL AND ref <> ''
                         AND created_at > NOW() - INTERVAL '1 day'
                       ORDER BY id DESC LIMIT 3""", (email,))
        filas = cur.fetchall()
        cur.close()
        conn.close()
        for pago_id, ref in filas:
            if orden_pagada(ref, pago_id):
                marcar_pagado(pago_id)
    except:
        pass


@app.route('/webhook-paymegate', methods=['POST'])
def webhook_paymegate():
    """PaymeGate avisa aqui. Comprobamos la orden con su API antes de activar nada."""
    try:
        datos = request.get_json(force=True, silent=True) or {}
        evento = datos.get('data', datos) if isinstance(datos.get('data'), dict) else datos
        order_uuid = evento.get('orderUUID') or datos.get('orderUUID')
        if not order_uuid:
            return jsonify({'ok': True, 'ignorado': 'sin_orden'})
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM payments WHERE ref = %s AND metodo = 'paymegate'", (str(order_uuid),))
        fila = cur.fetchone()
        cur.close()
        conn.close()
        if not fila:
            return jsonify({'ok': True, 'ignorado': 'pago_no_encontrado'})
        if not orden_pagada(order_uuid, fila[0]):
            return jsonify({'ok': True, 'ignorado': 'no_pagada'})
        marcar_pagado(fila[0])
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/webhook-pago', methods=['POST'])
def webhook_pago():
    """NOWPayments avisa aqui cuando se confirma el pago -> se activa la suscripcion."""
    # Sin clave secreta no se puede comprobar que el aviso es de verdad: se rechaza
    if not NOWPAY_IPN_SECRET:
        return jsonify({'error': 'webhook_no_configurado'}), 503
    try:
        datos = request.get_json(force=True, silent=True) or {}
        firma = request.headers.get('x-nowpayments-sig', '')
        ordenado = json.dumps(datos, sort_keys=True, separators=(',', ':'))
        esperada = hmac.new(NOWPAY_IPN_SECRET.encode(), ordenado.encode(), hashlib.sha512).hexdigest()
        if not hmac.compare_digest(firma, esperada):
            return jsonify({'error': 'firma_invalida'}), 401

        estado = str(datos.get('payment_status') or '').lower()
        order_id = str(datos.get('order_id') or '')
        if estado not in ('finished', 'confirmed') or not order_id.isdigit():
            return jsonify({'ok': True, 'ignorado': estado})
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM payments WHERE id = %s AND metodo = 'cripto'", (int(order_id),))
        existe = cur.fetchone()
        cur.close()
        conn.close()
        if not existe:
            return jsonify({'error': 'pago_no_encontrado'}), 404
        nuevo = marcar_pagado(int(order_id))
        return jsonify({'ok': True, 'ya_procesado': not nuevo})
    except Exception as e:
        return fallo(e)


def activar_plan(cur, email, plan, dias):
    """Activa un plan: si es el mismo y sigue activo se suman los dias;
    si es otro plan empieza desde hoy. El contador de links vuelve a 0."""
    cur.execute('INSERT INTO users (email) VALUES (%s) ON CONFLICT (email) DO NOTHING', (email,))
    cur.execute('''UPDATE users SET
                     sub_hasta = (CASE WHEN sub_plan = %s AND sub_hasta > NOW() THEN sub_hasta ELSE NOW() END)
                                 + (%s || ' days')::interval,
                     sub_plan = %s, links_usados = 0
                   WHERE email = %s''', (plan, str(dias), plan, email))


# ===================== CHAT DE SOPORTE (usuario) =====================

@app.route('/chat', methods=['GET', 'POST'])
def chat_usuario():
    email = email_de_sesion()
    if not email:
        return jsonify({'error': 'login_requerido'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        if request.method == 'POST':
            data = request.get_json(force=True, silent=True) or {}
            texto = str(data.get('texto', '') or '').strip()[:2000]
            if not texto:
                cur.close(); conn.close()
                return jsonify({'error': 'vacio'}), 400
            if limitado('chat:' + email, 20, 60):
                cur.close(); conn.close()
                return demasiadas()
            cur.execute("INSERT INTO chat (email, de, texto) VALUES (%s, 'user', %s)", (email, texto))
            conn.commit()
        # Marcar como leidos los mensajes del soporte
        cur.execute("UPDATE chat SET leido = TRUE WHERE email = %s AND de = 'admin' AND leido = FALSE", (email,))
        conn.commit()
        cur.execute('SELECT de, texto, created_at FROM chat WHERE email = %s ORDER BY id ASC LIMIT 500', (email,))
        filas = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'mensajes': [{'de': f[0], 'texto': f[1], 'fecha': str(f[2])} for f in filas]})
    except Exception as e:
        return fallo(e)


# ===================== CHAT DE SOPORTE (panel) =====================

@app.route('/chat-admin')
def chat_admin_lista():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''
            SELECT c.email, MAX(c.created_at) AS ultima,
                   SUM(CASE WHEN c.de = 'user' AND c.leido = FALSE THEN 1 ELSE 0 END) AS no_leidos,
                   MAX(u.name), MAX(u.picture),
                   (SELECT texto FROM chat c2 WHERE c2.email = c.email ORDER BY id DESC LIMIT 1)
            FROM chat c LEFT JOIN users u ON u.email = c.email
            GROUP BY c.email ORDER BY ultima DESC LIMIT 300''')
        filas = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'conversaciones': [{
            'email': f[0], 'ultima': str(f[1]), 'no_leidos': int(f[2] or 0),
            'name': f[3] or '', 'picture': f[4] or '', 'ultimo_texto': f[5] or ''} for f in filas]})
    except Exception as e:
        return fallo(e)


@app.route('/chat-admin/conversacion', methods=['GET', 'POST'])
def chat_admin_conv():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        if request.method == 'POST':
            data = request.get_json(force=True, silent=True) or {}
            email = (data.get('email', '') or '').strip()
            texto = (data.get('texto', '') or '').strip()[:2000]
        else:
            email = request.args.get('email', '').strip()
            texto = ''
        if not email:
            return jsonify({'error': 'falta_email'}), 400
        conn = get_db()
        cur = conn.cursor()
        if texto:
            cur.execute("INSERT INTO chat (email, de, texto) VALUES (%s, 'admin', %s)", (email, texto))
        cur.execute("UPDATE chat SET leido = TRUE WHERE email = %s AND de = 'user' AND leido = FALSE", (email,))
        conn.commit()
        cur.execute('SELECT de, texto, created_at FROM chat WHERE email = %s ORDER BY id ASC LIMIT 500', (email,))
        filas = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'mensajes': [{'de': f[0], 'texto': f[1], 'fecha': str(f[2])} for f in filas]})
    except Exception as e:
        return fallo(e)


# ===================== PANEL: USUARIOS, BLOQUEOS, AJUSTES =====================

@app.route('/usuarios')
def usuarios_panel():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''SELECT u.email, u.name, u.picture, u.debe_pagar, u.sub_hasta,
                              (u.sub_hasta IS NOT NULL AND u.sub_hasta > NOW()),
                              (SELECT COUNT(*) FROM searches s WHERE s.user_email = u.email),
                              (SELECT MAX(created_at) FROM searches s WHERE s.user_email = u.email),
                              u.created_at, COALESCE(u.sub_plan, 'ilimitado'), COALESCE(u.links_usados, 0)
                       FROM users u
                       ORDER BY 7 DESC, u.created_at DESC LIMIT 5000''')
        filas = cur.fetchall()
        cur.close()
        conn.close()
        planes = get_planes()
        return jsonify({'usuarios': [{
            'email': f[0], 'name': f[1] or '', 'picture': f[2] or '',
            'debe_pagar': bool(f[3]), 'sub_hasta': str(f[4]) if f[4] else '',
            'activo': bool(f[5]), 'busquedas': f[6] or 0,
            'ultima': str(f[7]) if f[7] else '', 'alta': str(f[8]) if f[8] else '',
            'plan': f[9], 'plan_nombre': planes.get(f[9], planes['ilimitado'])['nombre'],
            'usados': f[10], 'limite': planes.get(f[9], planes['ilimitado'])['links']} for f in filas]})
    except Exception as e:
        return fallo(e)


@app.route('/marcar-pago', methods=['POST'])
def marcar_pago():
    """Marca o desmarca una cuenta como 'tiene que pagar'."""
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        email = (data.get('email', '') or '').strip()
        activar = bool(data.get('activar', True))
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE users SET debe_pagar = %s WHERE email = %s', (activar, email))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/dar-dias', methods=['POST'])
def dar_dias():
    """Da dias de suscripcion a mano (dias > 0) o la cancela (dias = 0)."""
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        email = (data.get('email', '') or '').strip()
        dias = int(data.get('dias', 0) or 0)
        plan = data.get('plan', 'ilimitado')
        if plan not in PLANES_DEFECTO:
            plan = 'ilimitado'
        conn = get_db()
        cur = conn.cursor()
        if dias <= 0:
            cur.execute('UPDATE users SET sub_hasta = NULL WHERE email = %s', (email,))
        else:
            activar_plan(cur, email, plan, dias)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/perfiles-bloqueados')
def perfiles_bloqueados():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''SELECT b.username, b.created_at,
                              (SELECT COUNT(*) FROM searches s WHERE LOWER(s.username) = b.username)
                       FROM blocked_profiles b ORDER BY b.created_at DESC''')
        filas = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'perfiles': [{'username': f[0], 'fecha': str(f[1]), 'busquedas': f[2] or 0} for f in filas]})
    except Exception as e:
        return fallo(e)


@app.route('/bloquear-perfil', methods=['POST'])
def bloquear_perfil():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        username = (data.get('username', '') or '').strip().lstrip('@').lower()
        activar = bool(data.get('activar', True))
        if not username:
            return jsonify({'error': 'falta_usuario'}), 400
        conn = get_db()
        cur = conn.cursor()
        if activar:
            cur.execute('INSERT INTO blocked_profiles (username) VALUES (%s) ON CONFLICT DO NOTHING', (username,))
        else:
            cur.execute('DELETE FROM blocked_profiles WHERE username = %s', (username,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/ajustes', methods=['GET', 'POST'])
def ajustes():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        if request.method == 'POST':
            data = request.get_json(force=True, silent=True) or {}
            nuevos = {}
            for k in ORDEN_PLANES:
                v = (data.get('planes') or {}).get(k) or {}
                precio = round(float(v.get('precio', PLANES_DEFECTO[k]['precio'])), 2)
                links = v.get('links')
                links = None if links in (None, '', 0, '0') else int(links)
                if precio <= 0 or (links is not None and links < 0):
                    return jsonify({'error': 'valores_invalidos'}), 400
                nuevos[k] = {'precio': precio, 'links': links}
            dias = int(data.get('dias', DIAS_DEFECTO))
            promo = int(float(data.get('promo', promo_pct()) or 0))
            if dias <= 0 or promo < 0 or promo > 90:
                return jsonify({'error': 'valores_invalidos'}), 400
            conn = get_db()
            cur = conn.cursor()
            for k, v in (('planes', json.dumps(nuevos)), ('dias', str(dias)), ('promo', str(promo))):
                cur.execute('''INSERT INTO ajustes (clave, valor) VALUES (%s, %s)
                               ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor''', (k, v))
            conn.commit()
            cur.close()
            conn.close()
        return jsonify({'planes': lista_planes(), 'dias': dias_sub(), 'promo': promo_pct()})
    except Exception as e:
        return fallo(e)


@app.route('/forzar-ip', methods=['POST'])
def forzar_ip():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        ip_obj = (data.get('ip', '') or '').strip()
        activar = bool(data.get('activar', True))
        if not ip_obj:
            return jsonify({'error': 'falta_ip'}), 400
        conn = get_db()
        cur = conn.cursor()
        if activar:
            cur.execute('INSERT INTO forced_ips (ip) VALUES (%s) ON CONFLICT (ip) DO NOTHING', (ip_obj,))
        else:
            cur.execute('DELETE FROM forced_ips WHERE ip = %s', (ip_obj,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/pagos')
def pagos_panel():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''SELECT id, email, plan, creditos, importe, metodo, estado, created_at
                       FROM payments ORDER BY created_at DESC LIMIT 500''')
        filas = cur.fetchall()
        cur.execute("SELECT COALESCE(SUM(importe),0) FROM payments WHERE estado = 'pagado'")
        total = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({'total_cobrado': float(total or 0), 'pagos': [{
            'id': f[0], 'email': f[1] or '', 'plan': f[2] or '', 'dias': f[3] or 0,
            'importe': float(f[4] or 0), 'metodo': f[5] or '', 'estado': f[6] or '',
            'fecha': str(f[7]) if f[7] else ''} for f in filas]})
    except Exception as e:
        return fallo(e)


# ===================== MENSAJES DEL FORMULARIO ANTIGUO =====================

@app.route('/contact', methods=['POST'])
def contact():
    if limitado('contact:' + client_ip(), 5, 600):
        return demasiadas()
    try:
        data = request.get_json(force=True, silent=True) or {}
        email = str(data.get('email', '') or '').strip()[:200]
        message = str(data.get('message', '') or '').strip()[:2000]
        if not message:
            return jsonify({'error': 'Mensaje vacío'}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO messages (email, message, ip) VALUES (%s, %s, %s)',
                    (email, message, client_ip()))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


@app.route('/messages')
def get_messages():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id, email, message, replied, created_at FROM messages ORDER BY created_at DESC LIMIT 200')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'messages': [{'id': r[0], 'email': r[1] or '', 'message': r[2] or '',
                                      'replied': r[3], 'date': str(r[4])} for r in rows]})
    except Exception as e:
        return fallo(e)


@app.route('/mark-replied', methods=['POST'])
def mark_replied():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE messages SET replied = TRUE WHERE id = %s', (data.get('id'),))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return fallo(e)


# ===================== ESTADISTICAS DEL PANEL =====================

@app.route('/stats')
def stats():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()
        q = lambda sql, p=(): (cur.execute(sql, p), cur.fetchone()[0])[1]
        total = q('SELECT COUNT(*) FROM searches')
        unique_ips = q('SELECT COUNT(DISTINCT ip) FROM searches')
        logged_users = q("SELECT COUNT(DISTINCT user_email) FROM searches WHERE user_email IS NOT NULL AND user_email != ''")
        today = q("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE")
        week = q("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'")
        month = q("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'")
        pending_msgs = q("SELECT COUNT(*) FROM messages WHERE replied = FALSE")
        chat_no_leidos = q("SELECT COUNT(*) FROM chat WHERE de = 'user' AND leido = FALSE")
        suscritos = q("SELECT COUNT(*) FROM users WHERE sub_hasta > NOW()")

        cur.execute('SELECT platform, COUNT(*) FROM searches GROUP BY platform ORDER BY COUNT(*) DESC')
        platforms = cur.fetchall()
        cur.execute("SELECT DATE(created_at), COUNT(*) FROM searches GROUP BY DATE(created_at) ORDER BY DATE(created_at) DESC LIMIT 30")
        daily = cur.fetchall()
        cur.execute('SELECT username, platform, COUNT(*) FROM searches GROUP BY username, platform ORDER BY COUNT(*) DESC LIMIT 30')
        top_profiles = cur.fetchall()
        cur.execute('''SELECT email, MAX(name), MAX(picture), COUNT(*), MAX(created_at)
                       FROM logins WHERE email IS NOT NULL AND email != ''
                       GROUP BY email ORDER BY MAX(created_at) DESC LIMIT 5000''')
        accounts = cur.fetchall()
        cur.execute('''SELECT username, platform, count, user_email, ip, created_at
                       FROM searches ORDER BY created_at DESC LIMIT 5000''')
        history = cur.fetchall()
        cur.execute("SELECT ip FROM forced_ips")
        forced = [f[0] for f in cur.fetchall()]
        cur.execute("SELECT username FROM blocked_profiles")
        bloqueados = [f[0] for f in cur.fetchall()]
        cur.execute("SELECT email FROM users WHERE debe_pagar = TRUE")
        marcados = [f[0] for f in cur.fetchall()]
        cur.close()
        conn.close()

        return jsonify({
            'total': total, 'unique_ips': unique_ips, 'logged_users': logged_users,
            'today': today, 'week': week, 'month': month,
            'pending_msgs': pending_msgs, 'chat_no_leidos': chat_no_leidos, 'suscritos': suscritos,
            'forced_ips': forced, 'blocked_profiles': bloqueados, 'marcados': marcados,
            'platforms': [{'name': p[0], 'count': p[1]} for p in platforms],
            'daily': [{'date': str(d[0]), 'count': d[1]} for d in daily],
            'top_profiles': [{'username': p[0], 'platform': p[1], 'count': p[2]} for p in top_profiles],
            'accounts': [{'email': a[0], 'name': a[1], 'picture': a[2], 'logins': a[3],
                          'last_login': str(a[4])} for a in accounts],
            'history': [{'username': h[0], 'platform': h[1], 'count': h[2], 'user_email': h[3] or '',
                         'ip': h[4] or '', 'date': str(h[5])} for h in history],
        })
    except Exception as e:
        return fallo(e)


# ===================== EXTRACCION DE LINKS =====================

def get_avatar(user, platform):
    try:
        if platform == 'tiktok':
            prof_url = f'https://www.tiktok.com/@{user}'
        elif platform == 'youtube':
            prof_url = f'https://www.youtube.com/@{user}'
        else:
            return None
        opts = {'quiet': True, 'skip_download': True, 'playlist_items': '0', 'ignoreerrors': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(prof_url, download=False) or {}
        for key in ('thumbnail', 'channel_thumbnail', 'uploader_avatar'):
            if info.get(key):
                return info.get(key)
        thumbs = info.get('thumbnails')
        if thumbs and isinstance(thumbs, list):
            return thumbs[-1].get('url')
        return None
    except:
        return None


def get_instagram_reels(user, days):
    """Saca reels de Instagram via Apify, filtrados por fecha en origen y ordenados."""
    if not APIFY_TOKEN:
        return None, 'Instagram no está configurado'
    user = user.lower().lstrip('@')
    api_url = f'https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}'
    newer = '1 day' if days == 1 else f'{days} days'
    payload = {'username': [user], 'resultsLimit': INSTAGRAM_MAX_REELS,
               'onlyPostsNewerThan': newer, 'skipPinnedPosts': True}
    try:
        resp = requests.post(api_url, json=payload, timeout=280)
    except requests.exceptions.Timeout:
        return None, 'Instagram tardó demasiado, prueba de nuevo'
    except Exception as e:
        app.logger.warning('apify: %s', str(e)[:300])
        return None, 'Error de conexión con Instagram'
    if resp.status_code not in (200, 201):
        return None, f'Instagram no disponible ({resp.status_code})'
    try:
        items = resp.json()
    except:
        return None, 'Respuesta inválida de Instagram'
    if not isinstance(items, list) or not items:
        return {'links': [], 'stats': None}, None

    recogidos = []
    for it in items:
        url = it.get('url')
        if url and str(url).startswith('https://'):
            recogidos.append({'url': url, 'timestamp': it.get('timestamp') or '',
                              'likes': it.get('likesCount') or 0, 'coment': it.get('commentsCount') or 0})
    recogidos.sort(key=lambda x: x['timestamp'], reverse=True)
    links = [f['url'] for f in recogidos]

    stats = None
    if recogidos:
        likes = [r['likes'] for r in recogidos if isinstance(r['likes'], int) and r['likes'] > 0]
        coms = [r['coment'] for r in recogidos if isinstance(r['coment'], int) and r['coment'] > 0]
        top = sorted(recogidos, key=lambda r: r['likes'] if isinstance(r['likes'], int) else 0, reverse=True)[:3]
        stats = {'total': len(recogidos),
                 'media_likes': int(sum(likes) / len(likes)) if likes else 0,
                 'media_coment': int(sum(coms) / len(coms)) if coms else 0,
                 'por_dia': round(len(recogidos) / days, 1) if days else len(recogidos),
                 'top': [{'url': r['url'], 'likes': r['likes']} for r in top if r['likes']]}
    return {'links': links, 'stats': stats}, None


@app.route('/links')
def get_links():
    user = request.args.get('user', '').replace('@', '').strip()
    platform = request.args.get('platform', 'tiktok')
    try:
        count = max(1, min(int(request.args.get('count', 25)), MAX_LINKS))
        days = max(1, min(int(request.args.get('days', 1)), 7))
    except (TypeError, ValueError):
        return jsonify({'error': 'parametros_invalidos'}), 400
    ip = client_ip()
    email = email_de_sesion()

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400
    if not USUARIO_VALIDO.match(user):
        return jsonify({'error': 'usuario_invalido'}), 400
    if platform not in ('tiktok', 'youtube', 'instagram'):
        return jsonify({'error': 'Plataforma no válida'}), 400

    # Freno contra abusos: 15 busquedas por minuto y 150 por hora
    quien = 'links:' + (email or ip)
    if limitado(quien + ':m', 15, 60) or limitado(quien + ':h', 150, 3600):
        return demasiadas()

    # Guardar la busqueda (tambien los intentos bloqueados, para verlos en el panel)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO searches (ip, platform, username, count, user_email) VALUES (%s, %s, %s, %s, %s)',
                    (ip, platform, user, days if platform == 'instagram' else count, email))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

    u = get_user(email) if email else None
    activo = bool(u and u['activo'])
    contar = False  # si hay que descontar links del plan

    # 1) IP forzada a iniciar sesion
    if not email and ip_forzada(ip):
        return jsonify({'error': 'login_requerido', 'motivo': 'forzado'}), 401

    # 2) Perfil (@) bloqueado o cuenta marcada: anonimo -> login; sin suscripcion -> pagar
    if perfil_bloqueado(user) or (u and u['debe_pagar']):
        if not email:
            return jsonify({'error': 'login_requerido', 'motivo': 'forzado'}), 401
        if not activo:
            return jsonify({'error': 'pago_requerido', 'motivo': 'cuenta'}), 402
        contar = True

    # 3) Instagram: solo con un plan que incluya Instagram
    if platform == 'instagram':
        if not email:
            return jsonify({'error': 'login_requerido', 'motivo': 'instagram'}), 401
        if not activo or not u['instagram']:
            return jsonify({'error': 'pago_requerido', 'motivo': 'instagram'}), 402
        # Instagram cuesta dinero por cada busqueda: maximo 30 por hora y cuenta
        if limitado('ig:' + email, 30, 3600):
            return demasiadas()
        resultado, err = get_instagram_reels(user, days)
        if err:
            return jsonify({'error': err}), 502
        return jsonify({'links': resultado.get('links', []), 'stats': resultado.get('stats'),
                        'avatar': get_avatar(user, 'instagram')})

    # 4) Limite de links del plan
    restantes = None
    if contar and u['restantes'] is not None:
        restantes = u['restantes']
        if restantes <= 0:
            return jsonify({'error': 'pago_requerido', 'motivo': 'limite'}), 402
        count = min(count, restantes)

    # ===== TikTok y YouTube via yt-dlp =====
    if platform == 'tiktok':
        url = f'https://www.tiktok.com/@{user}'
    else:
        url = f'https://www.youtube.com/@{user}/shorts'

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True, 'playlistend': count}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            links = [e.get('url', '') for e in entries if e and str(e.get('url', '')).startswith('https://')]
            avatar = None
            for key in ('thumbnail', 'channel_thumbnail', 'uploader_avatar'):
                if info.get(key):
                    avatar = info.get(key)
                    break
            if not avatar:
                thumbs = info.get('thumbnails')
                if thumbs and isinstance(thumbs, list):
                    avatar = thumbs[-1].get('url')
            if not avatar:
                avatar = get_avatar(user, platform)
            links = links[:count]
            if restantes is not None:
                try:
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute('UPDATE users SET links_usados = COALESCE(links_usados, 0) + %s WHERE email = %s',
                                (len(links), email))
                    conn.commit()
                    cur.close()
                    conn.close()
                except:
                    pass
                restantes = max(0, restantes - len(links))
            return jsonify({'links': links, 'avatar': avatar, 'restantes': restantes})
    except Exception as e:
        app.logger.warning('links %s @%s: %s', platform, user, str(e)[:300])
        return jsonify({'error': 'no_disponible'}), 502


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)
