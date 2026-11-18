import tkinter as tk
from tkinter import messagebox, ttk

from preview import frame_image, video_properties


class SubtitleRegionDialog:
    def __init__(self, app, source):
        self.app = app
        self.source = source
        duration, _, _ = video_properties(source)
        self.region = app.region
        self.anchor = None
        self.timer = None
        self.max_height = min(520, int(app.root.winfo_screenheight() * .58))
        self.image = self.preview(max(0, min(duration * .12, 3)), app.root)
        self.width, self.height = self.image.width(), self.image.height()

        dialog = self.dialog = tk.Toplevel(app.root, bg=app.colors['panel'])
        dialog.title('원본 자막 영역 지정')
        dialog.transient(app.root)
        dialog.protocol('WM_DELETE_WINDOW', self.close)
        app.label(dialog, '자막이 나오는 구간을 찾고 글자 주변을 드래그하세요.', 10, app.colors['text']).pack(padx=16, pady=(12, 6))
        self.canvas = tk.Canvas(dialog, width=self.width, height=self.height, highlightthickness=0, cursor='crosshair')
        self.canvas.pack(padx=16)
        self.canvas.create_image(0, 0, image=self.image, anchor='nw', tags='preview')
        self.draw_region()
        self.canvas.bind('<ButtonPress-1>', self.begin)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.finish)
        position = tk.DoubleVar(value=max(0, min(duration * .12, 3)))
        self.position = position
        slider = ttk.Scale(dialog, from_=0, to=max(0, duration - .10), variable=position, style='Studio.Horizontal.TScale',
                           command=self.schedule, length=self.width)
        slider.pack(fill='x', padx=16, pady=10)
        actions = tk.Frame(dialog, bg=app.colors['panel'])
        actions.pack(fill='x', padx=16, pady=(0, 12))
        app.button(actions, '자동 탐지 사용', self.reset).pack(side='left')
        app.button(actions, '영역 적용', self.apply, primary=True).pack(side='right')
        dialog.grab_set()

    def preview(self, seconds, parent):
        return tk.PhotoImage(master=parent, data=frame_image(self.source, seconds, 760, self.max_height))

    def schedule(self, _):
        if self.timer is not None:
            self.dialog.after_cancel(self.timer)
        self.timer = self.dialog.after(180, self.refresh)

    def refresh(self):
        self.timer = None
        try:
            self.image = self.preview(self.position.get(), self.dialog)
        except (RuntimeError, ValueError):
            return
        self.canvas.itemconfigure('preview', image=self.image)

    def draw_region(self):
        self.canvas.delete('region')
        if self.region:
            x, y, right, bottom = self.region
            self.canvas.create_rectangle(x * self.width, y * self.height, right * self.width,
                                         bottom * self.height, outline=self.app.colors['accent'], width=2, tags='region')

    def point(self, event):
        return min(1, max(0, event.x / self.width)), min(1, max(0, event.y / self.height))

    def begin(self, event):
        self.anchor = self.point(event)

    def drag(self, event):
        if self.anchor is None:
            return
        x, y = self.point(event)
        ax, ay = self.anchor
        self.region = min(x, ax), min(y, ay), max(x, ax), max(y, ay)
        self.draw_region()

    def finish(self, event):
        self.drag(event)
        self.anchor = None

    def reset(self):
        self.app.reset_region()
        self.close()

    def apply(self):
        if not self.region:
            return
        x, y, right, bottom = self.region
        if (right - x) * self.width < 12 or (bottom - y) * self.height < 12:
            messagebox.showinfo('자막 영역', '자막 전체가 들어가도록 영역을 넓게 지정하세요.', parent=self.dialog)
            return
        self.app.region = self.region
        self.app.region_label.set('지정 영역 복원 · 원본 소리 제외')
        self.close()

    def close(self):
        if self.timer is not None:
            self.dialog.after_cancel(self.timer)
        self.dialog.destroy()
