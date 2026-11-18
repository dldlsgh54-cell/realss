import base64
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk

from studio import ff, media_info


def video_properties(path):
    info = media_info(path)
    video = next((stream for stream in info['streams'] if stream['codec_type'] == 'video'
                  and not stream.get('disposition', {}).get('attached_pic')), None)
    if video is None:
        raise ValueError('재생 가능한 영상 트랙을 찾을 수 없습니다.')
    duration = float(video.get('duration') or info['format'].get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('영상 길이를 읽을 수 없습니다.')
    width, height = video['width'], video['height']
    rotation = next((side['rotation'] for side in video.get('side_data_list', ()) if 'rotation' in side), 0)
    if round(rotation) % 180 == 90:
        width, height = height, width
    return duration, width, height


def frame_image(path, seconds, width, height, mirrored=False):
    mirror = 'hflip,' if mirrored else ''
    png = ff('-ss', f'{seconds:.4f}', '-i', path, '-map', '0:V:0', '-frames:v', 1,
             '-vf', f'{mirror}scale={width}:{height}:force_original_aspect_ratio=decrease:reset_sar=1',
             '-c:v', 'png', '-threads', 1, '-f', 'image2pipe', '-')
    if not png:
        raise ValueError('선택한 시점의 화면을 읽을 수 없습니다.')
    return base64.b64encode(png)


def clock_text(position):
    milliseconds = round(position * 1000)
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    return f'{minutes:02}:{second:02}.{fraction:03}'


class Viewer:
    def __init__(self, parent, colors, browse, open_media):
        self.parent = parent
        self.colors = colors
        self.browse = browse
        self.path = None
        self.mirrored = False
        self.initial_position = 0
        self.duration = 0
        self.position = tk.DoubleVar()
        self.time = tk.StringVar(value='00:00.000  /  00:00.000')
        self.detail = tk.StringVar()
        self.image = None
        self.timer = None
        self.generation = 0
        self.frame_number = 0
        self.requests = queue.Queue(maxsize=1)
        self.results = queue.Queue()
        self.canvas = tk.Canvas(parent, background=colors['stage'], highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.canvas.bind('<Configure>', self.resize)
        self.canvas.tag_bind('browse', '<Button-1>', lambda _: browse())
        self.canvas.tag_bind('browse', '<Enter>', lambda _: self.canvas.configure(cursor='hand2'))
        self.canvas.tag_bind('browse', '<Leave>', lambda _: self.canvas.configure(cursor=''))

        transport = tk.Frame(parent, bg=colors['panel'], padx=16, pady=12)
        transport.grid(row=2, column=0, sticky='ew')
        transport.columnconfigure(0, weight=1)
        tk.Label(transport, textvariable=self.time, bg=colors['panel'], fg=colors['text'],
                 font=('Consolas', 11)).grid(row=0, column=0, sticky='w')
        self.play = ttk.Button(transport, text='플레이어에서 재생', style='Quiet.TButton', command=open_media)
        self.play.grid(row=0, column=1, sticky='e')
        self.slider = ttk.Scale(transport, from_=0, to=1, variable=self.position,
                               style='Studio.Horizontal.TScale', command=self.seek)
        self.slider.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.slider.bind('<Left>', lambda _: self.step(-1 / 30))
        self.slider.bind('<Right>', lambda _: self.step(1 / 30))
        self.ruler = tk.Canvas(transport, height=21, background=colors['panel'], highlightthickness=0)
        self.ruler.grid(row=2, column=0, columnspan=2, sticky='ew')
        self.ruler.bind('<Configure>', lambda _: self.draw_ruler())
        self.slider.state(['disabled'])
        self.play.state(['disabled'])
        self.worker = threading.Thread(target=self.decode, daemon=True)
        self.worker.start()
        self.parent.bind('<Destroy>', self.close, add='+')
        self.parent.after(45, self.poll)

    def show(self, path, start=0, mirrored=False):
        if self.path == path and self.mirrored == mirrored:
            return
        self.generation += 1
        self.path = path
        self.mirrored = mirrored
        self.initial_position = start
        self.duration = 0
        self.position.set(0)
        self.detail.set('')
        self.time.set('00:00.000  /  00:00.000')
        self.slider.state(['disabled'])
        self.play.state(['disabled'])
        self.image = None
        if self.timer is not None:
            self.parent.after_cancel(self.timer)
        self.timer = None
        self.draw('영상 불러오는 중' if path else None)
        self.draw_ruler()
        if path:
            self.submit(('info', self.generation, path))

    def resize(self, _):
        self.draw()
        if self.duration:
            self.schedule()

    def draw(self, message=None):
        canvas = self.canvas
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        x, y = width / 2, height / 2
        if self.image is not None:
            half_w, half_h = self.image.width() / 2, self.image.height() / 2
            canvas.create_rectangle(x - half_w - 1, y - half_h - 1, x + half_w + 1, y + half_h + 1,
                                    outline=self.colors['line'])
            canvas.create_image(x, y, image=self.image)
            return
        if self.path:
            canvas.create_text(x, y, text=message or '프리뷰 준비 중', fill=self.colors['muted'],
                               font=('Malgun Gothic', 10))
            return

        card_h = min(max(140, height * .44), 244)
        card_w = card_h * 9 / 16
        top = max(14, y - card_h / 2 - 46)
        left, right, bottom = x - card_w / 2, x + card_w / 2, top + card_h
        canvas.create_rectangle(left, top, right, bottom, outline='#514967', fill='#1b1827', width=1)
        canvas.create_rectangle(left + 9, top + card_h * .16, right - 9, bottom - card_h * .19,
                                outline='#423854', dash=(3, 4))
        for ratio in (1 / 3, 2 / 3):
            canvas.create_line(left + 9, top + card_h * ratio, right - 9, top + card_h * ratio,
                               fill='#30283f', dash=(3, 4))
        middle = (top + bottom) / 2 - 3
        canvas.create_oval(x - 24, middle - 24, x + 24, middle + 24, fill='#2c2346', outline='#52416f')
        canvas.create_polygon(x - 5, middle - 9, x - 5, middle + 9, x + 10, middle,
                              fill=self.colors['accent'], outline='')
        canvas.create_text(x, bottom + 26, text='원본 영상 미리보기', fill=self.colors['text'],
                           font=('Malgun Gothic', 13, 'bold'))
        canvas.create_rectangle(x - 63, bottom + 43, x + 63, bottom + 76, outline='#4e426a',
                                fill='#2b233b', tags='browse')
        canvas.create_text(x, bottom + 59, text='영상 불러오기', fill=self.colors['accent'],
                           font=('Malgun Gothic', 10, 'bold'), tags='browse')

    def seek(self, _=None):
        if self.duration:
            self.time.set(f'{clock_text(self.position.get())}  /  {clock_text(self.duration)}')
            self.schedule()

    def step(self, delta):
        if self.duration:
            self.position.set(min(float(self.slider['to']), max(0, self.position.get() + delta)))
            self.seek()
        return 'break'

    def draw_ruler(self):
        self.ruler.delete('all')
        width = self.ruler.winfo_width()
        if width < 12:
            return
        for tick in range(17):
            x = 5 + (width - 10) * tick / 16
            major = tick % 4 == 0
            self.ruler.create_line(x, 2, x, 8 if major else 5, fill=self.colors['muted'] if major else self.colors['line'])
            if self.duration and major:
                timecode = clock_text(self.duration * tick / 16)
                value = timecode[:-2] if self.duration < 12 else timecode.split('.')[0]
                anchor = 'w' if tick == 0 else 'e' if tick == 16 else 'center'
                self.ruler.create_text(x, 16, text=value, fill=self.colors['muted'], anchor=anchor, font=('Consolas', 8))

    def schedule(self):
        if self.timer is not None:
            self.parent.after_cancel(self.timer)
        self.timer = self.parent.after(95, self.request_frame)

    def request_frame(self):
        self.timer = None
        self.frame_number += 1
        width = max(32, min(1200, self.canvas.winfo_width() - 26))
        height = max(32, min(1000, self.canvas.winfo_height() - 26))
        self.submit(('frame', self.generation, self.path, self.frame_number, self.position.get(), width, height,
                     self.mirrored))

    def submit(self, request):
        try:
            self.requests.put_nowait(request)
        except queue.Full:
            try:
                self.requests.get_nowait()
            except queue.Empty:
                pass
            self.requests.put_nowait(request)

    def decode(self):
        while True:
            request = self.requests.get()
            if request is None:
                return
            operation, generation, path, *args = request
            try:
                value = video_properties(path) if operation == 'info' else (args[0], frame_image(path, *args[1:]))
                self.results.put((operation, generation, value))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                self.results.put(('error', generation, operation))

    def poll(self):
        while not self.results.empty():
            operation, generation, value = self.results.get_nowait()
            if generation != self.generation:
                continue
            if operation == 'info':
                self.duration, width, height = value
                self.detail.set(f'{width} × {height}')
                limit = max(0, self.duration - .045)
                self.slider.configure(to=limit)
                self.position.set(min(self.initial_position, limit))
                self.slider.state(['!disabled'])
                self.play.state(['!disabled'])
                self.draw_ruler()
                self.seek()
            elif operation == 'frame' and value[0] == self.frame_number:
                self.image = tk.PhotoImage(master=self.canvas, data=value[1])
                self.draw()
            elif operation == 'error' and self.image is None:
                if value == 'info':
                    self.detail.set('영상 정보 읽기 실패')
                self.draw('미리보기를 불러올 수 없습니다')
        self.parent.after(45, self.poll)

    def close(self, event):
        if event.widget == self.parent:
            self.submit(None)
