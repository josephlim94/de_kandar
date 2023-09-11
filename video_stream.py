import threading
import logging
import errno
import time
from PIL import ImageTk, Image
import tkinter as tk
import asyncio
from typing import Optional, Set, Union

import janus_client
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import AUDIO_PTIME, MediaStreamError, MediaStreamTrack
import av
from av import AudioFrame, VideoFrame
from av.audio import AudioStream
from av.frame import Frame
from av.packet import Packet
from av.video.stream import VideoStream

format = "%(asctime)s: %(message)s"
logging.basicConfig(format=format, level=logging.INFO, datefmt="%H:%M:%S")
logger = logging.getLogger()


def player_worker_decode(
    loop,
    container,
    streams,
    audio_track,
    video_track,
    quit_event,
    throttle_playback,
    loop_playback,
):
    audio_sample_rate = 48000
    audio_samples = 0
    audio_time_base = fractions.Fraction(1, audio_sample_rate)
    audio_resampler = av.AudioResampler(
        format="s16",
        layout="stereo",
        rate=audio_sample_rate,
        frame_size=int(audio_sample_rate * AUDIO_PTIME),
    )

    video_first_pts = None

    frame_time = None
    start_time = time.time()

    while not quit_event.is_set():
        try:
            frame = next(container.decode(*streams))
        except Exception as exc:
            if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                time.sleep(0.01)
                continue
            if isinstance(exc, StopIteration) and loop_playback:
                container.seek(0)
                continue
            if audio_track:
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(None), loop)
            if video_track:
                asyncio.run_coroutine_threadsafe(video_track._queue.put(None), loop)
            break

        # read up to 1 second ahead
        if throttle_playback:
            elapsed_time = time.time() - start_time
            if frame_time and frame_time > elapsed_time + 1:
                time.sleep(0.1)

        if isinstance(frame, AudioFrame) and audio_track:
            for frame in audio_resampler.resample(frame):
                # fix timestamps
                frame.pts = audio_samples
                frame.time_base = audio_time_base
                audio_samples += frame.samples

                frame_time = frame.time
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(frame), loop)
        elif isinstance(frame, VideoFrame) and video_track:
            if frame.pts is None:  # pragma: no cover
                logger.warning(
                    "MediaPlayer(%s) Skipping video frame with no pts", container.name
                )
                continue

            # video from a webcam doesn't start at pts 0, cancel out offset
            if video_first_pts is None:
                video_first_pts = frame.pts
            frame.pts -= video_first_pts

            frame_time = frame.time
            asyncio.run_coroutine_threadsafe(video_track._queue.put(frame), loop)


def player_worker_demux(
    loop,
    container,
    streams,
    audio_track,
    video_track,
    quit_event,
    throttle_playback,
    loop_playback,
):
    video_first_pts = None
    frame_time = None
    start_time = time.time()

    while not quit_event.is_set():
        try:
            packet = next(container.demux(*streams))
            if not packet.size:
                raise StopIteration
        except Exception as exc:
            if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                time.sleep(0.01)
                continue
            if isinstance(exc, StopIteration) and loop_playback:
                container.seek(0)
                continue
            if audio_track:
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(None), loop)
            if video_track:
                asyncio.run_coroutine_threadsafe(video_track._queue.put(None), loop)
            break

        # read up to 1 second ahead
        if throttle_playback:
            elapsed_time = time.time() - start_time
            if frame_time and frame_time > elapsed_time + 1:
                time.sleep(0.1)

        track = None
        if isinstance(packet.stream, AudioStream) and audio_track:
            track = audio_track
        elif isinstance(packet.stream, VideoStream) and video_track:
            if packet.pts is None:  # pragma: no cover
                logger.warning(
                    "MediaPlayer(%s) Skipping video packet with no pts", container.name
                )
                continue
            track = video_track

            # video from a webcam doesn't start at pts 0, cancel out offset
            if video_first_pts is None:
                video_first_pts = packet.pts
            packet.pts -= video_first_pts

        if (
            track is not None
            and packet.pts is not None
            and packet.time_base is not None
        ):
            frame_time = int(packet.pts * packet.time_base)
            asyncio.run_coroutine_threadsafe(track._queue.put(packet), loop)


class PlayerStreamTrack(MediaStreamTrack):
    def __init__(self, player, kind):
        super().__init__()
        self.kind = kind
        self._player = player
        self._queue = asyncio.Queue()
        self._start = None

    async def recv(self) -> Union[Frame, Packet]:
        if self.readyState != "live":
            raise MediaStreamError

        self._player._start(self)
        data = await self._queue.get()
        if data is None:
            self.stop()
            raise MediaStreamError
        if isinstance(data, Frame):
            data_time = data.time
        elif isinstance(data, Packet):
            data_time = float(data.pts * data.time_base)

        # control playback rate
        if (
            self._player is not None
            and self._player._throttle_playback
            and data_time is not None
        ):
            if self._start is None:
                self._start = time.time() - data_time
            else:
                wait = self._start + data_time - time.time()
                await asyncio.sleep(wait)

        return data

    def stop(self):
        super().stop()
        if self._player is not None:
            self._player._stop(self)
            self._player = None


class MediaPlayer:
    """
    A media source that reads audio and/or video from a file.

    Examples:

    .. code-block:: python

        # Open a video file.
        player = MediaPlayer('/path/to/some.mp4')

        # Open an HTTP stream.
        player = MediaPlayer(
            'http://download.tsi.telecom-paristech.fr/'
            'gpac/dataset/dash/uhd/mux_sources/hevcds_720p30_2M.mp4')

        # Open webcam on Linux.
        player = MediaPlayer('/dev/video0', format='v4l2', options={
            'video_size': '640x480'
        })

        # Open webcam on OS X.
        player = MediaPlayer('default:none', format='avfoundation', options={
            'video_size': '640x480'
        })

        # Open webcam on Windows.
        player = MediaPlayer('video=Integrated Camera', format='dshow', options={
            'video_size': '640x480'
        })

    :param file: The path to a file, or a file-like object.
    :param format: The format to use, defaults to autodect.
    :param options: Additional options to pass to FFmpeg.
    :param timeout: Open/read timeout to pass to FFmpeg.
    :param loop: Whether to repeat playback indefinitely (requires a seekable file).
    """

    def __init__(
        self, file, format=None, options={}, timeout=None, loop=False, decode=True
    ):
        self.__container = av.open(
            file=file, format=format, mode="r", options=options, timeout=timeout
        )
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[PlayerStreamTrack] = set()
        self.__streams = []
        self.__decode = decode
        self.__audio: Optional[PlayerStreamTrack] = None
        self.__video: Optional[PlayerStreamTrack] = None
        for stream in self.__container.streams:
            if stream.type == "audio" and not self.__audio:
                if self.__decode:
                    self.__audio = PlayerStreamTrack(self, kind="audio")
                    self.__streams.append(stream)
                elif stream.codec_context.name in ["opus", "pcm_alaw", "pcm_mulaw"]:
                    self.__audio = PlayerStreamTrack(self, kind="audio")
                    self.__streams.append(stream)
            elif stream.type == "video" and not self.__video:
                if self.__decode:
                    self.__video = PlayerStreamTrack(self, kind="video")
                    self.__streams.append(stream)
                elif stream.codec_context.name in ["h264", "vp8"]:
                    self.__video = PlayerStreamTrack(self, kind="video")
                    self.__streams.append(stream)

        # check whether we need to throttle playback
        container_format = set(self.__container.format.name.split(","))
        self._throttle_playback = not container_format.intersection(REAL_TIME_FORMATS)

        # check whether the looping is supported
        assert (
            not loop or self.__container.duration is not None
        ), "The `loop` argument requires a seekable file"
        self._loop_playback = loop

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video

    def _start(self, track: PlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                target=player_worker_decode if self.__decode else player_worker_demux,
                args=(
                    asyncio.get_event_loop(),
                    self.__container,
                    self.__streams,
                    self.__audio,
                    self.__video,
                    self.__thread_quit,
                    self._throttle_playback,
                    self._loop_playback,
                ),
            )
            self.__thread.start()

    def _stop(self, track: PlayerStreamTrack) -> None:
        self.__started.discard(track)

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

        if not self.__started and self.__container is not None:
            self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"MediaPlayer(%s) {msg}", self.__container.name, *args)


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

        self.__thread_quit = threading.Event()
        self.__thread = threading.Thread(
            target=self.get_and_display_frame,
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

        # async def start_get_frame_task():
        #     return asyncio.create_task(self.get_frame_task())

        # self.__get_frame_task_quit = asyncio.Event()
        # self.__get_frame_task = asyncio.run_coroutine_threadsafe(
        #     start_get_frame_task(), self.loop
        # ).result()
        # self.__get_frame_task.add_done_callback(self.get_frame_task_done_cb)

        if self.__server_url:
            asyncio.run_coroutine_threadsafe(self.connect_server(), self.loop).result()

    def stop(self) -> None:
        if self.__thread:
            self.__thread_quit.set()
            # Give up on ending the thread cleanly
            # self.__thread.join()
            self.__thread = None

        # if self.__get_frame_task:
        #     self.__get_frame_task.cancel()

        if self.__thread_loop:
            if self.__server_url:
                asyncio.run_coroutine_threadsafe(
                    self.disconnect_server(), self.loop
                ).result()

            self.loop.call_soon_threadsafe(self.__thread_loop_quit.set)

            # self.loop.stop()
            self.__thread_loop.join()
            self.__thread_loop = None

        self.container.close()

    # async def get_frame_task(self) -> None:
    #     logging.info("Get frame task starting")

    #     video_first_pts = None

    #     while not self.__get_frame_task_quit.is_set():
    #         try:
    #             frame = next(self.container.decode())
    #         except Exception as exc:
    #             if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
    #                 logger.error(exc)
    #                 time.sleep(0.01)
    #                 continue

    #             break

    #         print(frame)

    #         if isinstance(frame, VideoFrame):
    #             if frame.pts is None:
    #                 logger.warning(
    #                     f"MediaPlayer({self.container.name}) Skipping video frame with no pts",
    #                 )
    #                 continue

    #             # video from a webcam doesn't start at pts 0, cancel out offset
    #             if video_first_pts is None:
    #                 video_first_pts = frame.pts
    #             frame.pts -= video_first_pts

    #             image = frame.to_image()

    #             self.current_frame_image = ImageTk.PhotoImage(image)

    #             self.video_player.config(image=self.current_frame_image)

    #     logging.info("Get frame task: finishing")

    def display_frame(self, frame: av.VideoFrame):
        image = frame.to_image()

        self.current_frame_image = ImageTk.PhotoImage(image)

        self.video_player.config(image=self.current_frame_image)

    def get_and_display_frame(self, name, container):
        logging.info("Thread %s: starting", name)

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

            print(frame)

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

        logging.info("Thread %s: finishing", name)
