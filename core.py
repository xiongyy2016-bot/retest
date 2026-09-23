"""Article identification and offline Safari WebArchive creation.

Only public article URLs that the user can normally open are supported. This
module does not attempt to bypass WeChat access controls or verification.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html as html_lib
import json
import mimetypes
from pathlib import Path
import plistlib
import re
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment

ARTICLE_RE = re.compile(r"https?://mp\.weixin\.qq\.com/(?:s/[^\s\"'<>\\]+|s\?[^\s\"'<>\\]+)", re.I)
KNOWN_IMAGE_HOSTS = ("mmbiz.qpic.cn", "mmbiz.qlogo.cn", "mmbiz.qpic.com")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 100 * 1024 * 1024


def normalize_article_url(candidate: str) -> str | None:
    """Keep only complete, public WeChat article links; discard tracking fields."""
    candidate = html_lib.unescape(candidate.strip().strip("\"'<>[]()"))
    candidate = candidate.replace('\\/', '/').replace('\\u0026', '&').replace('\\u003d', '=')
    for _ in range(2):
        if '%3A%2F%2F' in candidate.upper():
            candidate = unquote(candidate)
    candidate = candidate.rstrip('.,;，。；')
    parts = urlsplit(candidate)
    if parts.scheme not in ('http', 'https') or (parts.hostname or '').lower() != 'mp.weixin.qq.com':
        return None
    if parts.path.startswith('/s/'):
        token = parts.path[3:]
        if not re.fullmatch(r'[\w-]{8,}', token):
            return None
        return 'https://mp.weixin.qq.com/s/' + token
    if parts.path == '/s':
        qs = parse_qs(parts.query)
        needed = ('__biz', 'mid', 'idx', 'sn')
        if not all(qs.get(key) for key in needed):
            return None
        from urllib.parse import urlencode
        return 'https://mp.weixin.qq.com/s?' + urlencode([(k, qs[k][0]) for k in needed])
    return None


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    normalized = html_lib.unescape(text).replace('\\/', '/')
    normalized = normalized.replace('\\u0026', '&').replace('\\u003d', '=')
    # Some account-history responses store an escaped content_url.
    for _ in range(2):
        normalized = re.sub(r'https?%3A%2F%2Fmp\.weixin\.qq\.com%2F',
                            lambda m: unquote(m.group(0)), normalized, flags=re.I)
    urls, seen = [], set()
    for raw in ARTICLE_RE.findall(normalized):
        link = normalize_article_url(raw)
        if link and link not in seen:
            urls.append(link)
            seen.add(link)
    return urls



def identify_image_mime(payload: bytes, header: str = '') -> str | None:
    """Accept images served with generic CDN Content-Type headers."""
    if payload.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if payload.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if payload.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if payload.startswith(b'RIFF') and payload[8:12] == b'WEBP':
        return 'image/webp'
    if payload.startswith(b'BM'):
        return 'image/bmp'
    mime = header.split(';', 1)[0].strip().lower()
    if mime in ('image/svg+xml', 'image/avif', 'image/heic', 'image/heif'):
        return mime
    if mime.startswith('image/'):
        return mime
    return None


def safe_filename(title: str, limit: int = 86) -> str:
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', title)
    title = re.sub(r'\s+', ' ', title).strip(' ._')
    return (title[:limit].rstrip(' ._') or '未命名文章')


def text_of(tag) -> str:
    return tag.get_text(' ', strip=True) if tag else ''


def meta_content(soup, *, prop=None, name=None) -> str:
    tag = soup.find('meta', attrs={'property': prop}) if prop else soup.find('meta', attrs={'name': name})
    return str(tag.get('content', '')).strip() if tag else ''


def normalize_date(value: str) -> str:
    match = re.search(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})', value)
    if match:
        return f'{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'
    return value


def parse_publish_date(soup, html: str) -> str:
    tag = soup.select_one('#publish_time')
    if tag:
        result = text_of(tag)
        match = re.search(r'20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}', result)
        if match:
            return normalize_date(match.group(0))
    for regex in (
        r'\b(?:var\s+)?publish_time\s*[:=]\s*["\'](20\d{2}-\d{1,2}-\d{1,2})',
        r'\b(?:var\s+)?ct\s*[:=]\s*["\']?(1[4-9]\d{8})',
    ):
        m = re.search(regex, html)
        if m:
            v = m.group(1)
            if v.isdigit():
                return datetime.fromtimestamp(int(v), timezone.utc).strftime('%Y-%m-%d')
            return normalize_date(v)
    return ''


@dataclass
class Article:
    title: str
    author: str
    date: str
    url: str
    content_html: str
    image_urls: list[str]


def extract_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, 'html.parser')
    title = (text_of(soup.select_one('#activity-name')) or
             meta_content(soup, prop='og:title') or
             meta_content(soup, name='title') or
             text_of(soup.title))
    author = (text_of(soup.select_one('#js_name')) or
              meta_content(soup, name='author') or
              meta_content(soup, prop='og:article:author'))
    date = parse_publish_date(soup, html)
    content = soup.select_one('#js_content') or soup.select_one('.rich_media_content')
    if not title or content is None:
        raise ValueError('没有找到文章标题或正文；页面可能是验证页、已删除页，或尚未加载完成。')
    if '环境异常' in title or '访问过于频繁' in title:
        raise ValueError('网页要求验证或限制访问；请在浏览器中正常完成验证后重试。')
    # Work on an independent tree, avoiding mutation of the input snapshot.
    content = BeautifulSoup(str(content), 'html.parser')
    for el in content.find_all(['script', 'style', 'iframe', 'form', 'input', 'button', 'noscript', 'object', 'embed']):
        el.decompose()
    for el in content.find_all(string=lambda s: isinstance(s, Comment)):
        el.extract()
    image_urls = []
    for img in content.find_all('img'):
        src = (img.get('data-src') or img.get('data-actualsrc') or img.get('src') or '').strip()
        src = urljoin(url, src) if src and not src.startswith('data:') else src
        if src.startswith('https://') or src.startswith('http://'):
            img['src'] = src
            image_urls.append(src)
        else:
            img['src'] = ''
        for attr in list(img.attrs):
            if attr not in ('src', 'alt', 'width', 'height', 'title'):
                del img[attr]
        img['loading'] = 'eager'
    for el in content.find_all(True):
        for attr in list(el.attrs):
            if attr.lower().startswith('on') or attr in ('data-src', 'data-actualsrc', 'contenteditable'):
                del el[attr]
        if el.name == 'a' and el.get('href'):
            href = urljoin(url, el['href'])
            if urlsplit(href).scheme not in ('http', 'https', 'mailto') and not href.startswith('#'):
                del el['href']
            else:
                el['href'] = href
        if 'style' in el.attrs and re.search(r'url\s*\(|expression\s*\(', str(el['style']), re.I):
            del el['style']
    # Reject empty/interstitial pages masquerading as articles.
    if len(content.get_text(' ', strip=True)) < 15 and not image_urls:
        raise ValueError('正文过短，无法确认是完整文章。')
    return Article(title, author, date, url, str(content), list(dict.fromkeys(image_urls)))


BASE_STYLE = '''
:root{color-scheme:light}body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
max-width:780px;margin:30px auto;padding:0 22px;background:#fff;color:#242424;line-height:1.82;font-size:17px;overflow-wrap:anywhere}
h1{font-size:28px;line-height:1.4;margin:0 0 15px}header{border-bottom:1px solid #ddd;padding-bottom:16px;margin-bottom:24px}
.meta{color:#6e6e6e;font-size:13px}#js_content img{max-width:100%;height:auto;display:block;margin:12px auto}
#js_content video,#js_content iframe{max-width:100%}#js_content table{max-width:100%;display:block;overflow:auto}
#js_content p{margin:10px 0}#js_content a{color:#1769aa}footer{margin-top:42px;padding-top:20px;border-top:1px solid #ddd;color:#777;font-size:12px}
.missing{border:1px dashed #b66;padding:8px;background:#fff7f0;color:#8a3d22;font-size:12px}
'''


def make_archive(article: Article, images: dict[str, tuple[str, bytes]], archive_time: str | None = None) -> tuple[bytes, str]:
    """Generate an offline, text-selectable Safari .webarchive.

    Unlike Safari's own Save As, it is a research-friendly cleaned snapshot:
    script-free and images embedded as data URLs. The original URL is retained
    in WebMainResource and in an explicit provenance footer.
    """
    content = BeautifulSoup(article.content_html, 'html.parser')
    missing = []
    for img in content.find_all('img'):
        original = img.get('src', '')
        data = images.get(original)
        if data:
            mime, payload = data
            img['src'] = f'data:{mime};base64,{base64.b64encode(payload).decode("ascii")}'
        else:
            missing.append(original)
            img.replace_with(BeautifulSoup('<div class="missing">图片未能归档：' + html_lib.escape(original) + '</div>', 'html.parser'))
    timestamp = archive_time or datetime.now(timezone.utc).isoformat(timespec='seconds')
    source = html_lib.escape(article.url, quote=True)
    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html_lib.escape(article.title)}</title><style>{BASE_STYLE}</style></head><body>
<header><h1>{html_lib.escape(article.title)}</h1><div class="meta">{html_lib.escape(article.author)}　{html_lib.escape(article.date)}</div></header>
<article id="js_content">{str(content)}</article>
<footer>原文：<a href="{source}">{source}</a><br>归档时间（UTC）：{html_lib.escape(timestamp)}<br>本文件为研究用途的离线快照，非作者实时更新的页面。</footer>
</body></html>'''
    data = {
        'WebMainResource': {'WebResourceData': doc.encode('utf-8'),
                            'WebResourceMIMEType': 'text/html',
                            'WebResourceTextEncodingName': 'UTF-8',
                            'WebResourceFrameName': '',
                            'WebResourceURL': article.url},
        'WebSubresources': [],
        'WebSubframeArchives': [],
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_BINARY), doc


def archive_filename(article: Article) -> str:
    suffix = hashlib.sha256(article.url.encode()).hexdigest()[:10]
    date = article.date.replace('/', '-').replace('年', '-').replace('月', '-').replace('日', '') if article.date else '日期未知'
    return f'{date}_{safe_filename(article.title)}_{suffix}.webarchive'


def append_index(destination: Path, row: dict):
    columns = ['公众号', '发布日期', '标题', '作者', '原文链接', '归档文件', '图片总数', '成功图片数', '归档时间UTC', '状态', '备注']
    path = destination / '文章索引.csv'
    exists = path.exists() and path.stat().st_size > 0
    with path.open('a', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def load_indexed_links(destination: Path) -> set[str]:
    file = destination / '文章索引.csv'
    if not file.exists():
        return set()
    with file.open(newline='', encoding='utf-8-sig') as f:
        return {r.get('原文链接', '') for r in csv.DictReader(f) if r.get('状态') == '成功' and r.get('原文链接')}
