from __future__ import annotations


def build_run_index_html(*, run_id: str, body_html: str, nav_html: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{run_id} MathJax Review</title>
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    processEnvironments: true
  }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }}
}};
</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@4/tex-chtml.js"></script>
<style>
body {{ margin: 0; background: #f5f6f8; color: #17202a; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
header {{ position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid #d9dee7; padding: 14px 22px; }}
h1 {{ margin: 0 0 10px; font-size: 21px; }}
nav {{ display: flex; flex-wrap: wrap; gap: 6px; }}
nav a {{ padding: 4px 8px; border: 1px solid #cdd6e1; border-radius: 4px; color: #165e96; text-decoration: none; background: #f8fbff; font-size: 13px; }}
main {{ max-width: 980px; margin: 0 auto; padding: 18px; }}
.question {{ background: #fff; border: 1px solid #d9dee7; border-radius: 6px; padding: 14px 16px; margin: 0 0 14px; }}
.question h2 {{ margin: 0 0 10px; font-size: 18px; }}
.text-block {{ margin: 0 0 8px; line-height: 1.72; }}
.blank {{ display: inline-block; min-width: 3.4em; height: 0.72em; border-bottom: 1px solid #17202a; vertical-align: -0.08em; }}
.choice-blank {{ display: inline-flex; align-items: baseline; gap: 0.1em; vertical-align: -0.08em; }}
.choice-blank::before {{ content: "("; }}
.choice-blank::after {{ content: ")"; }}
.choice-blank-space {{ display: inline-block; min-width: 1.8em; height: 0.72em; }}
.options {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 18px; margin: 8px 0 4px; }}
.option {{ display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: start; }}
.option-key {{ font-weight: 700; color: #165e96; padding-top: 2px; }}
.option-body .text-block {{ margin: 0; }}
.step2-crops {{ margin: 0 0 12px; }}
.step2-crops summary {{ cursor: pointer; color: #165e96; font-weight: 700; }}
.step2-crop-group {{ margin-top: 10px; }}
.step2-crop-group h3 {{ margin: 0 0 6px; font-size: 14px; color: #5d6878; }}
.step2-crop-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
.step2-crop-card {{ margin: 0; border: 1px solid #d9dee7; border-radius: 4px; background: #f8fbff; padding: 6px; }}
.step2-crop-card img {{ display: block; width: 100%; height: auto; border-radius: 2px; background: #fff; }}
.asset {{ margin: 10px 0; display: block; }}
.asset img {{ display: block; width: auto; height: auto; max-width: min(420px, 90%); border: 1px solid #dce3ec; border-radius: 4px; background: #fff; padding: 4px; object-fit: contain; }}
.asset-table {{ overflow-x: auto; }}
.html-table, .asset table {{ border-collapse: collapse; margin: 0 auto; background: #fff; }}
.html-table td, .html-table th, .asset td, .asset th {{ border: 1px solid #2b3340; padding: 5px 9px; text-align: center; line-height: 1.45; vertical-align: top; }}
.html-table th, .asset th {{ font-weight: 700; background: #f5f8fb; }}
.qa-section {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #e1e7ef; }}
.qa-section h3 {{ margin: 0 0 6px; font-size: 15px; color: #165e96; }}
.qa-section summary {{ cursor: pointer; color: #165e96; font-weight: 700; }}
.answer-section {{ border-top-color: #cfe2d6; }}
.answer-section .text-block {{ color: #173f2a; font-weight: 600; }}
.analysis-section .qa-body {{ margin-top: 8px; }}
.issues {{ margin-top: 10px; color: #5d6878; font-size: 13px; }}
.issues summary {{ cursor: pointer; }}
.empty {{ color: #7a8494; }}
.options-missing, .asset-missing-label {{ border: 1px dashed #ba5c5c; color: #9a3d3d; padding: 2px 5px; border-radius: 4px; }}
mjx-container[jax="CHTML"][display="true"] {{ margin: .45em 0; }}
@media (max-width: 760px) {{ main {{ padding: 10px; }} .options {{ grid-template-columns: 1fr; }} .step2-crop-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
<h1>{run_id} MathJax Web Review</h1>
<nav>{nav_html}</nav>
</header>
<main>
{body_html}
</main>
</body>
</html>
"""
