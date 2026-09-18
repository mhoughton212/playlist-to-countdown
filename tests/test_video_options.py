import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from countdown.web import create_app
from countdown.studio import Studio


def test_animation_setting_is_validated_saved_and_used(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    client = app.test_client()
    studio = app.config['STUDIO']
    headers = {'X-Studio-Token': app.config['WRITE_TOKEN']}
    payload = dict(duration=10, crossfade=1, video_animation=True, video_zoom=25)
    assert client.post('/api/settings', json=payload, headers=headers).status_code == 200
    assert Studio(tmp_path).project['settings']['video_animation'] is True
    assert Studio(tmp_path).project['settings']['video_zoom'] == .25
    payload.pop('video_animation')
    client.post('/api/settings', json=payload, headers=headers)
    assert studio.project['settings']['video_animation'] is True
    monkeypatch.setattr(studio, 'start', lambda *args: None)
    assert client.post('/api/build-video', json=dict(mode='final', duration=11, crossfade=1.5, video_animation=False), headers=headers).status_code == 200
    built = Studio(tmp_path).project['settings']
    assert built['video_animation'] is False
    assert built['duration'] == 11
    assert built['crossfade'] == 1.5
    assert client.post('/api/build-video', json=dict(mode='final', video_animation='false'), headers=headers).status_code == 400
    assert client.post('/api/settings', json=dict(duration=10, crossfade=1, video_zoom=26), headers=headers).status_code == 400


def test_completion_band_without_save_button(tmp_path):
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    studio.project['tracks'] = [dict(key='test',file='missing.mp3',title='Test',artist='Artist',rank=1,
                                    selected=True,start=0,duration=1,description='')]
    studio.save()
    client = app.test_client()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':1440,'height':1080})
        def route_request(route):
            r = route.request
            response = client.open(r.url.split('127.0.0.1:8765',1)[1], method=r.method,
                data=r.post_data_buffer, headers={k:v for k,v in r.headers.items() if k.lower() not in ('host','content-length')})
            route.fulfill(status=response.status_code, body=response.get_data(), headers=dict(response.headers))
            response.close()
        page.route('http://127.0.0.1:8765/**',route_request)
        page.goto('http://127.0.0.1:8765/')
        page.get_by_role('button',name='4 Build compilation').click()
        toggle=page.locator('#video-animation')
        assert not toggle.is_checked()
        toggle.check()
        assert page.get_by_role('button',name='Save settings',exact=True).count() == 0
        assert page.get_by_role('button',name='Build video →',exact=True).count() == 1
        page.evaluate("() => {state.job={...state.job,running:false,kind:'video build',message:'Compilation video ready: 100 clips.',errors:[],done:100,total:100};renderJob()}")
        panel=page.locator('#job-panel')
        assert panel.evaluate("e=>getComputedStyle(e).backgroundColor") == 'rgb(113, 56, 189)'
        assert panel.is_visible()
        artifact=Path(__file__).resolve().parents[1]/'exports/batch-comparison/completion-band.png'
        page.screenshot(path=str(artifact),full_page=True)
        for message,errors in [('Canceled.',[]),('Failed',['Failed'])]:
            page.evaluate("data=>{state.job.message=data.message;state.job.errors=data.errors;renderJob()}",dict(message=message,errors=errors))
            assert 'video-ready' not in panel.get_attribute('class')
        browser.close()
