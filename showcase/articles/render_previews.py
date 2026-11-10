from pathlib import Path
import re,shutil,html,json
from markdown_it import MarkdownIt

def copy_file(source,target):
 if source.resolve()!=target.resolve():shutil.copy2(source,target)

ROOT=Path(__file__).resolve().parent;OUT=ROOT
md=MarkdownIt('commonmark',{'html':False}).enable('table')
css='''*{box-sizing:border-box}html{color:#20242b;background:#faf9f6}body{margin:0;font:18px/1.68 Georgia,serif}main{max-width:840px;margin:0 auto;padding:50px 32px 80px}h1,h2,h3{font-family:system-ui,sans-serif;line-height:1.18;letter-spacing:-.025em}h1{font-size:44px;margin:18px 0 28px}h2{font-size:28px;margin:48px 0 20px}h3{font-size:22px;margin-top:32px}p{margin:0 0 23px}a{color:#155b90;text-underline-offset:3px;overflow-wrap:anywhere}a:focus-visible,summary:focus-visible{outline:3px solid #f3b844;outline-offset:3px}img{max-width:100%;height:auto;background:white}figure{margin:28px 0}figcaption{font:14px/1.5 system-ui,sans-serif;color:#505968;margin-top:7px}figure a{display:block}table{border-collapse:collapse;width:100%;font:14px/1.45 system-ui,sans-serif}th,td{border-bottom:1px solid #d4d8dd;padding:11px 10px;text-align:left}th{font-weight:650;background:#eceff1}th:first-child,td:first-child{min-width:150px}.table-scroll{overflow:auto;margin:24px 0;max-width:100%}code,pre{font:14px/1.5 ui-monospace,monospace}code{overflow-wrap:anywhere}pre{padding:18px;background:#eef1f4;overflow:auto}pre code{white-space:pre}blockquote{border-left:3px solid #8da5b8;margin:26px 0;padding:0 0 0 18px}nav{font:13px/1.5 system-ui,sans-serif;color:#596471;margin-bottom:25px}nav a{margin-right:15px}.review{font:12px/1.6 system-ui,sans-serif;letter-spacing:.05em;color:#596471;text-transform:uppercase}@media(max-width:600px){main{padding:28px 20px 60px}h1{font-size:34px}h2{font-size:25px}body{font-size:17px}.table-scroll table{min-width:0}.table-scroll thead{display:none}.table-scroll tr{display:block;border:1px solid #d4d8dd;margin:0 0 14px}.table-scroll td{display:flex;justify-content:space-between;gap:20px;text-align:right!important}.table-scroll td::before{content:attr(data-label);text-align:left;color:#505968;max-width:60%}.table-scroll td:first-child{display:block;text-align:left!important;font-weight:700;background:#eceff1}.table-scroll td:first-child::before{display:none}}'''
projects=[('meta-harness-controls',ROOT/'meta-harness-controls'),('harness2-agent-lens',ROOT/'harness2-agent-lens')]
for slug,src in projects:
 dst=OUT/slug;dst.mkdir(exist_ok=True);text=(src/'article.md').read_text();copy_file(src/'article.md',dst/'article.md')
 # Only article-referenced local assets are exposed by the preview.
 for u in re.findall(r'\]\(([^)]+)\)',text):
  rel=u.split('#')[0].split('?')[0]
  if '://' in rel or not rel or rel.startswith('../'):continue
  f=src/rel
  if not f.is_file():raise ValueError(f'missing local asset {rel}')
  target=dst/rel;target.parent.mkdir(parents=True,exist_ok=True);copy_file(f,target)
 body=md.render(text)
 if slug=='meta-harness-controls':
  for name in ['validation-repeatability','voting-paired']:
   narrow=name+'-narrow.png';copy_file(src/'assets'/narrow,dst/'assets'/narrow)
   body=re.sub(r'<img src="assets/'+name+r'\.png"([^>]+)>',r'<picture><source media="(max-width: 600px)" srcset="assets/'+narrow+r'"><img src="assets/'+name+r'.png"\1></picture>',body)

 def label_table(match):
  table=match[0];labels=[re.sub('<[^>]+>','',x) for x in re.findall(r'<th[^>]*>(.*?)</th>',table,re.S)]
  index=0
  def cell(m):
   nonlocal index
   label=labels[index%len(labels)];index+=1
   return '<td data-label="'+html.escape(label,quote=True)+'"'+m[1]+'>'+m[2]+'</td>'
  return re.sub(r'<td([^>]*)>(.*?)</td>',cell,table,flags=re.S)
 body=re.sub(r'<table>.*?</table>',label_table,body,flags=re.S)
 body=re.sub(r'<table>(.*?)</table>',r'<div class="table-scroll" tabindex="0" aria-label="Scrollable data table"><table>\1</table></div>',body,flags=re.S)
 body=re.sub(r'<p>(<img src="([^"]+)"[^>]*>)</p>',r'<figure><a href="\2" aria-label="Open full-size figure">\1</a><figcaption>Open figure at full size.</figcaption></figure>',body)
 title=text.splitlines()[0].removeprefix('# ')
 header='Review draft · saved experiment results' if slug=='meta-harness-controls' else 'Local copy of current Forge report · native panels open online'
 content=f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{css}</style></head><body><main><div class="review">{header}</div><nav><a href="article.md">Markdown</a><a href="../index.html">Both articles</a></nav>{body}</main></body></html>'
 (dst/'index.html').write_text(content)
 (dst/'mobile.html').write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>390px review</title><style>body{{margin:0;background:#dce0e5}}iframe{{display:block;margin:0 auto;width:390px;height:100vh;border:0}}</style></head><body><iframe title="390 pixel article preview" src="index.html"></iframe></body></html>')
(OUT/'index.html').write_text('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Harness articles</title><style>'+css+'</style></head><body><main><h1>Harness articles</h1><p><a href="meta-harness-controls/index.html">Before we let an agent optimize the harness</a> — new review draft and calculation download.</p><p><a href="harness2-agent-lens/index.html">Recreating Harness²</a> — synced from the current Forge report.</p></main></body></html>')
print('Rendered two article previews with only referenced assets.')
