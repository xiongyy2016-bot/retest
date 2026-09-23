"""Playwright controller: all browser API calls run on one dedicated thread."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright, Error as PlaywrightError
from core import (extract_urls, normalize_article_url, extract_article,
                  make_archive, archive_filename, append_index, load_indexed_links,
                  MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES, identify_image_mime)


def resource_path(name: str) -> str:
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return str(Path(root) / name)


class BrowserWorker(threading.Thread):
    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.commands: queue.Queue = queue.Queue()
        self.events = events
        self.stop_event = threading.Event()
        self.context = None
        self.responses = []
        self.image_responses = []
        self.discovered: list[str] = []
        self.discovered_set: set[str] = set()

    def emit(self, event: str, **kwargs):
        self.events.put((event, kwargs))

    def send(self, action: str, **kwargs):
        self.commands.put((action, kwargs))

    def collect_text(self, text: str):
        added = []
        for url in extract_urls(text):
            if url not in self.discovered_set:
                self.discovered_set.add(url)
                self.discovered.append(url)
                added.append(url)
        return added

    def _response(self, response):
        try:
            request = response.request
            if request.resource_type in ('document', 'xhr', 'fetch') and len(self.responses) < 500:
                self.responses.append(response)
            elif request.resource_type == 'image' and len(self.image_responses) < 300:
                self.image_responses.append(response)
        except Exception:
            pass

    def run(self):
        try:
            browser_root = Path(resource_path('playwright_browsers'))
            if browser_root.is_dir():
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_root)
            profile = Path.home() / 'Library' / 'Application Support' / 'WeChatArchiver' / 'browser-profile'
            profile.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                self.context = p.chromium.launch_persistent_context(
                    str(profile), headless=False, accept_downloads=False,
                    viewport={'width': 1200, 'height': 850},
                    locale='zh-CN',
                )
                self.context.on('response', self._response)
                self.emit('ready')
                while True:
                    action, data = self.commands.get()
                    if action == 'exit':
                        break
                    try:
                        if action == 'open':
                            self.open_search(data['input'])
                        elif action == 'collect':
                            self.collect()
                        elif action == 'scan':
                            self.scan_current_page()
                        elif action == 'archive':
                            self.archive_batch(**data)
                    except Exception as exc:
                        self.emit('error', message=str(exc))
                self.context.close()
        except Exception as exc:
            self.emit('error', message='浏览器启动失败：' + str(exc) + '。若从源码运行，请执行 python -m playwright install chromium。')

    def open_search(self, value: str):
        value = value.strip()
        if not value:
            raise ValueError('请填写公众号名称或链接。')
        if value.startswith(('https://', 'http://')):
            url = value
        else:
            # Search is discovery only; search results are incomplete and may require manual verification.
            url = 'https://weixin.sogou.com/weixin?type=2&query=' + quote(value)
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        self.emit('log', message='已打开浏览器：请手动翻页/滚动，或进入公众号历史消息页，然后点「采集浏览器中的链接」。搜索结果不保证覆盖所有文章。')

    def collect(self):
        before = len(self.discovered)
        for page in self.context.pages:
            if page.is_closed():
                continue
            try:
                anchors = page.eval_on_selector_all('a[href]', '(nodes) => nodes.map(n => n.href).join("\\n")')
                self.collect_text(anchors)
                self.collect_text(page.content())
                # Record only visible, user-browsed URLs, no direct history endpoint requests.
                self.collect_text(page.url)
            except Exception:
                continue
        for response in self.responses:
            try:
                if int(response.headers.get('content-length', '0')) > 5 * 1024 * 1024:
                    continue
                body = response.body()
                if len(body) <= 5 * 1024 * 1024:
                    self.collect_text(body.decode('utf-8', errors='ignore'))
            except Exception:
                pass
        self.responses.clear()
        self.emit('links', urls=list(self.discovered), added=len(self.discovered)-before)
        self.emit('log', message=f'浏览器中累计识别 {len(self.discovered)} 条不同文章链接，本次新增 {len(self.discovered)-before} 条。')

    def scan_current_page(self):
        """Scroll an already-opened page normally; do not call private endpoints."""
        pages = [p for p in self.context.pages if not p.is_closed()]
        if not pages:
            raise ValueError('请先打开一个公众号历史消息或搜索结果页面。')
        page = pages[-1]
        if page.url == 'about:blank':
            raise ValueError('请先打开一个公众号历史消息或搜索结果页面。')
        self.stop_event.clear()
        stable = 0
        previous = (-1, -1)
        self.emit('log', message='开始向下滚动当前页面，采集已加载的链接；遇到验证码请手动处理。最多滚动80次。')
        for i in range(80):
            if self.stop_event.is_set():
                self.emit('log', message='自动滚动已停止。')
                break
            page.evaluate('''() => window.scrollBy(0, Math.max(window.innerHeight * .82, 450))''')
            page.wait_for_timeout(550)
            try:
                self.collect_text(page.content())
                height = page.evaluate('document.documentElement.scrollHeight')
                now = (height, len(self.discovered))
                if now == previous:
                    stable += 1
                else:
                    stable = 0
                previous = now
                if stable >= 5 and page.evaluate('window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 30'):
                    break
            except Exception as exc:
                self.emit('log', message='页面发生变化：' + str(exc))
                break
        self.collect()

    def archive_batch(self, urls: list[str], folder: str, account: str, delay: float):
        destination = Path(folder).expanduser().resolve() / (re.sub(r'[\\/:*?"<>|]', '_', account.strip())[:60] or '公众号文章')
        destination.mkdir(parents=True, exist_ok=True)
        good = load_indexed_links(destination)
        self.stop_event.clear()
        count_success = count_failed = count_skipped = 0
        unique = list(dict.fromkeys(x for raw in urls if (x := normalize_article_url(raw))))
        self.emit('log', message=f'准备归档 {len(unique)} 篇；保存目录：{destination}')
        page = self.context.new_page()
        try:
            for i, url in enumerate(unique, 1):
                if self.stop_event.is_set():
                    self.emit('log', message='已按要求停止，已成功归档的文件保留。')
                    break
                if url in good:
                    count_skipped += 1
                    self.emit('progress', index=i, total=len(unique), message='跳过已成功归档：' + url)
                    continue
                self.emit('progress', index=i, total=len(unique), message='正在保存：' + url)
                last_error = ''
                for attempt in range(2):
                    try:
                        article, images = self.archive_one(page, url)
                        blob, _ = make_archive(article, images)
                        target = destination / archive_filename(article)
                        tmp = target.with_suffix('.webarchive.tmp')
                        tmp.write_bytes(blob)
                        os.replace(tmp, target)
                        # Confirm the file has the expected binary-plist header.
                        if not target.read_bytes()[:8] == b'bplist00':
                            raise RuntimeError('归档文件验证失败')
                        append_index(destination, {
                            '公众号': account, '发布日期': article.date,
                            '标题': article.title, '作者': article.author,
                            '原文链接': url, '归档文件': target.name,
                            '图片总数': len(article.image_urls), '成功图片数': len(images),
                            '归档时间UTC': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                            '状态': '成功', '备注': ('部分图片未成功嵌入' if len(images)<len(article.image_urls) else ''),
                        })
                        count_success += 1
                        self.emit('log', message=f'✓ {target.name} （图片 {len(images)}/{len(article.image_urls)}）')
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt == 0:
                            self.emit('log', message=f'尝试失败，将重试：{url}；原因：{last_error}')
                            page.wait_for_timeout(2000)
                        else:
                            count_failed += 1
                            append_index(destination, {'公众号':account,'原文链接':url,'状态':'失败','备注':last_error})
                            self.emit('log', message=f'✗ {url}；{last_error}')
                # Stay well below normal user reading speed; stop if asked.
                if i < len(unique) and not self.stop_event.is_set():
                    self.stop_event.wait(max(1.5, float(delay)))
        finally:
            page.close()
            self.emit('done', successful=count_success, failed=count_failed,
                      skipped=count_skipped, folder=str(destination))

    def archive_one(self, page, url: str):
        self.image_responses.clear()
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        if response and response.status >= 400:
            raise RuntimeError(f'网页返回 HTTP {response.status}')
        page.wait_for_timeout(1200)
        # WeChat uses data-src and scroll-triggered lazy loading. Scroll gradually
        # instead of bypassing verification or querying restricted APIs.
        page.evaluate('''() => document.querySelectorAll('img[data-src], img[data-actualsrc]').forEach(img => {
            const src = img.getAttribute('data-src') || img.getAttribute('data-actualsrc');
            if (src && /^https?:/.test(src)) img.setAttribute('src', src);
        })''')
        for _ in range(14):
            if self.stop_event.is_set():
                raise RuntimeError('用户停止')
            at_end = page.evaluate('''() => {window.scrollBy(0, Math.max(700, window.innerHeight));
              return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 30;}''')
            page.wait_for_timeout(250)
            if at_end:
                break
        page.wait_for_timeout(750)
        html = page.content()
        article = extract_article(html, url)
        images = {}
        total_bytes = 0
        for img_url in article.image_urls:
            if self.stop_event.is_set():
                raise RuntimeError('用户停止')
            if total_bytes >= MAX_TOTAL_IMAGE_BYTES:
                self.emit('log', message='图片累计超过100MB，剩余图片未嵌入。')
                break
            try:
                r = self.context.request.get(img_url, headers={'Referer':url}, timeout=20000)
                if not r.ok:
                    raise RuntimeError(f'HTTP {r.status}')
                payload = r.body()
                mime = identify_image_mime(payload, r.headers.get('content-type',''))
                if not mime:
                    raise RuntimeError('返回资源不是图片')
            except Exception as exc:
                # The rendered browser may have loaded it with page-specific
                # cookies or referrer even if a separate request is rejected.
                payload = b''
                mime = None
                for seen in reversed(self.image_responses):
                    if seen.url.split('?',1)[0] != img_url.split('?',1)[0]:
                        continue
                    try:
                        candidate = seen.body()
                        candidate_mime = identify_image_mime(candidate, seen.headers.get('content-type',''))
                        if candidate_mime:
                            payload, mime = candidate, candidate_mime
                            break
                    except Exception:
                        pass
                if not mime:
                    self.emit('log', message=f'图片未保存：{img_url[:90]}（{exc}）')
                    continue
            if not payload or len(payload) > MAX_IMAGE_BYTES:
                self.emit('log', message='图片为空或超过20MB：' + img_url[:90])
                continue
            images[img_url] = (mime, payload)
            total_bytes += len(payload)
        return article, images
