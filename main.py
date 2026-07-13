from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import psycopg2
import stripe

app = Flask(__name__)
CORS(app)

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

def get_db():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        # Tabla de busquedas (ampliada)
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
        # Por si la tabla ya existia sin las columnas nuevas, las añadimos
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS count INTEGER")
        cur.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS user_email TEXT")
        # Tabla de logins
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
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

init_db()

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
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stats')
def stats():
    try:
        conn = get_db()
        cur = conn.cursor()

        # Totales
        cur.execute('SELECT COUNT(*) FROM searches')
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT ip) FROM searches')
        unique_ips = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT user_email) FROM searches WHERE user_email IS NOT NULL AND user_email != %s', ('',))
        logged_users = cur.fetchone()[0]

        # Busquedas hoy / semana / mes
        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE")
        today = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'")
        week = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM searches WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'")
        month = cur.fetchone()[0]

        # Por plataforma
        cur.execute('SELECT platform, COUNT(*) FROM searches GROUP BY platform ORDER BY COUNT(*) DESC')
        platforms = cur.fetchall()

        # Por dia (ultimos 30)
        cur.execute("SELECT DATE(created_at), COUNT(*) FROM searches GROUP BY DATE(created_at) ORDER BY DATE(created_at) DESC LIMIT 30")
        daily = cur.fetchall()

        # Perfiles mas buscados
        cur.execute('SELECT username, platform, COUNT(*) FROM searches GROUP BY username, platform ORDER BY COUNT(*) DESC LIMIT 30')
        top_profiles = cur.fetchall()

        # Cuentas logueadas (resumen: cuantas veces se ha logueado cada email + ultima vez)
        cur.execute('''
            SELECT email, MAX(name) as name, MAX(picture) as picture,
                   COUNT(*) as logins, MAX(created_at) as last_login
            FROM logins
            WHERE email IS NOT NULL AND email != ''
            GROUP BY email
            ORDER BY last_login DESC
            LIMIT 100
        ''')
        accounts = cur.fetchall()

        # Actividad de cada usuario logueado: cuantas busquedas y ultima
        cur.execute('''
            SELECT user_email, COUNT(*) as busquedas, MAX(created_at) as ultima
            FROM searches
            WHERE user_email IS NOT NULL AND user_email != ''
            GROUP BY user_email
            ORDER BY busquedas DESC
            LIMIT 100
        ''')
        user_activity = cur.fetchall()

        # Historial reciente (ultimas 100 busquedas con todo el detalle)
        cur.execute('''
            SELECT username, platform, count, user_email, ip, created_at
            FROM searches
            ORDER BY created_at DESC
            LIMIT 100
        ''')
        history = cur.fetchall()

        cur.close()
        conn.close()

        return jsonify({
            'total': total,
            'unique_ips': unique_ips,
            'logged_users': logged_users,
            'today': today,
            'week': week,
            'month': month,
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
    """Saca la foto de perfil del usuario. Si falla, devuelve None."""
    try:
        if platform == 'tiktok':
            prof_url = f'https://www.tiktok.com/@{user}'
        elif platform == 'instagram':
            prof_url = f'https://www.instagram.com/{user}/'
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

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    count = int(request.args.get('count', 25))
    user_email = request.args.get('email', '')
    ip = request.remote_addr

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO searches (ip, platform, username, count, user_email) VALUES (%s, %s, %s, %s, %s)',
                    (ip, platform, user, count, user_email))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

    if platform == 'tiktok':
        url = f'https://www.tiktok.com/@{user}'
    elif platform == 'instagram':
        url = f'https://www.instagram.com/{user}/reels/'
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
