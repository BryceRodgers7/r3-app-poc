# Sports Replay POC

Windows desktop proof of concept for live sports replay using Python and PySide6, with a temporary OpenCV-based vertical slice for webcam capture, recording, and replay.

## Run

1. Create and activate a virtual environment:
   `python -m venv .venv`
   `.venv\Scripts\activate`
2. Install dependencies:
   `python -m pip install -e .`
3. Launch the app:
   `python main.py`

If a webcam is available, the app uses it as the test source. If not, it falls back to a moving synthetic feed so the preview, recording, and replay workflow still works.

## Current Vertical Slice

- Live preview updates in the UI from the temporary test source.
- Recording starts automatically and writes to `C:\SportsReplay\sessions\session_###\recording`.
- The replay buffer keeps about two minutes of recent frames in memory.
- `Pause` freezes the viewed frame only.
- `Rewind 10s` switches the view to buffered content while ingest and recording continue.
- `Jump to Live` returns the UI to the newest frame.

## Temporary Vs Intended To Remain

Temporary for this milestone:

- OpenCV webcam capture and synthetic fallback in `app/media/test_source.py`
- OpenCV video writing in `app/media/recorder.py`
- In-memory JPEG-compressed replay history in `app/media/replay_buffer.py`

Intended to remain:

- Source abstraction in `app/media/source_interface.py`
- Media coordination in `app/media/pipeline_manager.py`
- View-state orchestration in `app/core/playback_controller.py`
- Separation between preview, recording, and replay responsibilities
