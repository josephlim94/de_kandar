import logging
import sys
import threading
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk
import ffmpeg


format = "%(asctime)s: %(message)s"
logging.basicConfig(format=format, level=logging.INFO, datefmt="%H:%M:%S")


class Application:
    video_width: int = 640
    video_height: int = 480

    def __init__(self) -> None:
        self.bg = "#E6FBFF"
        self.fg = "#8A84E2"

        self.main_window = tk.Tk()
        self.main_window.title("De Kandar")
        self.main_window.geometry("")
        self.main_window.config(bg=self.bg)
        self.main_window.resizable(0, 0)

        self.title = tk.Label(
            self.main_window,
            text="De Kandar",
            font=("HELVETICA", 28, "bold"),
            bg=self.bg,
            fg=self.fg,
        )
        self.title.grid(row=0, column=0, sticky=tk.N, pady=(40, 20), padx=40)

        self.snip_button = tk.Button(
            self.main_window,
            text="Select Area",
            font=("TIMES NEW ROMAN", 14),
            bg=self.fg,
            fg=self.bg,
            height=2,
            width=12,
            command=self.selectArea,
            bd=7,
            relief=tk.RAISED,
        )
        self.snip_button.grid(row=1, column=0, pady=30, padx=20)

        self.video_player = tk.Label(
            self.main_window,
            bg=self.fg,
            fg=self.bg,
        )
        self.video_player.grid(row=2, column=0, pady=30, padx=20)

        blank_image = Image.new(
            "RGB", (self.video_width, self.video_height), (255, 255, 255)
        )
        self.current_frame = ImageTk.PhotoImage(blank_image)
        self.video_player.config(image=self.current_frame)

        self.menu = tk.Menu(self.main_window)

        self.ex = tk.Menu(self.menu, tearoff=0)
        self.ex.add_command(label="Exit", command=self.eex)

        self.menu.add_cascade(label="Exit", menu=self.ex)

        self.main_window.config(menu=self.menu)

        # bring to front
        self.raise_above_all(self.main_window)

    def startMainLoop(self):
        self.main_window.mainloop()

    def get_and_display_frame(self, name):
        logging.info("Thread %s: starting", name)
        while True:
            in_bytes = self.record_video_process.stdout.read(
                self.video_width * self.video_height * 3
            )
            if not in_bytes:
                break

            in_frame = np.frombuffer(in_bytes, np.uint8).reshape(
                [self.video_height, self.video_width, 3]
            )

            im = Image.fromarray(in_frame)

            self.current_frame = ImageTk.PhotoImage(im)

            self.video_player.config(image=self.current_frame)

        self.record_video_process.wait()

        logging.info("Thread %s: finishing", name)

    def selectArea(self):
        self.rect = None
        self.x = self.y = 0
        self.start_x = None
        self.start_y = None
        self.curX = None
        self.curY = None

        self.master_screen = tk.Toplevel(self.main_window)
        self.master_screen.withdraw()
        self.master_screen.attributes("-transparent", "blue")
        self.picture_frame = tk.Frame(self.master_screen, background="blue")
        self.picture_frame.pack(fill=tk.BOTH, expand=tk.YES)

        self.master_screen.deiconify()
        self.main_window.withdraw()

        self.screenCanvas = tk.Canvas(self.picture_frame, cursor="cross", bg="grey11")
        self.screenCanvas.pack(fill=tk.BOTH, expand=tk.YES)

        self.screenCanvas.bind("<ButtonPress-1>", self.on_button_press)
        self.screenCanvas.bind("<B1-Motion>", self.on_move_press)
        self.screenCanvas.bind("<ButtonRelease-1>", self.on_button_release)

        self.master_screen.attributes("-fullscreen", True)
        self.master_screen.attributes("-alpha", 0.3)
        self.master_screen.lift()
        self.master_screen.attributes("-topmost", True)

    def on_button_release(self, event):
        self.end_x = self.screenCanvas.canvasx(event.x)
        self.end_y = self.screenCanvas.canvasy(event.y)

        self.offset_x = int(min(self.start_x, self.end_x))
        self.offset_y = int(min(self.start_y, self.end_y))
        self.video_width = int(max(self.start_x, self.end_x) - self.offset_x)
        self.video_height = int(max(self.start_y, self.end_y) - self.offset_y)

        logging.info(
            f"({self.offset_x}, {self.offset_y}) - {self.video_width}x{self.video_height}"
        )

        if self.video_width <= 0 or self.video_height <= 0:
            logging.info("Video size too small")
            return event

        self.master_screen.withdraw()

        self.main_window.deiconify()

        # Get pix_fmt by running "ffmpeg -pix_fmts"
        self.record_video_process = (
            ffmpeg.input(
                "desktop",
                format="gdigrab",
                framerate=30,
                offset_x=self.offset_x,
                offset_y=self.offset_y,
                # s=f"{width}x{height}",
                video_size=[
                    self.video_width,
                    self.video_height,
                ],  # Using this video_size=[] or s="" is the same
                show_region=1,
            )
            .output("pipe:", format="rawvideo", pix_fmt="rgb24")
            .run_async(pipe_stdout=True)
        )
        x = threading.Thread(target=self.get_and_display_frame, args=(1,))
        x.start()

        return event

    def on_button_press(self, event):
        # save mouse drag start position
        self.start_x = self.screenCanvas.canvasx(event.x)
        self.start_y = self.screenCanvas.canvasy(event.y)

        self.rect = self.screenCanvas.create_rectangle(
            self.x, self.y, 1, 1, outline="white", width=2, fill="blue"
        )

    def on_move_press(self, event):
        self.curX = self.screenCanvas.canvasx(event.x)
        self.curY = self.screenCanvas.canvasy(event.y)
        # expand rectangle as you drag the mouse
        self.screenCanvas.coords(
            self.rect, self.start_x, self.start_y, self.curX, self.curY
        )

    def raise_above_all(self, window):
        """brings window to the front"""
        window.attributes("-topmost", 1)
        window.attributes("-topmost", 0)

    def eex(self):
        self.main_window.destroy()
        sys.exit()


app = Application()
app.startMainLoop()
