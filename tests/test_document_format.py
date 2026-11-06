import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_service.study_agent.document_format import (
    DocumentFormatError,document_html,inspect_document,parse_document)
from assistant_service.study_agent.exports import save_pack,write_pdf
from assistant_service.study_agent.math_renderer import preview_html


class DocumentFormatTests(unittest.TestCase):
    def test_math_code_and_indentation_are_separate(self):
        text=r'''# Membrane
The steady value is $u_{\infty}$, the function is \(f(I)\).

$$
\begin{aligned}
u_{\infty}&=RI\\
\tau&=RC
\end{aligned}
$$

```vhdl
  q_next <= a_b & c;
  v := "$not_math$";
```
'''
        result=inspect_document(text)
        self.assertEqual(len(result['math']),3)
        self.assertEqual([b.text for b in result['blocks'] if b.kind=='code'],
                         ['  q_next <= a_b & c;\n  v := "$not_math$";'])
        markup,math,_=document_html(text)
        self.assertIn('  q_next &lt;= a_b &amp; c;',markup)
        self.assertEqual(math[0]['tex'],r'u_{\infty}')
        self.assertFalse(result['warnings'])

    def test_delimiter_errors_and_double_escaped_json_are_actionable(self):
        for text in ['title\n\n$u_{x}', '```vhdl\na <= b;', '$$\nu_x',
                     '$$\n' + r'\\frac{1}{2}' + '\n$$', 'text\n\x0crac', 'text\n\right']:
            with self.subTest(text=repr(text)):
                with self.assertRaisesRegex(DocumentFormatError,'第[123]行'):parse_document(text)

    def test_currency_code_and_plain_identifier_warning(self):
        inspected=inspect_document(r'Cost \$5; `u_inf` is code, u_inf is ambiguous.')
        self.assertEqual(inspected['math'],[])
        self.assertEqual(len(inspected['warnings']),1)

    def test_table_does_not_split_pipe_inside_code_or_math(self):
        markup,math,_=document_html('| A | B |\n| --- | --- |\n| `a | b` | $|x|$ |')
        self.assertEqual(markup.count('<td>'),2)
        self.assertIn('<code>a | b</code>',markup)
        self.assertEqual(math[0]['tex'],'|x|')

    def test_generated_html_is_escaped_and_offline(self):
        markup,meta=preview_html(r'**Value** `<script>alert(1)</script>` $x_1$')
        self.assertIn('<strong>Value</strong>',markup)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',markup)
        self.assertNotIn('<script>alert(1)</script>',markup)
        self.assertIn("default-src 'none'",markup)
        self.assertNotIn('<script src=',markup)
        self.assertNotIn('url(fonts/',markup)
        self.assertEqual(meta['math'],1)

    def test_math_and_code_use_actual_renderer(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'x.pdf'
            with patch('assistant_service.study_agent.math_renderer.render_document') as render:
                write_pdf('$u_{\\infty}$',target)
                render.assert_called_once_with('$u_{\\infty}$',target)
            with patch('assistant_service.study_agent.math_renderer.render_document') as render:
                write_pdf('```vhdl\n  a <= b;\n```',target)
                render.assert_called_once()
            for source in ['Use `q_next <= a;` here.','| A | B |\n| --- | --- |\n| x | y |']:
                with patch('assistant_service.study_agent.math_renderer.render_document') as render:
                    write_pdf(source,target)
                    render.assert_called_once_with(source,target)

    def test_invalid_pdf_does_not_publish_markdown_first(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('assistant_service.study_agent.exports.pack_markdown',return_value='$x$'), \
                 patch('assistant_service.study_agent.exports.write_pdf',side_effect=DocumentFormatError('bad math')):
                with self.assertRaises(DocumentFormatError):save_pack(None,folder,'job1','course','topic',[],['md','pdf'])
            self.assertEqual(list((Path(folder)/'study-exports').iterdir()),[])

    def test_markdown_math_is_render_checked_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('assistant_service.study_agent.exports.pack_markdown',return_value='$x$'), \
                 patch('assistant_service.study_agent.math_renderer.render_document',side_effect=DocumentFormatError('bad math')):
                with self.assertRaises(DocumentFormatError):save_pack(None,folder,'job2','course','topic',[],['md'])
            self.assertEqual(list((Path(folder)/'study-exports').iterdir()),[])

    @unittest.skipUnless(os.getenv('SCUT_RENDER_INTEGRATION')=='1','requires local headless browser')
    def test_real_browser_writes_readable_math_and_code_pdf(self):
        from assistant_service.study_agent.math_renderer import render_document
        from pypdf import PdfReader
        source=r'''# Math export
Steady value $u_{\infty}=RI$.

$$
\frac{1}{1+x^{2}}
$$

```vhdl
  q_next <= a_b & c;
  v := 1;
```
'''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'printed.pdf'
            report=render_document(source,path)
            self.assertEqual(report['errors'],[])
            self.assertTrue(path.read_bytes().startswith(b'%PDF-'))
            extracted='\n'.join(page.extract_text() for page in PdfReader(path).pages)
            self.assertIn('q_next <= a_b & c;',extracted)
            self.assertIn('v := 1;',extracted)
            self.assertNotIn(r'\frac',extracted)
            self.assertNotIn(r'u_{\infty}',extracted)

    @unittest.skipUnless(os.getenv('SCUT_RENDER_INTEGRATION')=='1','requires local headless browser')
    def test_real_browser_rejects_invalid_latex_without_pdf(self):
        from assistant_service.study_agent.math_renderer import render_document
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bad.pdf'
            with self.assertRaisesRegex(DocumentFormatError,'第3行'):
                render_document('# Broken\n\n$\\notARealMathCommand{x}$',path)
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
