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
        cur.execute('''
            CREATE TABLE IF NOT EXISTS searches (
                id SERIAL PRIMARY KEY,
                ip TEXT,
                platform TEXT,
                username TEXT,
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

@app.route('/stats')
def stats():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM searches')
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(DISTINCT ip) FROM searches')
        unique_users = cur.fetchone()[0]
        cur.execute('SELECT platform, COUNT(*) FROM searches GROUP BY platform ORDER BY COUNT(*) DESC')
        platforms = cur.fetchall()
        cur.execute('SELECT DATE(created_at), COUNT(*) FROM searches GROUP BY DATE(created_at) ORDER BY DATE(created_at) DESC LIMIT 7')
        daily = cur.fetchall()
        cur.execute('SELECT username, COUNT(*) FROM searches GROUP BY username ORDER BY COUNT(*) DESC')
        top_users = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({
            'total': total,
            'unique_users': unique_users,
            'platforms': [{'name': p[0], 'count': p[1]} for p in platforms],
            'daily': [{'date': str(d[0]), 'count': d[1]} for d in daily],
            'top_users': [{'username': u[0], 'count': u[1]} for u in top_users]
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

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    count = int(request.args.get('count', 25))
    ip = request.remote_addr

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO searches (ip, platform, username) VALUES (%s, %s, %s)', (ip, platform, user))
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
            return jsonify({'links': links})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
