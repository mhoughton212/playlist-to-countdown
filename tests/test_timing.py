"""Playback-clock and encoded-frame regressions, not manually refreshed stills."""
import json
import subprocess

from PIL import Image
from playwright.sync_api import sync_playwright, expect
from pydub.generators import Sine

from countdown.web import create_app
from countdown.studio import video_frame


def test_real_playback_seeks_fractional_saves_and_end_stop(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_FILE_DIR', str(tmp_path / 'local_audio'))
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    studio.audio_dir.mkdir()
    Sine(440).to_audio_segment(duration=6000).export(studio.audio_dir / 'test.mp3', format='mp3').close()
    studio.project['tracks'] = [dict(key='timing', file='test.mp3', title='Timing', artist='Test',
        rank=1, start=.126, duration=2, selected=True, source_url='', description='',
        visual_frames=[dict(type='text', start=.63, end=1.2, text='Cue')])]
    studio.save()
    client = app.test_client()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':1500,'height':1000})
        errors=[]
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route_request(route):
            request=route.request
            response=client.open(request.url.split('127.0.0.1:8765',1)[1], method=request.method,
                data=request.post_data_buffer,
                headers={k:v for k,v in request.headers.items() if k.lower() not in ('host','content-length')})
            route.fulfill(status=response.status_code, body=response.get_data(), headers=dict(response.headers))
            response.close()
        page.route('http://127.0.0.1:8765/**',route_request)
        page.goto('http://127.0.0.1:8765/')
        page.wait_for_function('audio.readyState >= 2')
        expect(page.locator('#workspace-preview-status')).to_be_hidden()
        expect(page.locator('#clip-start')).to_have_value('0.126')
        page.locator('#track-description').fill('Unrelated save retains fractional start')
        expect(page.locator('#save-status')).to_have_text('Changes saved locally')
        assert studio.project['tracks'][0]['start']==.126
        page.reload()
        page.wait_for_function('audio.readyState >= 2')
        expect(page.locator('#clip-start')).to_have_value('0.126')
        expect(page.locator('#workspace-preview-status')).to_be_hidden()
        page.evaluate('''() => {
          window.crossings=[]; window.capture=false;
          const original=updateWorkspaceVisual;
          window.updateWorkspaceVisual=()=>{
            original();
            if(capture && !document.getElementById('workspace-text-preview').hidden) {
              crossings.push(audio.currentTime); capture=false;
            }
          };
          window.timelineNode=document.getElementById('visual-timeline').firstChild;
        }''')
        for start in [.126,.25,.4,.51]:
            page.evaluate('async start => { playback.pause(); await playback.seek(start); capture=true; await playback.play(); }',start)
            page.wait_for_function('!capture')
            page.evaluate('playback.pause()')
        crossings=page.evaluate('crossings')
        due=.126+19/30
        assert all(-.002 <= value-due < .085 for value in crossings),crossings
        assert page.evaluate("timelineNode===document.getElementById('visual-timeline').firstChild")
        # Seek both directions without pausing; end stop must re-arm.
        page.evaluate('() => playback.seek(3,true)')
        page.evaluate('() => playback.seek(2.03,true)')
        page.wait_for_function('audio.paused && Math.abs(audio.currentTime-2.126)<.002')
        page.evaluate('() => playback.seek(.9)')
        expect(page.locator('#workspace-text-preview')).to_be_visible()
        page.evaluate('() => playback.seek(.3)')
        expect(page.locator('#workspace-song-preview')).to_be_visible()
        # Numeric trim and duration survive another save/reload.
        page.locator('#clip-start').fill('0.237')
        page.locator('#clip-start').press('Tab')
        expect(page.locator('#save-status')).to_have_text('Changes saved locally')
        assert studio.project['tracks'][0]['start']==.237
        assert studio.project['tracks'][0]['duration']==2
        # A save acknowledgement must not replace the current text-frame draft.
        page.evaluate('''() => {
          editorFrames[0].text='New draft';
          const old=structuredClone(state); old.tracks[0].visual_frames[0].text='Old acknowledgement';
          applyState(old); renderTextFrames();
        }''')
        expect(page.locator('[data-frame-field=text]')).to_have_value('New draft')
        assert not errors
        browser.close()


def test_export_absolute_frame_boundaries_with_crossfade(tmp_path):
    studio=create_app(tmp_path).config['STUDIO']
    colors=['red','green','blue','yellow']
    assets=[]
    for i,color in enumerate(colors):
        path=tmp_path/f'{i}.png';Image.new('RGB',(32,32),color).save(path);assets.append(path)
    # Many sub-frame durations expose cumulative rounding; include a non-frame
    # aligned compilation start and a real song-card dissolve.
    frames=[dict(type='text',start=.11+i*.17,end=.19+i*.17,text=str(i)) for i in range(12)]
    tracks=[dict(rank=2,title='Before',visual_frames=[]),dict(rank=1,title='Cues',visual_frames=frames)]
    cues=[dict(start=0,end=1.913),dict(start=1.413,end=4.413)]
    text_cards={(1,i):assets[2+i%2] for i in range(len(frames))}
    args,graph,label,count=studio._video_filter(assets[:2],tracks,cues,4413,text_cards)
    output=tmp_path/'timing.mkv'
    subprocess.run(['ffmpeg','-y','-v','error',*args,'-filter_complex',graph,'-map',label,
        '-frames:v',str(count),'-c:v','ffv1',str(output)],check=True)
    raw=subprocess.run(['ffmpeg','-v','error','-i',str(output),'-vf','scale=1:1',
        '-f','rawvideo','-pix_fmt','rgb24','-'],capture_output=True,check=True).stdout
    pixels=[tuple(raw[i:i+3]) for i in range(0,len(raw),3)]
    assert len(pixels)==video_frame(4.413)
    # After the dissolve finishes, inspect EVERY output frame against absolute
    # cue boundaries. Green song card, alternating blue/yellow text cards.
    for index in range(video_frame(1.9),len(pixels)):
        active=next((i for i,f in enumerate(frames)
            if video_frame(1.413+f['start'])<=index<video_frame(1.413+f['end'])),None)
        rgb=pixels[index]
        if active is None: assert rgb[1]>100 and rgb[0]<10 and rgb[2]<10,(index,rgb)
        elif active%2==0: assert rgb[2]>240 and rgb[0]<10,(index,rgb)
        else: assert rgb[0]>240 and rgb[1]>240 and rgb[2]<10,(index,rgb)
