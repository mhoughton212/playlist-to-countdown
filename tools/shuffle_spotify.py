from countdown.paths import CACHE_DIR
import argparse
import os
import random

import spotipy
from spotipy.oauth2 import SpotifyOAuth


def get_spotify_client() -> spotipy.Spotify:

    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing SPOTIPY_CLIENT_ID or SPOTIPY_CLIENT_SECRET in .env"
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="playlist-read-private playlist-modify-private playlist-modify-public",
        cache_path=str(CACHE_DIR / ".spotify_cache"),
    )

    return spotipy.Spotify(auth_manager=auth_manager)


def get_playlist_items(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    items = []

    results = sp.playlist_items(
        playlist_id,
        limit=100,
        offset=0,
    )

    while True:
        items.extend(results["items"])

        if not results["next"]:
            break

        results = sp.next(results)

    return items


def shuffle_playlist_after(
    sp: spotipy.Spotify,
    playlist_id: str,
    stable_count: int,
) -> None:
    items = get_playlist_items(sp, playlist_id)
    total_count = len(items)

    if stable_count < 0:
        raise ValueError("stable_count cannot be negative.")

    if stable_count >= total_count:
        print(
            f"Playlist has {total_count} items. "
            f"Nothing exists after the first {stable_count} items."
        )
        return

    movable_count = total_count - stable_count

    if movable_count <= 1:
        print("There are fewer than two movable items. Nothing to shuffle.")
        return

    # Represents each item's original position within the shuffleable section.
    current_order = list(range(movable_count))

    target_order = current_order.copy()
    random.shuffle(target_order)

    print(f"Total tracks: {total_count}")
    print(f"Leaving first {stable_count} tracks untouched.")
    print(f"Shuffling remaining {movable_count} tracks...")

    for target_index, item_id in enumerate(target_order):
        current_index = current_order.index(item_id)

        if current_index == target_index:
            continue

        absolute_current = stable_count + current_index
        absolute_target = stable_count + target_index

        # Spotify's insert_before is based on the playlist before removal.
        if absolute_current < absolute_target:
            insert_before = absolute_target + 1
        else:
            insert_before = absolute_target

        sp.playlist_reorder_items(
            playlist_id=playlist_id,
            range_start=absolute_current,
            insert_before=insert_before,
            range_length=1,
        )

        # Keep our local representation synchronized with Spotify.
        moved_item = current_order.pop(current_index)
        current_order.insert(target_index, moved_item)

    print("Shuffle complete.")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Shuffle a Spotify playlist while keeping the first N tracks fixed."
        )
    )

    parser.add_argument(
        "playlist_id",
        help="Spotify playlist ID",
    )

    parser.add_argument(
        "stable_count",
        type=int,
        help="Number of tracks at the beginning to leave untouched",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible shuffling",
    )

    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    sp = get_spotify_client()

    shuffle_playlist_after(
        sp=sp,
        playlist_id=args.playlist_id,
        stable_count=args.stable_count,
    )


if __name__ == "__main__":
    main()