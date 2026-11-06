"""Small, explicit Markdown/math contract shared by study exports.

Math and code are separate tokens. Never escape or rewrite their internals as
ordinary prose. Rendering is the final syntax check; this parser is not an LLM.
"""
from __future__ import annotations
from dataclasses import dataclass
import html,re

class DocumentFormatError(ValueError):pass

@dataclass(frozen=True)
class Token:
    kind:str
    text:str
    line:int
    language:str=''

def _error(line,message):raise DocumentFormatError(f'第{line}行：{message}')

def _math(text,line):
    if not text.strip():_error(line,'公式为空')
    if re.search(r'\\\\(?:frac|sqrt|sum|prod|begin|end|text|mathrm|left|right)\b',text):
        _error(line,'公式似乎经过重复转义；JSON解码后LaTeX命令只保留一个反斜杠')
    return text.strip()

def inline_tokens(text,line):
    result=[];plain=[];i=0
    def flush():
        if plain:result.append(Token('text',''.join(plain),line));plain.clear()
    while i<len(text):
        if text.startswith('\\$',i):plain.append('$');i+=2;continue
        if text[i]=='`':
            run=re.match(r'`+',text[i:])[0];end=text.find(run,i+len(run))
            if end<0:_error(line,'行内代码缺少结束反引号')
            flush();result.append(Token('inline_code',text[i+len(run):end],line));i=end+len(run);continue
        if text[i]=='$' or text.startswith('\\(',i):
            opener='\\(' if text.startswith('\\(',i) else '$';closer='\\)' if opener=='\\(' else '$'
            if text.startswith('$$',i):_error(line,'块公式的$$须独立成行')
            end=i+len(opener)
            while True:
                end=text.find(closer,end)
                if end<0:_error(line,'行内公式未闭合；数学用$...$，金额中的美元符号写成\\$')
                if end==0 or text[end-1]!='\\' or closer=='\\)':break
                end+=len(closer)
            flush();result.append(Token('math',_math(text[i+len(opener):end],line),line));i=end+len(closer);continue
        plain.append(text[i]);i+=1
    flush();return result

def parse_document(text):
    if len(text)>2_000_000:raise DocumentFormatError('文档超过200万字符，请分章导出')
    for match in re.finditer(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',text):
        _error(text.count('\n',0,match.start())+1,'含控制字符；可能是JSON中\\frac/\\bar被错误解码，未静默修复')
    normalized=text.replace('\r\n','\n')
    if '\r' in normalized:_error(normalized.count('\n',0,normalized.index('\r'))+1,'含孤立回车；可能是JSON中的\\right被错误解码，请检查反斜杠')
    lines=normalized.split('\n');blocks=[];i=0
    while i<len(lines):
        value=lines[i];stripped=value.strip();line=i+1;i+=1
        if not stripped:continue
        fence=re.match(r'^\s*(`{3,}|~{3,})([A-Za-z0-9_+-]*)\s*$',value)
        if fence:
            marker,language=fence.groups();body=[]
            while i<len(lines) and not re.fullmatch(r'\s*'+re.escape(marker[0])+r'{'+str(len(marker))+r',}\s*',lines[i]):body.append(lines[i]);i+=1
            if i==len(lines):_error(line,'代码围栏未闭合')
            i+=1;blocks.append(Token('code','\n'.join(body),line,language));continue
        if stripped.startswith('$$') or stripped.startswith('\\['):
            opener='$$' if stripped.startswith('$$') else '\\[';closer='$$' if opener=='$$' else '\\]';tail=stripped[len(opener):]
            body=[]
            if tail.endswith(closer) and tail:
                body=[tail[:-len(closer)]]
            else:
                if tail:body.append(tail)
                while i<len(lines) and lines[i].strip()!=closer:body.append(lines[i]);i+=1
                if i==len(lines):_error(line,'块公式未闭合')
                i+=1
            blocks.append(Token('display_math',_math('\n'.join(body),line),line));continue
        heading=re.match(r'^(#{1,6})\s+(.+)',stripped)
        if heading:blocks.append(Token('heading',heading[2],line,str(len(heading[1]))));continue
        if stripped.startswith('|') and i<len(lines) and re.fullmatch(r'\s*\|?[ :|\-]+\|?\s*',lines[i]) and '-' in lines[i]:
            rows=[value];i+=1
            while i<len(lines) and lines[i].strip().startswith('|'):rows.append(lines[i]);i+=1
            blocks.append(Token('table','\n'.join(rows),line));continue
        kind='quote' if stripped.startswith('> ') else 'list' if re.match(r'^(?:[-*+] |\d+[.)] )',stripped) else 'paragraph'
        body=[stripped[2:] if kind=='quote' else stripped]
        while kind=='paragraph' and i<len(lines) and lines[i].strip() and not re.match(r'^\s*(?:[#|>]|`{3,}|~{3,}|\$\$|\\\[|[-*+] |\d+[.)] )',lines[i]):body.append(lines[i].strip());i+=1
        blocks.append(Token(kind,' '.join(body),line))
    # Validate delimiters in every prose/table cell without interpreting code.
    for block in blocks:
        if block.kind not in {'code','display_math'}:
            for row in block.text.splitlines():inline_tokens(row,block.line)
    return blocks

def inspect_document(text):
    blocks=parse_document(text);math=[];warnings=[];rich=False
    for b in blocks:
        if b.kind in {'code','table'}:rich=True
        if b.kind=='display_math':math.append(b)
        elif b.kind!='code':
            for row in b.text.splitlines():
                for token in inline_tokens(row,b.line):
                    if token.kind=='math':math.append(token)
                    elif token.kind=='inline_code':rich=True
                    elif token.kind=='text' and re.search(r'\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9{]',token.text):warnings.append({'line':b.line,'message':'正文含下划线标记；数学变量请用公式，程序标识符请用行内代码'})
    return {'blocks':blocks,'math':math,'warnings':warnings,'requires_browser':bool(math) or rich}

def document_html(text):
    inspected=inspect_document(text);formulas=[]
    def render_tokens(tokens):
        out=[]
        for t in tokens:
            if t.kind=='math':
                idx=len(formulas);formulas.append({'tex':t.text,'display':False,'line':t.line});out.append(f'<span class="math" data-math="{idx}"></span>')
            elif t.kind=='inline_code':out.append('<code>'+html.escape(t.text)+'</code>')
            else:
                # Formatting is allowed only after escaping source text. Raw HTML
                # and generated script/link attributes never enter the document.
                escaped=html.escape(t.text)
                escaped=re.sub(r'\*\*([^*\n]+)\*\*',r'<strong>\1</strong>',escaped)
                out.append(escaped)
        return ''.join(out)
    def inline(value,line):return render_tokens(inline_tokens(value,line))
    out=[]
    for b in inspected['blocks']:
        if b.kind=='display_math':
            idx=len(formulas);formulas.append({'tex':b.text,'display':True,'line':b.line});out.append(f'<div class="math display" data-math="{idx}"></div>')
        elif b.kind=='code':out.append('<pre><code>'+html.escape(b.text)+'</code></pre>')
        elif b.kind=='heading':out.append(f'<h{b.language}>'+inline(b.text,b.line)+f'</h{b.language}>')
        elif b.kind=='quote':out.append('<blockquote>'+inline(b.text,b.line)+'</blockquote>')
        elif b.kind=='table':
            rows=[]
            for n,row in enumerate(b.text.splitlines()):
                value=row.strip();value=value[1:] if value.startswith('|') else value;value=value[:-1] if value.endswith('|') else value
                cells=[[]]
                for token in inline_tokens(value,b.line):
                    if token.kind!='text':cells[-1].append(token);continue
                    for k,piece in enumerate(re.split(r'(?<!\\)\|',token.text)):
                        if k:cells.append([])
                        cells[-1].append(Token('text',piece.replace(r'\|','|'),token.line))
                tag='th' if n==0 else 'td';rows.append('<tr>'+''.join('<'+tag+'>'+render_tokens(cell)+'</'+tag+'>' for cell in cells)+'</tr>')
            out.append('<table>'+''.join(rows)+'</table>')
        else:out.append('<p>'+inline(b.text,b.line)+'</p>')
    return '\n'.join(out),formulas,inspected['warnings']
