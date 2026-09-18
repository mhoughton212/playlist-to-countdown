"""Export-only photo motion; masks and foreground stay in production coordinates."""
import hashlib
from pathlib import Path

MOTION_FPS = 60
# Subtle motion across the full card. This is independent of the image
# overscan used to prevent edges during the zoom.
ZOOM_AMOUNT = .05

# These are capture modes of the production HTML, not a second card layout.
# Overscan supplies pixels around the subtle zoom. The mask never moves.
LAYER_CSS = """
html[data-motion-layer] .performance img {transform:scale(1.08)}
html[data-motion-layer="photo"] .frame-label,
html[data-motion-layer="photo"] .album-cover,
html[data-motion-layer="photo"] .song-details,
html[data-motion-layer="mask"] .frame-label,
html[data-motion-layer="mask"] .album-cover,
html[data-motion-layer="mask"] .song-details {visibility:hidden}
html[data-motion-layer="photo"] .video-frame::after,
html[data-motion-layer="mask"] .video-frame::after {display:none}
html[data-motion-layer="photo"] .performance {
  -webkit-mask-image:none;mask-image:none;overflow:visible;
}
html[data-motion-layer="mask"],html[data-motion-layer="mask"] body,
html[data-motion-layer="mask"] .video-frame {background:#000}
html[data-motion-layer="mask"] .performance {background:#fff}
html[data-motion-layer="mask"] .performance img {visibility:hidden}
html[data-motion-layer="foreground"],html[data-motion-layer="foreground"] body,
html[data-motion-layer="foreground"] .video-frame {background:transparent}
html[data-motion-layer="foreground"] .performance {visibility:hidden}
"""


def render_layers(page, static_card, check_cancelled):
    """Cache three browser-rendered layers beside their content-addressed still."""
    center = page.locator('.performance').evaluate('(element) => {const r=element.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}')
    layers = {'still': static_card, 'photo_center': center}
    style = page.add_style_tag(content=LAYER_CSS)
    try:
        for kind in ('photo', 'mask', 'foreground'):
            check_cancelled()
            version = hashlib.sha256(LAYER_CSS.encode()).hexdigest()[:10]
            path = Path(static_card).with_name(f'{Path(static_card).stem}-zoom-{version}-{kind}.png')
            if not path.is_file():
                page.evaluate('(kind) => document.documentElement.dataset.motionLayer=kind', kind)
                temporary = path.with_suffix('.tmp.png')
                try:
                    page.locator('#frame').screenshot(path=str(temporary), omit_background=True)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            layers[kind] = path
    finally:
        page.evaluate('() => delete document.documentElement.dataset.motionLayer')
        style.evaluate('(element) => element.remove()')
    return layers


def motion_filters(first_input, label, frame_count, elapsed_frames, total_frames,
                   photo_center=(1190.4, 540), zoom_amount=ZOOM_AMOUNT):
    """Subpixel centered zoom from absolute card time, including interruptions."""
    progress = f'(on+{elapsed_frames})/{max(1, total_frames - 1)}'
    cx, cy = photo_center
    growth = f'({zoom_amount}*{progress})'
    x0, x1 = f'-{cx}*{growth}', f'W+(W-{cx})*{growth}'
    y0, y1 = f'-{cy}*{growth}', f'H+(H-{cy})*{growth}'
    return [
        f'[{first_input}:v]format=gbrp,perspective=x0=\'{x0}\':y0=\'{y0}\':'
        f'x1=\'{x1}\':y1=\'{y0}\':x2=\'{x0}\':y2=\'{y1}\':x3=\'{x1}\':y3=\'{y1}\':'
        f'sense=destination:eval=frame:interpolation=cubic[{label}photo]',
        f'color=c=0x111215:s=1920x1080:r={MOTION_FPS},'
        f'trim=end_frame={frame_count},format=gbrp[{label}base]',
        f'[{first_input + 1}:v]format=gbrp[{label}mask]',
        f'[{label}base][{label}photo][{label}mask]maskedmerge[{label}background]',
        f'[{first_input + 2}:v]format=rgba[{label}foreground]',
        f'[{label}background][{label}foreground]overlay=format=rgb:shortest=1,'
        f'trim=end_frame={frame_count},setpts=N/({MOTION_FPS}*TB),format=yuv420p[{label}]',
    ]
