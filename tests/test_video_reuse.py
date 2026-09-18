"""Incremental video regression tests; every encode uses animation off."""
import json
import subprocess

import pytest
from PIL import Image
from pydub import AudioSegment

from countdown.studio import Studio, JobCancelled
from countdown.web import create_app


def test_reuse_is_automatic_and_not_a_project_setting(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    monkeypatch.setattr(studio, 'start', lambda *args: None)
    client = app.test_client()
    headers = {'X-Studio-Token': app.config['WRITE_TOKEN']}
    assert 'video_reuse' not in studio.project['settings']
    assert client.post('/api/settings', json=dict(duration=10, crossfade=1, video_animation=False), headers=headers).status_code == 200
    assert 'video_reuse' not in Studio(tmp_path).project['settings']
    assert client.post('/api/build-video', json=dict(mode='final', video_animation=False), headers=headers).status_code == 200
    assert 'video_reuse' not in studio.project['settings']


def test_static_segment_reuse_edit_timing_corruption_and_bypass(tmp_path):
    studio = Studio(tmp_path)
    cards = []
    for index, color in enumerate(('red', 'green', 'blue')):
        path = tmp_path / f'card-{index}.png'
        Image.new('RGB', (1920, 1080), color).save(path)
        cards.append(path)
    tracks = [dict(rank=3-i, title=str(i), visual_frames=[]) for i in range(3)]
    cues = [dict(start=i*.6, end=(i+1)*.6) for i in range(3)]
    audio = tmp_path / 'audio.wav'
    AudioSegment.silent(duration=1800, frame_rate=44100).set_channels(2).export(audio, format='wav')
    text_cards = {}

    def build(name, expected, reuse=True):
        folder = tmp_path / name
        folder.mkdir()
        result = folder / 'result.mp4'
        stats = {}
        studio._encode_video_batches(cards, tracks, cues, 1800, text_cards, audio, folder, result, reuse=reuse, video_stats=stats)
        assert (stats['reused_segments'], stats['encoded_segments']) == expected
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(result)]))
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert video['r_frame_rate'] == '30/1' and int(video['nb_frames']) == 54
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(result), '-f', 'null', '-'], check=True, capture_output=True)
        return result

    initial = build('cold', (0, 3))
    repeat = build('warm', (3, 0))
    def frames(path):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-f', 'framemd5', '-'])
    assert frames(initial) == frames(repeat)
    Image.new('RGB', (1920, 1080), 'yellow').save(cards[1])
    edited = build('edit', (2, 1))
    assert frames(initial) != frames(edited)
    text_image = tmp_path / 'text.png'
    Image.new('RGB', (1920, 1080), 'purple').save(text_image)
    tracks[1]['visual_frames'] = [dict(start=.1, end=.4, text='Text insert')]
    text_cards[(1, 0)] = text_image
    build('text-insert', (2, 1))
    tracks[1]['visual_frames'][0]['end'] = .5
    build('text-timing', (2, 1))
    # The middle boundary moves; only the first two visible durations change.
    cues[0]['end'] += .2
    cues[1]['start'] += .2
    build('timing', (1, 2))
    # Damage every entry; no bad cached stream can leak into the export.
    for path in (studio.cache_dir / 'video-segments').glob('*.mp4'):
        path.write_bytes(b'broken')
    build('corrupt', (0, 3))
    build('repaired', (3, 0))
    build('bypass', (0, 3), reuse=False)


def test_segment_signature_covers_text_motion_and_encoder(tmp_path):
    studio = Studio(tmp_path)
    image = tmp_path / 'image.png'
    image.write_bytes(b'first image')
    def key(graph='timing', encoder='encoder', implementation='v1'):
        return studio._video_segment_cache(['-i', str(image)], graph, [encoder], implementation)
    baseline = key()
    assert key(graph='changed text timing') != baseline
    assert key(encoder='other encoder') != baseline
    assert key(implementation='v2') != baseline
    image.write_bytes(b'changed foreground')
    assert key() != baseline


def test_cancelled_encode_does_not_publish_cache(tmp_path, monkeypatch):
    studio = Studio(tmp_path)
    image = tmp_path / 'image.png'
    image.write_bytes(b'image')
    def cancel(command, output, timeout):
        output.write_bytes(b'incomplete')
        raise JobCancelled()
    monkeypatch.setattr(studio, '_run_video_command', cancel)
    with pytest.raises(JobCancelled):
        studio._encode_video_batches([image], [dict(rank=1, title='Test')], [dict(start=0, end=1)],
                                     1000, {}, tmp_path/'audio.wav', tmp_path, tmp_path/'result.mp4', reuse=True)
    assert not list((studio.cache_dir/'video-segments').glob('*'))
