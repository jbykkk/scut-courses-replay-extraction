"""Offline KaTeX preview and browser PDF for math/code-bearing study documents."""
from __future__ import annotations
import base64,json,re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from .document_format import document_html,DocumentFormatError

ASSETS=Path(__file__).parent/'assets/katex'
CSS='''@page{size:A4;margin:19mm 18mm 19mm}*{box-sizing:border-box}body{font:15px/1.8 "Microsoft YaHei","Noto Sans CJK SC",sans-serif;color:#273633;margin:0;width:174mm;max-width:100%}h1{font-size:27px}h2{font-size:22px;margin-top:1.5em}h3,h4{font-size:18px}h1,h2,h3,h4{break-after:avoid}p,blockquote{orphans:3;widows:3}code{font-family:Consolas,"Courier New",monospace;font-size:.92em}pre{white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4;background:#f5f6f6;border:1px solid #dfe5e3;padding:13px;line-height:1.55;box-decoration-break:clone}blockquote{border-left:3px solid #c8d7d1;padding-left:14px;color:#566a62}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #dfe5e3;text-align:left;padding:7px;vertical-align:top}tr{break-inside:avoid}thead{display:table-header-group}.display{padding:7px 0;break-inside:avoid}.katex-display{margin:.6em 0}.math-error{border:2px solid #b63c32;padding:8px;color:#b63c32}'''

def _assets():
    if not (ASSETS/'katex.min.js').is_file():raise DocumentFormatError('本地公式资源缺失，请更新安装包；未把公式当普通文字导出')
    css=(ASSETS/'katex.min.css').read_text(encoding='utf8')
    def embed(match):
        name=re.search(r'fonts/([A-Za-z0-9_-]+\.woff2)',match[0])
        if not name:raise DocumentFormatError('本地公式字体声明不完整')
        data=base64.b64encode((ASSETS/'fonts'/name[1]).read_bytes()).decode()
        return 'src:url(data:font/woff2;base64,'+data+') format("woff2");'
    css=re.sub(r'src:[^;]+;',embed,css)
    return css,(ASSETS/'katex.min.js').read_text(encoding='utf8')

def preview_html(text):
    body,formulas,warnings=document_html(text);css,js=_assets()
    # Escape the JSON script delimiter. Course text itself is always HTML escaped.
    payload=json.dumps(formulas,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    initialize='''(async()=>{const formulas=JSON.parse(document.getElementById("formulas").textContent);const errors=[];for(let i=0;i<formulas.length;i++){const f=formulas[i],target=document.querySelector('[data-math="'+i+'"]');try{katex.render(f.tex,target,{displayMode:f.display,throwOnError:true,trust:false,strict:"error",maxExpand:1000,maxSize:40,macros:{}});}catch(e){errors.push({line:f.line,index:i,message:String(e.message).slice(0,240)});target.classList.add("math-error");target.textContent="公式排版失败（源文件第"+f.line+"行）："+e.message;}}await document.fonts.ready;for(const el of document.querySelectorAll(".display")){if(el.scrollWidth>el.clientWidth+3){errors.push({line:formulas[Number(el.dataset.math)].line,message:"块公式超出版心，请用aligned分行"});}}window.__studyRender={ready:true,math:formulas.length,errors};})();'''
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; font-src data:; img-src data:"><title>学习资料预览</title><style>'+css+'\n'+CSS+'</style><body>'+body+'<script id="formulas" type="application/json">'+payload+'</script><script>'+js+'</script><script>'+initialize+'</script></body></html>'
    return page,{'math':len(formulas),'warnings':warnings}

def _render(page_html,destination):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:raise DocumentFormatError('浏览器排版组件缺失；请安装项目依赖') from exc
    with sync_playwright() as p:
        browser=None
        # A fresh, ephemeral browser. Never opens the user's authenticated profile.
        for channel in ['msedge','chrome',None]:
            try:
                options={'headless':True,'timeout':20000}
                if channel:options['channel']=channel
                browser=p.chromium.launch(**options);break
            except Exception:continue
        if browser is None:raise DocumentFormatError('公式检查需要本机Edge、Chrome或Playwright Chromium；请保留原稿并安装排版组件后重试，未输出未经验证的文件')
        try:
            page=browser.new_page(viewport={'width':900,'height':1000})
            page.route('**/*',lambda route:route.abort())
            page.set_content(page_html,wait_until='load',timeout=20000)
            page.wait_for_function('window.__studyRender?.ready === true',timeout=20000)
            report=page.evaluate('window.__studyRender')
            if report['errors']:
                error=report['errors'][0]
                raise DocumentFormatError(f"第{error['line']}行公式排版失败：{error['message']}")
            if destination is not None:
                page.pdf(path=str(destination),format='A4',print_background=True,prefer_css_page_size=True,timeout=30000,
                         display_header_footer=True,header_template='<span></span>',
                         footer_template='<div style="width:100%;font-size:9px;color:#75857f;padding:0 18mm">SCUT · 学习资料<span class="pageNumber" style="float:right"></span></div>')
            return report
        finally:browser.close()

def render_document(text,destination=None):
    page,metadata=preview_html(text)
    # Also works when called from a host that already has an asyncio loop.
    with ThreadPoolExecutor(max_workers=1) as worker:report=worker.submit(_render,page,destination).result()
    return {**metadata,**report,'renderer':'local-katex-browser'}
