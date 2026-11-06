"""Source-linked study packs; generated conclusions keep their verification labels."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import tempfile
from xml.sax.saxutils import escape

from .planner import TZ
from .document_format import inspect_document


def pack_markdown(store, course, topic, results):
    lines = [f"# {course} · 学习资料", "", topic, "", datetime.now(TZ).strftime("生成于 %Y-%m-%d %H:%M"), "",
             "> 根据所列资料生成。引文存在不等于结论已被证明；转写和图片识读部分请对照原件。", ""]
    references, answers = {}, []
    def cites(items):
        labels = []
        for c in items:
            eid = c["evidence_id"]
            key = (eid, c["quote"])
            if key not in references:
                references[key] = {"number": len(references) + 1, "quote": c["quote"], "evidence": store.evidence(eid)}
            labels.append(f"[{references[key]['number']}]")
        return " ".join(dict.fromkeys(labels))
    for result in results:
        if result.get("claims"):
            lines += ["## 复习要点", ""]
        for claim in result.get("claims", []):
            lines += [claim["text"] + " " + cites(claim["citations"]), "",
                      "核对状态：" + claim.get("verification", {}).get("label", "尚未独立核验"), ""]
        for link in result.get("connections", []):
            lines += [f"### {link['concept_a']} / {link['concept_b']}", "", link["relationship"] + " " + cites(link["citations"]), ""]
        if result.get("exercise"):
            practice = store.get(result["exercise"]["id"], "practice")
            number = len(answers) + 1
            lines += [f"## 练习 {number}", "", practice["question"], "", "思路与解答：", "", "_______________________________", "", "_______________________________", ""]
            answers.append((number, practice["expected_answer"], cites(practice["citations"])))
        if result.get("uncertainties"):
            lines += ["### 待核对", ""] + ["- " + x for x in result["uncertainties"]] + [""]
    if answers:
        lines += ["## 参考答案（完成练习后再看）", ""]
        for n, answer, citations in answers:
            lines += [f"### 第 {n} 题", "", answer + " " + citations, ""]
    lines += ["## 来源", ""]
    for ref in references.values():
        item = ref["evidence"]; loc = item.get("locator", {})
        names={"page":"页", "slide":"幻灯片", "section":"段", "start":"起点（秒）", "end":"终点（秒）", "url":"链接"}
        location = " · ".join(f"{names[k]} {v}" for k, v in loc.items() if k in names)
        lines += [f"[{ref['number']}] {item['title']} · {location}", "", "> " + ref["quote"].replace("\n", "\n> "), ""]
    return "\n".join(lines)


def write_pdf(text, destination):
    inspected=inspect_document(text)
    if inspected['requires_browser']:
        from .math_renderer import render_document
        return render_document(text,destination)
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.colors import HexColor
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
        from reportlab.lib.pagesizes import A4
    except ImportError as exc:
        raise ValueError("PDF 组件缺失；请安装项目 study 依赖。Markdown 仍可导出。") from exc
    font = "StudyCJK"
    if font not in pdfmetrics.getRegisteredFontNames():
        for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
            if path.is_file():
                try:
                    pdfmetrics.registerFont(TTFont(font, str(path), subfontIndex=0)); break
                except Exception:
                    pass
        else:
            font = "STSong-Light"; pdfmetrics.registerFont(UnicodeCIDFont(font))
    styles = {"body": ParagraphStyle("body", fontName=font, fontSize=10.5, leading=18, spaceAfter=7, wordWrap="CJK", textColor=HexColor("#273633")),
              "heading": ParagraphStyle("heading", fontName=font, fontSize=16, leading=24, spaceBefore=16, spaceAfter=10, wordWrap="CJK", keepWithNext=True),
              "title": ParagraphStyle("title", fontName=font, fontSize=23, leading=32, spaceAfter=18, wordWrap="CJK")}
    styles["reference"] = ParagraphStyle("reference", parent=styles["body"], keepWithNext=True)
    flow = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        if block.startswith("## 参考答案"):
            flow.append(PageBreak())
        # PDF extraction line breaks belong to one quoted paragraph, not to
        # separate paragraphs with extra vertical space on every short line.
        paragraphs = block.splitlines() if block.startswith("- ") else [block]
        for paragraph in paragraphs:
            style = styles["title"] if paragraph.startswith("# ") else styles["heading"] if paragraph.startswith("##") else styles["reference"] if re.match(r"^\[\d+\] ", paragraph) else styles["body"]
            clean = " ".join(re.sub(r"^#{1,3} |^> ", "", line).strip() for line in paragraph.splitlines()).replace("**", "")
            flow.append(Paragraph(escape(clean), style))
    def footer(canvas, doc):
        canvas.setFont(font, 8); canvas.setFillColor(HexColor("#74857F"))
        canvas.drawString(42, 25, "SCUT · 学习资料"); canvas.drawRightString(A4[0]-42, 25, str(doc.page))
    SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=46, leftMargin=46, topMargin=42, bottomMargin=44,
                      title="SCUT 学习资料", author="SCUT 课堂助手").build(flow, onFirstPage=footer, onLaterPages=footer)


def save_pack(store, root, jid, course, topic, results, formats):
    folder = Path(root) / "study-exports"; folder.mkdir(exist_ok=True)
    text = pack_markdown(store, course, topic, results)
    inspected=inspect_document(text)
    if 'pdf' not in formats and inspected['math']:
        from .math_renderer import render_document
        render_document(text)  # Validate syntax in the actual renderer before publishing Markdown.
    if any(fmt not in {'md','pdf'} for fmt in formats):raise ValueError("仅支持 PDF 和 Markdown")
    files = []
    # Finish every format before publishing any file, including a Markdown-first
    # request whose PDF subsequently fails. Temporary outputs are removed on error.
    with tempfile.TemporaryDirectory(prefix='render-',dir=folder) as staging:
        for fmt in dict.fromkeys(formats):
            name = jid + "." + fmt
            path = Path(staging) / name
            if fmt == "md": path.write_text(text, encoding="utf-8")
            else: write_pdf(text, path)
            files.append({"name": course + ("复习题" if any(r.get("exercise") for r in results) else "复习资料") + "." + fmt,
                          "file": name, "format": fmt})
        for item in files:(Path(staging)/item['file']).replace(folder/item['file'])
    return files
