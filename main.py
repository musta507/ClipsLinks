from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import psycopg2
import stripe
import requests
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
CORS(app)

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '')

# ===== APIFY (Instagram) =====
APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '')
# Actor: Instagram Reel Scraper (apify/instagram-reel-scraper)
APIFY_ACTOR = 'apify~instagram-reel-scraper'
# Sin tope de reels: el filtro de dias ya limita cuantos saca
INSTAGRAM_MAX_REELS = 99999

# ===== SISTEMA DE CREDITOS (solo Instagram) =====
# 1 credito = 1 video/reel de Instagram
CREDITOS_GRATIS = 100        # al registrarse
DIAS_MAX_GRATIS = 3          # el plan gratis solo llega a 3 dias
PLANES = {
    'gratis':  {'creditos': 100,   'dias_max': 3, 'precio': 0},
    'starter': {'creditos': 1000,  'dias_max': 7, 'precio': 9},
    'pro':     {'creditos': 5000,  'dias_max': 7, 'precio': 24},
    'agency':  {'creditos': 15000, 'dias_max': 7, 'precio': 49},
}
PRECIO_POR_CREDITO = 0.006   # recarga personalizada: 0.006 USD por video
RECARGA_MINIMA = 500         # creditos minimos por recarga

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
        cur.execute('''
            CREATE TABLE IF NOT EXISTS searches (
                id SERIAL PRIMARY KEY,
                ip TEXT,
                platform TEXT,
                username TEXT,
                count INTEGER,
                user_email TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS count INTEGER")
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS user_email TEXT")
        cur.execute('''
            CREATE TABLE IF NOT EXISTS logins (
                id SERIAL PRIMARY KEY,
                email TEXT,
                name TEXT,
                picture TEXT,
                ip TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                email TEXT,
                message TEXT,
                ip TEXT,
                replied BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # ===== Usuarios con creditos (solo para Instagram) =====
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                picture TEXT,
                creditos INTEGER DEFAULT 100,
                plan TEXT DEFAULT 'gratis',
                plan_renueva TIMESTAMP,
                forzar_login BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # ===== Pagos recibidos =====
        cur.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                email TEXT,
                plan TEXT,
                creditos INTEGER,
                importe NUMERIC,
                metodo TEXT,
                estado TEXT DEFAULT 'pendiente',
                ref TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

init_db()

def check_password():
    pw = request.args.get('pw', '')
    return PANEL_PASSWORD != '' and pw == PANEL_PASSWORD

# ===== FUNCIONES DE CREDITOS =====
def get_user(email):
    """Devuelve los datos del usuario; lo crea con creditos gratis si no existe."""
    if not email:
        return None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT email, name, creditos, plan, forzar_login FROM users WHERE email = %s', (email,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                'INSERT INTO users (email, creditos, plan) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING',
                (email, CREDITOS_GRATIS, 'gratis'))
            conn.commit()
            cur.execute('SELECT email, name, creditos, plan, forzar_login FROM users WHERE email = %s', (email,))
            row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {'email': row[0], 'name': row[1] or '', 'creditos': row[2] or 0,
                'plan': row[3] or 'gratis', 'forzar_login': bool(row[4])}
    except:
        return None

def restar_creditos(email, cantidad):
    """Resta creditos al usuario. Nunca baja de 0."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE users SET creditos = GREATEST(creditos - %s, 0) WHERE email = %s',
                    (cantidad, email))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except:
        return False

@app.route('/mi-cuenta')
def mi_cuenta():
    """Devuelve los creditos y plan del usuario logueado (para la web)."""
    email = request.args.get('email', '').strip()
    if not email:
        return jsonify({'logueado': False})
    u = get_user(email)
    if not u:
        return jsonify({'logueado': False})
    plan_info = PLANES.get(u['plan'], PLANES['gratis'])
    return jsonify({
        'logueado': True,
        'email': u['email'],
        'creditos': u['creditos'],
        'plan': u['plan'],
        'dias_max': plan_info['dias_max'],
        'forzar_login': u['forzar_login'],
    })

# ===== PAGOS CON CRIPTO =====
@app.route('/crear-pago', methods=['POST'])
def crear_pago():
    """Crea una factura en NOWPayments y devuelve el enlace de pago."""
    if not NOWPAY_API_KEY:
        return jsonify({'error': 'pagos_no_configurados',
                        'mensaje': 'Los pagos aún no están activos. Contáctanos.'}), 503
    try:
        data = request.get_json(force=True)
        email = (data.get('email', '') or '').strip()
        tipo = data.get('tipo', 'plan')        # 'plan' o 'recarga'
        plan = data.get('plan', '')
        creditos = int(data.get('creditos', 0) or 0)

        if not email:
            return jsonify({'error': 'login_requerido'}), 401

        if tipo == 'plan':
            if plan not in PLANES or plan == 'gratis':
                return jsonify({'error': 'plan_invalido'}), 400
            importe = PLANES[plan]['precio']
            creditos = PLANES[plan]['creditos']
            descripcion = f'ClipLinks {plan} - {creditos} creditos'
        else:
            if creditos < RECARGA_MINIMA:
                return jsonify({'error': 'minimo',
                                'mensaje': f'Mínimo {RECARGA_MINIMA} créditos'}), 400
            importe = round(creditos * PRECIO_POR_CREDITO, 2)
            plan = 'recarga'
            descripcion = f'ClipLinks recarga - {creditos} creditos'

        # Guardar el pago como pendiente y usar su id como referencia
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''INSERT INTO payments (email, plan, creditos, importe, metodo, estado)
                       VALUES (%s, %s, %s, %s, 'cripto', 'pendiente') RETURNING id''',
                    (email, plan, creditos, importe))
        pago_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # Crear la factura en NOWPayments
        r = requests.post(
            NOWPAY_API + '/invoice',
            headers={'x-api-key': NOWPAY_API_KEY, 'Content-Type': 'application/json'},
            json={
                'price_amount': importe,
                'price_currency': 'usd',
                'order_id': str(pago_id),
                'order_description': descripcion,
                'ipn_callback_url': request.host_url.rstrip('/') + '/webhook-pago',
                'success_url': request.host_url.rstrip('/') + '/?pago=ok',
                'cancel_url': request.host_url.rstrip('/') + '/?pago=cancelado',
            },
            timeout=30)

        if r.status_code not in (200, 201):
            return jsonify({'error': 'error_pasarela', 'detalle': r.text[:200]}), 502

        inv = r.json()
        url_pago = inv.get('invoice_url')
        if not url_pago:
            return jsonify({'error': 'sin_url'}), 502

        # Guardar la referencia de la factura
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('UPDATE payments SET ref = %s WHERE id = %s',
                        (str(inv.get('id', '')), pago_id))
            conn.commit()
            cur.close()
            conn.close()
        except:
            pass

        return jsonify({'url': url_pago, 'importe': importe, 'creditos': creditos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/webhook-pago', methods=['POST'])
def webhook_pago():
    """NOWPayments avisa aqui cuando el pago se confirma -> sumamos creditos."""
    try:
        cuerpo = request.get_data()
        datos = request.get_json(force=True, silent=True) or {}

        # Comprobar la firma para asegurar que viene de NOWPayments
        if NOWPAY_IPN_SECRET:
            import hmac, hashlib, json as _json
            firma = request.headers.get('x-nowpayments-sig', '')
            ordenado = _json.dumps(datos, sort_keys=True, separators=(',', ':'))
            esperada = hmac.new(NOWPAY_IPN_SECRET.encode(),
                                ordenado.encode(), hashlib.sha512).hexdigest()
            if not hmac.compare_digest(firma, esperada):
                return jsonify({'error': 'firma_invalida'}), 401

        estado = (datos.get('payment_status') or '').lower()
        order_id = datos.get('order_id')

        # Solo sumamos creditos cuando el pago esta confirmado
        if estado not in ('finished', 'confirmed') or not order_id:
            return jsonify({'ok': True, 'ignorado': estado})

        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT email, plan, creditos, estado FROM payments WHERE id = %s', (int(order_id),))
        fila = cur.fetchone()
        if not fila:
            cur.close(); conn.close()
            return jsonify({'error': 'pago_no_encontrado'}), 404

        email, plan, creditos, estado_actual = fila
        # Evitar sumar dos veces si llegan avisos repetidos
        if estado_actual == 'pagado':
            cur.close(); conn.close()
            return jsonify({'ok': True, 'ya_procesado': True})

        cur.execute('UPDATE payments SET estado = %s WHERE id = %s', ('pagado', int(order_id)))
        if plan in PLANES and plan != 'gratis':
            # Plan mensual: fija los creditos del plan y renueva en 30 dias
            cur.execute('''UPDATE users SET creditos = creditos + %s, plan = %s,
                           plan_renueva = NOW() + INTERVAL '30 days' WHERE email = %s''',
                        (creditos, plan, email))
        else:
            # Recarga suelta: solo suma creditos
            cur.execute('UPDATE users SET creditos = creditos + %s WHERE email = %s',
                        (creditos, email))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/panel')
def panel():
    return send_from_directory('.', 'panel.html')

@app.route('/track-login', methods=['POST'])
def track_login():
    try:
        data = request.get_json(force=True)
        email = data.get('email', '')
        name = data.get('name', '')
        picture = data.get('picture', '')
        ip = request.remote_addr
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO logins (email, name, picture, ip) VALUES (%s, %s, %s, %s)',
                    (email, name, picture, ip))
        # Crear el usuario con sus creditos gratis si es la primera vez
        if email:
            cur.execute('''INSERT INTO users (email, name, picture, creditos, plan)
                           VALUES (%s, %s, %s, %s, 'gratis')
                           ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, picture = EXCLUDED.picture''',
                        (email, name, picture, CREDITOS_GRATIS))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/contact', methods=['POST'])
def contact():
    try:
        data = request.get_json(force=True)
        email = (data.get('email', '') or '').strip()[:200]
        message = (data.get('message', '') or '').strip()[:2000]
        ip = request.remote_addr
        if not message:
            return jsonify({'error': 'Mensaje vacío'}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO messages (email, message, ip) VALUES (%s, %s, %s)',
                    (email, message, ip))
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
        return jsonify({'messages': [{
            'id': r[0], 'email': r[1] or '', 'message': r[2] or '',
            'replied': r[3], 'date': str(r[4])
        } for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/mark-replied', methods=['POST'])
def mark_replied():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        data = request.get_json(force=True)
        mid = data.get('id')
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE messages SET replied = TRUE WHERE id = %s', (mid,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stats')
def stats():
    if not check_password():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute('SELECT COUNT(*) FROM searches')
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT ip) FROM searches')
        unique_ips = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT user_email) FROM searches WHERE user_email IS NOT NULL AND user_email != %s', ('',))
        logged_users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE")
        today = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'")
        week = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'")
        month = cur.fetchone()[0]

        cur.execute('SELECT platform, COUNT(*) FROM searches GROUP BY platform ORDER BY COUNT(*) DESC')
        platforms = cur.fetchall()

        cur.execute("SELECT DATE(created_at), COUNT(*) FROM searches GROUP BY DATE(created_at) ORDER BY DATE(created_at) DESC LIMIT 30")
        daily = cur.fetchall()

        cur.execute('SELECT username, platform, COUNT(*) FROM searches GROUP BY username, platform ORDER BY COUNT(*) DESC LIMIT 30')
        top_profiles = cur.fetchall()

        cur.execute('''
            SELECT email, MAX(name) as name, MAX(picture) as picture,
                   COUNT(*) as logins, MAX(created_at) as last_login
            FROM logins
            WHERE email IS NOT NULL AND email != ''
            GROUP BY email
            ORDER BY last_login DESC
            LIMIT 5000
        ''')
        accounts = cur.fetchall()

        cur.execute('''
            SELECT user_email, COUNT(*) as busquedas, MAX(created_at) as ultima
            FROM searches
            WHERE user_email IS NOT NULL AND user_email != ''
            GROUP BY user_email
            ORDER BY busquedas DESC
            LIMIT 5000
        ''')
        user_activity = cur.fetchall()

        cur.execute('''
            SELECT username, platform, count, user_email, ip, created_at
            FROM searches
            ORDER BY created_at DESC
            LIMIT 5000
        ''')
        history = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM messages WHERE replied = FALSE")
        pending_msgs = cur.fetchone()[0]

        cur.close()
        conn.close()

        return jsonify({
            'total': total,
            'unique_ips': unique_ips,
            'logged_users': logged_users,
            'today': today,
            'week': week,
            'month': month,
            'pending_msgs': pending_msgs,
            'platforms': [{'name': p[0], 'count': p[1]} for p in platforms],
            'daily': [{'date': str(d[0]), 'count': d[1]} for d in daily],
            'top_profiles': [{'username': p[0], 'platform': p[1], 'count': p[2]} for p in top_profiles],
            'accounts': [{
                'email': a[0], 'name': a[1], 'picture': a[2],
                'logins': a[3], 'last_login': str(a[4])
            } for a in accounts],
            'user_activity': [{
                'email': u[0], 'searches': u[1], 'last': str(u[2])
            } for u in user_activity],
            'history': [{
                'username': h[0], 'platform': h[1], 'count': h[2],
                'user_email': h[3] or '', 'ip': h[4] or '', 'date': str(h[5])
            } for h in history]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/create-checkout', methods=['POST'])
def create_checkout():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': { 'name': 'ClipLinks Pro' },
                    'unit_amount': 100,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.host_url + '?payment=success',
            cancel_url=request.host_url + '?payment=cancel',
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_avatar(user, platform):
    try:
        if platform == 'tiktok':
            prof_url = f'https://www.tiktok.com/@{user}'
        elif platform == 'youtube':
            prof_url = f'https://www.youtube.com/@{user}'
        else:
            return None

        opts = {
            'quiet': True,
            'skip_download': True,
            'playlist_items': '0',
            'ignoreerrors': True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(prof_url, download=False) or {}

        for key in ('thumbnail', 'channel_thumbnail', 'uploader_avatar'):
            if info.get(key):
                return info.get(key)

        thumbs = info.get('thumbnails')
        if thumbs and isinstance(thumbs, list) and len(thumbs) > 0:
            return thumbs[-1].get('url')

        return None
    except:
        return None

def get_instagram_reels(user, days, limite=None):
    """Saca reels de Instagram via Apify. Usa onlyPostsNewerThan para que
    Apify filtre por fecha en origen (saca pocos = barato). Ordena por reciente.
    'limite' = maximo de reels a pedir (los creditos del usuario)."""
    if not APIFY_TOKEN:
        return None, 'Instagram no está configurado'

    user = user.lower().lstrip('@')

    # Llamar al Actor de Apify y esperar los resultados
    api_url = f'https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}'
    # onlyPostsNewerThan: le decimos a Apify que SOLO saque los reels de los
    # ultimos X dias. Formato correcto: "1 day" (singular) o "5 days" (plural).
    if days == 1:
        newer = '1 day'
    else:
        newer = f'{days} days'
    tope = limite if limite else INSTAGRAM_MAX_REELS
    payload = {
        'username': [user],
        'resultsLimit': tope,
        'onlyPostsNewerThan': newer,
        'skipPinnedPosts': True,
    }

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

    if not isinstance(items, list) or len(items) == 0:
        return {'links': [], 'stats': None}, None

    # Recoger links con su fecha (sin filtro de perfil: salen todos los del perfil buscado)
    recogidos = []
    for it in items:
        url = it.get('url')
        if not url:
            continue
        ts = it.get('timestamp') or ''
        recogidos.append({
            'url': url,
            'timestamp': ts,
            'likes': it.get('likesCount') or 0,
            'coment': it.get('commentsCount') or 0,
        })

    # Ordenar: mas reciente primero
    recogidos.sort(key=lambda x: x['timestamp'], reverse=True)
    links = [f['url'] for f in recogidos]

    # ===== ESTADISTICAS del conjunto =====
    stats = None
    if recogidos:
        likes_validos = [r['likes'] for r in recogidos if isinstance(r['likes'], int) and r['likes'] > 0]
        coment_validos = [r['coment'] for r in recogidos if isinstance(r['coment'], int) and r['coment'] > 0]
        top = sorted(recogidos, key=lambda r: (r['likes'] if isinstance(r['likes'], int) else 0), reverse=True)[:3]
        stats = {
            'total': len(recogidos),
            'media_likes': int(sum(likes_validos) / len(likes_validos)) if likes_validos else 0,
            'media_coment': int(sum(coment_validos) / len(coment_validos)) if coment_validos else 0,
            'por_dia': round(len(recogidos) / days, 1) if days else len(recogidos),
            'top': [{'url': r['url'], 'likes': r['likes']} for r in top if r['likes']],
        }

    return {'links': links, 'stats': stats}, None

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    count = int(request.args.get('count', 25))
    days = int(request.args.get('days', 1))
    user_email = request.args.get('email', '')
    ip = request.remote_addr

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400

    user = user.replace('@', '').strip()

    # Guardar la busqueda (para Instagram guardamos 'days' en count)
    try:
        conn = get_db()
        cur = conn.cursor()
        saved_count = days if platform == 'instagram' else count
        cur.execute('INSERT INTO searches (ip, platform, username, count, user_email) VALUES (%s, %s, %s, %s, %s)',
                    (ip, platform, user, saved_count, user_email))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

    # ===== INSTAGRAM via Apify (requiere login + creditos) =====
    if platform == 'instagram':
        # 1) Login obligatorio
        if not user_email:
            return jsonify({'error': 'login_requerido',
                            'mensaje': 'Inicia sesión para usar Instagram'}), 401

        u = get_user(user_email)
        if not u:
            return jsonify({'error': 'login_requerido',
                            'mensaje': 'Inicia sesión para usar Instagram'}), 401

        plan_info = PLANES.get(u['plan'], PLANES['gratis'])

        # 2) Limite de dias segun el plan
        if days < 1:
            days = 1
        if days > plan_info['dias_max']:
            return jsonify({'error': 'plan_insuficiente',
                            'mensaje': f"Tu plan permite hasta {plan_info['dias_max']} días. Mejora tu plan para más.",
                            'dias_max': plan_info['dias_max']}), 403

        # 3) Sin creditos
        if u['creditos'] <= 0:
            return jsonify({'error': 'sin_creditos',
                            'mensaje': 'Te has quedado sin créditos. Recarga para seguir usando Instagram.',
                            'creditos': 0}), 402

        # 4) Buscar, limitando a los creditos que tiene (no gasta de mas)
        resultado, err = get_instagram_reels(user, days, limite=u['creditos'])
        if err:
            return jsonify({'error': err}), 500
        links = resultado.get('links', [])
        stats = resultado.get('stats')

        # 5) Restar 1 credito por video
        gastados = len(links)
        if gastados > 0:
            restar_creditos(user_email, gastados)
        restantes = max(u['creditos'] - gastados, 0)

        # 6) Si salieron exactamente sus creditos, es que se corto a mitad
        cortado = (gastados > 0 and gastados >= u['creditos'])

        avatar = get_avatar(user, 'instagram')
        return jsonify({
            'links': links,
            'avatar': avatar,
            'stats': stats,
            'creditos_gastados': gastados,
            'creditos_restantes': restantes,
            'cortado_por_creditos': cortado,
        })

    # ===== TIKTOK y YOUTUBE via yt-dlp =====
    if platform == 'tiktok':
        url = f'https://www.tiktok.com/@{user}'
    elif platform == 'youtube':
        url = f'https://www.youtube.com/@{user}/shorts'
    else:
        return jsonify({'error': 'Plataforma no válida'}), 400

    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'playlistend': count,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            links = [entry.get('url', '') for entry in entries if entry.get('url')]

            avatar = None
            for key in ('thumbnail', 'channel_thumbnail', 'uploader_avatar'):
                if info.get(key):
                    avatar = info.get(key)
                    break
            if not avatar:
                thumbs = info.get('thumbnails')
                if thumbs and isinstance(thumbs, list) and len(thumbs) > 0:
                    avatar = thumbs[-1].get('url')
            if not avatar:
                avatar = get_avatar(user, platform)

            return jsonify({'links': links, 'avatar': avatar})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
