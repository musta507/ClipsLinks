from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yt_dlp
import os

app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/links')
def get_links():
    user = request.args.get('user', '')
    count = int(request.args.get('count', 25))
    
    if not user:
        return jsonify({'error': 'Usuario requerido'}), 400
    
    url = f'https://www.tiktok.com/@{user}'
    
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'playlistend': count,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            links = [entry['url'] for entry in info['entries'][:count]]
            return jsonify({'links': links})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
