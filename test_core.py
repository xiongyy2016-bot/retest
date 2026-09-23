import plistlib
from bs4 import BeautifulSoup
from core import extract_urls, normalize_article_url, extract_article, make_archive, archive_filename

U='https://mp.weixin.qq.com/s/1r8Gqpmx1UWsDb5uyzk6LA'

def test_url_normalization():
    assert normalize_article_url(U+'?foo=bar') == U
    assert normalize_article_url('https://other.example/s/123456789') is None
    assert normalize_article_url('https://mp.weixin.qq.com/mp/profile_ext?action=home') is None
    assert extract_urls(U+' '+U+'?utm_source=abc') == [U]


def test_escaped_url():
    raw = '{"content_url":"https:\\/\\/mp.weixin.qq.com\\/s\\/1r8Gqpmx1UWsDb5uyzk6LA"}'
    assert extract_urls(raw) == [U]


def test_archive_offline_image():
    doc = '''<html><head><meta property="og:title" content="测试标题"></head>
    <body><span id="js_name">作者</span><em id="publish_time">2026-09-18</em>
    <div id="js_content"><p>这是一个用于验证归档功能的正文内容。</p>
    <img data-src="https://mmbiz.qpic.cn/pic/one"/><script>alert(1)</script></div></body></html>'''
    a = extract_article(doc, U)
    assert a.title == '测试标题' and a.date == '2026-09-18'
    assert a.image_urls == ['https://mmbiz.qpic.cn/pic/one']
    payload, offline_html = make_archive(a, {a.image_urls[0]: ('image/png', b'\x89PNG\r\n\x1a\n')})
    assert payload.startswith(b'bplist00')
    content = plistlib.loads(payload)
    assert content['WebMainResource']['WebResourceURL'] == U
    html = content['WebMainResource']['WebResourceData'].decode('utf-8')
    assert 'data:image/png;base64,' in html
    assert 'alert(1)' not in html
    assert '用于验证归档功能' in html
    assert archive_filename(a).endswith('.webarchive')


def test_missing_images_are_disclosed():
    html = '<html><h1 id="activity-name">标题</h1><div id="js_content"><p>这里有充足的内容用于检测。</p><img data-src="https://example.com/no.jpg"></div></html>'
    article=extract_article(html,U)
    _, archive_html=make_archive(article,{})
    assert '图片未能归档' in archive_html
    assert '<script' not in archive_html


def test_generic_cdn_mime_detection():
    from core import identify_image_mime
    assert identify_image_mime(b'\xff\xd8\xffdata','application/octet-stream') == 'image/jpeg'
    assert identify_image_mime(b'<script>no</script>','text/html') is None
