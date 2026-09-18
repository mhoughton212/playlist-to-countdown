"""Browser regression: preview scaling must never change production layout."""
import base64
from io import BytesIO

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

from countdown.web import create_app


def test_card_preview_matches_export_at_every_window_size(tmp_path):
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    track = dict(file='missing.mp3', key='parity', rank=50, title='Source title', artist='China',
                 card_title='Beijing Welcomes You', card_artist='Primary artist', year='2008',
                 description='worlds first good celebrity collab')
    studio.project['tracks'] = [track]
    html = app.test_client().get('/card-preview/parity').get_data(as_text=True)
    cards, _ = studio._render_cards([track], [{}], tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        page.set_content(html)
        page.evaluate('() => window.cardReady')
        preview = Image.open(BytesIO(page.screenshot())).convert('RGB')
        assert ImageChops.difference(Image.open(cards[0]).convert('RGB'), preview).getbbox() is None
        assert page.locator('#title').inner_text() == 'Beijing Welcomes You'
        assert page.locator('#artist-name').inner_text() == 'Primary artist'
        assert page.locator('#year').inner_text() == '2008'
        measure = """() => ['rank','artwork','artist','title','description'].map(id=>{
            const e=document.getElementById(id);
            return [e.offsetLeft,e.offsetTop,e.offsetWidth,e.offsetHeight,getComputedStyle(e).fontSize];
        })"""
        baseline = page.evaluate(measure)
        for width, height in [(1916, 940), (960, 540), (1280, 900), (375, 812)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.goto('about:blank')
            page.set_content(html)
            page.evaluate('() => window.cardReady')
            assert page.evaluate(measure) == baseline
            box = page.locator('#frame').bounding_box()
            assert box['x'] >= -1 and box['y'] >= -1
            assert box['x'] + box['width'] <= width + 1
            assert box['y'] + box['height'] <= height + 1
        for dimensions in [(100, 800), (800, 100), (300, 300)]:
            buffer = BytesIO()
            Image.new('RGB', dimensions, '#345678').save(buffer, format='PNG')
            source = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
            page.evaluate('(data) => window.renderCard(data)', dict(
                rank=1, title='Long title ' * 50, artist='Long artist ' * 30, year='2026',
                description='Long description ' * 100, artwork=source, performanceImage=source))
            assert page.evaluate('() => window.cardOverflow()') == []
            assert page.evaluate("() => {const t=document.getElementById('title');return t.offsetHeight <= parseFloat(getComputedStyle(t).lineHeight)*2+1 && parseFloat(getComputedStyle(t).fontSize)>=72}")
            assert page.evaluate("() => document.getElementById('title').textContent") == 'Long title ' * 50

        page.evaluate('(data) => window.renderCard(data)', dict(
            rank=track['rank'], title=track['card_title'], artist=track['card_artist'],
            year=track['year'], description=track['description']))
        assert page.evaluate(measure) == baseline
        browser.close()


def test_card_preview_preserves_description_line_breaks(tmp_path):
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    track = dict(file='missing.mp3', key='lines', rank=62, title='come again', artist='m-flo',
                 description='pop rap >:(\npop rap (japan) :D')
    studio.project['tracks'] = [track]
    html = app.test_client().get('/card-preview/lines').get_data(as_text=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        page.set_content(html)
        page.wait_for_timeout(250)
        assert page.evaluate("() => getComputedStyle(document.getElementById('description')).whiteSpace") == 'pre-line'
        assert page.evaluate("() => getComputedStyle(document.getElementById('description')).webkitLineClamp") == '4'
        assert page.evaluate("() => document.getElementById('description').getBoundingClientRect().width") > 1000
        assert page.evaluate("() => document.getElementById('description').getBoundingClientRect().height") > 36
        browser.close()


def test_transparent_performance_padding_moves_fade_to_visible_pixels(tmp_path):
    app = create_app(tmp_path)
    studio = app.config['STUDIO']
    track = dict(file='missing.mp3', key='alpha', rank=39, title='Armageddon', artist='aespa',
                 card_title='Armageddon', card_artist='aespa', year='2024', description='test')
    studio.project['tracks'] = [track]
    html = app.test_client().get('/card-preview/alpha').get_data(as_text=True)
    buffer = BytesIO()
    performance = Image.new('RGBA', (800, 500), (0, 0, 0, 0))
    Image.new('RGBA', (600, 450), '#ddeeff').save(buffer, format='PNG')
    performance.alpha_composite(Image.open(BytesIO(buffer.getvalue())), (100, 0))
    buffer = BytesIO()
    performance.save(buffer, format='PNG')
    source = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        page.set_content(html)
        page.evaluate('(source) => window.renderCard({rank:39,title:"Armageddon",artist:"aespa",year:"2024",description:"test",performanceImage:source})', source)
        fade = page.evaluate("""() => {
            const style = document.querySelector('.performance').style;
            return [style.getPropertyValue('--fade-left-start'), style.getPropertyValue('--fade-left-end')];
        }""")
        assert float(fade[0].rstrip('%')) > 2
        assert float(fade[1].rstrip('%')) - float(fade[0].rstrip('%')) == 16
        browser.close()

