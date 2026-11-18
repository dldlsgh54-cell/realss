import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from preview import Viewer
from regions import SubtitleRegionDialog
from studio import Job, PLATFORMS, ROOT, VOICE_OPTIONS, render


COLORS = {
    'bg': '#111118', 'panel': '#1a1a24', 'surface': '#222230', 'field': '#14141d',
    'stage': '#101018', 'line': '#303043', 'text': '#f4f1fb', 'muted': '#aaa7bc',
    'accent': '#c0aeff', 'primary': '#aa92ff', 'selected': '#302648',
    'success': '#83d7b0', 'danger': '#ffaaa8',
}
FONT = 'Malgun Gothic'
LAYOUTS = {'자동 · 원본 비율에 맞춤': 'auto', '화면 채우기 · 중앙 크롭': 'crop',
           '원본 전체 · 블러 배경': 'blur'}
VIDEO_TYPES = [('동영상', '*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.ts'), ('모든 파일', '*.*')]
MUSIC_TYPES = [('오디오', '*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.wma'), ('모든 파일', '*.*')]
PLATFORM_CARDS = {
    'reels': ('IG', '#462b50', '#f3acff', '결과 중심 편집'),
    'tiktok': ('TT', '#213e46', '#99e8ef', '짧은 훅 · 빠른 컷'),
    'naver': ('N', '#213e36', '#a1eac2', '정보 중심 편집'),
}


def compact_name(text, max_chars=30):
    if len(text) <= max_chars:
        return text
    return f'{text[:max_chars - 9]}…{text[-8:]}'


class Application:
    def __init__(self, root):
        self.root = root
        self.colors = COLORS
        self.events = queue.Queue()
        self.running = False
        self.result = None
        self.result_platforms = ()
        self.sources = []
        self.regions = {}
        self.mirrors = set()
        self.mode = 'source'
        self.controls = []
        self.cards = {}

        self.video = tk.StringVar()
        self.music = tk.StringVar()
        self.folder = tk.StringVar(value=str(ROOT / '결과물'))
        self.duration = tk.StringVar(value='30초')
        self.voice = tk.StringVar(value=next(iter(VOICE_OPTIONS)))
        self.layout = tk.StringVar(value=next(iter(LAYOUTS)))
        self.status = tk.StringVar(value='원본 영상 선택 대기 중')
        self.region_label = tk.StringVar(value='자동 자막 복원 · 원본 소리 제외')
        self.preview = tk.StringVar(value=PLATFORMS[0].name)
        self.output_checks = {platform.profile: tk.BooleanVar(value=True) for platform in PLATFORMS}
        self.percent = tk.StringVar(value='0%')
        self.source_name = tk.StringVar(value='원본 추가')
        self.mirror = tk.BooleanVar()
        self.music_name = tk.StringVar(value='플랫폼별 기본 합성곡')
        self.folder_name = tk.StringVar(value='결과물')

        root.title('릴스 스튜디오')
        width = min(1360, root.winfo_screenwidth() - 60)
        height = min(830, root.winfo_screenheight() - 80)
        root.geometry(f'{width}x{height}')
        root.minsize(min(1020, width), min(760, height))
        root.configure(bg=COLORS['bg'])
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self.configure_styles()
        self.build_header()

        workspace = tk.Frame(root, bg=COLORS['bg'])
        workspace.grid(row=1, column=0, sticky='nsew', padx=18, pady=(14, 12))
        workspace.columnconfigure(1, weight=1)
        workspace.rowconfigure(0, weight=1)
        self.build_project(workspace)
        self.build_viewer(workspace)
        self.build_exports(workspace)
        self.build_footer()

        self.video.trace_add('write', self.source_changed)
        self.music.trace_add('write', self.music_changed)
        self.folder.trace_add('write', self.folder_changed)
        root.bind('<Control-o>', lambda _: self.browse_source())
        root.bind('<F5>', lambda _: self.start_job())
        self.sync_view()
        root.after(80, self.poll)

    def configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('Quiet.TButton', font=(FONT, 9, 'bold'), foreground=COLORS['text'],
                        background=COLORS['surface'], bordercolor=COLORS['line'], padding=(10, 6),
                        focusthickness=1, focuscolor=COLORS['accent'], lightcolor=COLORS['surface'],
                        darkcolor=COLORS['surface'])
        style.map('Quiet.TButton', background=[('active', '#333347')],
                  foreground=[('disabled', '#77758a')])
        style.configure('Primary.TButton', font=(FONT, 10, 'bold'), padding=(17, 9),
                        foreground='#161021', background=COLORS['primary'], bordercolor=COLORS['primary'],
                        lightcolor=COLORS['primary'], darkcolor=COLORS['primary'], focuscolor='#e5ddff')
        style.map('Primary.TButton', background=[('disabled', '#3b324e'), ('active', '#c0acff')],
                  foreground=[('disabled', '#c0b7d0')])
        style.configure('Tab.TButton', font=(FONT, 9, 'bold'), padding=(12, 6),
                        background=COLORS['panel'], foreground=COLORS['muted'], borderwidth=0,
                        focuscolor=COLORS['accent'], lightcolor=COLORS['panel'], darkcolor=COLORS['panel'])
        style.map('Tab.TButton', background=[('active', COLORS['surface'])],
                  foreground=[('disabled', '#77758a'), ('active', COLORS['text'])])
        style.configure('Current.Tab.TButton', background=COLORS['selected'], foreground=COLORS['accent'])
        style.map('Current.Tab.TButton', background=[('active', '#42345f')])
        self.configure_output_checkbox(style)
        style.configure('Studio.TCombobox', fieldbackground=COLORS['field'], background=COLORS['surface'],
                        foreground=COLORS['text'], padding=5, arrowcolor=COLORS['muted'],
                        bordercolor=COLORS['line'], lightcolor=COLORS['field'], darkcolor=COLORS['field'])
        style.map('Studio.TCombobox', fieldbackground=[('readonly', COLORS['field'])],
                  foreground=[('readonly', COLORS['text'])], selectbackground=[('readonly', COLORS['field'])],
                  selectforeground=[('readonly', COLORS['text'])])
        self.root.option_add('*TCombobox*Listbox.background', COLORS['field'])
        self.root.option_add('*TCombobox*Listbox.foreground', COLORS['text'])
        self.root.option_add('*TCombobox*Listbox.selectBackground', COLORS['selected'])
        self.root.option_add('*TCombobox*Listbox.selectForeground', COLORS['text'])
        style.configure('Studio.Horizontal.TScale', background=COLORS['primary'],
                        troughcolor=COLORS['line'], bordercolor=COLORS['panel'], lightcolor=COLORS['primary'],
                        darkcolor=COLORS['primary'], gripcount=0, sliderlength=12, sliderthickness=10)

    def configure_output_checkbox(self, style):
        self.output_indicators = []
        for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
            image = tk.PhotoImage(master=self.root, width=16, height=16)
            border = COLORS['line'] if disabled else COLORS['muted']
            fill = COLORS['surface'] if disabled else COLORS['field']
            if selected and not disabled:
                border = fill = COLORS['primary']
            image.put(border, to=(0, 0, 16, 16))
            image.put(fill, to=(1, 1, 15, 15))
            if selected:
                ink = COLORS['muted'] if disabled else COLORS['bg']
                for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)):
                    image.put(ink, to=(x, y, x + 2, y + 2))
            self.output_indicators.append(image)
        unchecked, checked, disabled, disabled_checked = self.output_indicators
        style.element_create('Output.indicator', 'image', unchecked, ('disabled', 'selected', disabled_checked),
                             ('disabled', disabled), ('selected', checked))
        layout = style.layout('TCheckbutton')
        layout[0][1]['children'][0] = ('Output.indicator', {'side': 'left', 'sticky': ''})
        style.layout('Output.TCheckbutton', layout)
        style.configure('Output.TCheckbutton', background=COLORS['surface'])
        style.map('Output.TCheckbutton', background=[('active', COLORS['surface'])])

    def label(self, parent, text='', size=10, color=None, bold=False, bg=None, **kwargs):
        return tk.Label(parent, text=text, fg=color or COLORS['text'], bg=bg or COLORS['panel'],
                        font=(FONT, size, 'bold' if bold else 'normal'), **kwargs)

    def button(self, parent, text, command, primary=False):
        return ttk.Button(parent, text=text, command=command, style='Primary.TButton' if primary else 'Quiet.TButton')

    def panel(self, parent, column, width=None):
        options = {'width': width} if width else {}
        area = tk.Frame(parent, bg=COLORS['panel'], highlightbackground=COLORS['line'], highlightthickness=1, **options)
        area.grid(row=0, column=column, sticky='nsew', padx=(0, 11) if column < 2 else 0)
        if width:
            area.pack_propagate(False)
        return area

    def line(self, parent):
        tk.Frame(parent, bg=COLORS['line'], height=1).pack(fill='x', pady=9)

    def build_header(self):
        bar = tk.Frame(self.root, bg=COLORS['panel'], padx=19, pady=12)
        bar.grid(row=0, column=0, sticky='ew')
        bar.columnconfigure(2, weight=1)
        mark = tk.Canvas(bar, width=40, height=40, bg=COLORS['panel'], highlightthickness=0)
        mark.grid(row=0, column=0, padx=(0, 11))
        mark.create_rectangle(0, 0, 40, 40, fill=COLORS['selected'], outline='')
        mark.create_rectangle(12, 8, 28, 32, outline=COLORS['accent'], width=2)
        mark.create_polygon(18, 15, 18, 25, 25, 20, fill=COLORS['accent'], outline='')
        title = tk.Frame(bar, bg=COLORS['panel'])
        title.grid(row=0, column=1, sticky='w')
        self.label(title, '릴스 스튜디오', 15, bold=True).pack(anchor='w')
        self.label(title, '숏폼 영상 편집', 9, COLORS['muted']).pack(anchor='w')
        self.start = self.button(bar, '영상 3개 내보내기', self.start_job, primary=True)
        self.start.grid(row=0, column=3, sticky='e')

    def build_project(self, workspace):
        area = self.panel(workspace, 0, 258)
        body = tk.Frame(area, bg=COLORS['panel'], padx=14, pady=12)
        body.pack(fill='both', expand=True)
        self.label(body, '프로젝트', 12, bold=True).pack(anchor='w', pady=(0, 9))
        self.path_control(body, '원본 영상', self.source_name, '추가', self.browse_source)
        source_row = tk.Frame(body, bg=COLORS['panel'])
        source_row.pack(fill='x', pady=(5, 0))
        self.source_picker = ttk.Combobox(source_row, state='disabled', style='Studio.TCombobox', font=(FONT, 8), width=1)
        self.source_picker.pack(side='left', fill='x', expand=True)
        self.source_picker.bind('<<ComboboxSelected>>', self.pick_source)
        self.remove_source_button = self.button(source_row, '삭제', self.remove_source)
        self.remove_source_button.pack(side='right', padx=(4, 0))
        self.flip_button = tk.Checkbutton(body, text='선택 원본 좌우 반전', variable=self.mirror, command=self.toggle_mirror,
                                         font=(FONT, 8), padx=0, pady=0, bg=COLORS['panel'], fg=COLORS['muted'],
                                         activebackground=COLORS['panel'], activeforeground=COLORS['text'],
                                         selectcolor=COLORS['field'], disabledforeground='#676477')
        self.flip_button.pack(anchor='w', pady=(3, 0))
        self.combo(body, '화면 구성', self.layout, tuple(LAYOUTS))

        self.line(body)
        self.label(body, '원본 자막', 10, bold=True).pack(anchor='w')
        self.label(body, size=8, color=COLORS['muted'], textvariable=self.region_label).pack(anchor='w', pady=(1, 5))
        self.region_button = self.button(body, '자막 영역 지정', self.select_region)
        self.region_button.pack(fill='x')
        self.controls.append(self.region_button)

        self.line(body)
        self.combo(body, '나레이션 음성', self.voice, tuple(VOICE_OPTIONS), top=0)
        self.label(body, '온라인 음성은 대본을 Microsoft로 전송합니다.', 8, COLORS['muted'],
                   wraplength=220, justify='left').pack(anchor='w', pady=(3, 0))
        self.label(body, '대본', 9, bold=True).pack(anchor='w', pady=(8, 1))
        self.label(body, '비워두면 발화를 바탕으로 새 대본 구성', 8, COLORS['muted']).pack(anchor='w')
        self.script = tk.Text(body, height=2, width=1, wrap='word', undo=True, bg=COLORS['field'],
                              fg=COLORS['text'], insertbackground=COLORS['text'], selectbackground=COLORS['selected'],
                              relief='flat', padx=8, pady=6, highlightthickness=1,
                              highlightbackground=COLORS['line'], highlightcolor=COLORS['accent'], font=(FONT, 10))
        self.script.pack(fill='both', expand=True, pady=(5, 0))

    def path_control(self, parent, title, filename, button, action):
        if title:
            self.label(parent, title, 9, bold=True).pack(anchor='w', pady=(0, 4))
        row = tk.Frame(parent, bg=COLORS['field'], highlightthickness=1,
                       highlightbackground=COLORS['line'], padx=7, pady=5)
        row.pack(fill='x')
        browse = self.button(row, button, action)
        browse.pack(side='right', padx=(3, 0))
        self.controls.append(browse)
        self.label(row, size=8, color=COLORS['muted'], bg=COLORS['field'], textvariable=filename,
                   anchor='w', width=1).pack(side='left', fill='x', expand=True)

    def combo(self, parent, title, variable, values, top=8):
        self.label(parent, title, 9, bold=True).pack(anchor='w', pady=(top, 4))
        entry = ttk.Combobox(parent, textvariable=variable, values=values, state='readonly',
                             style='Studio.TCombobox', font=(FONT, 9))
        entry.pack(fill='x')
        self.controls.append(entry)

    def build_viewer(self, workspace):
        center = self.panel(workspace, 1)
        center.columnconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)
        top = tk.Frame(center, bg=COLORS['panel'], padx=14, pady=9)
        top.grid(row=0, column=0, sticky='ew')
        self.label(top, '미리보기', 11, bold=True).pack(side='left', padx=(0, 14))
        self.source_tab = ttk.Button(top, text='원본', style='Tab.TButton', command=lambda: self.set_mode('source'))
        self.result_tab = ttk.Button(top, text='완성본', style='Tab.TButton', command=lambda: self.set_mode('output'))
        self.source_tab.pack(side='left', padx=(0, 3))
        self.result_tab.pack(side='left')
        self.viewer = Viewer(center, COLORS, self.browse_source, self.open_video)
        self.label(top, size=9, color=COLORS['muted'], textvariable=self.viewer.detail).pack(side='right')

    def build_exports(self, workspace):
        area = self.panel(workspace, 2, 272)
        body = tk.Frame(area, bg=COLORS['panel'], padx=14, pady=12)
        body.pack(fill='both', expand=True)
        title = tk.Frame(body, bg=COLORS['panel'])
        title.pack(fill='x')
        self.label(title, '플랫폼 출력', 12, bold=True).pack(side='left')
        self.open = ttk.Button(title, text='결과 폴더', style='Tab.TButton', command=self.open_result)
        self.open.pack(side='right')
        for platform in PLATFORMS:
            badge, tint, glyph, description = PLATFORM_CARDS[platform.profile]
            card = tk.Frame(body, bg=COLORS['surface'], highlightthickness=1, highlightbackground=COLORS['line'],
                            padx=9, pady=7, takefocus=True, cursor='hand2')
            card.pack(fill='x', pady=(7, 0))
            icon = tk.Label(card, text=badge, bg=tint, fg=glyph, width=4, height=2, font=('Segoe UI', 10, 'bold'))
            icon.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky='nw')
            name = self.label(card, platform.name, 10, bold=True, bg=COLORS['surface'])
            name.grid(row=0, column=1, sticky='w')
            check = ttk.Checkbutton(card, variable=self.output_checks[platform.profile],
                                    style='Output.TCheckbutton', command=self.sync_view)
            check.grid(row=0, column=2, sticky='e')
            self.controls.append(check)
            status = self.label(card, '선택', 8, COLORS['muted'], bg=COLORS['surface'])
            status.grid(row=1, column=2, sticky='e')
            spec = self.label(card, f'{platform.caption_size}px · {description}', 8, COLORS['muted'], bg=COLORS['surface'])
            spec.grid(row=1, column=1, sticky='w')
            card.columnconfigure(1, weight=1)
            self.cards[platform.name] = card, name, spec, status
            for target in (card, icon, name, spec, status):
                target.bind('<Button-1>', lambda _, selected=platform.name: self.select_platform(selected))
            card.bind('<Return>', lambda _, selected=platform.name: self.select_platform(selected))
            card.bind('<space>', lambda _, selected=platform.name: self.select_platform(selected))
            card.bind('<FocusIn>', lambda _: self.refresh_cards())
            card.bind('<FocusOut>', lambda _: self.refresh_cards())

        format_row = tk.Frame(body, bg=COLORS['panel'])
        format_row.pack(fill='x', pady=(7, 0))
        self.label(format_row, '1080 × 1920 · 30 fps', 8, COLORS['muted']).pack(side='left')
        ttk.Button(format_row, text='템플릿 샘플', style='Tab.TButton',
                   command=lambda: self.open_path(ROOT / 'template-preview.png')).pack(side='right')
        self.line(body)
        self.combo(body, '영상 최대 길이', self.duration, ('15초', '30초', '45초', '60초', '90초'), top=0)
        music_header = tk.Frame(body, bg=COLORS['panel'])
        music_header.pack(fill='x', pady=(7, 4))
        self.label(music_header, 'BGM · 15%', 9, bold=True).pack(side='left')
        self.clear_music = ttk.Button(music_header, text='기본곡', style='Tab.TButton',
                                      command=lambda: self.music.set(''))
        self.clear_music.pack(side='right')
        self.clear_music.state(['disabled'])
        self.controls.append(self.clear_music)
        self.path_control(body, None, self.music_name, '변경', self.browse_music)
        tk.Frame(body, bg=COLORS['panel']).pack(fill='both', expand=True)
        self.line(body)
        self.path_control(body, '저장 위치', self.folder_name, '변경', self.choose_folder)

    def build_footer(self):
        footer = tk.Frame(self.root, bg=COLORS['bg'], padx=20, pady=8)
        footer.grid(row=2, column=0, sticky='ew')
        footer.columnconfigure(1, weight=1)
        self.signal = tk.Canvas(footer, width=9, height=9, highlightthickness=0, bg=COLORS['bg'])
        self.signal.grid(row=0, column=0, padx=(0, 8), sticky='w')
        self.dot = self.signal.create_oval(1, 1, 8, 8, fill=COLORS['muted'], outline='')
        self.label(footer, size=9, color=COLORS['muted'], bg=COLORS['bg'], textvariable=self.status).grid(
            row=0, column=1, sticky='w')
        tk.Label(footer, textvariable=self.percent, fg=COLORS['accent'], bg=COLORS['bg'],
                 font=('Consolas', 10)).grid(row=0, column=2, sticky='e')
        self.progress = tk.Canvas(footer, height=5, bg=COLORS['surface'], highlightthickness=0)
        self.progress.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(7, 1))
        self.progress_fill = self.progress.create_rectangle(0, 0, 0, 5, fill=COLORS['primary'], outline='')
        self.progress.bind('<Configure>', lambda _: self.draw_progress())

    def source_changed(self, *_):
        self.mode = 'source'
        path = self.source_path()
        if path and path not in self.sources:
            self.sources.append(path)
            self.reset_outputs()
        self.sync_view()

    def reset_outputs(self):
        self.result = None
        self.result_platforms = ()
        self.set_progress(0)
        self.set_status('편집 준비 완료' if self.sources else '원본 영상 선택 대기 중', 'muted')

    def refresh_sources(self):
        current = self.source_path()
        self.source_name.set(f'원본 {len(self.sources)}개' if self.sources else '원본 추가')
        self.source_picker.configure(values=[f'{index + 1}. {path.name}' for index, path in enumerate(self.sources)])
        if current in self.sources:
            self.source_picker.current(self.sources.index(current))
        else:
            self.source_picker.set('선택한 파일 없음')
        available = bool(current) and not self.running
        self.source_picker.configure(state='readonly' if available else 'disabled')
        self.remove_source_button.state(['!disabled'] if available else ['disabled'])
        self.flip_button.configure(state='normal' if available else 'disabled')
        self.mirror.set(current in self.mirrors)
        self.region_label.set('지정 영역 복원 · 원본 소리 제외' if self.region else '자동 자막 복원 · 원본 소리 제외')

    def music_changed(self, *_):
        value = self.music.get()
        self.music_name.set(compact_name(Path(value).name, 23) if value else '플랫폼별 기본 합성곡')
        self.clear_music.state(['!disabled'] if value and not self.running else ['disabled'])

    def folder_changed(self, *_):
        self.folder_name.set(compact_name(Path(self.folder.get()).name, 23) or self.folder.get())

    def source_path(self):
        name = self.video.get().strip().strip('"')
        return Path(name) if name else None

    def choose_file(self, variable, types, title):
        if not self.running:
            path = filedialog.askopenfilename(parent=self.root, filetypes=types, title=title)
            if path:
                variable.set(str(Path(path)))

    def browse_source(self):
        if self.running:
            return
        names = filedialog.askopenfilenames(parent=self.root, filetypes=VIDEO_TYPES, title='원본 영상 추가')
        added = [path for path in dict.fromkeys(Path(name).resolve() for name in names) if path not in self.sources]
        if added:
            self.sources.extend(added)
            self.reset_outputs()
            self.video.set(str(added[0]))

    def pick_source(self, _):
        index = self.source_picker.current()
        if 0 <= index < len(self.sources):
            self.video.set(str(self.sources[index]))

    def remove_source(self):
        source = self.source_path()
        if self.running or source not in self.sources:
            return
        index = self.sources.index(source)
        self.sources.remove(source)
        self.regions.pop(source, None)
        self.mirrors.discard(source)
        self.reset_outputs()
        self.video.set(str(self.sources[min(index, len(self.sources) - 1)]) if self.sources else '')

    def toggle_mirror(self):
        source = self.source_path()
        if source:
            if self.mirror.get():
                self.mirrors.add(source)
            else:
                self.mirrors.discard(source)
            if self.mode == 'source':
                self.viewer.show(source, start=self.viewer.position.get(), mirrored=self.mirror.get())

    @property
    def region(self):
        return self.regions.get(self.source_path())

    @region.setter
    def region(self, value):
        source = self.source_path()
        if source and value is not None:
            self.regions[source] = value
        else:
            self.regions.pop(source, None)

    def browse_music(self):
        self.choose_file(self.music, MUSIC_TYPES, '상업 이용권을 보유한 BGM 선택')

    def choose_folder(self):
        if not self.running:
            folder = Path(self.folder.get())
            selected = filedialog.askdirectory(parent=self.root, initialdir=folder if folder.is_dir() else ROOT)
            if selected:
                self.folder.set(str(Path(selected)))

    def reset_region(self):
        self.region = None
        self.region_label.set('자동 자막 복원 · 원본 소리 제외')

    def select_region(self):
        source = self.source_path()
        if self.running or source is None:
            return
        try:
            SubtitleRegionDialog(self, source)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror('자막 영역', str(error), parent=self.root)

    def active_media(self):
        if self.mode == 'output' and self.result:
            return self.result / f'{self.preview.get()}.mp4'
        return self.source_path()

    def select_platform(self, selected):
        if self.result and selected not in self.result_platforms:
            return
        self.preview.set(selected)
        if self.result:
            self.mode = 'output'
        self.sync_view()

    def set_mode(self, mode):
        self.mode = mode
        self.sync_view()

    def selected_profiles(self):
        return frozenset(profile for profile, flag in self.output_checks.items() if flag.get())

    def sync_view(self):
        self.source_tab.configure(style='Current.Tab.TButton' if self.mode == 'source' else 'Tab.TButton')
        self.result_tab.configure(style='Current.Tab.TButton' if self.mode == 'output' else 'Tab.TButton')
        self.result_tab.state(['!disabled'] if self.result else ['disabled'])
        self.open.state(['!disabled'] if self.result else ['disabled'])
        self.refresh_sources()
        self.region_button.state(['!disabled'] if self.source_path() and not self.running else ['disabled'])
        count = len(self.selected_profiles())
        self.start.configure(text='내보내는 중…' if self.running else
                             f'영상 {count}개 내보내기' if count else '플랫폼을 선택하세요')
        self.start.state(['!disabled'] if count and self.source_path() and not self.running else ['disabled'])
        self.refresh_cards()
        self.viewer.show(self.active_media(), start=.35 if self.mode == 'output' else 0,
                         mirrored=self.mode == 'source' and self.source_path() in self.mirrors)

    def refresh_cards(self):
        selected = self.preview.get()
        chosen = self.selected_profiles()
        for platform in PLATFORMS:
            card, name, spec, status = self.cards[platform.name]
            included = platform.profile in chosen
            finished = platform.name in self.result_platforms
            active = platform.name == selected and (finished if self.mode == 'output' else included)
            surface = COLORS['selected'] if active else COLORS['surface']
            border = COLORS['primary'] if active or card == self.root.focus_get() else COLORS['line']
            card.configure(bg=surface, highlightbackground=border, highlightcolor=border)
            for label in (name, spec, status):
                label.configure(bg=surface)
            state = '제외' if not included else '처리 중' if self.running else '완료' if finished else '선택'
            status.configure(text=state, fg=COLORS['success'] if state == '완료' else COLORS['muted'])

    def set_status(self, message, color):
        self.status.set(message)
        self.signal.itemconfigure(self.dot, fill=COLORS[color])

    def set_progress(self, percent):
        self.percent.set(f'{percent}%')
        self.draw_progress()

    def draw_progress(self):
        value = int(self.percent.get().removesuffix('%')) / 100
        self.progress.coords(self.progress_fill, 0, 0, self.progress.winfo_width() * value, 5)

    def start_job(self):
        profiles = self.selected_profiles()
        if self.running or not self.sources or not profiles:
            return
        if any(not source.is_file() for source in self.sources):
            messagebox.showerror('원본 영상', '원본 영상 파일을 선택하세요.', parent=self.root)
            return
        folder, music = self.folder.get(), self.music.get()
        if not folder or (music and not Path(music).is_file()):
            messagebox.showerror('입력 확인', '저장 위치 또는 BGM 파일을 확인하세요.', parent=self.root)
            return
        job = Job(source=self.sources[0], output_dir=Path(folder), target=int(self.duration.get().removesuffix('초')),
                  script=self.script.get('1.0', 'end-1c'), music=Path(music) if music else None,
                  voice=VOICE_OPTIONS[self.voice.get()], layout=LAYOUTS[self.layout.get()],
                  extra_sources=tuple(self.sources[1:]), subtitle_regions=tuple(self.regions.get(path) for path in self.sources),
                  mirrored=frozenset(index for index, path in enumerate(self.sources) if path in self.mirrors),
                  output_profiles=profiles)
        self.running = True
        self.set_progress(0)
        self.set_status(f'영상 {len(profiles)}개를 내보내는 중', 'accent')
        self.lock_controls()
        threading.Thread(target=self.perform, args=(job,), daemon=True).start()

    def lock_controls(self):
        for control in self.controls:
            control.state(['disabled'] if self.running else ['!disabled'])
        self.script.configure(state='disabled' if self.running else 'normal')
        self.music_changed()
        self.sync_view()

    def perform(self, job):
        try:
            result = render(job, lambda percent, stage: self.events.put(('progress', (percent, stage))))
            names = tuple(platform.name for platform in PLATFORMS
                          if job.output_profiles is None or platform.profile in job.output_profiles)
            self.events.put(('done', (result, names)))
        except Exception as error:
            self.events.put(('error', str(error)))

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'progress':
                percent, stage = value
                self.set_progress(percent)
                self.set_status(stage, 'accent')
                continue
            self.running = False
            if kind == 'done':
                self.result, self.result_platforms = value
                if self.preview.get() not in self.result_platforms:
                    self.preview.set(self.result_platforms[0])
                self.mode = 'output'
                count = len(self.result_platforms)
                self.set_status(f'내보내기 완료 · MP4 {count}개 및 SRT {count}개 저장', 'success')
            else:
                self.set_status('내보내기를 완료하지 못했습니다', 'danger')
            self.lock_controls()
            if kind == 'error':
                messagebox.showerror('릴스 스튜디오', value, parent=self.root)
        self.root.after(80, self.poll)

    def open_path(self, path):
        try:
            os.startfile(path)
        except OSError as error:
            messagebox.showerror('파일 열기', str(error), parent=self.root)

    def open_video(self):
        path = self.active_media()
        if path:
            self.open_path(path)

    def open_result(self):
        if self.result:
            self.open_path(self.result)

    def close(self):
        if self.running:
            messagebox.showinfo('내보내는 중', '현재 작업이 끝나면 창을 닫을 수 있습니다.', parent=self.root)
        else:
            self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    Application(root)
    root.mainloop()
