import threading
import logging
import errno
import time
from PIL import ImageTk
import tkinter as tk
import asyncio
from typing import Union
import fractions

import janus_client
from aiortc.mediastreams import AUDIO_PTIME, MediaStreamError, MediaStreamTrack
import av
from av import AudioFrame, VideoFrame
from av.frame import Frame
from av.packet import Packet

format = "%(asctime)s: %(message)s"
logging.basicConfig(format=format, level=logging.INFO, datefmt="%H:%M:%S")
logger = logging.getLogger()


class PlayerStreamTrack(MediaStreamTrack):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self._queue = asyncio.Queue()
        self._start = None

    async def recv(self) -> Union[Frame, Packet]:
        if self.readyState != "live":
            raise MediaStreamError

        data = await self._queue.get()
        logger.info(data)
        if data is None:
            self.stop()
            raise MediaStreamError
        # if isinstance(data, Frame):
        #     data_time = data.time
        # elif isinstance(data, Packet):
        #     data_time = float(data.pts * data.time_base)

        # # control playback rate
        # if (
        #     self._player is not None
        #     and self._player._throttle_playback
        #     and data_time is not None
        # ):
        #     if self._start is None:
        #         self._start = time.time() - data_time
        #     else:
        #         wait = self._start + data_time - time.time()
        #         await asyncio.sleep(wait)

        return data

    def stop(self):
        super().stop()


class VideoStreamPlayer:
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

        self.__thread = None

        self.__audio_stream_track = None
        self.__video_stream_track = None

        self.audio_sample_rate = 48000
        self.audio_samples = 0
        self.audio_time_base = fractions.Fraction(1, self.audio_sample_rate)
        self.audio_resampler = av.AudioResampler(
            format="s16",
            layout="stereo",
            rate=self.audio_sample_rate,
            frame_size=int(self.audio_sample_rate * AUDIO_PTIME),
        )

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
        room_id = 1234

        response = await self.plugin_handle.join(
            room_id=room_id, display_name="Test video room publish"
        )
        if not response:
            raise Exception("Failed to join room")

        # player = MediaPlayer("./Into.the.Wild.2007.mp4")
        stream_tracks = []
        if self.__audio_stream_track:
            stream_tracks.append(self.__audio_stream_track)
        if self.__video_stream_track:
            stream_tracks.append(self.__video_stream_track)
        response = await self.plugin_handle.publish(stream_track=stream_tracks)
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

        async def main():
            await self.__thread_loop_quit.wait()

        self.loop.run_until_complete(main())
        logger.info("Event loop thread finishing")

    def get_frame_task_done_cb(self, task: asyncio.Task, context=None) -> None:
        try:
            # Check if any exceptions are raised
            task.exception()
        except asyncio.CancelledError:
            logger.info("Get frame task ended")
        except asyncio.InvalidStateError:
            logger.info("get_frame_task_done_cb called with invalid state")
        except Exception as err:
            logger.error(err)

    def start(self) -> None:
        file = "desktop"
        format = "gdigrab"
        options = {
            "video_size": f"{self.video_width}x{self.video_height}",
            "framerate": "30",
            "offset_x": str(self.offset_x),
            "offset_y": str(self.offset_y),
            "show_region": "1",
        }
        self.container = av.open(
            file=file, format=format, mode="r", options=options, timeout=None
        )

        self.__audio_stream = None
        self.__video_stream = None
        self.__stream = []
        for stream in self.container.streams:
            if stream.type == "audio" and not self.__audio_stream:
                self.__audio_stream = stream
                self.__audio_stream_track = PlayerStreamTrack(kind="audio")
                self.__stream.append(stream)
            elif stream.type == "video" and not self.__video_stream:
                self.__video_stream = stream
                self.__video_stream_track = PlayerStreamTrack(kind="video")
                self.__stream.append(stream)

        self.__thread_quit = threading.Event()
        self.__thread = threading.Thread(
            target=self.get_frame,
            args=(1, self.container),
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
            asyncio.run_coroutine_threadsafe(self.start_publish(), self.loop).result()

    def stop(self) -> None:
        if self.__thread:
            self.__thread_quit.set()
            # Give up on ending the thread cleanly
            # self.__thread.join()
            self.__thread = None

        if self.__thread_loop:
            if self.__server_url:
                asyncio.run_coroutine_threadsafe(
                    self.stop_publish(), self.loop
                ).result()
                asyncio.run_coroutine_threadsafe(
                    self.disconnect_server(), self.loop
                ).result()

            self.loop.call_soon_threadsafe(self.__thread_loop_quit.set)

            # self.loop.stop()
            self.__thread_loop.join()
            self.__thread_loop = None

        self.container.close()

    def send_frame(self, frame: av.VideoFrame) -> None:
        if isinstance(frame, AudioFrame) and self.__audio_stream:
            for frame in self.audio_resampler.resample(frame):
                # fix timestamps
                frame.pts = self.audio_samples
                frame.time_base = self.audio_time_base
                self.audio_samples += frame.samples

                asyncio.run_coroutine_threadsafe(
                    self.__audio_stream_track._queue.put(frame), self.loop
                )
        elif isinstance(frame, VideoFrame) and self.__video_stream:
            if frame.pts is None:  # pragma: no cover
                logger.warning(
                    "MediaPlayer(%s) Skipping video frame with no pts",
                    self.container.name,
                )
                return

            # # video from a webcam doesn't start at pts 0, cancel out offset
            # if video_first_pts is None:
            #     video_first_pts = frame.pts
            # frame.pts -= video_first_pts

            asyncio.run_coroutine_threadsafe(
                self.__video_stream_track._queue.put(frame), self.loop
            )

    def display_frame(self, frame: av.VideoFrame):
        image = frame.to_image()

        self.current_frame_image = ImageTk.PhotoImage(image)

        self.video_player.config(image=self.current_frame_image)

    def get_frame(self, name, container):
        logging.info("Thread %s: starting", name)

        video_first_pts = None

        while not self.__thread_quit.is_set():
            try:
                frame = next(container.decode(*self.__stream))
            except Exception as exc:
                if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                    logger.error(exc)
                    time.sleep(0.01)
                    continue

                break

            # print(frame)

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

                self.display_frame(frame=frame)
                self.send_frame(frame=frame)

        logging.info("Thread %s: finishing", name)
