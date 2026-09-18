"""Exercise the real dashboard against an isolated project without a second server."""
import json
import io

from playwright.sync_api import sync_playwright, expect
from pydub.generators import Sine

from countdown.web import create_app


def test_workspace_edit_preview_review_and_missing_audio(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_FILE_DIR', str(tmp_path / 'local_audio'))
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    studio.audio_dir.mkdir()
    Sine(440).to_audio_segment(duration=4000).export(studio.audio_dir / 'test.mp3', format='mp3').close()
    studio.project['tracks'] = [
        dict(key=key, file='test.mp3' if key != 'missing' else 'missing.mp3',
             title=('Long song title ' * 12) if key == 'first' else key,
             artist='Long artist ' * 15, rank=3-index, start=0, duration=2,
             selected=key == 'last', source_url='', description='', visual_frames=[])
        for index, key in enumerate(['first', 'missing', 'last'])
    ]
    studio.project['tracks'][0].update(year='2024', description='')
    studio.project['tracks'][1].update(year='', description='Has a description')
    studio.project['tracks'][2].update(year='2020', description='Has a description')
    studio.save()
    client = app.test_client()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1500, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            path = request.url.split('127.0.0.1:8765', 1)[1]
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in ('host', 'content-length')}
            data = request.post_data_buffer
            if path.startswith('/api/upload/'):
                # Chromium omits file bytes from intercepted multipart post data.
                # Supply the same file selected above to the isolated Flask client.
                headers.pop('content-type', None)
                data = {'audio': (io.BytesIO((studio.audio_dir / 'test.mp3').read_bytes()), 'test.mp3')}
            response = client.open(path, method=request.method, data=data, headers=headers)
            route.fulfill(status=response.status_code, body=response.get_data(), headers=dict(response.headers))
            response.close()

        page.route('http://127.0.0.1:8765/**', route_request)
        page.goto('http://127.0.0.1:8765/')
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == 'dark'
        assert page.locator('#theme-toggle').count() == 0
        page.reload()
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == 'dark'
        for _ in range(3):
            page.reload()
            expect(page.locator('#workspace-preview-status')).to_be_hidden(timeout=15000)
            assert page.locator('#workspace-song-preview').content_frame.locator('#title').inner_text().startswith('Long song title')
        ids = page.locator('[id]').evaluate_all('(nodes) => nodes.map(node => node.id)')
        assert len(ids) == len(set(ids))
        expect(page.locator('#filter-match-count')).to_have_text('3 songs shown')
        page.locator('#song-filter').select_option('missing_year')
        expect(page.locator('#filter-match-count')).to_have_text('1 song shown')
        expect(page.locator('#song-list .song')).to_have_count(1)
        expect(page.locator('#song-list .song-title')).to_have_text('missing')
        page.locator('#song-search').fill('does not exist')
        expect(page.locator('#filter-match-count')).to_have_text('0 songs shown')
        page.locator('#song-search').fill('')
        page.locator('#song-filter').select_option('all')
        assert not studio.project['tracks'][0]['selected']
        page.wait_for_function("document.getElementById('song-audio').readyState >= 1")
        page.locator('#source-play').click()
        page.wait_for_function('!audio.paused')
        page.locator('#source-play').click()
        page.wait_for_function('audio.paused')
        page.locator('#preview-toggle').click(force=True)
        page.wait_for_function('!audio.paused')
        assert page.locator('#preview-toggle').get_attribute('aria-label') == 'Pause preview'
        page.locator('#workspace-song-preview').content_frame.locator('#frame').click()
        page.wait_for_function('audio.paused')
        page.locator('#clip-position').focus()
        page.locator('#clip-position').press('ArrowRight')
        page.locator('#clip-position').press('Tab')
        expect(page.locator('#clip-start')).to_have_value('0.1')
        expect(page.locator('#clip-end')).to_have_value('2.1')
        expect(page.locator('#clip-duration')).to_have_value('2')
        page.locator('#clip-position').focus()
        page.locator('#clip-position').press('Home')
        page.locator('#clip-position').press('Tab')
        expect(page.locator('#clip-start')).to_have_value('0')
        page.locator('#track-description').fill('A saved description\nwith a second line')
        page.locator('#card-title-input').fill('A custom card title')
        page.locator('#card-artist-input').fill('A custom card artist')
        page.locator('#track-year').fill('1998')
        expect(page.locator('#save-status')).to_have_text('Changes saved locally')
        page.wait_for_function("document.getElementById('workspace-song-preview').contentWindow.document.getElementById('description').textContent.startsWith('A saved description')")
        expect(page.locator('#workspace-song-preview').content_frame.locator('#title')).to_have_text('A custom card title')
        expect(page.locator('#workspace-song-preview').content_frame.locator('#artist-name')).to_have_text('A custom card artist')
        expect(page.locator('#workspace-song-preview').content_frame.locator('#year')).to_have_text('1998')
        assert studio.project['tracks'][0]['description'].endswith('second line')
        if not page.locator('#add-text-frame').is_visible():
            page.locator('#visual-frames-details summary').click()
        page.locator('#add-text-frame').click()
        page.locator('[data-frame-field=text]').fill('')
        expect(page.locator('#visual-frames-error')).to_have_text('Add text before saving a text frame.')
        expect(page.locator('#notice')).to_be_hidden()
        page.locator('[data-frame-field=text]').fill('Text during\nthe excerpt')
        expect(page.locator('#visual-frames-error')).to_be_hidden()
        page.locator('[data-frame-field=end]').fill('1')
        expect(page.locator('#save-status')).to_have_text('Changes saved locally')
        page.wait_for_function("document.getElementById('song-audio').readyState >= 1")
        page.evaluate("audio.currentTime = .5; updateWorkspaceVisual()")
        expect(page.locator('#workspace-text-preview')).to_be_visible()
        assert page.locator('#workspace-text-preview').content_frame.locator('#text').text_content() == 'Text during\nthe excerpt'
        page.evaluate('audio.currentTime = 1.5; updateWorkspaceVisual()')
        expect(page.locator('#workspace-song-preview')).to_be_visible()
        assert page.locator('#visual-timeline .visual-segment').count() == 2
        page.locator('#preview-clip').click()
        page.wait_for_function('!audio.paused')
        page.evaluate('audio.pause()')
        page.locator('#next-track').click()
        expect(page.locator('#missing-audio')).to_be_visible()
        assert page.locator('#audio-replacement').evaluate('(e) => e.open')
        expect(page.locator('#preview-clip')).to_be_disabled()
        expect(page.locator('#set-start')).to_be_disabled()
        expect(page.locator('#preview-card')).to_be_enabled()
        page.locator('#upload-audio').set_input_files(str(studio.audio_dir / 'test.mp3'))
        expect(page.locator('#missing-audio')).to_be_hidden()
        assert studio.project['tracks'][1]['file'] == 'custom-missing.mp3'
        page.locator('#song-list [data-key=first]').click()
        assert not studio.project['tracks'][0]['selected']
        page.reload()
        expect(page.locator('#track-description')).to_have_value('A saved description\nwith a second line')
        expect(page.locator('#card-title-input')).to_have_value('A custom card title')
        expect(page.locator('#card-artist-input')).to_have_value('A custom card artist')
        expect(page.locator('#track-year')).to_have_value('1998')
        assert json.loads(studio.file.read_text(encoding='utf-8'))['tracks'][0]['visual_frames'][0]['end'] == 1
        for width in (1500, 1000, 700, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if width <= 1180:
                assert page.locator('.workspace-preview').bounding_box()['y'] < page.locator('.excerpt-section').bounding_box()['y']
        assert not errors
        browser.close()
