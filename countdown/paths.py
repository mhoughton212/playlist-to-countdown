"""Project locations are anchored here, never to the shell's working directory."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
CACHE_DIR = DATA_DIR / 'cache'
EXPORT_DIR = ROOT / 'exports'
UI_DIR = Path(__file__).resolve().parent / 'ui'
load_dotenv(ROOT / '.env')


def local_audio_dir(root=ROOT):
    local = Path(os.getenv('LOCAL_FILE_DIR') or 'local_audio')
    return local if local.is_absolute() else Path(root) / local
