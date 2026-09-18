"""Local regression checks. No Spotify login or internet downloads are performed."""
import io
import json
import subprocess
import threading
import base64
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydub import AudioSegment
from pydub.generators import Sine
from mutagen.id3 import APIC, ID3
from PIL import Image

from countdown.studio import Studio, playlist_id, seconds
from countdown.web import create_app


def test_project_paths_and_assets_ignore_working_directory(tmp_path, monkeypatch):
    project_root = tmp_path / 'project'
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv('LOCAL_FILE_DIR', raising=False)
    app = create_app(project_root)
    studio = app.config['STUDIO']
    studio.save()
    assert studio.file == project_root / 'data' / 'countdown_project.json'
    assert studio.audio_dir == project_root / 'data' / 'snippets'
    assert studio.output_dir == project_root / 'exports'
    assert studio.local_dir == project_root / 'local_audio'
    assert Studio(project_root).project == studio.project
    for asset in ('app.js', 'clip-range.js', 'style.css'):
        response = app.test_client().get('/ui/' + asset)
        assert response.status_code == 200
        response.close()
    assert list(elsewhere.iterdir()) == []


def test_write_token_belongs_to_project_and_survives_restart(tmp_path):
    first = create_app(tmp_path / 'first')
    second = create_app(tmp_path / 'second')
    restarted = create_app(tmp_path / 'first')
    token = first.config['WRITE_TOKEN']
    assert restarted.config['WRITE_TOKEN'] == token
    assert second.config['WRITE_TOKEN'] != token
    assert second.test_client().post('/api/settings', json={},
                                    headers={'X-Studio-Token': token}).status_code == 403


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_FILE_DIR', str(tmp_path / 'local_audio'))
    app = create_app(tmp_path)
    app.config['TESTING'] = True
    return app


def post(app, path, data=None, **kwargs):
    return app.test_client().post('/api' + path, json=data,
                                  headers={'X-Studio-Token': app.config['WRITE_TOKEN']}, **kwargs)


def add_track(studio, key='a', selected=True, ready=True, start=.2, duration=1):
    item = dict(key=key, spotify_id=key, file=key + '.mp3', title=key,
                artist='Test', start=start, duration=duration, selected=selected,
                source_url='', rank=len(studio.project['tracks']) + 1)
    studio.project['tracks'].append(item)
    if ready:
        studio.audio_dir.mkdir(exist_ok=True)
        Sine(440).to_audio_segment(duration=2200).apply_gain(-12).export(studio.audio_dir / item['file'], format='mp3').close()
    return item


def test_job_reports_elapsed_and_phase_timings(app):
    studio = app.config['STUDIO']
    finished = threading.Event()

    def work():
        studio.timing('Test phase', .125)
        finished.set()

    studio.start('video build', work)
    assert finished.wait(2)
    for _ in range(20):
        if not studio.view()['job']['running']:
            break
        time.sleep(.01)
    job = studio.view()['job']
    assert not job['running']
    assert job['elapsed_seconds'] >= 0
    assert job['timings']['Test phase'] == .125


def test_empty_page_and_request_boundary(app):
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert b'Playlist to Countdown' in client.get('/').data
    assert client.get('/api/state').json['settings']['duration'] == 10


def test_cancel_endpoint_requires_post_and_returns_operation_error_when_idle(app):
    client = app.test_client()
    assert client.get('/api/cancel').status_code == 405
    response = post(app, '/cancel', {})
    assert response.status_code == 400
    assert 'There is no operation to cancel.' in response.json['error']
    assert client.get('/api/state').json['settings']['preview_context'] == 4
    assert client.post('/api/settings', json={}).status_code == 403
    assert client.get('/api/state', headers={'Host': 'untrusted.example'}).status_code == 403
    assert client.get('/.env').status_code == 404
    assert client.get('/ui/../.env').status_code == 404
    assert client.get('/api/export/../../.env').status_code == 404


def test_thumbnail_grid_deduplicates_artwork_and_downloads(app):
    studio = app.config['STUDIO']
    studio.artwork_dir.mkdir(parents=True)
    colors = ('#e63946', '#457b9d', '#e63946', '#f4a261')
    for index, color in enumerate(colors):
        track = add_track(studio, key=f'cover-{index}', ready=False)
        filename = f'cover-{index}.png'
        if index == 2:
            (studio.artwork_dir / filename).write_bytes((studio.artwork_dir / 'cover-0.png').read_bytes())
        else:
            Image.new('RGB', (80 + index, 100), color).save(studio.artwork_dir / filename)
        track['artwork_file'] = filename

    response = post(app, '/build-thumbnail', {})
    assert response.status_code == 200
    thumbnail = response.json['exports']['thumbnail']
    assert thumbnail['covers'] == 3
    assert thumbnail['width'] == 1920
    assert thumbnail['height'] == 1080
    assert thumbnail['columns'] * thumbnail['rows'] >= 3
    path = studio.output_dir / thumbnail['file']
    assert Image.open(path).size == (1920, 1080)

    download = app.test_client().get('/api/export-thumbnail?download=1')
    assert download.status_code == 200
    assert download.mimetype == 'image/png'
    assert 'attachment' in download.headers['Content-Disposition']


def test_timestamp_and_playlist_parsing():
    assert seconds('1:23.50') == 83.5
    assert seconds('1:02:03') == 3723
    assert seconds('') == 0
    sid = '3CdZ1kHptIn7okYqV6NFxd'
    assert playlist_id(f'https://open.spotify.com/playlist/{sid}?si=example') == sid
    assert playlist_id('spotify:playlist:' + sid) == sid
    for value in ('nan', '-1', '1:2:3:4'):
        with pytest.raises(ValueError):
            seconds(value)
    with pytest.raises(ValueError):
        playlist_id('https://other.example/playlist/' + sid)


def test_edit_persistence_and_invalid_input(app):
    studio = app.config['STUDIO']
    add_track(studio, ready=False)
    response = post(app, '/track/a', {'start': '1:23.5', 'duration': '', 'selected': True})
    assert response.status_code == 200
    loaded = Studio(studio.root)
    assert loaded.project['tracks'][0]['start'] == 83.5
    assert loaded.project['tracks'][0]['duration'] is None
    assert loaded.project['tracks'][0]['selected'] is True
    before = studio.file.read_bytes()
    for value in ('nan', -1, 999):
        assert post(app, '/settings', {'duration': value, 'crossfade': 2}).status_code == 400
    assert studio.file.read_bytes() == before
    response = post(app, '/settings', {'duration': 10, 'crossfade': 2, 'preview_context': 4})
    assert response.status_code == 200
    assert response.json['settings']['preview_context'] == 4
    assert post(app, '/track/a', {'start': 0, 'source_url': 'https://evil.example/foo'}).status_code == 400
    assert post(app, '/track/a', {'start': 0, 'duration': 0}).status_code == 400


def test_rejected_track_update_does_not_mutate_live_state(app):
    studio = app.config['STUDIO']
    track = add_track(studio, ready=False, start=4, duration=2)
    track['description'] = 'Original'
    studio.save()
    response = post(app, '/track/a', {
        'start': 9, 'duration': 2, 'selected': True,
        'description': 'Should not survive',
        'visual_frames': [{'start': 0, 'end': 3, 'text': 'outside clip'}],
    })
    assert response.status_code == 400
    assert track['start'] == 4
    assert track['description'] == 'Original'
    assert Studio(studio.root).project['tracks'][0]['start'] == 4


def test_card_copy_fields_are_editable_and_allow_blanks(app):
    studio = app.config['STUDIO']
    track = add_track(studio, ready=False)
    track.update(primary_artist='Primary Artist', card_title='Imported title',
                 card_artist='Primary Artist', year='2024')

    response = post(app, '/track/a', {
        'start': 0, 'selected': True, 'card_title': 'Custom title',
        'card_artist': 'Custom artist', 'year': '1999',
    })
    assert response.status_code == 200
    loaded = Studio(studio.root).project['tracks'][0]
    assert (loaded['card_title'], loaded['card_artist'], loaded['year']) == ('Custom title', 'Custom artist', '1999')
    assert loaded['year_customized'] is True

    response = post(app, '/track/a', {
        'start': 0, 'selected': True, 'card_title': '', 'card_artist': '', 'year': '',
    })
    assert response.status_code == 200
    assert response.json['tracks'][0]['card_title'] == ''
    assert response.json['tracks'][0]['card_artist'] == ''
    assert response.json['tracks'][0]['year'] == ''
    assert post(app, '/track/a', {'start': 0, 'year': '99'}).status_code == 400


def test_playlist_reload_preserves_choices_for_existing_songs(app, monkeypatch):
    def song(key, title=None):
        return {'track': {'id': key, 'name': title or key, 'type': 'track',
                          'artists': [{'name': 'Primary'}, {'name': 'Guest'}],
                          'album': {'release_date': '2023-04-12'}}}

    spotify = Mock()
    spotify.playlist.side_effect = [
        {'name': 'First playlist', 'tracks': {'items': [song('a'), song('b')], 'next': None}},
        {'name': 'Swapped playlist', 'tracks': {'items': [song('b', 'Updated B'), song('c')], 'next': None}},
        {'name': 'Temporary playlist', 'tracks': {'items': [song('c')], 'next': None}},
        {'name': 'Restored playlist', 'tracks': {'items': [song('b', 'Back Again')], 'next': None}},
    ]
    monkeypatch.setattr('spotipy.Spotify', lambda **_: spotify)
    studio = app.config['STUDIO']

    studio.sync('first-playlist-id', object())
    saved = next(track for track in studio.project['tracks'] if track['spotify_id'] == 'b')
    saved.update(start=42.3, duration=8.7, selected=True, source_url='https://youtu.be/example')
    saved.update(card_title='My title', card_artist='My artist', year='1997', year_customized=True)
    saved_file = saved['file']
    saved_key = saved['key']
    studio.save()

    studio.sync('swapped-playlist-id', object())
    preserved = next(track for track in studio.project['tracks'] if track['spotify_id'] == 'b')
    new = next(track for track in studio.project['tracks'] if track['spotify_id'] == 'c')
    assert preserved['key'] == saved_key
    assert preserved['file'] == saved_file
    assert preserved['start'] == 42.3
    assert preserved['duration'] == 8.7
    assert preserved['selected'] is True
    assert preserved['source_url'] == 'https://youtu.be/example'
    assert preserved['card_title'] == 'My title'
    assert preserved['card_artist'] == 'My artist'
    assert preserved['year'] == '1997'
    assert new['card_artist'] == 'Primary'
    assert new['year'] == '2023'
    assert new['start'] == 0
    assert new['duration'] is None
    assert new['selected'] is False

    studio.sync('temporary-playlist-id', object())
    studio.sync('restored-playlist-id', object())
    restored = studio.project['tracks'][0]
    assert restored['spotify_id'] == 'b'
    assert restored['start'] == 42.3
    assert restored['duration'] == 8.7
    assert restored['selected'] is True


def test_playlist_reload_preserves_local_choices_without_spotify_id(app, monkeypatch):
    def song(song_id, title):
        return {'track': {'id': song_id, 'name': title, 'type': 'track',
                          'artists': [{'name': 'Local Artist'}]}}

    spotify = Mock()
    spotify.playlist.side_effect = [
        {'name': 'First playlist', 'tracks': {'items': [song(None, 'Local Song'), song('new', 'New Song')], 'next': None}},
        {'name': 'Updated playlist', 'tracks': {'items': [song('new', 'New Song'), song(None, 'Local Song')], 'next': None}},
    ]
    monkeypatch.setattr('spotipy.Spotify', lambda **_: spotify)
    studio = app.config['STUDIO']

    studio.sync('first-playlist-id', object())
    saved = next(track for track in studio.project['tracks'] if track['title'] == 'Local Song')
    saved.update(file='custom-local.mp3', start=18.4, duration=6.2, selected=True,
                 source_url='https://youtu.be/local-replacement',
                 visual_frames=[{'type': 'text', 'start': 0.5, 'end': 1.5, 'text': 'Saved note'}])
    saved_key = saved['key']
    studio.save()

    studio.sync('updated-playlist-id', object())
    restored = next(track for track in studio.project['tracks'] if track['title'] == 'Local Song')
    assert restored['key'] == saved_key
    assert restored['file'] == 'custom-local.mp3'
    assert restored['start'] == 18.4
    assert restored['duration'] == 6.2
    assert restored['selected'] is True
    assert restored['source_url'] == 'https://youtu.be/local-replacement'
    assert restored['visual_frames'][0]['text'] == 'Saved note'


def test_playlist_reload_gives_duplicate_occurrences_distinct_keys(app, monkeypatch):
    item = {'track': {'id': 'same-id', 'name': 'Repeated', 'type': 'track',
                      'artists': [{'name': 'Artist'}], 'album': {}}}
    spotify = Mock()
    spotify.playlist.return_value = {'name': 'Duplicates', 'tracks': {'items': [item, item], 'next': None}}
    monkeypatch.setattr('spotipy.Spotify', lambda **_: spotify)
    studio = app.config['STUDIO']
    studio.sync('duplicate-playlist-id', object())
    keys = [track['key'] for track in studio.project['tracks']]
    assert len(keys) == 2
    assert len(set(keys)) == 2


def test_playlist_reload_backfills_an_unset_spotify_year(app, monkeypatch):
    spotify = Mock()
    spotify.playlist.side_effect = [
        {'name': 'Playlist', 'tracks': {'items': [{'track': {
            'id': 'a', 'name': 'Song', 'type': 'track',
            'artists': [{'name': 'Primary'}, {'name': 'Guest'}],
            'album': {}}}], 'next': None}},
        {'name': 'Playlist', 'tracks': {'items': [{'track': {
            'id': 'a', 'name': 'Song', 'type': 'track',
            'artists': [{'name': 'Primary'}, {'name': 'Guest'}],
            'album': {'release_date': '2020-03'}}}], 'next': None}},
    ]
    monkeypatch.setattr('spotipy.Spotify', lambda **_: spotify)
    studio = app.config['STUDIO']

    studio.sync('first-playlist-id', object())
    assert studio.project['tracks'][0]['year'] == ''
    studio.sync('first-playlist-id', object())
    track = studio.project['tracks'][0]
    assert track['card_artist'] == 'Primary'
    assert track['year'] == '2020'
    assert track['year_customized'] is False


def test_legacy_import_endpoint_is_removed(app):
    studio = app.config['STUDIO']
    response = post(app, '/import', {})
    assert response.status_code == 404
    assert studio.project['tracks'] == []


def test_local_mp3_embedded_artwork_is_cached(app):
    studio = app.config['STUDIO']
    studio.local_dir.mkdir()
    audio_path = studio.local_dir / 'Local Artist-Local Song.mp3'
    Sine(440).to_audio_segment(duration=1000).export(audio_path, format='mp3').close()
    cover = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
        'YAAAAAYAAjCB0C8AAAAASUVORK5CYII='
    )
    tags = ID3(str(audio_path))
    tags.add(APIC(encoding=3, mime='image/png', type=3, data=cover))
    tags.save(str(audio_path))
    track = {'key': 'local', 'file': audio_path.name, 'artwork_file': ''}

    artwork = studio.artwork_path(track)

    assert artwork and artwork.read_bytes() == cover
    assert track['artwork_mime'] == 'image/png'
    assert track['artwork_file'].startswith('embedded-')


def test_audio_range_and_real_waveform(app):
    studio = app.config['STUDIO']
    add_track(studio)
    response = app.test_client().get('/api/audio/a', headers={'Range': 'bytes=0-99'})
    assert response.status_code == 206
    assert len(response.data) == 100
    response.close()
    wave = app.test_client().get('/api/wave/a')
    assert wave.status_code == 200
    assert len(wave.json['peaks']) == 500
    assert max(wave.json['peaks']) == 1


def test_build_real_mp3_crossfade_and_stale_export(app):
    studio = app.config['STUDIO']
    add_track(studio, 'a')
    add_track(studio, 'b')
    studio.project['settings']['crossfade'] = .25
    studio.save()
    revision = studio.project['revision']
    studio.build('final')
    result = AudioSegment.from_file(studio.output_dir / 'countdown-final.mp3')
    assert abs(len(result) - 1750) <= 30
    assert result.channels == 2
    assert result.frame_rate == 44100
    assert result.max_dBFS < 0
    assert studio.project['revision'] == revision
    assert Studio(studio.root).project['revision'] == revision
    assert studio.project['exports']['final']['revision'] == revision
    cues = studio.project['exports']['final']['tracks']
    assert [cue['start'] for cue in cues] == [0, .75]
    assert [cue['end'] for cue in cues] == [1, 1.75]
    assert [cue['key'] for cue in cues] == ['a', 'b']
    assert not (studio.data_dir / 'playlist_prev.json').exists()
    response = app.test_client().get('/api/export/final?download=1')
    assert response.status_code == 200
    assert 'attachment' in response.headers['Content-Disposition']
    response.close()
    post(app, '/settings', {'duration': 10, 'crossfade': 0})
    assert studio.project['exports']['final']['revision'] != studio.project['revision']


def test_build_reuses_cached_prepared_clips(app, monkeypatch):
    studio = app.config['STUDIO']
    add_track(studio, 'a')
    add_track(studio, 'b')
    studio.build('preview')

    def should_not_prepare_again(*args, **kwargs):
        raise AssertionError('cached build unexpectedly re-prepared a clip')

    monkeypatch.setattr(studio, '_clip_piece', should_not_prepare_again)
    studio.build('preview')
    assert studio.project['exports']['preview']['clips'] == 2


def test_video_song_limit_applies_before_final_video_clips_are_prepared(app):
    studio = app.config['STUDIO']
    add_track(studio, 'first')
    add_track(studio, 'second')
    add_track(studio, 'third')
    add_track(studio, 'fourth')

    _, tracks, cues, skipped = studio._assemble(studio.project, 'final', preview_limit=3)

    assert [track['key'] for track in tracks] == ['first', 'second', 'third']
    assert [cue['key'] for cue in cues] == ['first', 'second', 'third']
    assert skipped == []


def test_transition_preview_requires_and_uses_kept_neighbors(app):
    studio = app.config['STUDIO']
    add_track(studio, 'before', start=.1, duration=1, selected=True)
    current = add_track(studio, 'current', start=.2, duration=1, selected=False)
    add_track(studio, 'after', start=.3, duration=1, selected=True)

    preview = studio.transition_preview(current['key'])
    assert preview.exists()
    assert len(AudioSegment.from_file(preview)) >= 900

    studio.project['tracks'][0]['selected'] = False
    with pytest.raises(ValueError, match='before and after'):
        studio.transition_preview(current['key'])


def test_replace_audio_uses_saved_youtube_link_without_resetting_clip(app, monkeypatch):
    studio = app.config['STUDIO']
    track = add_track(studio, 'replace-me', selected=True, start=12.3, duration=7.4)
    track['source_url'] = 'https://www.youtube.com/watch?v=example'
    original = (studio.audio_dir / track['file']).read_bytes()
    monkeypatch.setattr('countdown.studio.shutil.which', lambda _: 'node')

    def run(command, **kwargs):
        output = command[command.index('--output') + 1].replace('%%', '%')
        Path(output).write_bytes(original)
        return SimpleNamespace(returncode=0, stderr=b'', stdout=b'')

    monkeypatch.setattr('countdown.studio.subprocess.run', run)
    studio.replace_audio(track['key'])

    assert track['start'] == 12.3
    assert track['duration'] == 7.4
    assert track['selected'] is True
    assert track['audio_revision']
    assert (studio.audio_dir / track['file']).read_bytes() == original


def test_replace_endpoint_does_not_reject_its_own_background_job(app, monkeypatch):
    studio = app.config['STUDIO']
    track = add_track(studio, 'replace-endpoint', selected=True)
    track['source_url'] = 'https://www.youtube.com/watch?v=example'
    original = (studio.audio_dir / track['file']).read_bytes()
    monkeypatch.setattr('countdown.studio.shutil.which', lambda _: 'node')

    def run(command, **kwargs):
        output = command[command.index('--output') + 1].replace('%%', '%')
        Path(output).write_bytes(original)
        return SimpleNamespace(returncode=0, stderr=b'', stdout=b'')

    monkeypatch.setattr('countdown.studio.subprocess.run', run)
    response = post(app, f'/replace/{track["key"]}', {})
    assert response.status_code == 200

    for _ in range(50):
        if not studio.job['running']:
            break
        time.sleep(.01)
    assert studio.job['errors'] == []
    assert studio.job['message'].startswith('Replaced audio for')


def test_build_requires_review_but_skips_missing_audio(app):
    studio = app.config['STUDIO']
    add_track(studio, selected=False)
    with pytest.raises(ValueError, match='every clip'):
        studio.build('final')
    with pytest.raises(ValueError, match='at least one'):
        studio.build('preview')
    studio.project['tracks'][0]['selected'] = True
    add_track(studio, 'b', selected=False, ready=False)
    studio.build('preview')
    assert studio.project['exports']['preview']['clips'] == 1
    studio.build('final')
    assert studio.project['exports']['final']['clips'] == 1
    assert studio.project['exports']['final']['skipped'][0]['title'] == 'b'
    studio.project['tracks'][1]['selected'] = True
    studio.build('preview')
    assert studio.project['exports']['preview']['clips'] == 1
    assert studio.project['exports']['preview']['skipped'][0]['title'] == 'b'
    assert 'Warning: skipped 1' in studio.job['message']
    studio.project['tracks'].pop()
    studio.project['tracks'][0]['start'] = 999
    with pytest.raises(ValueError, match='past the end'):
        studio.build('preview')


@pytest.mark.parametrize('mode', ['preview', 'final'])
def test_all_missing_keeps_previous_export(app, mode):
    studio = app.config['STUDIO']
    add_track(studio)
    studio.build(mode)
    path = studio.output_dir / f'countdown-{mode}.mp3'
    previous = path.read_bytes()
    metadata = dict(studio.project['exports'][mode])
    (studio.audio_dir / 'a.mp3').unlink()
    with pytest.raises(ValueError, match='No audio is available'):
        studio.build(mode)
    assert path.read_bytes() == previous
    assert studio.project['exports'][mode] == metadata


def test_missing_middle_track_adds_no_silence_and_persists_warning(app):
    studio = app.config['STUDIO']
    add_track(studio, 'a')
    add_track(studio, 'missing', ready=False)
    add_track(studio, 'c')
    studio.project['settings']['crossfade'] = .25
    studio.build('final')
    result = AudioSegment.from_file(studio.output_dir / 'countdown-final.mp3')
    assert abs(len(result) - 1750) <= 30
    export = Studio(studio.root).project['exports']['final']
    assert export['clips'] == 2
    assert [song['title'] for song in export['skipped']] == ['missing']
    assert [song['key'] for song in export['tracks']] == ['a', 'c']
    assert [song['start'] for song in export['tracks']] == [0, .75]
    studio.project['tracks'][0]['title'] = 'Edited later'
    assert export['tracks'][0]['title'] == 'a'


def test_short_clips_and_silence_padding(app):
    studio = app.config['STUDIO']
    add_track(studio, 'a', start=2, duration=.5)
    add_track(studio, 'b', duration=.5)
    studio.project['settings']['crossfade'] = 2
    studio.build('final')
    result = AudioSegment.from_file(studio.output_dir / 'countdown-final.mp3')
    assert 490 <= len(result) <= 530
    assert studio.project['exports']['final']['tracks'][1]['start'] == .001


def test_upload_verifies_audio_and_invalidates_review(app):
    studio = app.config['STUDIO']
    add_track(studio, ready=False)
    data = io.BytesIO()
    Sine(220).to_audio_segment(duration=1000).export(data, format='mp3')
    data.seek(0)
    response = app.test_client().post('/api/upload/a', data={'audio': (data, 'replacement.mp3')},
                                    headers={'X-Studio-Token': app.config['WRITE_TOKEN']})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['ready'] and not item['selected'] and item['audio_revision']
    assert item['file'] == 'custom-a.mp3'


def test_performance_image_url_and_upload_are_cached_per_track(app, monkeypatch):
    studio = app.config['STUDIO']
    add_track(studio, ready=False)
    jpeg = b'\xff\xd8\xff' + b'performance-test'

    class Headers:
        def get_content_type(self): return 'image/jpeg'

    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return jpeg

    monkeypatch.setattr('countdown.studio.urllib.request.urlopen', lambda request, timeout: Response())
    response = post(app, '/performance-image/a/url', {'url': 'https://images.example/performance.jpg'})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['performance_image_ready']
    assert item['performance_image_url'] == 'https://images.example/performance.jpg'
    assert (studio.performance_dir / item['performance_image_file']).read_bytes() == jpeg

    response = post(app, '/performance-image/a/remove', {})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert not item['performance_image_ready']
    assert item['performance_image_url'] == ''
    assert item['performance_image_file'] == ''
    assert not list(studio.performance_dir.glob('performance-a.*'))

    png_bytes = b'\x89PNG\r\n\x1a\nperformance-test'
    png = io.BytesIO(png_bytes)
    response = app.test_client().post('/api/performance-image/a/upload', data={'image': (png, 'replacement.png')},
                                       headers={'X-Studio-Token': app.config['WRITE_TOKEN']})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['performance_image_ready']
    assert item['performance_image_url'] == ''
    assert (studio.performance_dir / item['performance_image_file']).read_bytes() == png_bytes


def test_restore_album_artwork_keeps_original_and_removes_override(app, monkeypatch):
    studio = app.config['STUDIO']
    track = add_track(studio, ready=False)
    studio.artwork_dir.mkdir(parents=True, exist_ok=True)
    original = studio.artwork_dir / 'spotify-original.jpg'
    original.write_bytes(b'original')
    track['artwork_file'] = original.name
    jpeg = b'\xff\xd8\xff' + b'override'

    class Headers:
        def get_content_type(self): return 'image/jpeg'

    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return jpeg

    monkeypatch.setattr('countdown.studio.urllib.request.urlopen', lambda request, timeout: Response())
    assert post(app, '/artwork/a/url', {'url': 'https://images.example/cover.jpg'}).status_code == 200
    assert studio.artwork_path(track).name.startswith('artwork-override-a')

    response = post(app, '/artwork/a/restore', {})

    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['artwork_override_file'] == ''
    assert item['artwork_override_url'] == ''
    assert studio.artwork_path(track) == original
    assert original.read_bytes() == b'original'
    assert not list(studio.artwork_dir.glob('artwork-override-a.*'))


@pytest.mark.parametrize(
    ('failure_kind', 'expected'),
    [
        ('http', 'HTTP 403 (Forbidden)'),
        ('timeout', 'timed out after 20 seconds'),
        ('refused', 'refused or blocked'),
    ],
)
def test_performance_image_url_reports_specific_download_failures(app, monkeypatch, failure_kind, expected):
    add_track(app.config['STUDIO'], ready=False)
    failures = {
        'http': urllib.error.HTTPError('https://images.example/performance.jpg', 403, 'Forbidden', {}, None),
        'timeout': urllib.error.URLError(TimeoutError('timed out')),
        'refused': urllib.error.URLError(ConnectionRefusedError('connection refused')),
    }
    monkeypatch.setattr('countdown.studio.urllib.request.urlopen', Mock(side_effect=failures[failure_kind]))

    response = post(app, '/performance-image/a/url', {'url': 'https://images.example/performance.jpg'})

    assert response.status_code == 400
    assert expected in response.json['error']


def test_album_artwork_override_url_and_upload_are_cached_per_track(app, monkeypatch):
    studio = app.config['STUDIO']
    add_track(studio, ready=False)
    jpeg = b'\xff\xd8\xff' + b'artwork-test'

    class Headers:
        def get_content_type(self): return 'image/jpeg'

    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return jpeg

    monkeypatch.setattr('countdown.studio.urllib.request.urlopen', lambda request, timeout: Response())
    response = post(app, '/artwork/a/url', {'url': 'https://images.example/cover.jpg'})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['artwork_ready']
    assert item['artwork_override_url'] == 'https://images.example/cover.jpg'
    assert (studio.artwork_dir / item['artwork_override_file']).read_bytes() == jpeg

    png_bytes = b'\x89PNG\r\n\x1a\nartwork-test'
    png = io.BytesIO(png_bytes)
    response = app.test_client().post('/api/artwork/a/upload', data={'image': (png, 'replacement.png')},
                                       headers={'X-Studio-Token': app.config['WRITE_TOKEN']})
    assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['artwork_ready']
    assert item['artwork_override_url'] == ''
    assert (studio.artwork_dir / item['artwork_override_file']).read_bytes() == png_bytes


def test_artwork_url_accepts_valid_image_with_generic_content_type_and_replacements(app, monkeypatch):
    studio = app.config['STUDIO']
    add_track(studio, ready=False)
    jpeg = b'\xff\xd8\xff' + b'cdn-image'

    class Headers:
        def get_content_type(self):
            return 'application/octet-stream'

    class Response:
        headers = Headers()
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return jpeg

    monkeypatch.setattr('countdown.studio.urllib.request.urlopen', lambda request, timeout: Response())
    for index in range(6):
        response = post(app, '/artwork/a/url', {'url': f'https://cdn.example/cover-{index}'})
        assert response.status_code == 200
    item = response.json['tracks'][0]
    assert item['artwork_ready']
    assert item['artwork_override_url'].endswith('cover-5')


def test_jobs_block_edits_and_release_after_failure(app):
    studio = app.config['STUDIO']
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(3)
        raise ValueError('Sample failure')
    studio.start('test', work)
    assert entered.wait(1)
    assert post(app, '/settings', {'duration': 10, 'crossfade': 2}).status_code == 400
    with pytest.raises(ValueError):
        studio.start('another', lambda: None)
    release.set()
    import time
    for _ in range(100):
        if not studio.job['running']:
            break
        time.sleep(.01)
    assert not studio.job['running']
    assert studio.job['errors'] == ['Sample failure']


@pytest.mark.parametrize('field', ['track', 'item'])
def test_spotify_countdown_pagination_and_new_payload(app, monkeypatch, field):
    studio = app.config['STUDIO']
    def song(name):
        return {field: {'id': name, 'name': name, 'type': 'track', 'artists': [{'name': 'Artist'}]}}
    sp = Mock()
    sp.playlist.return_value = {'name': 'Ranked', 'items': {'items': [song('first')], 'next': 'next'}}
    sp.next.return_value = {'items': [song('second')], 'next': None}
    monkeypatch.setattr('spotipy.Spotify', lambda **_: sp)
    studio.sync('id', object())
    assert [t['title'] for t in studio.project['tracks']] == ['second', 'first']
    assert [t['rank'] for t in studio.project['tracks']] == [2, 1]
    assert all(t['duration'] is None and not t['selected'] for t in studio.project['tracks'])


def test_download_starts_at_first_missing_song_and_reports_failures(app, monkeypatch):
    studio = app.config['STUDIO']
    add_track(studio, 'first', ready=False)
    add_track(studio, 'second', ready=False)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr('countdown.studio.shutil.which', lambda _: 'node')
    studio.download()
    assert len(commands) == 2
    assert 'first' in commands[0][-1]
    assert commands[0][-2] == '--'
    assert len(studio.job['errors']) == 2
    assert '--js-runtimes' in commands[0]


def test_download_error_explains_common_failures():
    assert 'not Spotify authentication' in Studio._download_error('ERROR: HTTP Error 403: Forbidden')
    assert 'challenge solving' in Studio._download_error('No supported JavaScript runtime could be found')
    assert 'network' in Studio._download_error('WinError 10013: socket access forbidden')
    assert 'specific video link' in Studio._download_error('Requested format is not available')


def test_oauth_state_and_callback(app, monkeypatch):
    studio = app.config['STUDIO']
    oauth = Mock()
    oauth.get_cached_token.return_value = None
    oauth.get_authorize_url.return_value = 'https://accounts.spotify.com/authorize'
    monkeypatch.setattr(studio, 'oauth', lambda: oauth)
    response = post(app, '/playlist', {'url': '3CdZ1kHptIn7okYqV6NFxd'})
    assert response.status_code == 401
    assert response.json['redirect_uri'] == 'http://127.0.0.1:8765/callback'
    assert post(app, '/auth', {'url': 'http://127.0.0.1:8765/callback?code=a&state=wrong'}).status_code == 400
    response = app.test_client().get('/callback', query_string={'code': 'test', 'state': studio.oauth_state})
    assert response.status_code == 302
    assert app.test_client().get('/api/auth/status').json['connected']
    oauth.get_access_token.assert_called_once_with('test', check_cache=False)
