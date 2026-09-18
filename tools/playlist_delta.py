"""Compare the tracks in two Spotify playlists."""
import argparse
from countdown.paths import CACHE_DIR
import os

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
        redirect_uri=os.getenv('SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:8765/callback'),
        scope="playlist-read-private",
        cache_path=str(CACHE_DIR / ".spotify_cache"),
    )

    return spotipy.Spotify(auth_manager=auth_manager)


def get_track_ids(sp: spotipy.Spotify, playlist_id: str) -> set[str]:
    track_ids = set()

    results = sp.playlist_items(
        playlist_id,
        limit=100,
        offset=0,
    )

    while True:
        for item in results["items"]:
            track = item.get("track")

            if track and track.get("id"):
                track_ids.add(track["id"])

        if not results["next"]:
            break

        results = sp.next(results)

    return track_ids


def main():
    parser = argparse.ArgumentParser(description='Compare two Spotify playlists.')
    parser.add_argument('playlist_id_1', help='First Spotify playlist ID')
    parser.add_argument('playlist_id_2', help='Second Spotify playlist ID')
    args = parser.parse_args()
    sp = get_spotify_client()

    tracks_1 = get_track_ids(sp, args.playlist_id_1)
    tracks_2 = get_track_ids(sp, args.playlist_id_2)

    only_in_playlist_1 = tracks_1 - tracks_2

    print(f"Playlist 1 tracks: {len(tracks_1)}")
    print(f"Playlist 2 tracks: {len(tracks_2)}")
    print(
        f"Tracks in Playlist 1 that are not in Playlist 2: "
        f"{len(only_in_playlist_1)}"
    )


if __name__ == "__main__":
    main()
