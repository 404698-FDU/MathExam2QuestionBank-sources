from __future__ import annotations


def build_run_index_html(*, run_id: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{run_id} - v12 Question Bank Render</title>
  <script>
    window.MathJax = {{
      tex: {{
        inlineMath: [['$', '$']],
        displayMath: [['$$', '$$']]
      }},
      svg: {{ fontCache: 'global' }}
    }};
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
  <style>
    :root {{
      --bg: #f7f3ea;
      --ink: #1b1b1b;
      --muted: #6d665c;
      --line: #d8cfbf;
      --card: #fffdfa;
      --accent: #17494d;
      --accent-soft: #dfeeea;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Noto Serif SC", "Source Han Serif SC", Georgia, serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(23,73,77,0.12), transparent 28%),
        linear-gradient(180deg, #f8f4ec 0%, #f2ebe0 100%);
    }}
    .page {{
      width: min(1200px, calc(100vw - 32px));
      margin: 24px auto 56px;
    }}
    .page-header {{
      padding: 24px 28px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .page-header h1 {{
      margin: 0;
      font-size: 28px;
    }}
    .question-card {{
      margin-top: 20px;
      padding: 20px 24px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .question-header h2 {{
      margin: 0 0 16px;
      font-size: 22px;
    }}
    .question-section {{
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
    }}
    .question-section h3 {{
      margin: 0 0 12px;
      font-size: 16px;
      color: var(--accent);
    }}
    .options-group {{
      margin: 12px 0;
      padding-left: 0;
      list-style: none;
    }}
    .option-item {{
      display: grid;
      grid-template-columns: 28px 1fr;
      gap: 12px;
      padding: 10px 12px;
      margin-bottom: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--accent-soft);
    }}
    .option-label {{
      font-weight: 700;
    }}
    .blank-slot, .choice-slot {{
      display: inline-block;
      min-width: 72px;
      border-bottom: 2px solid var(--accent);
      height: 1em;
      vertical-align: -0.1em;
      margin: 0 4px;
    }}
    .asset {{
      margin: 12px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    .asset img {{
      max-width: 100%;
      height: auto;
      display: block;
    }}
    .asset-table {{
      overflow-x: auto;
    }}
    .asset table {{
      width: max-content;
      min-width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .asset th,
    .asset td {{
      padding: 6px 8px;
      border: 1px solid var(--line);
      vertical-align: top;
    }}
    .asset-missing-label {{
      color: #8a3f2f;
      font-size: 14px;
    }}
    .empty {{
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="page-header">
      <h1>{run_id}</h1>
    </header>
    {body_html}
  </main>
</body>
</html>
"""
