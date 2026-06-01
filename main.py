from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import psycopg2

app = Flask(__name__)
CORS(app)

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

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
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

    has_dates = date_from or date_to

    ydl_opts = {
        'quiet': True,
        'extract_flat': False,
    }

    if not has_dates:
        ydl_opts['playlistend'] = 100

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            links = []
            for entry in entries:
                upload_date = entry.get('upload_date', '')
                link = entry.get('webpage_url') or entry.get('url', '')
                if not link:
                    continue
                if has_dates and upload_date:
                    from_str = date_from.replace('-', '') if date_from else ''
                    to_str = date_to.replace('-', '') if date_to else ''
                    if from_str and upload_date < from_str:
                        continue
                    if to_str and upload_date > to_str:
                        continue
                links.append(link)
            return jsonify({'links': links})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
