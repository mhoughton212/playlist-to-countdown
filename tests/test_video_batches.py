"""Real encoded batch joins retain absolute cue timing and a single audio stream."""
import json
import subprocess

from PIL import Image
from pydub import AudioSegment

from countdown.studio import Studio, video_frame


def test_mixed_motion_batches_join_at_exact_frames(tmp_path):
    studio = Studio(tmp_path)
    studio.performance_dir.mkdir(parents=True)
    Image.new('RGB', (300, 600), '#884422').save(studio.performance_dir / 'photo.png')
    tracks = [dict(key=str(i), rank=3-i, title=f'Song {i}', artist='Test',
                   file='missing.mp3', description='', visual_frames=[]) for i in range(3)]
    tracks[1].update(performance_image_file='photo.png', performance_image_mime='image/png',
                     visual_frames=[dict(type='text', start=.2, end=.4, text='Cue')])
    cues = [dict(start=0, end=.713), dict(start=.413, end=1.213), dict(start=.913, end=1.713)]
    cards, _ = studio._render_cards(tracks, cues, tmp_path, motion=True)
    audio = tmp_path / 'audio.wav'
    AudioSegment.silent(duration=1713, frame_rate=48000).export(audio, format='wav').close()
    output = tmp_path / 'joined.mp4'
    studio._encode_video_batches(cards, tracks, cues, 1713, studio._rendered_text_cards,
                                  audio, tmp_path, output)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                               '-of', 'json', str(output)]))
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert video['r_frame_rate'] == '60/1'
    assert int(video['nb_frames']) == video_frame(1.713) * 2
    assert len([s for s in probe['streams'] if s['codec_type'] == 'audio']) == 1
    assert abs(float(next(s for s in probe['streams'] if s['codec_type'] == 'audio')['duration']) - 1.713) < .025
    # Decode the joined video and each part to prove joining neither duplicates nor drops frames.
    def frames(path):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
            '-vf', 'scale=32:18', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    assert frames(output) == b''.join(frames(part) for part in sorted(tmp_path.glob('batch-*.mp4')))
    for part, (start, end) in zip(sorted(tmp_path.glob('batch-*.mp4')), studio._video_boundaries(cues, 1713)):
        assert len(frames(part)) == (end-start) * 2 * 32 * 18 * 3


def test_hundred_songs_never_share_an_encoding_process(tmp_path, monkeypatch):
    studio = Studio(tmp_path)
    tracks = [dict(rank=100-i, title='Song', visual_frames=[]) for i in range(100)]
    cues = [dict(start=i*.8, end=i*.8+1) for i in range(100)]
    calls = []
    def run(command, output, timeout):
        calls.append(command)
        output.touch()
    monkeypatch.setattr(studio, '_run_video_command', run)
    studio._encode_video_batches([tmp_path/'card.png']*100, tracks, cues, 80200, {},
                                 tmp_path/'audio.wav', tmp_path, tmp_path/'joined.mp4')
    assert len(calls) == 101
    assert all(command.count('-i') == 1 for command in calls[:-1])
    assert calls[-1][calls[-1].index('-c:v')+1] == 'copy'


def test_cancelled_video_build_removes_intermediates(tmp_path, monkeypatch):
    import pytest
    from countdown.studio import JobCancelled
    studio = Studio(tmp_path)
    studio.output_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(studio, '_assemble', lambda *args: (AudioSegment.silent(duration=100), [], [], []))
    monkeypatch.setattr(studio, '_render_cards', lambda *args, **kwargs: ([], []))
    def cancel(command, output, timeout):
        output.write_bytes(b'partial')
        raise JobCancelled()
    monkeypatch.setattr(studio, '_run_video_command', cancel)
    with pytest.raises(JobCancelled):
        studio.build_video('final')
    assert not list(studio.output_dir.iterdir())


def test_static_export_is_one_job_with_continuous_audio(tmp_path):
    studio=Studio(tmp_path)
    assets=[]
    for name,color in [('red','red'),('green','green')]:
        path=tmp_path/(name+'.png')
        Image.new('RGB',(1920,1080),color).save(path)
        assets.append(path)
    tracks=[dict(rank=2-i,title='Static',visual_frames=[]) for i in range(2)]
    cues=[dict(start=0,end=.7),dict(start=.5,end=1.2)]
    audio=tmp_path/'audio.wav'
    AudioSegment.silent(duration=1200).export(audio,format='wav').close()
    output=tmp_path/'static.mp4'
    studio._encode_static_video(assets,tracks,cues,1200,{},audio,tmp_path,output)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(output)]))
    video=next(v for v in probe['streams'] if v['codec_type']=='video')
    assert int(video['nb_frames'])==36
    assert video['r_frame_rate']=='30/1'
    assert not list(tmp_path.glob('batch-*.mp4'))
