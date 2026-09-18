"""Saved projects, Spotify imports, downloads, and compilation audio."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
import logging
import mimetypes
import math
import os
from pathlib import Path
import re
import secrets
import socket
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .paths import local_audio_dir
from .motion import MOTION_FPS, ZOOM_AMOUNT, render_layers, motion_filters

PORT = 8765
LOGGER = logging.getLogger(__name__)
FINAL_AUDIO_FADE_SECONDS = 2
VIDEO_ENCODER = 'h264_qsv'
VIDEO_ENCODER_LABEL = 'Intel Quick Sync'
# Project title overrides remain persisted in countdown_project.json.
REDIRECT_URI = f'http://127.0.0.1:{PORT}/callback'
CLIP_CACHE_VERSION = 2
VIDEO_FPS = 30
RENDER_CACHE_VERSIONS = {'card': 6, 'text': 2}
ASSEMBLED_AUDIO_CACHE_VERSION = 1


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_IMAGE_OPENER = urllib.request.build_opener(_NoRedirectHandler)
_ORIGINAL_URLOPEN = urllib.request.urlopen


def _open_image(request, timeout=20):
    # Keeping the module-level hook usable makes the downloader straightforward
    # to test; production uses the no-redirect opener to prevent redirect SSRF.
    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        return urllib.request.urlopen(request, timeout=timeout)
    _assert_public_resolved_host(urlparse(request.full_url))
    return _IMAGE_OPENER.open(request, timeout=timeout)


def _validate_remote_image_url(value):
    """Validate an image URL before opening it; never follow redirects."""
    parsed = urlparse(str(value).strip())
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('Use a direct https image URL.')
    if parsed.hostname.casefold() in {'localhost', 'ip6-localhost'} or parsed.hostname.casefold().endswith('.localhost'):
        raise ValueError('Image URLs must point to a public host.')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved or address.is_multicast or address.is_unspecified):
        raise ValueError('Image URLs must point to a public host.')
    return parsed


def _assert_public_resolved_host(parsed):
    """Reject DNS names that resolve to local, reserved, or private addresses."""
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f'The image host {parsed.hostname} could not be found. Check the URL or try again later.') from exc
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise ValueError('Image URLs must point to a public host.')


def _image_signature_and_dimensions(data):
    """Return a safe image type and dimensions, retaining compatibility with old fixtures."""
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg', None
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        if len(data) >= 24:
            return 'image/png', '.png', (int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big'))
        return 'image/png', '.png', None
    # RIFF alone is not sufficient: require the WEBP form marker.
    if data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP':
        return 'image/webp', '.webp', None
    return None


def _validate_image_bytes(data):
    match = _image_signature_and_dimensions(data)
    if not match:
        return None
    mime, suffix, dimensions = match
    if dimensions and (dimensions[0] > 10000 or dimensions[1] > 10000 or dimensions[0] * dimensions[1] > 40_000_000):
        raise ValueError('Choose an image no larger than 40 megapixels.')
    return mime, suffix


def video_frame(seconds):
    """Nearest absolute video boundary; ties round up, as in playback.js."""
    return math.floor(seconds * VIDEO_FPS + .5 + 1e-9)


class JobCancelled(Exception):
    """Raised when the user cancels a background operation."""


def number(value, low, high, label):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a number.')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be between {low} and {high}.')
    return value


def seconds(value):
    parts = str(value or '0').strip().split(':')
    if len(parts) > 3:
        raise ValueError('Use a start time such as 1:23.5.')
    total = 0.0
    for part in parts:
        total = total * 60 + number(part, 0, 86400, 'Start time')
    return number(total, 0, 86400, 'Start time')


def playlist_id(value):
    value = str(value).strip()
    if value.startswith('spotify:playlist:'):
        value = value.split(':')[-1]
    elif value.startswith('https://'):
        parsed = urlparse(value)
        if parsed.hostname != 'open.spotify.com':
            raise ValueError('Paste a Spotify playlist link or playlist ID.')
        match = re.search(r'/playlist/([A-Za-z0-9]{22})(?:/|$)', parsed.path)
        value = match.group(1) if match else ''
    if not re.fullmatch(r'[A-Za-z0-9]{22}', value):
        raise ValueError('Paste a Spotify playlist link or playlist ID.')
    return value


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', value).strip(' .')[:180]
    return value or 'Untitled'


def visual_frames(value, duration):
    """Validate text-only visual overrides inside one continuous audio clip."""
    if not isinstance(value, list):
        raise ValueError('Visual frames must be a list.')
    frames = []
    for item in value:
        if not isinstance(item, dict) or item.get('type') != 'text':
            raise ValueError('Each visual frame must be a text frame.')
        start = number(item.get('start'), 0, duration, 'Text frame start')
        end = number(item.get('end'), 0, duration, 'Text frame end')
        text = str(item.get('text') or '').strip()[:2000]
        if not text:
            raise ValueError('Add text before saving a text frame.')
        if end - start < .01:
            raise ValueError('A text frame must be at least 0.01 seconds long.')
        frames.append({'type': 'text', 'start': round(start, 2), 'end': round(end, 2), 'text': text})
    frames.sort(key=lambda frame: (frame['start'], frame['end']))
    for previous, current in zip(frames, frames[1:]):
        if current['start'] < previous['end']:
            raise ValueError('Text frames cannot overlap.')
    return frames


def track_identity(artist, title):
    """Return a stable fallback identity for tracks without a Spotify ID."""
    def clean(value):
        return re.sub(r'[^\w]+', ' ', str(value or '').casefold(), flags=re.UNICODE).strip()
    return f'identity:{clean(artist)}|{clean(title)}'


def saved_clip_choice(track):
    return {field: track.get(field) for field in
            ('key', 'file', 'start', 'duration', 'selected', 'source_url',
              'card_title', 'card_artist', 'year', 'year_customized', 'description', 'visual_frames', 'audio_revision',
              'album_id', 'artwork_file', 'artwork_mime',
             'artwork_override_url', 'artwork_override_file', 'artwork_override_mime',
             'performance_image_url', 'performance_image_file', 'performance_image_mime')}


def empty_project():
    return {'name': 'My Top 100', 'playlist_id': '', 'tracks': [],
            'settings': {'duration': 10, 'crossfade': 2, 'preview_context': 4, 'video_preview_count': 0, 'video_animation': False, 'video_zoom': ZOOM_AMOUNT}, 'revision': 0, 'exports': {},
            'track_history': {}}


class Studio:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data_dir = self.root / 'data'
        self.cache_dir = self.data_dir / 'cache'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_file = self.data_dir / 'video_build_metrics.jsonl'
        self.file = self.data_dir / 'countdown_project.json'
        self.audio_dir = self.data_dir / 'snippets'
        self.output_dir = self.root / 'exports'
        self.artwork_dir = self.data_dir / 'album_covers'
        self.performance_dir = self.data_dir / 'performance_images'
        self.card_template = Path(__file__).parent / 'ui' / 'video-card.html'
        self.text_card_template = Path(__file__).parent / 'ui' / 'text-card.html'
        self.local_dir = local_audio_dir(self.root)
        self.lock = threading.RLock()
        self._current_zoom_amount = ZOOM_AMOUNT
        self.project = json.loads(self.file.read_text('utf-8')) if self.file.exists() else empty_project()
        self.project.setdefault('settings', {}).setdefault('preview_context', 4)
        self.project['settings'].setdefault('video_preview_count', 0)
        self.project['settings'].setdefault('video_animation', False)
        self.project['settings'].setdefault('video_zoom', ZOOM_AMOUNT)
        self.project['settings'].pop('video_reuse', None)
        for track in self.project.setdefault('tracks', []):
            track.setdefault('card_title', track.get('title') or '')
            track.setdefault('card_artist', track.get('primary_artist') or (track.get('artist') or '').split(',')[0].strip())
            track.setdefault('year', '')
            # Non-empty years from projects saved before this flag existed may
            # have been typed by the user, so preserve them conservatively.
            track.setdefault('year_customized', bool(track.get('year')))
            track.setdefault('description', '')
            track.setdefault('visual_frames', [])
            track.setdefault('album_id', '')
            track.setdefault('artwork_file', '')
            track.setdefault('artwork_mime', '')
            track.setdefault('artwork_override_url', '')
            track.setdefault('artwork_override_file', '')
            track.setdefault('artwork_override_mime', '')
            track.setdefault('performance_image_url', '')
            track.setdefault('performance_image_file', '')
            track.setdefault('performance_image_mime', '')
        self.job = {'running': False, 'kind': '', 'message': '', 'done': 0, 'total': 0, 'errors': [],
                    'started_at': None, 'elapsed_seconds': 0, 'timings': {},
                    'cache': {'clip_hits': 0, 'clip_misses': 0, 'assembled_hits': 0, 'assembled_misses': 0,
                              'render_hits': 0, 'render_misses': 0}}
        self.cancel_requested = threading.Event()
        self.wave_cache = {}
        self.oauth_state = None
        self.oauth_connected = False

    def save(self, content_changed=True):
        if content_changed:
            self.project['revision'] += 1
        temp = self.file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.project, indent=2, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.file)

    def idle(self):
        if self.job['running']:
            raise ValueError('Wait for the current operation to finish.')

    def track(self, key):
        for track in self.project['tracks']:
            if track['key'] == key:
                return track
        raise ValueError('Song not found. Reload the dashboard.')

    def resolve(self, track):
        name = track['file']
        if Path(name).name != name or '/' in name or '\\' in name:
            return None
        for folder in (self.audio_dir, self.local_dir):
            candidate = folder / name
            if candidate.is_file() and candidate.resolve().parent == folder.resolve():
                return candidate
        return None

    def view(self):
        with self.lock:
            data = copy.deepcopy(self.project)
            for track in data['tracks']:
                track['ready'] = self.resolve(track) is not None
                track['artwork_ready'] = self.artwork_path(track) is not None
                track['performance_image_ready'] = self.performance_path(track) is not None
            data['job'] = copy.deepcopy(self.job)
            if data['job']['running'] and data['job']['started_at'] is not None:
                data['job']['elapsed_seconds'] = max(0, time.monotonic() - data['job']['started_at'])
            data['job'].pop('started_at', None)
            return data

    def start(self, kind, work):
        with self.lock:
            self.idle()
            self.cancel_requested.clear()
            self.job = {'running': True, 'kind': kind, 'message': 'Starting…', 'done': 0, 'total': 0, 'errors': [],
                        'started_at': time.monotonic(), 'elapsed_seconds': 0, 'timings': {},
                        'cache': {'clip_hits': 0, 'clip_misses': 0, 'assembled_hits': 0, 'assembled_misses': 0,
                                  'render_hits': 0, 'render_misses': 0}}

        def run():
            try:
                work()
            except JobCancelled:
                with self.lock:
                    self.job['message'] = 'Canceled.'
            except Exception as exc:
                # Do not return OAuth responses, credentials, or downloader logs to the browser.
                if isinstance(exc, ValueError):
                    message = str(exc)
                elif kind == 'video build':
                    detail = str(exc).strip().replace('\r', ' ').replace('\n', ' ')
                    message = f'Video build failed: {type(exc).__name__}: {detail[:300] or "no additional details"}'
                else:
                    message = f'{kind.capitalize()} failed. Check the terminal for the error type.'
                if not isinstance(exc, ValueError):
                    print(f'{kind}: {type(exc).__name__}', flush=True)
                with self.lock:
                    self.job['errors'].append(message)
                    self.job['message'] = message
            finally:
                with self.lock:
                    if self.job.get('started_at') is not None:
                        self.job['elapsed_seconds'] = max(0, time.monotonic() - self.job['started_at'])
                    self.job['running'] = False
        threading.Thread(target=run, daemon=True).start()

    def cancel(self):
        with self.lock:
            if not self.job['running']:
                return False
            self.cancel_requested.set()
            self.job['message'] = 'Canceling…'
            return True

    def check_cancelled(self):
        if self.cancel_requested.is_set():
            raise JobCancelled()

    def progress(self, message, done=0, total=0):
        with self.lock:
            self.job.update(message=message, done=done, total=total)

    def timing(self, name, seconds):
        with self.lock:
            self.job.setdefault('timings', {})[name] = round(max(0, seconds), 3)

    def record_video_build_metrics(self, mode, tracks, combined, published, skipped, snapshot, *, video_stats):
        with self.lock:
            started_at = self.job.get('started_at')
            elapsed_seconds = (time.monotonic() - started_at) if started_at is not None else self.job.get('elapsed_seconds', 0)
            timings = copy.deepcopy(self.job.get('timings', {}))
            cache = copy.deepcopy(self.job.get('cache', {}))
        record = {
            'schema_version': 2,
            'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'mode': mode,
            'clips': len(tracks),
            'skipped': len(skipped),
            'audio_seconds': round(len(combined) / 1000, 3),
            'total_seconds': round(max(0, elapsed_seconds), 3),
            'phase_seconds': timings,
            'cache': cache,
            'video_encoder': VIDEO_ENCODER,
            'video_encoder_label': VIDEO_ENCODER_LABEL,
            'video_animation': snapshot['settings'].get('video_animation', False),
            'video_reuse': True,
            'video_settings': {
                'animation': 'on' if snapshot['settings'].get('video_animation', False) else 'off',
                'zoom_amount': snapshot['settings'].get('video_zoom', ZOOM_AMOUNT) if snapshot['settings'].get('video_animation', False) else 0,
                'reuse_unchanged_video': 'on',
            },
            'video_batching': 'one_song',
            'video_pipeline': video_stats,
            'project_revision': snapshot.get('revision'),
            'video_bytes': published.stat().st_size if published.is_file() else None,
        }
        try:
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
            with self.metrics_file.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError as error:
            LOGGER.warning('Could not append video build metrics: %s', error)

    def oauth(self):
        from spotipy.oauth2 import SpotifyOAuth
        if not all(os.getenv(k) for k in ('SPOTIPY_CLIENT_ID', 'SPOTIPY_CLIENT_SECRET')):
            raise ValueError('Spotify setup is missing. Add SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET to .env, then restart.')
        return SpotifyOAuth(client_id=os.getenv('SPOTIPY_CLIENT_ID'),
                            client_secret=os.getenv('SPOTIPY_CLIENT_SECRET'),
                            redirect_uri=REDIRECT_URI,
                            scope='playlist-read-private', cache_path=str(self.cache_dir / '.cache'),
                            open_browser=False)

    def _cache_artwork(self, album_id, url):
        """Cache Spotify artwork by album identity; artwork is never fetched during export."""
        if not album_id or not url:
            return ''
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        target = self.artwork_dir / f'{safe_name(album_id)}.jpg'
        if target.is_file():
            return target.name
        temporary = target.with_suffix('.tmp')
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Playlist to Countdown'})
            with urllib.request.urlopen(request, timeout=20) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            temporary.replace(target)
            return target.name
        except Exception:
            temporary.unlink(missing_ok=True)
            return ''

    def artwork_path(self, track):
        override = track.get('artwork_override_file') or ''
        if override and Path(override).name == override:
            override_path = self.artwork_dir / override
            if override_path.is_file():
                return override_path
        filename = track.get('artwork_file') or ''
        if not filename:
            self._cache_embedded_artwork(track)
            filename = track.get('artwork_file') or ''
        if not filename or Path(filename).name != filename:
            return None
        path = self.artwork_dir / filename
        return path if path.is_file() else None

    @staticmethod
    def _remove_asset_variants(directory, prefix):
        """Remove only known cached variants for one track and asset type."""
        if not directory.is_dir():
            return
        for candidate in directory.iterdir():
            if (candidate.is_file() and candidate.name.startswith(prefix)
                    and candidate.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')):
                candidate.unlink(missing_ok=True)

    def restore_artwork(self, track):
        """Drop a custom album-art override and return to the original artwork."""
        self._remove_asset_variants(self.artwork_dir, f'artwork-override-{track["key"]}')
        track['artwork_override_url'] = ''
        track['artwork_override_file'] = ''
        track['artwork_override_mime'] = ''


    def cache_artwork_override(self, track, url):
        image_url = str(url).strip()
        _validate_remote_image_url(image_url)
        request = urllib.request.Request(image_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        })
        try:
            with _open_image(request, timeout=20) as response:
                content_type = (response.headers.get_content_type() or '').lower()
                data = response.read(12 * 1024 * 1024 + 1)
        except Exception as exc:
            raise ValueError('The album cover could not be downloaded.') from exc
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('Choose an album cover smaller than 12 MB.')
        match = _validate_image_bytes(data)
        # Some CDNs send application/octet-stream (or omit Content-Type) for
        # copied image links. The file signature is the authoritative check.
        if not match:
            raise ValueError('Use a direct JPG, PNG, or WebP album cover URL.')
        mime, suffix = match
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        target = self.artwork_dir / f'artwork-override-{track["key"]}{suffix}'
        temporary = target.with_suffix(target.suffix + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)
        track['artwork_override_url'] = image_url
        track['artwork_override_file'] = target.name
        track['artwork_override_mime'] = mime
        return target.name

    def performance_path(self, track):
        filename = track.get('performance_image_file') or ''
        if not filename or Path(filename).name != filename:
            return None
        path = self.performance_dir / filename
        return path if path.is_file() else None

    def remove_performance_image(self, track):
        """Remove the optional performance image for one track."""
        self._remove_asset_variants(self.performance_dir, f'performance-{track["key"]}')
        track['performance_image_url'] = ''
        track['performance_image_file'] = ''
        track['performance_image_mime'] = ''

    def cache_performance_image(self, track, url):
        image_url = str(url).strip()
        parsed = _validate_remote_image_url(image_url)
        host = parsed.hostname or parsed.netloc
        request = urllib.request.Request(image_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        })
        try:
            with _open_image(request, timeout=20) as response:
                content_type = (response.headers.get_content_type() or '').lower()
                data = response.read(12 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            LOGGER.warning('Performance image download failed: host=%s error=http_%s reason=%s',
                           host, exc.code, exc.reason)
            raise ValueError(
                f'The image host returned HTTP {exc.code} ({exc.reason}). '
                'It may be blocking automated downloads, or the link may be unavailable.'
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            detail = str(reason).strip().replace('\r', ' ').replace('\n', ' ')[:240]
            detail_text = f' ({type(reason).__name__}: {detail})' if detail else ''
            if isinstance(reason, (socket.timeout, TimeoutError)):
                message = f'The image host {host} timed out after 20 seconds{detail_text}.'
                category = 'timeout'
            elif isinstance(reason, socket.gaierror):
                message = f'The image host {host} could not be found{detail_text}. Check the URL or try again later.'
                category = 'dns'
            elif isinstance(reason, (ConnectionRefusedError, PermissionError, ConnectionResetError)):
                message = f'The connection to {host} was refused or blocked{detail_text}.'
                category = 'connection_blocked'
            else:
                message = f'The image host {host} could not be reached{detail_text}.'
                category = 'network'
            LOGGER.warning('Performance image download failed: host=%s error=%s detail=%s',
                           host, category, reason)
            raise ValueError(message) from exc
        except (socket.timeout, TimeoutError) as exc:
            LOGGER.warning('Performance image download failed: host=%s error=timeout',
                           host)
            raise ValueError(f'The image host {host} timed out after 20 seconds ({type(exc).__name__}: {exc}).') from exc
        except ssl.SSLError as exc:
            LOGGER.warning('Performance image download failed: host=%s error=ssl detail=%s',
                           host, exc)
            raise ValueError(f'The image host {host} has an HTTPS certificate or connection problem ({type(exc).__name__}: {exc}).') from exc
        except OSError as exc:
            LOGGER.warning('Performance image download failed: host=%s error=os detail=%s',
                           host, exc)
            raise ValueError(f'The connection to {host} was blocked or could not be opened ({type(exc).__name__}: {exc}).') from exc
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('Choose a performance image smaller than 12 MB.')
        match = _validate_image_bytes(data)
        # Validate the bytes, not the server's sometimes-unreliable MIME type.
        if not match:
            LOGGER.warning('Performance image download returned non-image data: host=%s content_type=%s bytes=%s',
                           host, content_type or 'unknown', len(data))
            raise ValueError(
                f'The URL downloaded successfully, but it returned {content_type or "unknown content"}, '
                'not a JPG, PNG, or WebP image. Use the direct image URL.'
            )
        mime, suffix = match
        self.performance_dir.mkdir(parents=True, exist_ok=True)
        target = self.performance_dir / f'performance-{track["key"]}{suffix}'
        temporary = target.with_suffix(target.suffix + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)
        track['performance_image_url'] = image_url
        track['performance_image_file'] = target.name
        track['performance_image_mime'] = mime
        return target.name

    def _cache_embedded_artwork(self, track):
        """Cache the front-cover image embedded in a local MP3's ID3 tags."""
        if track.get('artwork_file'):
            return track['artwork_file']
        audio_path = self.resolve(track)
        if not audio_path:
            return ''
        try:
            from mutagen.id3 import ID3
            pictures = ID3(str(audio_path)).getall('APIC')
            if not pictures:
                return ''
            picture = next((item for item in pictures if item.type == 3), pictures[0])
            mime = picture.mime if picture.mime.startswith('image/') else 'image/jpeg'
            suffix = mimetypes.guess_extension(mime) or '.jpg'
            digest = hashlib.sha256(picture.data).hexdigest()[:24]
            target = self.artwork_dir / f'embedded-{digest}{suffix}'
            self.artwork_dir.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                temporary = target.with_suffix(target.suffix + '.tmp')
                temporary.write_bytes(picture.data)
                temporary.replace(target)
            track['artwork_file'] = target.name
            track['artwork_mime'] = mime
            return target.name
        except Exception:
            return ''

    def sync(self, sid, oauth):
        import spotipy
        self.progress('Reading Spotify playlist…')
        try:
            sp = spotipy.Spotify(auth_manager=oauth, requests_timeout=20)
            result = sp.playlist(sid)
            page = result.get('tracks') or result.get('items')
            if not isinstance(page, dict):
                raise ValueError('Spotify did not return playlist tracks for this account.')
            items = list(page['items'])
            while page.get('next'):
                page = sp.next(page)
                items.extend(page['items'])
                if len(items) > 500:
                    raise ValueError('Use a playlist of 500 songs or fewer.')
        except ValueError:
            raise
        except Exception as exc:
            status = getattr(exc, 'http_status', None)
            if status == 401:
                raise ValueError('Spotify authorization expired. Connect Spotify again, then reload the playlist.')
            if status == 403:
                raise ValueError('Spotify denied access to this playlist. Sign in with an account that can view it and confirm the app has playlist-read-private permission.')
            if status == 404:
                raise ValueError('Spotify could not find this playlist. Check that the link is complete and the playlist is still available to your account.')
            detail = str(exc).lower()
            if 'winerror 10013' in detail or 'forbidden by its access permissions' in detail:
                raise ValueError('Windows blocked Playlist to Countdown from reaching Spotify. Allow Python through the firewall or temporarily disable the VPN, then retry.')
            if isinstance(exc, (ConnectionError, TimeoutError)) or 'connection' in detail or 'timed out' in detail:
                raise ValueError('Spotify could not be reached. Check your internet, VPN, or firewall connection, then retry.')
            raise ValueError('Spotify could not read this playlist. Check the link, your account access, and Spotify app permissions.')
        if len(items) > 500:
            raise ValueError('Use a playlist of 500 songs or fewer.')
        with self.lock:
            history = self.project.setdefault('track_history', {})
            previous_by_spotify_id = {}
            previous_by_identity = {}
            for previous in self.project['tracks']:
                spotify_id = previous.get('spotify_id')
                if spotify_id:
                    history[spotify_id] = saved_clip_choice(previous)
                    previous_by_spotify_id.setdefault(spotify_id, []).append(previous)
                identity = track_identity(previous.get('artist'), previous.get('title'))
                history[identity] = saved_clip_choice(previous)
                previous_by_identity.setdefault(identity, []).append(previous)
        tracks, skipped, preserved = [], 0, 0
        used_keys = set()
        for index, item in enumerate(reversed(items)):
            track = item.get('track') or item.get('item')
            if not track or track.get('type', 'track') != 'track':
                skipped += 1
                continue
            spotify_artists = track.get('artists') or []
            artist = ', '.join(a['name'] for a in spotify_artists) or 'Unknown artist'
            first_artist = spotify_artists[0].get('name', '') if spotify_artists else ''
            imported_title = track.get('name') or ''
            title = imported_title or 'Untitled'
            spotify_id = track.get('id')
            album = track.get('album') or {}
            album_id = album.get('id') or ''
            release_date = str(album.get('release_date') or '')
            year = release_date[:4] if re.match(r'^\d{4}', release_date) else ''
            artwork_url = ((album.get('images') or [{}])[0] or {}).get('url') or ''
            identity = track_identity(artist, title)
            previous_choices = previous_by_spotify_id.get(spotify_id, []) if spotify_id else []
            previous = previous_choices.pop(0) if previous_choices else None
            if not previous:
                identity_choices = previous_by_identity.get(identity, [])
                previous = identity_choices.pop(0) if identity_choices else None
            if not previous:
                previous = history.get(spotify_id) if spotify_id else None
            if not previous:
                previous = history.get(identity)
            if previous:
                preserved += 1
            artwork_file = previous.get('artwork_file', '') if previous else ''
            if artwork_url and album_id:
                artwork_file = self._cache_artwork(album_id, artwork_url) or artwork_file
            key = previous.get('key') if previous else None
            if not key or key in used_keys:
                key = secrets.token_hex(8)
                while key in used_keys:
                    key = secrets.token_hex(8)
            used_keys.add(key)
            tracks.append({'key': key,
                           'spotify_id': spotify_id, 'title': title, 'artist': artist,
                           'primary_artist': first_artist,
                           'card_title': previous.get('card_title', imported_title) if previous else imported_title,
                           'card_artist': previous.get('card_artist', first_artist) if previous else first_artist,
                           'year': previous.get('year', '') if previous and previous.get('year_customized') else year,
                           'year_customized': bool(previous.get('year_customized')) if previous else False,
                           'file': previous.get('file') if previous else safe_name(f'{first_artist}-{title}') + '.mp3',
                           'start': previous.get('start', 0) if previous else 0,
                           'duration': previous.get('duration') if previous else None,
                           'selected': bool(previous.get('selected')) if previous else False,
                           'source_url': previous.get('source_url', '') if previous else '',
                           'description': previous.get('description', '') if previous else '',
                           'visual_frames': copy.deepcopy(previous.get('visual_frames', [])) if previous else [],
                           'audio_revision': previous.get('audio_revision', 0) if previous else 0,
                           'album_id': album_id or (previous.get('album_id', '') if previous else ''),
                           'artwork_file': artwork_file,
                            'artwork_mime': previous.get('artwork_mime', '') if previous else '',
                            'artwork_override_url': previous.get('artwork_override_url', '') if previous else '',
                            'artwork_override_file': previous.get('artwork_override_file', '') if previous else '',
                            'artwork_override_mime': previous.get('artwork_override_mime', '') if previous else '',
                            'performance_image_url': previous.get('performance_image_url', '') if previous else '',
                            'performance_image_file': previous.get('performance_image_file', '') if previous else '',
                            'performance_image_mime': previous.get('performance_image_mime', '') if previous else '',
                           'rank': len(items) - index})
            self._cache_embedded_artwork(tracks[-1])
        if not tracks:
            raise ValueError('No playable songs found in this playlist.')
        with self.lock:
            self.project.update(name=result.get('name', 'My Top 100'), playlist_id=sid, tracks=tracks)
            self.project['track_history'] = {
                **self.project.get('track_history', {}),
                **{track['spotify_id']: saved_clip_choice(track)
                   for track in tracks if track.get('spotify_id')},
                **{track_identity(track['artist'], track['title']): saved_clip_choice(track)
                   for track in tracks},
            }
            self.save()
        self.progress(f'Loaded {len(tracks)} songs in countdown order.'
                      + (f' Preserved clip choices for {preserved} existing song(s).' if preserved else '')
                      + (f' {skipped} unavailable items skipped.' if skipped else ''))

    def download(self):
        tracks = [t for t in self.project['tracks'] if not self.resolve(t)]
        self.audio_dir.mkdir(exist_ok=True)
        node = shutil.which('node')
        if not node:
            raise ValueError('YouTube downloads need Node.js 22 or newer for yt-dlp’s challenge solver. Install Node.js, then restart Playlist to Countdown.')
        for index, track in enumerate(tracks):
            self.progress(f'Downloading {track["artist"]} — {track["title"]}', index, len(tracks))
            query = track.get('source_url') or f'ytsearch1:{track["artist"]} {track["title"]} official audio'
            # yt-dlp treats percent signs as template syntax; escape literal filenames.
            target = str(self.audio_dir / track['file']).replace('%', '%%')
            command = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-progress',
                       '--socket-timeout', '20', '--retries', '2', '--extract-audio',
                       '--js-runtimes', f'node:{node}', '--format', 'bestaudio/best',
                       '--audio-format', 'mp3', '--output', target, '--', query]
            detail = ''
            try:
                result = subprocess.run(command, capture_output=True, timeout=300)
                success = result.returncode == 0 and self.resolve(track)
            except subprocess.TimeoutExpired:
                success = False
                detail = 'timed out after 5 minutes'
            if not success:
                if not detail:
                    stderr = getattr(result, 'stderr', b'') or b''
                    stdout = getattr(result, 'stdout', b'') or b''
                    if isinstance(stderr, str): stderr = stderr.encode('utf-8', errors='replace')
                    if isinstance(stdout, str): stdout = stdout.encode('utf-8', errors='replace')
                    output = (stderr + b'\n' + stdout).decode('utf-8', errors='replace')
                    detail = self._download_error(output)
                with self.lock:
                    self.job['errors'].append(f'{track["title"]}: {detail}')
        ready = sum(self.resolve(t) is not None for t in self.project['tracks'])
        self.progress(f'{ready} of {len(self.project["tracks"])} songs ready.', len(tracks), len(tracks))

    def replace_audio(self, key):
        with self.lock:
            track = self.track(key)
            source_url = str(track.get('source_url') or '').strip()
            parsed = urlparse(source_url)
            if not source_url or parsed.scheme != 'https' or parsed.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'):
                raise ValueError('Paste a full https YouTube video link before replacing the audio.')
            if Path(track['file']).name != track['file']:
                raise ValueError('This song has an invalid audio filename.')
            node = shutil.which('node')
            if not node:
                raise ValueError('YouTube downloads need Node.js 22 or newer for yt-dlp’s challenge solver. Install Node.js, then restart Playlist to Countdown.')
            self.audio_dir.mkdir(exist_ok=True)
            target = self.audio_dir / track['file']
            temporary = self.audio_dir / f'.replace-{key}.mp3'
            self.progress(f'Replacing audio for {track["artist"]} — {track["title"]}…', 0, 1)
            command = [sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-progress',
                       '--socket-timeout', '20', '--retries', '2', '--force-overwrites',
                       '--extract-audio', '--js-runtimes', f'node:{node}', '--format', 'bestaudio/best',
                       '--audio-format', 'mp3', '--output', str(temporary).replace('%', '%%'), '--', source_url]
            detail = ''
            try:
                result = subprocess.run(command, capture_output=True, timeout=300)
                success = result.returncode == 0 and temporary.is_file()
            except subprocess.TimeoutExpired:
                success = False
                detail = 'timed out after 5 minutes'
            if not success:
                if not detail:
                    stderr = getattr(result, 'stderr', b'') or b''
                    stdout = getattr(result, 'stdout', b'') or b''
                    if isinstance(stderr, str): stderr = stderr.encode('utf-8', errors='replace')
                    if isinstance(stdout, str): stdout = stdout.encode('utf-8', errors='replace')
                    detail = self._download_error((stderr + b'\n' + stdout).decode('utf-8', errors='replace'))
                temporary.unlink(missing_ok=True)
                raise ValueError(f'{track["title"]}: {detail}')
            temporary.replace(target)
            track['audio_revision'] = time.time_ns()
            self.save()
            self.progress(f'Replaced audio for {track["artist"]} — {track["title"]}.', 1, 1)

    @staticmethod
    def _download_error(output):
        """Turn yt-dlp's verbose output into a useful, non-secret explanation."""
        lower = output.lower()
        if 'yt-dlp-ejs' in lower or 'challenge solver' in lower or 'no supported javascript runtime' in lower:
            return 'YouTube challenge solving is unavailable. Install/update yt-dlp with its default components, then restart Playlist to Countdown.'
        if 'winerror 10013' in lower or 'socket' in lower and 'forbidden' in lower:
            return 'Windows blocked the network connection. Check firewall/VPN/network access, then retry.'
        if 'http error 403' in lower or 'forbidden' in lower:
            return 'YouTube rejected the download (HTTP 403). Try a specific YouTube link or retry later; this is not Spotify authentication.'
        if 'unable to download webpage' in lower or 'connection' in lower or 'timed out' in lower:
            return 'The YouTube request could not connect. Check internet/VPN access, then retry.'
        if 'requested format is not available' in lower or 'only images are available' in lower:
            return 'YouTube returned no downloadable audio format for this result. Try a specific video link.'
        errors = [line.strip() for line in output.splitlines() if 'error:' in line.lower()]
        if errors:
            return errors[-1][:280]
        return 'yt-dlp could not download a usable audio file. Try a specific YouTube link.'

    def _clip_piece(self, track, settings):
        from pydub import AudioSegment
        import numpy as np
        import pyloudnorm as pyln
        source = AudioSegment.from_file(self.resolve(track))
        start = round(track['start'] * 1000)
        duration = round((track['duration'] or settings['duration']) * 1000)
        if start >= len(source):
            raise ValueError(f'{track["title"]}: the selected start is past the end of the song.')
        piece = source[start:start + duration].set_frame_rate(44100).set_channels(2)
        samples = np.array(piece.get_array_of_samples(), dtype=np.float64).reshape((-1, 2))
        samples /= float(1 << (8 * piece.sample_width - 1))
        if len(piece) >= 400:
            loudness = pyln.Meter(piece.frame_rate).integrated_loudness(samples)
            if np.isfinite(loudness):
                # Normalize toward -13 LUFS while retaining 1 dB of peak headroom.
                piece = piece.apply_gain(min(-13 - loudness, -1 - piece.max_dBFS))
        if len(piece) < duration:
            piece += AudioSegment.silent(duration=duration - len(piece), frame_rate=44100).set_channels(2)
        return piece

    def _clip_cache_path(self, track, settings):
        source = self.resolve(track)
        if not source:
            return None
        stat = source.stat()
        duration = track['duration'] or settings['duration']
        signature = '|'.join((str(CLIP_CACHE_VERSION), str(source.resolve()),
                              str(stat.st_mtime_ns), str(stat.st_size),
                              str(track.get('audio_revision', '')), f'{track["start"]:.6f}',
                              f'{duration:.6f}'))
        digest = hashlib.sha256(signature.encode('utf-8')).hexdigest()
        return self.cache_dir / 'clip-pieces' / f'{digest}.wav'

    def _cached_clip_piece(self, track, settings):
        from pydub import AudioSegment
        cache = self._clip_cache_path(track, settings)
        if cache and cache.is_file():
            try:
                with self.lock:
                    if self.job['running']:
                        self.job['cache']['clip_hits'] += 1
                return AudioSegment.from_file(cache).set_frame_rate(44100).set_channels(2)
            except Exception:
                cache.unlink(missing_ok=True)

        with self.lock:
            if self.job['running']:
                self.job['cache']['clip_misses'] += 1
        piece = self._clip_piece(track, settings)
        if not cache:
            return piece
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(f'.{cache.stem}-{threading.get_ident()}.tmp.wav')
        try:
            piece.export(temporary, format='wav')
            temporary.replace(cache)
        except OSError:
            # The cache is an optimization; a locked or unavailable cache must
            # never make an otherwise valid export fail.
            temporary.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return piece

    def _prepare_clips(self, tracks, settings):
        total = len(tracks)
        pieces = [None] * total
        workers = min(4, total, max(1, os.cpu_count() or 1))
        self.progress(f'Preparing {total} clips (up to {workers} at a time)…', 0, total)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='clip-prep') as pool:
            futures = {pool.submit(self._cached_clip_piece, track, settings): index
                       for index, track in enumerate(tracks)}
            for done, future in enumerate(as_completed(futures), 1):
                pieces[futures[future]] = future.result()
                self.progress(f'Prepared {done} of {total} clips…', done, total)
        return pieces

    @staticmethod
    def _append_with_crossfade(combined, piece, requested):
        overlap = min(requested, max(0, len(combined) - 1), max(0, len(piece) - 1))
        return combined.append(piece, crossfade=overlap)

    def _assembled_audio_cache_path(self, tracks, settings):
        sources = []
        for track in tracks:
            source = self.resolve(track)
            stat = source.stat() if source else None
            sources.append({'key': track['key'], 'file': track['file'], 'start': track['start'],
                            'duration': track['duration'] or settings['duration'],
                            'revision': track.get('audio_revision', ''),
                            'mtime_ns': stat.st_mtime_ns if stat else None,
                            'size': stat.st_size if stat else None})
        signature = {'version': ASSEMBLED_AUDIO_CACHE_VERSION, 'duration': settings['duration'],
                     'crossfade': settings['crossfade'], 'final_fade': FINAL_AUDIO_FADE_SECONDS,
                     'tracks': sources}
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode('utf-8')).hexdigest()
        return self.cache_dir / 'assembled-audio' / f'{digest}.wav'

    def transition_preview(self, key):
        from pydub import AudioSegment
        with self.lock:
            self.idle()
            snapshot = copy.deepcopy(self.project)
            current_index = next((i for i, track in enumerate(snapshot['tracks']) if track['key'] == key), None)
            if current_index is None:
                raise ValueError('Song not found. Reload the dashboard.')
            current = snapshot['tracks'][current_index]
            if not self.resolve(current):
                raise ValueError('The current song has no audio available.')
            previous = next((track for track in reversed(snapshot['tracks'][:current_index])
                             if track['selected'] and self.resolve(track)), None)
            following = next((track for track in snapshot['tracks'][current_index + 1:]
                              if track['selected'] and self.resolve(track)), None)
            if not previous or not following:
                raise ValueError('Include a neighboring clip before and after this song to preview the transition.')

            context = int(float(snapshot['settings'].get('preview_context', 4)) * 1000)
            fade = min(round(snapshot['settings']['crossfade'] * 1000), context)
            previous_piece = self._clip_piece(previous, snapshot['settings'])
            following_piece = self._clip_piece(following, snapshot['settings'])
            before = previous_piece[max(0, len(previous_piece) - context):]
            middle = self._clip_piece(current, snapshot['settings'])
            after = following_piece[:context]
            combined = self._append_with_crossfade(before, middle, fade)
            combined = self._append_with_crossfade(combined, after, fade)

            self.output_dir.mkdir(exist_ok=True)
            output = self.output_dir / f'transition-preview-{key}-{snapshot["revision"]}.mp3'
            for old in self.output_dir.glob(f'transition-preview-{key}-*.mp3'):
                if old != output:
                    old.unlink(missing_ok=True)
            temporary = self.output_dir / f'.transition-preview-{key}-building.mp3'
            try:
                combined.export(temporary, format='mp3', bitrate='192k')
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            return output

    def build_thumbnail(self, output=None, *, width=1920, height=1080, gap=4,
                        background='#111111', save_export=True):
        """Build a deduplicated collage from the covers included in the video."""
        from .thumbnail import render_thumbnail, unique_artwork
        with self.lock:
            self.idle()
            tracks = [track for track in self.project['tracks'] if track.get('selected')]
            limit = int(self.project.get('settings', {}).get('video_preview_count') or 0)
            if limit:
                tracks = tracks[:limit]
            paths, missing = unique_artwork(self, tracks)
            target = Path(output) if output else self.output_dir / 'countdown-thumbnail.png'
            result = render_thumbnail(paths, target, width=width, height=height,
                                      gap=gap, background=background)
            result['missing'] = missing
            result['file'] = result['path'].name
            result['created'] = time.time_ns()
            result['revision'] = self.project['revision']
            if save_export:
                self.project.setdefault('exports', {})['thumbnail'] = {
                    key: value for key, value in result.items() if key != 'path'
                }
                self.save(content_changed=False)
            return result

    def _assemble(self, snapshot, mode, preview_limit=None):
        """Prepare one immutable export snapshot and return audio, cues, and skips."""
        from pydub import AudioSegment
        tracks = snapshot['tracks']
        # A video song limit is a fast, self-contained export. It applies to
        # the kept songs before any files are opened or clips are prepared,
        # regardless of whether the video uses the preview or final filename.
        if mode == 'preview' or preview_limit is not None:
            tracks = [t for t in tracks if t['selected']]
            if preview_limit is not None:
                tracks = tracks[:preview_limit]
        if not tracks:
            raise ValueError('Include at least one clip before building a preview.')
        skipped = [{'rank': t['rank'], 'title': t['title'], 'artist': t['artist']}
                   for t in tracks if not self.resolve(t)]
        tracks = [t for t in tracks if self.resolve(t)]
        if not tracks:
            raise ValueError('No audio is available for these clips. Nothing was exported.')
        if mode == 'final' and any(not t['selected'] for t in tracks):
            raise ValueError('Include every clip with available audio before exporting the full compilation.')
        fade = round(snapshot['settings']['crossfade'] * 1000)
        cue_sheet = []
        cache = self._assembled_audio_cache_path(tracks, snapshot['settings'])
        combined = None
        if cache.is_file():
            try:
                combined = AudioSegment.from_file(cache).set_frame_rate(44100).set_channels(2)
                with self.lock:
                    if self.job['running']: self.job['cache']['assembled_hits'] += 1
                self.progress('Reusing assembled audio timeline…', len(tracks), len(tracks))
            except Exception:
                cache.unlink(missing_ok=True)
        if combined is None:
            with self.lock:
                if self.job['running']: self.job['cache']['assembled_misses'] += 1
            pieces = self._prepare_clips(tracks, snapshot['settings'])
            if pieces:
                pieces[-1] = pieces[-1].fade_out(min(round(FINAL_AUDIO_FADE_SECONDS * 1000), len(pieces[-1])))
            for index, (track, piece) in enumerate(zip(tracks, pieces)):
                self.check_cancelled()
                self.progress(f'Assembling clip {index + 1} of {len(tracks)}: {track["title"]}', index, len(tracks))
                overlap = min(fade, len(piece) - 1, len(combined) - 1) if combined is not None else 0
                cue_start = (len(combined) - max(0, overlap)) if combined is not None else 0
                cue_sheet.append({'key': track['key'], 'rank': track['rank'], 'title': track['title'],
                                  'artist': track['artist'], 'description': track.get('description', ''),
                                  'artwork_file': track.get('artwork_file', ''), 'start': cue_start / 1000,
                                  'end': (cue_start + len(piece)) / 1000})
                combined = piece if combined is None else combined.append(piece, crossfade=max(0, overlap))
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_name(f'.{cache.stem}-{threading.get_ident()}.tmp.wav')
            try:
                combined.export(temporary, format='wav')
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
        if not cue_sheet:
            elapsed = 0
            for track in tracks:
                length = round((track['duration'] or snapshot['settings']['duration']) * 1000)
                overlap = min(fade, max(0, length - 1), max(0, elapsed - 1)) if elapsed else 0
                cue_start = elapsed - overlap
                cue_sheet.append({'key': track['key'], 'rank': track['rank'], 'title': track['title'],
                                  'artist': track['artist'], 'description': track.get('description', ''),
                                  'artwork_file': track.get('artwork_file', ''), 'start': cue_start / 1000,
                                  'end': (cue_start + length) / 1000})
                elapsed = cue_start + length
        return combined, tracks, cue_sheet, skipped

    @staticmethod
    def _video_boundaries(cues, milliseconds):
        """Return non-overlapping card frame ranges using midpoint hard cuts."""
        fps = 30
        boundaries = [0]
        for left, right in zip(cues, cues[1:]):
            midpoint = (left['end'] + right['start']) / 2
            boundaries.append(max(boundaries[-1], round(midpoint * fps)))
        boundaries.append(max(boundaries[-1], round(milliseconds / 1000 * fps)))
        return [(boundaries[i], boundaries[i + 1]) for i in range(len(cues))]

    @staticmethod
    def _visual_track_segments(track, cue, visible_start, visible_end, card, text_cards, track_index):
        """Return (image, frame count) segments from absolute compilation boundaries.

        Text frames remain discrete image segments. They are deliberately kept
        out of the song-to-song xfade path by checking the first and last
        segment assets at each track boundary.
        """
        clip_duration = max(.01, cue['end'] - cue['start'])
        start = max(0, visible_start - cue['start'])
        end = min(clip_duration, visible_end - cue['start'])
        if end <= start:
            return []
        points = {start, end}
        for frame in track.get('visual_frames') or []:
            points.add(max(start, min(end, float(frame['start']))))
            points.add(max(start, min(end, float(frame['end']))))
        ordered = sorted(points)
        segments = []
        for left, right in zip(ordered, ordered[1:]):
            if right - left < .0001:
                continue
            midpoint = (left + right) / 2
            asset = card
            for frame_index, frame in enumerate(track.get('visual_frames') or []):
                if frame['start'] <= midpoint < frame['end']:
                    asset = text_cards[(track_index, frame_index)]
                    break
            length = video_frame(cue['start'] + right) - video_frame(cue['start'] + left)
            if length > 0:
                segments.append((asset, length))
        return segments

    def _video_filter(self, cards, tracks, cues, milliseconds, text_cards, *, visible_ranges=None, output_fps=None, zoom_amount=ZOOM_AMOUNT):
        """Build an ffmpeg graph with hard visual cuts and text frame timing."""
        fps = output_fps or (MOTION_FPS if any(isinstance(card, dict) for card in cards) else VIDEO_FPS)
        frame_multiplier = fps // VIDEO_FPS
        total = milliseconds / 1000
        overlaps = [max(0, left['end'] - right['start'])
                    for left, right in zip(cues, cues[1:])]
        dissolve = [False for _ in overlaps]
        visual_overlaps = [0 for _ in overlaps]

        starts = [0.0]
        for i, overlap in enumerate(overlaps):
            midpoint = (cues[i]['end'] + cues[i + 1]['start']) / 2
            starts.append(midpoint - visual_overlaps[i] / 2 if dissolve[i] else midpoint)
        ends = []
        for i in range(len(cues)):
            ends.append(total if i == len(cues) - 1 else
                        ((cues[i]['end'] + cues[i + 1]['start']) / 2 + visual_overlaps[i] / 2
                         if dissolve[i] else
                         (cues[i]['end'] + cues[i + 1]['start']) / 2))

        if visible_ranges is not None:
            starts, ends = map(list, zip(*visible_ranges))
        input_args, filters, track_labels = [], [], []
        input_index = 0
        for track_index, (track, cue, start, end) in enumerate(zip(tracks, cues, starts, ends)):
            segments = self._visual_track_segments(track, cue, start, end, cards[track_index],
                                                    text_cards, track_index)
            if not segments:
                raise ValueError(f'Could not create a video segment for #{track["rank"]} {track["title"]}.')
            segment_labels = []
            elapsed_frames = 0
            track_frames = sum(length for _, length in segments) * frame_multiplier
            for segment_index, (asset, frame_count) in enumerate(segments):
                frame_count *= frame_multiplier
                duration = frame_count / fps
                label = f's{track_index}_{segment_index}'
                assets = [asset[kind] for kind in ('photo', 'mask', 'foreground')] if isinstance(asset, dict) else [asset]
                for visual_asset in assets:
                    input_args.extend(['-loop', '1', '-framerate', str(fps), '-t', f'{duration:.6f}',
                                       '-i', str(visual_asset)])
                if isinstance(asset, dict):
                    filters.extend(motion_filters(input_index, label, frame_count, elapsed_frames, track_frames,
                                                   asset['photo_center'], zoom_amount))
                else:
                    filters.append(f'[{input_index}:v]trim=end_frame={frame_count},setpts=N/({fps}*TB),'
                                   f'fps={fps},format=yuv420p[{label}]')
                segment_labels.append(f'[{label}]')
                input_index += len(assets)
                elapsed_frames += frame_count
            track_label = f'track{track_index}'
            if len(segment_labels) == 1:
                filters.append(f'{segment_labels[0]}setpts=PTS-STARTPTS,settb=1/{fps}[{track_label}]')
            else:
                filters.append(''.join(segment_labels) +
                               f'concat=n={len(segment_labels)}:v=1:a=0,setpts=PTS-STARTPTS,settb=1/{fps}[{track_label}]')
            track_labels.append(track_label)

        starts = [video_frame(value) * frame_multiplier for value in starts]
        ends = [video_frame(value) * frame_multiplier for value in ends]
        current = f'[{track_labels[0]}]'
        current_duration = ends[0] - starts[0]
        for i in range(1, len(track_labels)):
            incoming = f'[{track_labels[i]}]'
            if dissolve[i - 1] and ends[i - 1] > starts[i]:
                overlap_frames = ends[i - 1] - starts[i]
                duration = overlap_frames / fps
                offset = max(0, current_duration - overlap_frames) / fps
                output = f'mix{i}'
                filters.append(f'{current}{incoming}xfade=transition=fade:duration={duration:.6f}:'
                               f'offset={offset:.6f},format=yuv420p,settb=1/{fps}[{output}]')
                current_duration += (ends[i] - starts[i]) - overlap_frames
            else:
                output = f'mix{i}'
                filters.append(f'{current}{incoming}concat=n=2:v=1:a=0,setpts=PTS-STARTPTS,settb=1/{fps}[{output}]')
                current_duration += ends[i] - starts[i]
            current = f'[{output}]'
        return input_args, ';'.join(filters), current, current_duration

    def _run_video_command(self, command, output, timeout):
        """Keep cancellation responsive and drain diagnostics without pipe deadlocks."""
        self.check_cancelled()
        log = output.with_suffix('.ffmpeg.log')
        with log.open('wb') as errors:
            process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.DEVNULL, stderr=errors)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if self.cancel_requested.wait(.25):
                        raise JobCancelled()
                    if time.monotonic() - started > timeout:
                        raise ValueError('FFmpeg timed out while creating the MP4.')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        self.check_cancelled()
        if process.returncode or not output.is_file():
            with log.open('rb') as errors:
                errors.seek(max(0, log.stat().st_size - 4000))
                detail = errors.read().decode('utf-8', errors='replace').strip()
            raise ValueError(f'FFmpeg could not create the MP4. {detail}')

    def _encode_static_video(self, cards, tracks, cues, milliseconds, text_cards, audio, temp_dir, output):
        """Use one encoding job for the cheaper static-card export."""
        args, graph, label, count = self._video_filter(cards, tracks, cues, milliseconds, text_cards,
                                                       zoom_amount=self._current_zoom_amount)
        aliases = {}
        for index, argument in enumerate(args):
            if index and args[index - 1] == '-i':
                if argument not in aliases:
                    alias = temp_dir / f'input-{len(aliases)}.png'
                    try:
                        os.link(argument, alias)
                    except OSError:
                        shutil.copyfile(argument, alias)
                    aliases[argument] = str(alias.relative_to(self.root))
                args[index] = aliases[argument]
        script = temp_dir / 'static-filter.txt'
        script.write_text(graph, encoding='utf-8')
        command = ['ffmpeg', '-y', '-v', 'error', *args, '-i', str(audio),
                   '-filter_complex_threads', '4', '-filter_complex_script', str(script),
                   '-map', label, '-map', f'{args.count("-i")}:a:0', '-frames:v', str(count),
                   '-c:v', VIDEO_ENCODER, '-global_quality', '23', '-pix_fmt', 'yuv420p',
                   '-r', str(VIDEO_FPS), '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output)]
        self.progress(f'Encoding static video with {VIDEO_ENCODER_LABEL}…', 0, len(tracks))
        try:
            self._run_video_command(command, output, max(900, milliseconds / 1000 * 6))
        finally:
            output.with_suffix('.ffmpeg.log').unlink(missing_ok=True)

    def _video_segment_cache(self, args, graph, encoding, implementation):
        # Hash the actual renderer inputs, not track IDs or project revision.
        # The graph contains local frame counts/timing, so unrelated edits and
        # timeline shifts can reuse a segment when its output is identical.
        inputs = list(args)
        for index, value in enumerate(inputs):
            if index and inputs[index - 1] == '-i':
                inputs[index] = hashlib.sha256(Path(value).read_bytes()).hexdigest()
        signature = {'inputs': inputs, 'graph': graph, 'encoding': encoding, 'implementation': implementation}
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / 'video-segments' / f'{digest}.mp4'

    @staticmethod
    def _reuse_video_segment(cached, destination):
        try:
            manifest = json.loads(cached.with_suffix('.json').read_text('utf-8'))
            if hashlib.sha256(cached.read_bytes()).hexdigest() != manifest['sha256']:
                return False
            shutil.copyfile(cached, destination)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _store_video_segment(self, part, cached, frames, fps):
        # Verify a completed encode before publishing; incomplete/corrupt cache
        # entries simply miss next time. Never publish on cancellation/failure.
        temporary = None
        metadata = None
        try:
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=nb_frames,r_frame_rate,width,height', '-of', 'json', str(part)], timeout=30))['streams'][0]
            if (int(info['nb_frames']) != frames or info['r_frame_rate'] != f'{fps}/1'
                    or (info['width'], info['height']) != (1920, 1080)):
                return
            self.check_cancelled()
            cached.parent.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_name(f'.{cached.stem}-{secrets.token_hex(6)}.tmp')
            metadata = temporary.with_suffix('.json')
            shutil.copyfile(part, temporary)
            metadata.write_text(json.dumps({'sha256': hashlib.sha256(temporary.read_bytes()).hexdigest(),
                                            'frames': frames, 'fps': fps}), encoding='utf-8')
            temporary.replace(cached)
            metadata.replace(cached.with_suffix('.json'))
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            LOGGER.warning('Could not cache encoded video segment; export will continue.')
        finally:
            for path in (temporary, metadata):
                if path is not None:
                    path.unlink(missing_ok=True)

    def _encode_video_batches(self, cards, tracks, cues, milliseconds, text_cards, audio, temp_dir, output,
                              *, reuse=False, video_stats=None):
        """Encode one song at a time, then copy video and encode continuous audio once."""
        fps = MOTION_FPS if any(isinstance(card, dict) for card in cards) else VIDEO_FPS
        boundaries = self._video_boundaries(cues, milliseconds)
        parts = []
        hits = misses = 0
        implementation = ''
        if reuse:
            implementation = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('motion.py').read_bytes()
                + subprocess.check_output(['ffmpeg', '-version'], timeout=30)).hexdigest()
        for index, (card, track, cue, (start, end)) in enumerate(zip(cards, tracks, cues, boundaries)):
            self.check_cancelled()
            if end <= start:
                continue
            local_text = {(0, frame): asset for (song, frame), asset in text_cards.items() if song == index}
            args, graph, label, count = self._video_filter(
                [card], [track], [cue], milliseconds, local_text,
                visible_ranges=[(start / VIDEO_FPS, end / VIDEO_FPS)], output_fps=fps,
                zoom_amount=self._current_zoom_amount)
            script = temp_dir / 'batch-filter.txt'
            script.write_text(graph, encoding='utf-8')
            part = temp_dir / f'batch-{index:04d}.mp4'
            encoding = ['-c:v', VIDEO_ENCODER, '-global_quality', '23', '-pix_fmt', 'yuv420p',
                        '-r', str(fps), '-video_track_timescale', '15360']
            cached = self._video_segment_cache(args, graph, encoding, implementation) if reuse else None
            if cached and self._reuse_video_segment(cached, part):
                hits += 1
                with self.lock:
                    self.job['cache']['video_hits'] = hits
                self.progress(f'Reusing song {index + 1} of {len(tracks)}…', index + 1, len(tracks))
                parts.append(part)
                continue
            misses += 1
            with self.lock:
                self.job['cache']['video_misses'] = misses
            self.progress(f'Encoding song {index + 1} of {len(tracks)} with {VIDEO_ENCODER_LABEL}…', index, len(tracks))
            command = ['ffmpeg', '-y', '-v', 'error', *args,
                       '-filter_complex_threads', '4', '-filter_complex_script', str(script),
                       '-map', label, '-frames:v', str(count), '-an',
                       *encoding, str(part)]
            self._run_video_command(command, part, max(900, count / fps * 6))
            if cached:
                self._store_video_segment(part, cached, count, fps)
            parts.append(part)
        if video_stats is not None:
            video_stats.update(reused_segments=hits, encoded_segments=misses)
        manifest = temp_dir / 'batches.txt'
        manifest.write_text(''.join(f"file '{part.name}'\n" for part in parts), encoding='utf-8')
        self.progress('Joining video batches and adding continuous audio…', len(tracks), len(tracks))
        command = ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '1', '-i', str(manifest),
                   '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
                   '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output)]
        try:
            self._run_video_command(command, output, max(900, milliseconds / 1000 * 2))
        finally:
            output.with_suffix('.ffmpeg.log').unlink(missing_ok=True)

    def _render_cache_key(self, kind, template, payload, media):
        signature = {'version': RENDER_CACHE_VERSIONS.get(kind, 1), 'kind': kind,
                     'template': hashlib.sha256(template.encode('utf-8')).hexdigest(),
                     'payload': payload, 'media': media}
        return hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    def _render_cache_file(self, kind, template, payload, media):
        key = self._render_cache_key(kind, template, payload, media)
        return self.cache_dir / 'rendered-cards' / f'{kind}-{key}.png'

    def _cache_rendered_image(self, page, cache_file):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_name(f'.{cache_file.stem}-{threading.get_ident()}.tmp.png')
        try:
            page.locator('#frame').screenshot(path=str(temporary))
            temporary.replace(cache_file)
        finally:
            temporary.unlink(missing_ok=True)
        return cache_file

    def _render_cards(self, tracks, cues, temp_dir, *, motion=False):
        from playwright.sync_api import sync_playwright
        import base64
        template = self.card_template.read_text(encoding='utf-8')
        text_template = self.text_card_template.read_text(encoding='utf-8')
        cards, text_cards = [], {}
        artwork_fallbacks = []
        # The context manager owns shutdown. Closing the browser again from a
        # finally block after an error makes Playwright report “Event loop is
        # closed! Is Playwright already stopped?” instead of the real error.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
            text_page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
            page.set_content(template, wait_until='load')
            text_page.set_content(text_template, wait_until='load')
            page.evaluate('() => document.fonts.ready')
            text_page.evaluate('() => document.fonts.ready')
            for index, (track, cue) in enumerate(zip(tracks, cues)):
                self.check_cancelled()
                self.progress(f'Rendering card {index + 1} of {len(tracks)}: {track["title"]}', index, len(tracks))
                artwork = self.artwork_path(track)
                if artwork:
                    mime = track.get('artwork_override_mime') or track.get('artwork_mime') or 'image/jpeg'
                    artwork_data = artwork.read_bytes()
                    encoded = base64.b64encode(artwork_data).decode('ascii')
                    artwork_source = f'data:{mime};base64,{encoded}'
                else:
                    artwork_fallbacks.append(track)
                    artwork_source = ''
                performance = self.performance_path(track)
                performance_data_bytes = performance.read_bytes() if performance else b''
                performance_data = base64.b64encode(performance_data_bytes).decode('ascii') if performance else ''
                performance_mime = track.get('performance_image_mime') or 'image/jpeg'
                card_data = {'rank': track['rank'], 'layout': 'alternate' if index % 2 else 'default',
                             'title': track.get('card_title', track.get('title', '')),
                             'artist': track.get('card_artist', track.get('primary_artist') or track.get('artist', '').split(',')[0].strip()),
                             'year': track.get('year', ''),
                             'description': track.get('description', ''), 'artwork': artwork_source,
                             'performanceImage': f'data:{performance_mime};base64,{performance_data}' if performance_data else '',
                             'performanceMime': performance_mime if performance_data else ''}
                media = {'artwork': hashlib.sha256(artwork_data).hexdigest() if artwork else '',
                         'performance': hashlib.sha256(performance_data_bytes).hexdigest() if performance else ''}
                page.evaluate('(data) => window.renderCard(data)', card_data)
                overflow = page.evaluate('() => window.cardOverflow()')
                if overflow:
                    self.progress(f'Keeping adjusted text inside card {index + 1}: {", ".join(overflow)}', index, len(tracks))
                output = self._render_cache_file('card', template, card_data, media)
                if output.is_file():
                    with self.lock:
                        if self.job['running']: self.job['cache']['render_hits'] += 1
                else:
                    with self.lock:
                        if self.job['running']: self.job['cache']['render_misses'] += 1
                    output = self._cache_rendered_image(page, output)
                if not output.is_file():
                    raise ValueError(f'Could not render card for #{track["rank"]} {track["title"]}.')
                cards.append(render_layers(page, output, self.check_cancelled)
                             if motion and performance else output)
                for frame_index, frame in enumerate(track.get('visual_frames') or []):
                    self.check_cancelled()
                    text_data = {'text': frame['text']}
                    text_output = self._render_cache_file('text', text_template, text_data, {})
                    if text_output.is_file():
                        with self.lock:
                            if self.job['running']: self.job['cache']['render_hits'] += 1
                    else:
                        with self.lock:
                            if self.job['running']: self.job['cache']['render_misses'] += 1
                        text_page.evaluate('(data) => window.renderTextCard(data)', text_data)
                        text_output = self._cache_rendered_image(text_page, text_output)
                    if not text_output.is_file():
                        raise ValueError(f'Could not render a text frame for #{track["rank"]} {track["title"]}.')
                    text_cards[(index, frame_index)] = text_output
                self.progress(f'Rendered card {index + 1} of {len(tracks)}: {track["title"]}', index + 1, len(tracks))
        self._rendered_text_cards = text_cards
        return cards, artwork_fallbacks

    def build_video(self, mode):
        from pydub import AudioSegment
        snapshot = copy.deepcopy(self.project)
        self._current_zoom_amount = max(0, min(.25, float(snapshot['settings'].get('video_zoom', ZOOM_AMOUNT))))
        # Fingerprint the implementation automatically so later timing comparisons
        # can distinguish rendering changes without relying on manual version bumps.
        pipeline_sources = [Path(__file__), Path(__file__).with_name('motion.py'),
                            self.card_template, self.text_card_template]
        video_stats = {
            'implementation': hashlib.sha256(b''.join(path.read_bytes() for path in pipeline_sources)).hexdigest()[:16],
            'animation_type': 'photo_zoom' if snapshot['settings'].get('video_animation', False) else 'none',
            'zoom_amount': ZOOM_AMOUNT if snapshot['settings'].get('video_animation', False) else 0,
            'filter_threads': 4,
            'encoder_global_quality': 23,
            'reuse_enabled': True,
            'implementation_sources': [os.path.relpath(path, self.root) for path in pipeline_sources],
        }
        configured_limit = snapshot['settings'].get('video_preview_count', 0)
        preview_limit = configured_limit if configured_limit > 0 else None
        self.progress('Preparing audio clips…', 0, 0)
        phase_started = time.monotonic()
        combined, tracks, cues, skipped = self._assemble(snapshot, mode, preview_limit)
        self.timing('Audio preparation and assembly', time.monotonic() - phase_started)
        temp_dir = self.output_dir / f'.video-{mode}-{secrets.token_hex(6)}'
        temp_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f'countdown-{mode}.mp4'
        published = output
        audio_output = self.output_dir / f'countdown-{mode}.mp3'
        audio_published = audio_output
        audio_temporary = self.output_dir / f'.countdown-{mode}-audio-building.mp3'
        temporary = self.output_dir / f'.countdown-{mode}-building.mp4'
        try:
            animation_state = 'on' if snapshot['settings'].get('video_animation', False) else 'off'
            self.progress(f'Video settings: animation {animation_state}; reuse unchanged video on.', 0, len(tracks))
            self.progress('Rendering video cards…', 0, len(tracks))
            phase_started = time.monotonic()
            cards, artwork_fallbacks = self._render_cards(tracks, cues, temp_dir, motion=snapshot['settings'].get('video_animation', False))
            animated_cards = sum(isinstance(card, dict) for card in cards)
            video_stats.update(animated_cards=animated_cards, static_cards=len(cards) - animated_cards,
                               output_fps=MOTION_FPS if animated_cards else VIDEO_FPS,
                               batch_size=1)
            self.timing('Card rendering', time.monotonic() - phase_started)
            text_cards = getattr(self, '_rendered_text_cards', {})
            audio = temp_dir / 'audio.wav'
            combined.export(audio, format='wav')
            phase_started = time.monotonic()
            self._encode_video_batches(cards, tracks, cues, len(combined), text_cards, audio, temp_dir, temporary,
                                       reuse=True, video_stats=video_stats)
            self.timing('MP4 encoding', time.monotonic() - phase_started)
            phase_started = time.monotonic()
            combined.export(audio_temporary, format='mp3', bitrate='192k')
            # Keep both artifacts private until the video and companion audio
            # have successfully completed. The project metadata is written
            # only after both publications below succeed.
            try:
                audio_temporary.replace(audio_output)
            except PermissionError:
                audio_published = self.output_dir / f'countdown-{mode}-{secrets.token_hex(6)}.mp3'
                audio_temporary.replace(audio_published)
            self.timing('Final audio publishing', time.monotonic() - phase_started)
            try:
                temporary.replace(output)
            except PermissionError:
                # Windows may keep the previous MP4 open while it is playing
                # in the dashboard. Publish this completed build beside it.
                published = self.output_dir / f'countdown-{mode}-{secrets.token_hex(6)}.mp4'
                temporary.replace(published)
        finally:
            temporary.unlink(missing_ok=True)
            audio_temporary.unlink(missing_ok=True)
            shutil.rmtree(temp_dir, ignore_errors=True)
        key = f'video_{mode}'
        with self.lock:
            self.project['exports'][key] = {'file': published.name, 'audio_file': audio_published.name, 'seconds': len(combined) / 1000,
                                             'clips': len(tracks), 'revision': snapshot['revision'], 'created': time.time(),
                                             'skipped': skipped, 'artwork_fallbacks': [
                                                 {'rank': track['rank'], 'title': track['title'], 'artist': track['artist']}
                                             for track in artwork_fallbacks], 'tracks': cues}
            self.save(content_changed=False)
        self.record_video_build_metrics(mode, tracks, combined, published, skipped, snapshot, video_stats=video_stats)
        warning = f' Warning: skipped {len(skipped)} song(s) with missing audio; see the export for the list.' if skipped else ''
        artwork_warning = f' Left artwork blank for {len(artwork_fallbacks)} song(s).' if artwork_fallbacks else ''
        reuse_summary = f' Reused {video_stats["reused_segments"]} songs; encoded {video_stats["encoded_segments"]}.'
        self.progress(f'{"Preview" if mode == "preview" else "Compilation"} video ready: {len(tracks)} clips.' + reuse_summary + warning + artwork_warning, len(tracks), len(tracks))

    def build(self, mode):
        snapshot = copy.deepcopy(self.project)
        combined, tracks, cue_sheet, skipped = self._assemble(snapshot, mode)
        self.output_dir.mkdir(exist_ok=True)
        name = f'countdown-{mode}.mp3'
        temporary = self.output_dir / f'.{mode}-building.mp3'
        self.progress('Encoding MP3…', len(tracks), len(tracks))
        try:
            combined.export(temporary, format='mp3', bitrate='192k')
            temporary.replace(self.output_dir / name)
        finally:
            temporary.unlink(missing_ok=True)
        with self.lock:
            self.project['exports'][mode] = {'file': name, 'seconds': len(combined) / 1000,
                                             'clips': len(tracks), 'revision': snapshot['revision'], 'created': time.time(),
                                             'skipped': skipped, 'tracks': cue_sheet}
            self.save(content_changed=False)
        warning = f' Warning: skipped {len(skipped)} song(s) with missing audio; see the export for the list.' if skipped else ''
        self.progress(f'{"Preview" if mode == "preview" else "Compilation"} ready: {len(tracks)} clips.' + warning, len(tracks), len(tracks))


