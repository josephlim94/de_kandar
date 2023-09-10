import threading
import logging
import errno
import time
from PIL import ImageTk
import tkinter as tk
import asyncio

import janus_client
from aiortc.contrib.media import MediaPlayer
import av
from av import VideoFrame

format = "%(asctime)s: %(message)s"
logging.basicConfig(format=format, level=logging.INFO, datefmt="%H:%M:%S")
logger = logging.getLogger()


class VideoStream:
    video_player: tk.Label
    video_width: int
    video_height: int
    offset_x: int
    offset_y: int
    __server_url: str
    __api_secret: str
    __token: str

    __thread: threading.Thread
    __thread_quit: threading.Event

    session: janus_client.JanusSession
    plugin_handle: janus_client.JanusVideoRoomPlugin

    loop: asyncio.AbstractEventLoop

    def __init__(
        self,
        video_player,
        video_width: int,
        video_height: int,
        offset_x: int,
        offset_y: int,
        server_url: str = "",
        api_secret: str = None,
        token: str = None,
    ) -> None:
        self.video_player = video_player
        self.video_width = video_width
        self.video_height = video_height
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.__server_url = server_url
        self.__api_secret = api_secret
        self.__token = token

    async def connect_server(self) -> None:
        if not self.__server_url:
            raise Exception("No server url")

        # Create session
        self.session = janus_client.JanusSession(
            base_url=self.__server_url,
            api_secret=self.__api_secret,
            token=self.__token,
        )

        # Create plugin
        self.plugin_handle = janus_client.JanusVideoRoomPlugin()

        # Attach to Janus session
        await self.plugin_handle.attach(session=self.session)

    async def start_publish(self) -> None:
        # Janus demo uses room_id = 1234
        room_id = 12345

        response = await self.plugin_handle.join(
            room_id=room_id, display_name="Test video room publish"
        )
        if not response:
            raise Exception("Failed to join room")

        player = MediaPlayer("./Into.the.Wild.2007.mp4")
        response = await self.plugin_handle.publish(player=player)
        if not response:
            raise Exception("Failed to publish")

    async def stop_publish(self) -> None:
        response = await self.plugin_handle.unpublish()
        if not response:
            logger.info("Failed to publish")

        response = await self.plugin_handle.leave()
        if not response:
            logger.info("Failed to publish")

    async def disconnect_server(self) -> None:
        await self.session.destroy()

    def run_event_loop(self) -> None:
        logger.info("Event loop thread starting")
        asyncio.set_event_loop(self.loop)

        self.loop.run_forever()
        logger.info("Event loop thread finishing")

    def start(self) -> None:
        self.__thread_quit = threading.Event()
        self.__thread = threading.Thread(
            target=self.get_and_display_frame,
            args=(1, self.video_width, self.video_height, self.offset_x, self.offset_y),
        )
        self.__thread.start()

        self.__thread_loop_quit = asyncio.Event()
        self.loop = asyncio.new_event_loop()
        self.__thread_loop = threading.Thread(
            target=self.run_event_loop,
            args=(),
        )
        self.__thread_loop.start()

        if self.__server_url:
            asyncio.run_coroutine_threadsafe(self.connect_server(), self.loop).result()

    def stop(self) -> None:
        if self.__thread:
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

        if self.__thread_loop:
            if self.__server_url:
                asyncio.run_coroutine_threadsafe(
                    self.disconnect_server(), self.loop
                ).result()

            self.loop.call_soon_threadsafe(self.__thread_loop_quit.set)

            self.loop.stop()
            self.__thread_loop.join()
            self.__thread_loop = None

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
