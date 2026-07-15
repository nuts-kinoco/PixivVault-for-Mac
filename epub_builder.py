import os
import zipfile
import uuid
from typing import List, Dict

def create_epub(output_path: str, title: str, author: str, content_html: str, cover_image_path: str = None, embedded_images: List[Dict[str, str]] = None):
    """
    指定されたパラメータからEPUB 2.0ファイルを生成します。
    
    :param output_path: 保存先のファイルパス
    :param title: 小説のタイトル
    :param author: 作者名
    :param content_html: 本文のHTML
    :param cover_image_path: 表紙画像のローカルパス
    :param embedded_images: 挿絵のリスト。辞書の形式: {'id': '画像ID', 'path': 'ローカルパス', 'ext': '.jpg'}
    """
    if embedded_images is None:
        embedded_images = []
        
    book_id = f"urn:uuid:{uuid.uuid4()}"
    
    # MIMEタイプ
    mimetype = "application/epub+zip"
    
    # container.xml
    container_xml = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
   <rootfiles>
      <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
   </rootfiles>
</container>"""

    # manifest用の項目を構築
    manifest_items = ""
    cover_meta = ""
    
    if cover_image_path and os.path.exists(cover_image_path):
        ext = os.path.splitext(cover_image_path)[1].lower()
        media_type = "image/png" if ext == ".png" else "image/jpeg"
        manifest_items += f'\n        <item id="cover-image" href="Images/cover{ext}" media-type="{media_type}"/>'
        cover_meta = '\n        <meta name="cover" content="cover-image"/>'
        
    for img in embedded_images:
        ext = img['ext'].lower()
        media_type = "image/png" if ext == ".png" else "image/jpeg"
        manifest_items += f'\n        <item id="img_{img["id"]}" href="Images/{img["id"]}{ext}" media-type="{media_type}"/>'

    # content.opf
    content_opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        <dc:creator opf:role="aut">{author}</dc:creator>
        <dc:language>ja</dc:language>
        <dc:identifier id="BookID">{book_id}</dc:identifier>{cover_meta}
    </metadata>
    <manifest>
        <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
        <item id="chapter1" href="Text/chapter1.xhtml" media-type="application/xhtml+xml"/>{manifest_items}
    </manifest>
    <spine toc="ncx">
        <itemref idref="chapter1"/>
    </spine>
</package>"""

    # toc.ncx
    toc_ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
    <head>
        <meta name="dtb:uid" content="{book_id}"/>
        <meta name="dtb:depth" content="1"/>
        <meta name="dtb:totalPageCount" content="0"/>
        <meta name="dtb:maxPageNumber" content="0"/>
    </head>
    <docTitle><text>{title}</text></docTitle>
    <navMap>
        <navPoint id="navPoint-1" playOrder="1">
            <navLabel><text>本文</text></navLabel>
            <content src="Text/chapter1.xhtml"/>
        </navPoint>
    </navMap>
</ncx>"""

    cover_html = ""
    if cover_image_path and os.path.exists(cover_image_path):
        ext = os.path.splitext(cover_image_path)[1].lower()
        cover_html = f'<div class="cover"><img src="../Images/cover{ext}" alt="Cover" /></div>\n'

    # chapter1.xhtml
    chapter1_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja">
<head>
    <title>{title}</title>
    <style>
        body {{ font-family: sans-serif; }}
        img {{ max-width: 100%; height: auto; display: block; margin: 1em auto; }}
        p {{ margin: 1em 0; line-height: 1.6; }}
        .cover {{ text-align: center; page-break-after: always; }}
    </style>
</head>
<body>
    {cover_html}
    <h1>{title}</h1>
    {content_html}
</body>
</html>"""

    # ZIPファイルとして書き出し
    # mimetype は非圧縮で最初に格納しなければならない
    with zipfile.ZipFile(output_path, 'w') as zf:
        zf.writestr('mimetype', mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr('META-INF/container.xml', container_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr('OEBPS/content.opf', content_opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr('OEBPS/toc.ncx', toc_ncx, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr('OEBPS/Text/chapter1.xhtml', chapter1_xhtml.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)
        
        # 画像の書き込み
        if cover_image_path and os.path.exists(cover_image_path):
            ext = os.path.splitext(cover_image_path)[1].lower()
            zf.write(cover_image_path, f'OEBPS/Images/cover{ext}', compress_type=zipfile.ZIP_DEFLATED)
            
        for img in embedded_images:
            if os.path.exists(img['path']):
                zf.write(img['path'], f'OEBPS/Images/{img["id"]}{img["ext"].lower()}', compress_type=zipfile.ZIP_DEFLATED)
