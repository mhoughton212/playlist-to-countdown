"""Build a YouTube-ready album-art collage from a Playlist to Countdown project."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageOps


def choose_grid(count: int, width: int, height: int) -> tuple[int, int]:
    """Choose a compact grid whose cells stay close to square."""
    if count < 1:
        raise ValueError('At least one album cover is required.')
    candidates = []
    for rows in range(1, count + 1):
        columns = math.ceil(count / rows)
        if columns < rows:
            continue
        cell_aspect = (width / columns) / (height / rows)
        empty_fraction = (columns * rows - count) / (columns * rows)
        score = abs(math.log(cell_aspect)) + 3 * empty_fraction
        candidates.append((score, columns, rows))
    _, columns, rows = min(candidates)
    return columns, rows


def unique_artwork(studio, tracks) -> tuple[list[Path], int]:
    """Return readable artwork files in track order, deduped by image bytes."""
    paths = []
    seen = set()
    missing = 0
    for track in tracks:
        path = studio.artwork_path(track)
        if not path:
            missing += 1
            continue
        digest = hashlib.sha256(path.read_bytes()).digest()
        if digest in seen:
            continue
        seen.add(digest)
        paths.append(path)
    return paths, missing


def render_thumbnail(paths, output, *, width=1920, height=1080, gap=4,
                     background='#111111') -> dict:
    """Render a centered, evenly spaced, cover-cropped collage."""
    width, height, gap = int(width), int(height), int(gap)
    if width < 640 or height < 360 or width > 7680 or height > 4320:
        raise ValueError('Thumbnail dimensions must be between 640×360 and 7680×4320.')
    if gap < 0 or gap > min(width, height) // 10:
        raise ValueError('Thumbnail gap is outside the supported range.')

    readable = []
    for path in paths:
        try:
            with Image.open(path) as source:
                readable.append((path, ImageOps.exif_transpose(source).convert('RGB').copy()))
        except (OSError, ValueError):
            continue
    if not readable:
        raise ValueError('No usable album artwork is available for the thumbnail.')

    columns, rows = choose_grid(len(readable), width, height)
    available_width = width - gap * (columns - 1)
    available_height = height - gap * (rows - 1)
    canvas = Image.new('RGB', (width, height), background)

    # Rounded grid boundaries distribute leftover pixels without a lopsided edge.
    x_edges = [round(i * available_width / columns) + i * gap for i in range(columns + 1)]
    y_edges = [round(i * available_height / rows) + i * gap for i in range(rows + 1)]
    for index, (_, source) in enumerate(readable):
        row = index // columns
        items_in_row = min(columns, len(readable) - row * columns)
        column_offset = (columns - items_in_row) / 2
        column = index % columns
        cell_width = round(available_width / columns)
        x = round((column + column_offset) * (available_width / columns + gap))
        y = y_edges[row]
        right = min(width, x + cell_width)
        bottom = y_edges[row + 1] - (gap if row < rows - 1 else 0)
        tile = ImageOps.fit(source, (right - x, bottom - y), method=Image.Resampling.LANCZOS)
        canvas.paste(tile, (x, y))

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f'.{output.stem}-building{output.suffix}')
    try:
        if output.suffix.lower() in ('.jpg', '.jpeg'):
            canvas.save(temporary, quality=94, optimize=True, progressive=True)
        else:
            canvas.save(temporary, format='PNG', optimize=True)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': output, 'width': width, 'height': height, 'columns': columns,
            'rows': rows, 'covers': len(readable)}
