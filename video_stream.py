import threading
import logging
import errno
import time
from PIL import ImageTk
import tkinter as tk
import asyncio

# import janus_client
import av
from av import VideoFrame

format = "%(asctime)s: %(message)s"
logging.basicConfig(format=format, level=logging.INFO, datefmt="%H:%M:%S")
logger = logging.getLogger()

loop = asyncio.new_event_loop()


class VideoStream:
    video_player: tk.Label
    video_width: int
    video_height: int
    offset_x: int
    offset_y: int
    server_url: str

    __thread: threading.Thread
    __thread_quit: threading.Event

    def __init__(
        self,
        video_player,
        video_width: int,
        video_height: int,
        offset_x: int,
        offset_y: int,
        server_url: str = "",
    ) -> None:
        self.video_player = video_player
        self.video_width = video_width
        self.video_height = video_height
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.server_url = server_url

    def start(self) -> None:
        self.__thread_quit = threading.Event()
        self.__thread = threading.Thread(
            target=self.get_and_display_frame,
            args=(1, self.video_width, self.video_height, self.offset_x, self.offset_y),
        )
        self.__thread.start()

    def stop(self) -> None:
        if self.__thread:
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

    def get_and_display_frame(
        self, name, video_width: int, video_height: int, offset_x: int, offset_y: int
    ):
        logging.info("Thread %s: starting", name)

        file = "desktop"
        format = "gdigrab"
        options = {
            "video_size": f"{video_width}x{video_height}",
            "framerate": "30",
            "offset_x": str(offset_x),
            "offset_y": str(offset_y),
            "show_region": "1",
        }
        container = av.open(
            file=file, format=format, mode="r", options=options, timeout=None
        )

        video_first_pts = None

        while not self.__thread_quit.is_set():
            try:
                frame = next(container.decode())
            except Exception as exc:
                if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                    logger.error(exc)
                    time.sleep(0.01)
                    continue

                break

            if isinstance(frame, VideoFrame):
                if frame.pts is None:
                    logger.warning(
                        f"MediaPlayer({container.name}) Skipping video frame with no pts",
                    )
                    continue

                # video from a webcam doesn't start at pts 0, cancel out offset
                if video_first_pts is None:
                    video_first_pts = frame.pts
                frame.pts -= video_first_pts

                image = frame.to_image()

                if not self.__thread_quit.is_set():
                    self.current_frame_image = ImageTk.PhotoImage(image)

                    self.video_player.config(image=self.current_frame_image)

        container.close()

        logging.info("Thread %s: finishing", name)
