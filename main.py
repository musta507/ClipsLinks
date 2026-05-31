from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    platform = request.args.get('platform', 'tiktok')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400

    if platform == 'tiktok':
        url = f'https://www.tiktok.com/@{user}'
    elif platform == 'instagram':
        url = f'https://www.instagram.com/{user}/reels/'
    elif platform == 'youtube':
        url = f'https://www.youtube.com/@{user}/shorts'
    else:
        return jsonify({'error': 'Plataforma no válida'}), 400

    # Si hay fechas, sacamos todos los videos sin límite
    # Si no hay fechas, limitamos a 100
    has_dates = date_from or date_to
    
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
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
                link = entry.get('url', '')
                
                if not link:
                    continue
                
                if has_dates and upload_date:
                    # upload_date formato: YYYYMMDD
                    # date_from/date_to formato: YYYY-MM-DD
                    date_str = upload_date  # YYYYMMDD
                    from_str = date_from.replace('-', '') if date_from else ''
                    to_str = date_to.replace('-', '') if date_to else ''
                    
                    if from_str and date_str < from_str:
                        continue
                    if to_str and date_str > to_str:
                        continue
                
                links.append(link)
            
            return jsonify({'links': links})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
