"""Validate study Markdown and optionally render the same offline math preview/PDF."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from assistant_service.study_agent.document_format import inspect_document
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path)
    parser.add_argument('--preview',type=Path);parser.add_argument('--pdf',type=Path);parser.add_argument('--render-check',action='store_true');a=parser.parse_args()
    text=a.source.read_text(encoding='utf-8-sig');inspection=inspect_document(text)
    result={'math':len(inspection['math']),'code_blocks':sum(t.kind=='code' for t in inspection['blocks']),'warnings':inspection['warnings']}
    if a.preview:
        from assistant_service.study_agent.math_renderer import preview_html
        a.preview.write_text(preview_html(text)[0],encoding='utf8')
    if a.render_check or a.pdf:
        from assistant_service.study_agent.math_renderer import render_document
        result['render']=render_document(text,a.pdf)
    print(json.dumps(result,ensure_ascii=True))
if __name__=='__main__':main()
