#!/usr/bin/env python3
"""Create one offline HTML containing the page, images, fonts, paper and downloads."""
import base64
import json
import mimetypes
import re
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]


def uri(path):
    path = ROOT / path
    content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return f'data:{content_type};base64,' + base64.b64encode(path.read_bytes()).decode()


def build():
    doc = BeautifulSoup((ROOT / 'index.html').read_text(), 'html.parser')
    css = '\n'.join((ROOT / name).read_text() for name in ['styles.css', 'leaderboard.css', 'visual.css'])
    css = re.sub(r"url\(['\"]?([^)'\"]+)['\"]?\)", lambda m: f'url("{uri(m[1])}")', css)
    style = doc.new_tag('style')
    style.string = css + '\n.standalone-credits{padding:5px 0 30px;font-size:12px;color:var(--muted)}.standalone-credits summary{cursor:pointer;color:var(--blue)}.standalone-credits h3{font-size:15px;margin:17px 0 8px}.standalone-credits p{font-size:12px;margin-bottom:9px}.standalone-credits .credit-grid{display:block}\n'
    for link in doc.select('link[rel="stylesheet"]'):
        link.decompose()
    doc.head.append(style)
    doc.select_one('link[rel="icon"]')['href'] = uri('assets/favicon.svg')
    for script in doc.select('script[src]'):
        script.decompose()
    for meta in doc.select('meta[property^="og:"]'):
        meta.decompose()
    for image in doc.select('img[src]'):
        image['src'] = uri(image['src'])
    for button in doc.select('[data-enlarge]'):
        button['data-enlarge'] = uri(button['data-enlarge'])
    for anchor in doc.select('a[href]'):
        href = anchor['href']
        if href == 'credits.html':
            anchor['href'] = '#source-credits'
        elif not href.startswith(('#', 'https:', 'http:', 'mailto:')):
            path = href.split('#')[0]
            anchor['href'] = uri(path)
            anchor['download'] = Path(path).name
            anchor.attrs.pop('target', None)

    # Retain the source credits within the single file, without repeating image assets.
    credits = BeautifulSoup((ROOT / 'credits.html').read_text(), 'html.parser')
    details = doc.new_tag('details', id='source-credits', attrs={'class':'container standalone-credits'})
    summary = doc.new_tag('summary'); summary.string = 'Image and font credits'; details.append(summary)
    for section in credits.select('main > section'):
        for image in section.select('img'):
            image.decompose()
        for heading in section.select('h2'):
            heading.name = 'h3'
        for anchor in section.select('a[href]'):
            if anchor['href'].startswith(('assets/', 'data/')):
                path = anchor['href']; anchor['href'] = uri(path); anchor['download'] = Path(path).name
        details.append(section)
    doc.select_one('footer').insert_before(details)

    bundle = (ROOT / 'data/site-data.js').read_text().split('window.REFCRAFT = ',1)[1].strip().removesuffix(';')
    payload = json.loads(bundle)
    # Only embed demo HTML and reference images when the optional viewer is present.
    if doc.select_one('#example-tabs') is None:
        payload['examples'] = []
    for example in payload['examples']:
        for image in [*example['references'], example['output']]:
            image['src'] = uri(image['src'])
    data_script = doc.new_tag('script')
    data_script.string = 'window.REFCRAFT = ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/') + ';'
    doc.body.append(data_script)
    app_script = doc.new_tag('script')
    app_script.string = '\n'.join((ROOT / name).read_text() for name in ['leaderboard.js', 'app.js']).replace('href="credits.html"', 'href="#source-credits"') + '\ndocument.addEventListener("click", event => {if(event.target.closest(\'[href="#source-credits"]\')) document.getElementById("source-credits").open = true;});\n'
    doc.body.append(app_script)
    target = ROOT / 'RefCraft.html'
    target.write_text(str(doc))
    print(f'Created {target.name}: {target.stat().st_size / 1024 / 1024:.2f} MiB, fully offline.')


if __name__ == '__main__':
    build()
