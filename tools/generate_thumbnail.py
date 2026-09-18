"""Generate an editable album-art thumbnail from the saved Playlist to Countdown project."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from countdown.studio import Studio  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'exports' / 'countdown-thumbnail.png')
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--gap', type=int, default=4)
    parser.add_argument('--background', default='#111111')
    args = parser.parse_args()
    result = Studio(PROJECT_ROOT).build_thumbnail(
        output=args.output, width=args.width, height=args.height,
        gap=args.gap, background=args.background, save_export=False)
    print(f'Created {result["path"]} ({result["width"]}×{result["height"]}, '
          f'{result["covers"]} unique covers in a {result["columns"]}×{result["rows"]} grid)')


if __name__ == '__main__':
    main()
