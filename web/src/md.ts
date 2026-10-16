/**
 * 极小的 Markdown 渲染。**不引依赖**（老项目也是手写的，3.6KB）。
 *
 * 为什么不用现成的库：数字人的回答里出现的东西很有限（标题、粗体、列表、
 * 引用、行内代码、代码块），为这点需求引一个几百 KB 的解析器不划算。
 * 但**必须先转义 HTML**——他的回答里如果有 `<script>`，绝不能被当成标签执行。
 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\n/g, '<br>')
}

export function renderMarkdown(src: string): string {
  const text = escapeHtml((src ?? '').replace(/\r\n/g, '\n'))
  const lines = text.split('\n')
  const out: string[] = []
  let list: 'ul' | 'ol' | null = null
  let inCode = false
  let para: string[] = []

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join('\n'))}</p>`)
      para = []
    }
  }
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`)
      list = null
    }
  }

  for (const raw of lines) {
    const line = raw
    if (line.trim().startsWith('```')) {
      flushPara()
      closeList()
      out.push(inCode ? '</code></pre>' : '<pre><code>')
      inCode = !inCode
      continue
    }
    if (inCode) {
      out.push(line + '\n')
      continue
    }
    if (!line.trim()) {
      flushPara()
      closeList()
      continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      flushPara()
      closeList()
      const level = Math.min(h[1].length + 2, 6) // h1 用在页面标题上，回答里的标题从 h3 起
      out.push(`<h${level}>${inline(h[2])}</h${level}>`)
      continue
    }
    if (/^\s*[-*·]\s+/.test(line)) {
      flushPara()
      if (list !== 'ul') {
        closeList()
        out.push('<ul>')
        list = 'ul'
      }
      out.push(`<li>${inline(line.replace(/^\s*[-*·]\s+/, ''))}</li>`)
      continue
    }
    if (/^\s*\d+[.、)]\s+/.test(line)) {
      flushPara()
      if (list !== 'ol') {
        closeList()
        out.push('<ol>')
        list = 'ol'
      }
      out.push(`<li>${inline(line.replace(/^\s*\d+[.、)]\s+/, ''))}</li>`)
      continue
    }
    if (/^\s*>\s?/.test(line)) {
      flushPara()
      closeList()
      out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`)
      continue
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushPara()
      closeList()
      out.push('<hr>')
      continue
    }
    para.push(line)
  }
  flushPara()
  closeList()
  if (inCode) out.push('</code></pre>')
  return out.join('\n')
}
