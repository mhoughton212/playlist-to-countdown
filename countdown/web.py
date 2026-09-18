"""Local HTTP routes and request validation for Playlist to Countdown."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, request, send_file, render_template, redirect, Response

from .paths import ROOT, UI_DIR
from .studio import Studio, REDIRECT_URI, number, seconds, playlist_id, visual_frames

APP_VERSION = '-'.join(str(path.stat().st_mtime_ns) for path in (
    Path(__file__), Path(__file__).with_name('studio.py'), Path(__file__).with_name('motion.py'),
    Path(__file__).with_name('__main__.py')))


def create_app(root=ROOT):
    app = Flask(__name__, template_folder=str(UI_DIR), static_folder=str(UI_DIR), static_url_path='/ui')
    # The dashboard is edited in place during local development. Do not let
    # Jinja serve an older index.html after the browser has refreshed.
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    app.jinja_env.cache = None
    app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024
    studio = Studio(root)
    app.config['STUDIO'] = studio
    # Keep the local write token stable across restarts so an already-open
    # dashboard tab does not become unusable every time the launcher runs.
    token_file = studio.cache_dir / '.dashboard-token'
    try:
        token = token_file.read_text(encoding='utf-8').strip()
        if len(token) < 32:
            raise ValueError
    except (OSError, ValueError):
        token = secrets.token_urlsafe(32)
        try:
            token_file.write_text(token, encoding='utf-8')
        except OSError:
            pass
    app.config['WRITE_TOKEN'] = token

    @app.before_request
    def protect():
        if request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
            return jsonify(error='Local access only.'), 403
        if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-Studio-Token', ''), token):
            return jsonify(error='Reload the dashboard before continuing.'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.path.startswith('/api') or request.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def unexpected(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        print(f'Request failed: {type(exc).__name__}', flush=True)
        return jsonify(error='This operation failed. Your saved project is still available; check the terminal for the error type.'), 500

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(413)
    def large(_):
        return jsonify(error='Choose an MP3 smaller than 256 MB.'), 413

    @app.get('/')
    def index():
        asset_version = max((UI_DIR / name).stat().st_mtime_ns for name in ('index.html', 'app.js', 'clip-range.js', 'style.css'))
        return render_template('index.html', token=token, asset_version=asset_version)

    @app.get('/card-preview/<key>')
    def card_preview(key):
        with studio.lock:
            track = studio.track(key)
            sequence = [item for item in studio.project.get('tracks', []) if item.get('selected') and studio.resolve(item)]
            track_index = next((index for index, item in enumerate(sequence) if item.get('key') == key), 0)
            artwork = studio.artwork_path(track)
            performance = studio.performance_path(track)
            data = {
                'rank': track['rank'],
                'title': track.get('card_title', track.get('title', '')),
                'artist': track.get('card_artist', track.get('primary_artist') or track.get('artist', '').split(',')[0].strip()),
                'year': track.get('year', ''),
                'description': track.get('description', ''),
                'artwork': f'/api/artwork/{key}?revision={track.get("artwork_override_file") or track.get("artwork_file") or ""}' if artwork else '',
                'performanceImage': f'/api/performance-image/{key}?revision={track.get("performance_image_file") or ""}' if performance else '',
                'performanceMime': track.get('performance_image_mime') or '',
                'layout': 'alternate' if track_index % 2 else 'default',
            }
            template = studio.card_template.read_text(encoding='utf-8')
        prefix, _ = template.rsplit('window.renderCard(', 1)
        page = prefix + f'''window.cardReady=window.renderCard({json.dumps(data, ensure_ascii=False).replace("<", chr(92) + "u003c")});
</script><script>
const fitPreview=()=>{{const scale=Math.min(innerWidth/1920,innerHeight/1080);const frame=document.getElementById('frame');frame.style.position='fixed';frame.style.left=`${{(innerWidth-1920*scale)/2}}px`;frame.style.top=`${{(innerHeight-1080*scale)/2}}px`;frame.style.transformOrigin='top left';frame.style.transform=`scale(${{scale}})`;}};
addEventListener('resize',fitPreview);fitPreview();
</script></body></html>'''
        return Response(page, mimetype='text/html')

    @app.get('/api/state')
    def state():
        return jsonify(studio.view())

    @app.get('/api/health')
    def health():
        return jsonify(app='countdown-studio', version=APP_VERSION)

    @app.post('/api/playlist')
    def load_playlist():
        sid = playlist_id((request.get_json() or {}).get('url', ''))
        with studio.lock:
            studio.idle()
            oauth = studio.oauth()
            try:
                cached = oauth.get_cached_token()
            except Exception:
                cached = None
            if not cached:
                studio.oauth_state = secrets.token_urlsafe(24)
                studio.oauth_connected = False
                return jsonify(auth_url=oauth.get_authorize_url(state=studio.oauth_state),
                               redirect_uri=REDIRECT_URI,
                               setup_required=os.getenv('SPOTIPY_REDIRECT_URI') != REDIRECT_URI), 401
            studio.start('playlist', lambda: studio.sync(sid, oauth))
        return jsonify(studio.view())

    def finish_auth(query):
        with studio.lock:
            studio.idle()
            if not studio.oauth_state or query.get('state', [''])[0] != studio.oauth_state:
                raise ValueError('That login link has expired. Load the playlist again to start a new login.')
            code = query.get('code', [''])[0]
            if not code:
                raise ValueError('Paste the complete address after Spotify redirects you, including code and state.')
            try:
                studio.oauth().get_access_token(code, check_cache=False)
            except Exception as exc:
                # Spotipy's OAuth error contains only Spotify's public error
                # code/description; surface that useful diagnosis without
                # exposing credentials or the authorization code.
                detail = str(exc).strip()
                if len(detail) > 240:
                    detail = detail[:240]
                print(f'Spotify OAuth exchange failed: {detail}', flush=True)
                raise ValueError(f'Spotify login failed: {detail or "Spotify rejected the authorization code."} Load the playlist again and retry signing in.')
            studio.oauth_state = None
            studio.oauth_connected = True

    @app.post('/api/auth')
    def auth():
        payload = request.get_json() or {}
        finish_auth(parse_qs(urlparse(payload.get('url', '')).query))
        return jsonify(ok=True)

    @app.get('/api/auth/status')
    def auth_status():
        return jsonify(connected=studio.oauth_connected)

    @app.get('/callback')
    def callback():
        finish_auth(request.args.to_dict(flat=False))
        return redirect('/?connected=1')

    @app.post('/api/settings')
    def settings():
        data = request.get_json() or {}
        duration = number(data.get('duration'), .5, 120, 'Default duration')
        fade = number(data.get('crossfade'), 0, 10, 'Crossfade')
        with studio.lock:
            current_context = studio.project.get('settings', {}).get('preview_context', 4)
            current_video_count = studio.project.get('settings', {}).get('video_preview_count', 0)
            current_zoom = studio.project.get('settings', {}).get('video_zoom', .05)
        animation = data.get('video_animation', studio.project['settings'].get('video_animation', False))
        zoom = number(data.get('video_zoom', current_zoom * 100), 0, 25, 'Zoom amount') / 100
        if not isinstance(animation, bool):
            raise ValueError('Animation must be on or off.')
        context = number(data.get('preview_context', current_context), .5, 10, 'Neighbor preview length')
        video_count = number(data.get('video_preview_count', current_video_count), 0, 500, 'Video song count')
        with studio.lock:
            studio.idle()
            studio.project['settings'] = {'duration': duration, 'crossfade': fade, 'preview_context': context,
                                          'video_preview_count': int(video_count), 'video_animation': animation, 'video_zoom': zoom}
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/track/<key>')
    def update_track(key):
        data = request.get_json() or {}
        start = seconds(data.get('start'))
        duration = number(data['duration'], .5, 120, 'Duration') if data.get('duration') not in (None, '') else None
        url = str(data.get('source_url', '')).strip()
        if url and (urlparse(url).scheme != 'https' or urlparse(url).hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be')):
            raise ValueError('Use a full https YouTube video link.')
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            candidate = dict(track)
            candidate.update(start=start, duration=duration, selected=bool(data.get('selected')), source_url=url)
            if 'card_title' in data:
                candidate['card_title'] = str(data.get('card_title') or '')[:500]
            if 'card_artist' in data:
                candidate['card_artist'] = str(data.get('card_artist') or '')[:500]
            if 'year' in data:
                year = str(data.get('year') or '').strip()
                if year and not re.fullmatch(r'\d{4}', year):
                    raise ValueError('Year must be four digits or blank.')
                candidate['year'] = year
                candidate['year_customized'] = True
            if 'description' in data:
                candidate['description'] = str(data.get('description') or '')[:2000]
            if 'visual_frames' in data:
                clip_duration = duration if duration is not None else studio.project['settings']['duration']
                candidate['visual_frames'] = visual_frames(data.get('visual_frames'), clip_duration)
            track.clear()
            track.update(candidate)
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/upload/<key>')
    def upload(key):
        from pydub import AudioSegment
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            file = request.files.get('audio')
            if not file or not file.filename.lower().endswith('.mp3'):
                raise ValueError('Choose an MP3 file.')
            studio.audio_dir.mkdir(exist_ok=True)
            target = studio.audio_dir / f'custom-{key}.mp3'
            temp = studio.audio_dir / f'.upload-{key}.mp3'
            try:
                file.save(temp)
                AudioSegment.from_file(temp, format='mp3')
                temp.replace(target)
            except Exception:
                raise ValueError('This file could not be read as MP3 audio.')
            finally:
                temp.unlink(missing_ok=True)
            track['file'] = target.name
            track['artwork_file'] = ''
            track['artwork_mime'] = ''
            studio._cache_embedded_artwork(track)
            track['selected'] = False
            track['audio_revision'] = time.time_ns()
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/replace/<key>')
    def replace(key):
        with studio.lock:
            track = studio.track(key)
            source_url = str(track.get('source_url') or '').strip()
            parsed = urlparse(source_url)
            if not source_url or parsed.scheme != 'https' or parsed.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'):
                raise ValueError('Paste a full https YouTube video link before replacing the audio.')
        studio.start('replace', lambda: studio.replace_audio(key))
        return jsonify(studio.view())

    @app.get('/api/audio/<key>')
    def audio(key):
        with studio.lock:
            path = studio.resolve(studio.track(key))
        if not path:
            return jsonify(error='Audio is missing.'), 404
        return send_file(path, mimetype='audio/mpeg', conditional=True)

    @app.get('/api/artwork/<key>')
    def artwork(key):
        with studio.lock:
            track = studio.track(key)
            path = studio.artwork_path(track)
            mime = track.get('artwork_override_mime') or track.get('artwork_mime') or 'image/jpeg'
        if not path:
            return jsonify(error='Album artwork is unavailable.'), 404
        return send_file(path, mimetype=mime, conditional=True)

    @app.post('/api/artwork/<key>/url')
    def artwork_url(key):
        data = request.get_json() or {}
        url = str(data.get('url') or '').strip()
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.cache_artwork_override(track, url)
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/artwork/<key>/upload')
    def artwork_upload(key):
        file = request.files.get('image')
        if not file:
            raise ValueError('Choose a JPG, PNG, or WebP album cover.')
        data = file.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('Choose an album cover smaller than 12 MB.')
        signatures = {'image/jpeg': (b'\xff\xd8\xff', '.jpg'), 'image/png': (b'\x89PNG\r\n\x1a\n', '.png'), 'image/webp': (b'RIFF', '.webp')}
        match = next(((mime, suffix) for mime, (signature, suffix) in signatures.items() if data.startswith(signature)), None)
        if not match:
            raise ValueError('Use a JPG, PNG, or WebP album cover.')
        mime, suffix = match
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.artwork_dir.mkdir(parents=True, exist_ok=True)
            target = studio.artwork_dir / f'artwork-override-{track["key"]}{suffix}'
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            track['artwork_override_url'] = ''
            track['artwork_override_file'] = target.name
            track['artwork_override_mime'] = mime
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/artwork/<key>/restore')
    def artwork_restore(key):
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.restore_artwork(track)
            studio.save()
        return jsonify(studio.view())

    @app.get('/api/performance-image/<key>')
    def performance_image(key):
        with studio.lock:
            track = studio.track(key)
            path = studio.performance_path(track)
            mime = track.get('performance_image_mime') or 'image/jpeg'
        if not path:
            return jsonify(error='Performance image is unavailable.'), 404
        return send_file(path, mimetype=mime, conditional=True)

    @app.post('/api/performance-image/<key>/url')
    def performance_image_url(key):
        data = request.get_json() or {}
        url = str(data.get('url') or '').strip()
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.cache_performance_image(track, url)
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/performance-image/<key>/upload')
    def performance_image_upload(key):
        file = request.files.get('image')
        if not file:
            raise ValueError('Choose a JPG, PNG, or WebP image.')
        data = file.read(12 * 1024 * 1024 + 1)
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('Choose a performance image smaller than 12 MB.')
        signatures = {'image/jpeg': (b'\xff\xd8\xff', '.jpg'), 'image/png': (b'\x89PNG\r\n\x1a\n', '.png'), 'image/webp': (b'RIFF', '.webp')}
        match = next(((mime, suffix) for mime, (signature, suffix) in signatures.items() if data.startswith(signature)), None)
        if not match:
            raise ValueError('Use a JPG, PNG, or WebP image.')
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.performance_dir.mkdir(parents=True, exist_ok=True)
            target = studio.performance_dir / f'performance-{track["key"]}{match[1]}'
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            track['performance_image_url'] = ''
            track['performance_image_file'] = target.name
            track['performance_image_mime'] = match[0]
            studio.save()
        return jsonify(studio.view())

    @app.post('/api/performance-image/<key>/remove')
    def performance_image_remove(key):
        with studio.lock:
            studio.idle()
            track = studio.track(key)
            studio.remove_performance_image(track)
            studio.save()
        return jsonify(studio.view())

    @app.get('/api/transition/<key>')
    def transition(key):
        path = studio.transition_preview(key)
        return send_file(path, mimetype='audio/mpeg', conditional=True)

    @app.get('/api/wave/<key>')
    def wave(key):
        import numpy as np
        with studio.lock:
            path = studio.resolve(studio.track(key))
        if not path:
            return jsonify(error='Audio is missing.'), 404
        cache_key = (str(path), path.stat().st_mtime_ns)
        if cache_key not in studio.wave_cache:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '3600', '-ac', '1', '-ar', '2000', '-f', 'f32le', '-'], capture_output=True, timeout=45)
            if result.returncode:
                raise ValueError('Waveform unavailable. You can still use the player.')
            values = np.abs(np.frombuffer(result.stdout, dtype='<f4'))
            peaks = [float(chunk.max()) if len(chunk) else 0 for chunk in np.array_split(values, 500)]
            maximum = max(peaks) or 1
            studio.wave_cache[cache_key] = {'peaks': [round(v / maximum, 4) for v in peaks]}
            if len(studio.wave_cache) > 120:
                studio.wave_cache.pop(next(iter(studio.wave_cache)))
        return jsonify(studio.wave_cache[cache_key])

    @app.post('/api/download')
    def download():
        if not studio.project['tracks']:
            raise ValueError('Load a playlist first.')
        studio.start('download', studio.download)
        return jsonify(studio.view())

    @app.post('/api/build')
    def build():
        payload = request.get_json() or {}
        mode = payload.get('mode')
        if mode not in ('preview', 'final'):
            raise ValueError('Choose preview or final export.')
        studio.start('build', lambda: studio.build(mode))
        return jsonify(studio.view())

    @app.post('/api/build-video')
    def build_video():
        payload = request.get_json() or {}
        mode = payload.get('mode')
        if mode not in ('preview', 'final'):
            raise ValueError('Choose preview or final video export.')
        duration = number(payload.get('duration', studio.project['settings'].get('duration', 10)), .5, 120, 'Default duration')
        fade = number(payload.get('crossfade', studio.project['settings'].get('crossfade', 2)), 0, 10, 'Crossfade')
        animation = payload.get('video_animation', studio.project['settings'].get('video_animation', False))
        zoom = number(payload.get('video_zoom', studio.project['settings'].get('video_zoom', .05) * 100), 0, 25, 'Zoom amount') / 100
        if not isinstance(animation, bool):
            raise ValueError('Animation must be on or off.')
        count = int(number(payload['count'], 0, 500, 'Video song count')) if 'count' in payload else None
        with studio.lock:
            studio.idle()
            studio.project.setdefault('settings', {})['duration'] = duration
            studio.project['settings']['crossfade'] = fade
            studio.project.setdefault('settings', {})['video_animation'] = animation
            studio.project['settings']['video_zoom'] = zoom
            if count is not None:
                studio.project['settings']['video_preview_count'] = count
            studio.save()
        studio.start('video build', lambda: studio.build_video(mode))
        return jsonify(studio.view())

    @app.post('/api/build-thumbnail')
    def build_thumbnail():
        if not studio.project['tracks']:
            raise ValueError('Load a playlist first.')
        studio.build_thumbnail()
        return jsonify(studio.view())

    @app.post('/api/cancel')
    def cancel():
        if not studio.cancel():
            raise ValueError('There is no operation to cancel.')
        return jsonify(studio.view())

    @app.get('/api/export/<mode>')
    def export(mode):
        if mode not in ('preview', 'final'):
            return jsonify(error='Export not found.'), 404
        with studio.lock:
            exports = studio.project.get('exports', {})
            filename = (exports.get(mode) or {}).get('file') or exports.get(f'video_{mode}', {}).get('audio_file') or f'countdown-{mode}.mp3'
        if Path(filename).name != filename:
            return jsonify(error='Export not found.'), 404
        path = studio.output_dir / filename
        if not path.exists():
            return jsonify(error='Build this export first.'), 404
        return send_file(path, mimetype='audio/mpeg', conditional=True, as_attachment=request.args.get('download') == '1')

    @app.get('/api/export-video/<mode>')
    def export_video(mode):
        if mode not in ('preview', 'final'):
            return jsonify(error='Video export not found.'), 404
        with studio.lock:
            filename = studio.project.get('exports', {}).get(f'video_{mode}', {}).get('file') or f'countdown-{mode}.mp4'
        if Path(filename).name != filename:
            return jsonify(error='Video export not found.'), 404
        path = studio.output_dir / filename
        if not path.exists():
            return jsonify(error='Build this video export first.'), 404
        return send_file(path, mimetype='video/mp4', conditional=True, as_attachment=request.args.get('download') == '1')

    @app.get('/api/export-thumbnail')
    def export_thumbnail():
        with studio.lock:
            filename = (studio.project.get('exports', {}).get('thumbnail') or {}).get('file') or 'countdown-thumbnail.png'
        if Path(filename).name != filename:
            return jsonify(error='Thumbnail not found.'), 404
        path = studio.output_dir / filename
        if not path.exists():
            return jsonify(error='Create the thumbnail first.'), 404
        return send_file(path, mimetype='image/png', conditional=True,
                         as_attachment=request.args.get('download') == '1',
                         download_name=filename)

    return app


