/** 轻量 Markdown 子集：先转义 HTML，再生成受控标签；不执行模型输出的 HTML。 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function inline(s: string): string {
  // 只接收已经转义的文本；这里生成的 code / strong / em 是程序控制的标签。
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\n/g, '<br>')
}

export function renderMarkdown(src: string): string {
  // 【核心】先转义再解析顺序不能颠倒，ChatView 会通过 v-html 插入返回值。
  const text = escapeHtml((src ?? '').replace(/\r\n/g, '\n'))
  const lines = text.split('\n')
  const out: string[] = []
  let list: 'ul' | 'ol' | null = null
  let inCode = false // 围栏代码块内跳过其他 Markdown 规则，保持代码的原始文本结构。
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
  // 流式文本可能暂时缺少闭合围栏，补齐标签以免影响后面的页面布局。
  if (inCode) out.push('</code></pre>')
  return out.join('\n')
}
