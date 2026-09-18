"""Exercise the actual browser layers and FFmpeg's subpixel sampler."""
import copy
import subprocess

from PIL import Image, ImageDraw

from countdown.studio import Studio
from countdown.motion import ZOOM_AMOUNT


def test_motion_zoom_amount_is_five_percent():
    assert ZOOM_AMOUNT == .05


def test_zoom_has_distinct_frames_and_continues_through_text(tmp_path):
    studio = Studio(tmp_path)
    studio.performance_dir.mkdir(parents=True)
    photo = Image.new('RGB', (1600, 1100), 'white')
    draw = ImageDraw.Draw(photo)
    for x in range(0, 1600, 17):
        draw.rectangle((x, 0, x + 7, 1100), fill='#222222')
    photo.save(studio.performance_dir / 'stripes.png')
    track = dict(key='motion', file='missing.mp3', rank=1, title='Motion', artist='Test', description='',
                 performance_image_file='stripes.png', performance_image_mime='image/png',
                 visual_frames=[])
    cards, _ = studio._render_cards([track], [{}], tmp_path, motion=True)
    assert isinstance(cards[0], dict)
    mask = Image.open(cards[0]['mask']).convert('RGB')
    foreground = Image.open(cards[0]['foreground'])
    assert mask.size == foreground.size == (1920, 1080)
    assert mask.getpixel((0, 500)) == (0, 0, 0)
    assert mask.getpixel((1100, 500)) == (255, 255, 255)
    assert foreground.mode == 'RGBA'
    assert foreground.getextrema()[3][0] < 255
    blank = tmp_path / 'text.png'
    Image.new('RGB', (1920, 1080), 'black').save(blank)
    cues = [dict(start=0, end=1)]

    def frames(current_track, asset):
        args, graph, label, count = studio._video_filter([asset], [current_track], cues, 1000, {(0, 0): blank})
        assert count == 60
        graph += f';{label}crop=96:96:1000:200,format=rgb24[out]'
        result = subprocess.run(['ffmpeg', '-v', 'error', *args, '-filter_complex_threads', '2',
            '-filter_complex', graph, '-map', '[out]', '-frames:v', str(count),
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True, timeout=120)
        size = 96 * 96 * 3
        return [result.stdout[i:i+size] for i in range(0, len(result.stdout), size)]

    uninterrupted = frames(track, cards[0])
    assert len(uninterrupted) == 60
    assert len(set(uninterrupted)) == 60
    interrupted = copy.deepcopy(track)
    interrupted['visual_frames'] = [dict(type='text', start=.3, end=.5, text='Cue')]
    actual = frames(interrupted, cards[0])
    assert actual[:18] == uninterrupted[:18]
    assert actual[30:] == uninterrupted[30:]
    assert all(not any(frame) for frame in actual[18:30])


def test_motion_missing_photo_keeps_static_path(tmp_path):
    studio = Studio(tmp_path)
    track = dict(key='missing', file='missing.mp3', rank=1, title='No photo', artist='Test', description='')
    cards, _ = studio._render_cards([track], [{}], tmp_path, motion=True)
    assert not isinstance(cards[0], dict)


def test_extreme_photo_shapes_leave_mask_and_long_text_unchanged(tmp_path):
    studio = Studio(tmp_path)
    studio.performance_dir.mkdir(parents=True)
    tracks = []
    for index, size in enumerate(((100, 800), (800, 100), (300, 300))):
        name = f'photo-{index}.png'
        Image.new('RGB', size, '#54789a').save(studio.performance_dir / name)
        tracks.append(dict(key=str(index), file='missing.mp3', rank=1,
            title='Long title ' * 50, artist='Long artist ' * 30,
            description='Long description ' * 100,
            performance_image_file=name, performance_image_mime='image/png'))
    cards, _ = studio._render_cards(tracks, [{}] * 3, tmp_path, motion=True)
    for kind in ('mask', 'foreground'):
        baseline = Image.open(cards[0][kind]).tobytes()
        # Alternating song-card layouts intentionally produce different
        # motion-layer geometry; compare the two default-layout cards.
        assert Image.open(cards[2][kind]).tobytes() == baseline
