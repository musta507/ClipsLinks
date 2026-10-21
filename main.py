from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import secrets
import psycopg2
import requests
import json
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

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
    'pro':       {'nombre': 'Pro',       'precio': 27, 'links': 3000, 'instagram': False},
    'ilimitado': {'nombre': 'Ilimitado', 'precio': 56, 'links': None, 'instagram': True},
}
ORDEN_PLANES = ['basico', 'pro', 'ilimitado']
DIAS_DEFECTO = 30       # dias de acceso por cada pago

# ===== PAGOS CON CRIPTO (NOWPayments) =====
NOWPAY_API_KEY = os.environ.get('NOWPAY_API_KEY', '')
NOWPAY_IPN_SECRET = os.environ.get('NOWPAY_IPN_SECRET', '')
NOWPAY_API = 'https://api.nowpayments.io/v1'


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
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('init_db error:', e)


init_db()


# ===================== UTILIDADES =====================

def check_password():
    pw = request.args.get('pw', '')
    return PANEL_PASSWORD != '' and pw == PANEL_PASSWORD


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


def leer_token():
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
        cur.execute('SELECT email FROM sessions WHERE token = %s', (token,))
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
    try:
        data = request.get_json(force=True)
        credential = data.get('credential', '')
        if not credential:
            return jsonify({'error': 'sin_credencial'}), 400
        r = requests.get('https://oauth2.googleapis.com/tokeninfo',
                         params={'id_token': credential}, timeout=15)
        if r.status_code != 200:
            return jsonify({'error': 'login_invalido'}), 401
        info = r.json()
        if info.get('aud') != GOOGLE_CLIENT_ID or str(info.get('email_verified')).lower() != 'true':
            return jsonify({'error': 'login_invalido'}), 401
        email = (info.get('email') or '').strip().lower()
        name = info.get('name', '') or ''
        picture = info.get('picture', '') or ''
        if not email:
            return jsonify({'error': 'login_invalido'}), 401

        token = secrets.token_urlsafe(32)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO logins (email, name, picture, ip) VALUES (%s, %s, %s, %s)',
                    (email, name, picture, request.remote_addr))
        cur.execute('''INSERT INTO users (email, name, picture) VALUES (%s, %s, %s)
                       ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, picture = EXCLUDED.picture''',
                    (email, name, picture))
        cur.execute('INSERT INTO sessions (token, email) VALUES (%s, %s)', (token, email))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True, 'token': token, 'email': email, 'name': name, 'picture': picture})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
                    'chat_no_leidos': no_leidos})


def lista_planes():
    planes = get_planes()
    return [dict(planes[k], id=k) for k in ORDEN_PLANES]


@app.route('/config')
def config():
    return jsonify({'planes': lista_planes(), 'dias': dias_sub()})


# ===================== PAGOS CON CRIPTO =====================

@app.route('/crear-pago', methods=['POST'])
def crear_pago():
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
        importe = planes[plan]['precio']
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
        return jsonify({'error': str(e)}), 500


@app.route('/webhook-pago', methods=['POST'])
def webhook_pago():
    """NOWPayments avisa aqui cuando se confirma el pago -> se activa la suscripcion."""
    try:
        datos = request.get_json(force=True, silent=True) or {}
        if NOWPAY_IPN_SECRET:
            import hmac, hashlib, json as _json
            firma = request.headers.get('x-nowpayments-sig', '')
            ordenado = _json.dumps(datos, sort_keys=True, separators=(',', ':'))
            esperada = hmac.new(NOWPAY_IPN_SECRET.encode(), ordenado.encode(), hashlib.sha512).hexdigest()
            if not hmac.compare_digest(firma, esperada):
                return jsonify({'error': 'firma_invalida'}), 401

        estado = (datos.get('payment_status') or '').lower()
        order_id = datos.get('order_id')
        if estado not in ('finished', 'confirmed') or not order_id:
            return jsonify({'ok': True, 'ignorado': estado})

        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT email, plan, creditos, estado FROM payments WHERE id = %s', (int(order_id),))
        fila = cur.fetchone()
        if not fila:
            cur.close(); conn.close()
            return jsonify({'error': 'pago_no_encontrado'}), 404
        email, plan, dias, estado_actual = fila
        if estado_actual == 'pagado':
            cur.close(); conn.close()
            return jsonify({'ok': True, 'ya_procesado': True})

        cur.execute("UPDATE payments SET estado = 'pagado' WHERE id = %s", (int(order_id),))
        activar_plan(cur, email, plan if plan in PLANES_DEFECTO else 'ilimitado', int(dias or DIAS_DEFECTO))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
            texto = (data.get('texto', '') or '').strip()[:2000]
            if not texto:
                cur.close(); conn.close()
                return jsonify({'error': 'vacio'}), 400
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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


@app.route('/marcar-pago', methods=['POST'])
def marcar_pago():
    """Marca o desmarca una cuenta como 'tiene que pagar'."""
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
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
        return jsonify({'error': str(e)}), 500


@app.route('/dar-dias', methods=['POST'])
def dar_dias():
    """Da dias de suscripcion a mano (dias > 0) o la cancela (dias = 0)."""
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


@app.route('/bloquear-perfil', methods=['POST'])
def bloquear_perfil():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
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
        return jsonify({'error': str(e)}), 500


@app.route('/ajustes', methods=['GET', 'POST'])
def ajustes():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        if request.method == 'POST':
            data = request.get_json(force=True)
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
            if dias <= 0:
                return jsonify({'error': 'valores_invalidos'}), 400
            conn = get_db()
            cur = conn.cursor()
            for k, v in (('planes', json.dumps(nuevos)), ('dias', str(dias))):
                cur.execute('''INSERT INTO ajustes (clave, valor) VALUES (%s, %s)
                               ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor''', (k, v))
            conn.commit()
            cur.close()
            conn.close()
        return jsonify({'planes': lista_planes(), 'dias': dias_sub()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/forzar-ip', methods=['POST'])
def forzar_ip():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
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
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


# ===================== MENSAJES DEL FORMULARIO ANTIGUO =====================

@app.route('/contact', methods=['POST'])
def contact():
    try:
        data = request.get_json(force=True)
        email = (data.get('email', '') or '').strip()[:200]
        message = (data.get('message', '') or '').strip()[:2000]
        if not message:
            return jsonify({'error': 'Mensaje vacío'}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO messages (email, message, ip) VALUES (%s, %s, %s)',
                    (email, message, request.remote_addr))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


@app.route('/mark-replied', methods=['POST'])
def mark_replied():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE messages SET replied = TRUE WHERE id = %s', (data.get('id'),))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
        return jsonify({'error': str(e)}), 500


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
        return None, f'Error de conexión: {e}'
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
        if url:
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
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    count = int(request.args.get('count', 25))
    days = int(request.args.get('days', 1))
    ip = request.remote_addr
    email = email_de_sesion()

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400
    user = user.replace('@', '').strip()

    # Guardar la busqueda (tambien los intentos bloqueados, para verlos en el panel)
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO searches (ip, platform, username, count, user_email) VALUES (%s, %s, %s, %s, %s)',
                    (ip, platform, user, days if platform == 'instagram' else count, email))
        conn.commit()
        cur.close()
        conn.close()
    except:
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
        days = max(1, min(days, 7))
        resultado, err = get_instagram_reels(user, days)
        if err:
            return jsonify({'error': err}), 500
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
    elif platform == 'youtube':
        url = f'https://www.youtube.com/@{user}/shorts'
    else:
        return jsonify({'error': 'Plataforma no válida'}), 400

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True, 'playlistend': count}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            links = [e.get('url', '') for e in entries if e.get('url')]
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
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
