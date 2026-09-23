"""macOS desktop UI for public WeChat article archiving."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from browser_worker import BrowserWorker
from core import extract_urls, normalize_article_url

APP_NAME = '公众号归档助手'


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME + ' · WebArchive')
        root.geometry('900x750')
        root.minsize(720, 610)
        self.events = queue.Queue()
        self.worker = BrowserWorker(self.events)
        self.running = False
        self.account = tk.StringVar()
        self.folder = tk.StringVar(value=str(Path.home()/'Documents'/'WeChatArchives'))
        self.delay = tk.StringVar(value='3')
        self.status = tk.StringVar(value='准备就绪。首次启动浏览器可能需要几秒钟。')
        self.progress = tk.DoubleVar(value=0)
        self._build()
        self.worker.start()
        root.after(120, self._poll)
        root.protocol('WM_DELETE_WINDOW', self._close)

    def _build(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text=APP_NAME, font=('Helvetica', 21, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='在普通浏览器中发现可访问文章，再逐篇保存为可离线打开的 .webarchive。仅凭名称不能保证找到全部历史文章。',
                  wraplength=830, foreground='#555').pack(anchor='w', pady=(4, 15))
        account_frame = ttk.Frame(outer)
        account_frame.pack(fill='x', pady=3)
        ttk.Label(account_frame, text='公众号名称或链接：').pack(side='left')
        ttk.Entry(account_frame, textvariable=self.account).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(account_frame, text='打开搜索 / 历史页', command=self._open).pack(side='left')
        ttk.Button(account_frame, text='自动下滑采集', command=lambda:self.worker.send('scan')).pack(side='left', padx=(7,0))
        ttk.Button(account_frame, text='采集链接', command=lambda:self.worker.send('collect')).pack(side='left', padx=(7,0))
        ttk.Label(outer, text='在弹出的浏览器里正常搜索、进入账号历史消息或手动翻页，然后点「自动下滑采集」或「采集链接」。也可直接将文章链接粘贴到下方。',
                  foreground='#666', wraplength=820).pack(anchor='w', pady=(2, 11))

        actions = ttk.Frame(outer)
        actions.pack(fill='x')
        ttk.Label(actions, text='待归档文章链接（每行一条；支持粘贴含链接的文字）：').pack(side='left')
        ttk.Button(actions, text='导入 TXT / CSV', command=self._import).pack(side='right')
        ttk.Button(actions, text='去重整理', command=self._normalize).pack(side='right', padx=6)
        frame = ttk.Frame(outer)
        frame.pack(fill='both', expand=True, pady=(6,12))
        self.text = tk.Text(frame, height=11, wrap='none', undo=True, font=('Menlo', 11))
        bar = ttk.Scrollbar(frame, orient='vertical', command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text='存储位置：').pack(side='left')
        ttk.Entry(row, textvariable=self.folder).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(row, text='选择文件夹', command=self._choose_folder).pack(side='left')
        ttk.Button(row, text='打开目录', command=self._open_folder).pack(side='left', padx=6)
        controls = ttk.Frame(outer)
        controls.pack(fill='x', pady=(9,4))
        ttk.Label(controls, text='文章间隔（秒）：').pack(side='left')
        ttk.Entry(controls, width=5, textvariable=self.delay).pack(side='left', padx=(0,17))
        self.start_button = ttk.Button(controls, text='开始批量归档', command=self._start)
        self.start_button.pack(side='left')
        ttk.Button(controls, text='停止', command=self._stop).pack(side='left', padx=7)
        ttk.Label(controls, textvariable=self.status, wraplength=450).pack(side='right')
        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill='x', pady=(8, 11))
        ttk.Label(outer, text='执行记录 / 失败原因：').pack(anchor='w')
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill='both', expand=True, pady=(6,0))
        self.log = tk.Text(log_frame, height=9, wrap='word', state='disabled', font=('Menlo', 10))
        log_bar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=log_bar.set)
        self.log.pack(side='left', fill='both', expand=True)
        log_bar.pack(side='right', fill='y')

    def _write_log(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', s+'\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _open(self):
        val = self.account.get().strip()
        if not val:
            messagebox.showinfo('提示', '请先输入公众号名称或页面链接。')
            return
        self.worker.send('open', input=val)

    def _import(self):
        fn = filedialog.askopenfilename(filetypes=[('文章链接文件','*.txt *.csv'), ('所有文件','*.*')])
        if not fn:
            return
        try:
            raw = Path(fn).read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            raw = Path(fn).read_text(encoding='gb18030', errors='replace')
        links = extract_urls(raw)
        self._add_links(links)
        self._write_log(f'从 {Path(fn).name} 导入 {len(links)} 条有效文章链接。')

    def _add_links(self, links):
        existing = extract_urls(self.text.get('1.0','end'))
        merged = list(dict.fromkeys(existing + links))
        self.text.delete('1.0','end')
        self.text.insert('1.0','\n'.join(merged) + ('\n' if merged else ''))
        self.status.set(f'待归档 {len(merged)} 篇')

    def _normalize(self):
        links = extract_urls(self.text.get('1.0','end'))
        self._add_links(links)
        self._write_log(f'去重后剩余 {len(links)} 条。')

    def _choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.folder.get())
        if path:
            self.folder.set(path)

    def _open_folder(self):
        path = Path(self.folder.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix':
            subprocess.Popen(['open', str(path)])

    def _start(self):
        if self.running:
            return
        urls = extract_urls(self.text.get('1.0','end'))
        if not urls:
            messagebox.showinfo('提示','请先粘贴文章链接，或打开浏览器采集。')
            return
        try:
            delay = float(self.delay.get())
            if delay < 1.5:
                raise ValueError
        except ValueError:
            messagebox.showinfo('提示','文章间隔至少为 1.5 秒。')
            return
        self.running = True
        self.start_button.configure(state='disabled')
        self.progress.set(0)
        self.worker.send('archive', urls=urls, folder=self.folder.get(),
                         account=self.account.get() or '未命名公众号', delay=delay)
        self._write_log(f'批次开始：{len(urls)} 条链接。')

    def _stop(self):
        self.worker.stop_event.set()
        self._write_log('收到停止指令；当前文章处理结束后停止。')

    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == 'ready':
                    self._write_log('浏览器已启动。浏览器保存独立的会话，不读取你的 Safari 历史记录。')
                elif kind == 'links':
                    self._add_links(data['urls'])
                elif kind == 'log':
                    self._write_log(data['message'])
                elif kind == 'progress':
                    self.progress.set(data['index']/max(1,data['total'])*100)
                    self.status.set(f"{data['index']}/{data['total']}")
                    self._write_log(data['message'])
                elif kind == 'done':
                    self.running = False
                    self.start_button.configure(state='normal')
                    self.status.set(f"完成：成功 {data['successful']}，失败 {data['failed']}，已存在 {data['skipped']}")
                    self._write_log(f"归档结束，目录：{data['folder']}")
                elif kind == 'error':
                    self._write_log('错误：'+data['message'])
                    self.status.set('发生错误，详见执行记录。')
                    if self.running:
                        self.running = False
                        self.start_button.configure(state='normal')
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _close(self):
        if self.running:
            if not messagebox.askyesno('确认退出','正在归档，退出可能中断当前文章，确定要关闭？'):
                return
            self.worker.stop_event.set()
        self.worker.send('exit')
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    try:
        ttk.Style().theme_use('aqua')
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
