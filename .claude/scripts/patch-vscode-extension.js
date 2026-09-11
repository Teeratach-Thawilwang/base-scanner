#!/usr/bin/env node
// Patches the installed Claude Code VSCode extension so that clicking a file
// link in the chat panel actually opens the file, whatever it is, and so that
// selecting text in the panel pops up a translation of it.
//
// Four things about links are broken out of the box:
//   1. openFile() calls showTextDocument() for every path, which cannot open a
//      png or an mp4, so the click does nothing.
//   2. The markdown renderer percent-encodes the href, so a filename in Thai or
//      any non-ASCII script never resolves on disk.
//   3. A path starting with ~/ is never expanded, so it never resolves either.
//   4. The webview only treats an href as a file when it has an extension, is
//      absolute, or sits under a hardcoded list of folder names, so a relative
//      path to an extensionless file is not even sent to the host.
// And when showTextDocument rejects on a binary VSCode has no viewer for, a
// .blend or a .zip, nothing happens at all, which reads as a dead link, where
// clicking that same file in the explorer opens a tab offering Open Anyway.
//
// Two more changes add the translation popup, the same one .vscode/md-translate
// gives the markdown preview:
//   5. The panel webview runs under default-src 'none' with no connect-src, so
//      nothing inside it may call out. One host is opened, the translate
//      endpoint, and nothing else.
//   6. The popup itself is appended to the webview bundle.
// Selecting text anywhere in the panel, except inside the composer, translates
// it into Thai, or into English when it is already Thai.
//
// The last one is not a bug, it is a default nobody asked for:
//   7. Every message carries whatever file the editor has open, and whatever
//      text is selected in it, and the composer keeps a chip for the pair. The
//      attach is switched off at the source, and the chip and the divider
//      beside it go with it. A file still goes in the way any other file does,
//      by @ mention.
//   8. The footer also counts down what is left of the prompt cache window,
//      "59m" beside a clock. Only the readout goes. The session keeps
//      recording the window and nothing about caching changes.
//
// Run this again after every extension update. It refuses to touch a file when
// the minified shape it anchors on is gone, so a rewritten source fails loudly
// instead of producing a broken extension.
//
//   node .claude/scripts/patch-vscode-extension.js            apply
//   node .claude/scripts/patch-vscode-extension.js --dry-run  report only
//   node .claude/scripts/patch-vscode-extension.js --restore  undo

const fs = require('fs')
const os = require('os')
const path = require('path')
const { execFileSync } = require('child_process')

const MARKER = '/*ccpatch:v8*/'
const MARKER_ANY = /\/\*ccpatch:v\d+\*\//
const BACKUP_SUFFIX = '.ccpatch-backup'

// Only what VSCode's built-in media-preview renders in an editor tab.
// svg is left out on purpose, it is text and people edit it.
const MEDIA = 'png|jpe?g|gif|bmp|ico|webp|avif|mp4|webm|mp3|wav|ogg|oga'

const TRANSLATE_ORIGIN = 'https://translate.googleapis.com'

const EXT_ROOTS = [
  path.join(os.homedir(), '.vscode', 'extensions'),
  path.join(os.homedir(), '.vscode-insiders', 'extensions'),
  path.join(os.homedir(), '.vscode-server', 'extensions'),
]

const ID = '[A-Za-z_$][\\w$]*'

// async openFile($,Q){ ... j6.isAbsolute($) ... v8.existsSync( ...
// Only the signature is consumed. The path and fs module names are read out of
// the body through lookaheads instead, because what sits between them is
// rewritten on nearly every release: 2.1.263 resolved the path in one
// expression, 2.1.268 walks the workspace folders first. All the prelude needs
// is the first brace to sit behind, and those two names.
const HOST_HEAD = new RegExp(
  `async openFile\\((${ID}),\\s*(${ID})\\)\\{` +
    `(?=[\\s\\S]{0,800}?(${ID})\\.isAbsolute\\(\\1\\))` +
    `(?=[\\s\\S]{0,800}?(${ID})\\.existsSync\\()`
)

// }catch{}_$.window.showTextDocument(X).then((z)=>{
const HOST_TAIL = new RegExp(
  `\\}catch\\{\\}(${ID})\\.window\\.showTextDocument\\((${ID})\\)\\.then\\(\\((${ID})\\)=>\\{`
)

// default-src 'none'; ${q}; ${N}; ${Z}; script-src 'nonce-${V}'; ${D};
// The panel's CSP, built in a template literal. The other CSP in the file
// writes its nonce as {{NONCE}}, so the ${ in the lookahead tells them apart,
// and [^"] keeps the lookahead inside one content="..." attribute.
const PANEL_CSP = /default-src 'none'; (?=[^"]*script-src 'nonce-\$\{)/

// let Q;if(Z&&!j_1(this.lastSentSelection,this.selection.value))Q=this.selection.value
// The one read of the editor's selection. That object is what turns into
// <ide_selection>, or <ide_opened_file> when nothing is selected, so refusing
// it here covers both and leaves the toggle's own state out of it.
const WEBVIEW_SEND_SELECTION = new RegExp(
  `if\\((${ID})&&!(${ID})\\(this\\.lastSentSelection,this\\.selection\\.value\\)\\)`
)

// Y&&D("div",{className:_7.divider}),Y&&D(k55,{includeSelection:G??!1,currentSelection:Y,onToggle:z??(()=>{})})
// The chip in the composer footer and the divider that only exists to sit
// next to it. Both hang off the same currentSelection, so both go.
const WEBVIEW_SELECTION_CHIP = new RegExp(
  `(${ID})&&(${ID})\\("div",\\{className:(${ID})\\.divider\\}\\),` +
    `\\1&&\\2\\((${ID}),\\{includeSelection:(${ID})\\?\\?!1,currentSelection:\\1,` +
    `onToggle:(${ID})\\?\\?\\(\\(\\)=>\\{\\}\\)\\}\\)`
)

// D(i05,{window:ex($.promptCacheRecord.value,Date.now())})
// What the footer's cache countdown renders. The two hook calls in front of it
// are left where they are, so the component still runs, it just draws nothing.
const WEBVIEW_CACHE_INDICATOR = new RegExp(
  `(${ID})\\((${ID}),\\{window:(${ID})\\((${ID})\\.promptCacheRecord\\.value,Date\\.now\\(\\)\\)\\}\\)`
)

// the folder-name whitelist the webview uses to decide an href is a file path
const WEBVIEW_WHITELIST =
  '/^(src|lib|test|tests|dist|build|node_modules|components|utils|services|api|' +
  'assets|public|private|config|scripts|docs)$/i'

function countMatches(src, re) {
  return [...src.matchAll(new RegExp(re.source, 'g'))].length
}

function patchHost(src) {
  const head = src.match(HOST_HEAD)
  const tail = src.match(HOST_TAIL)

  if (!head) throw new Error('openFile() head no longer matches, extension changed too much')
  if (!tail) throw new Error('showTextDocument() call no longer matches, extension changed too much')
  if (countMatches(src, HOST_HEAD) !== 1) throw new Error('openFile() head matched more than once')
  if (countMatches(src, HOST_TAIL) !== 1) throw new Error('showTextDocument() call matched more than once')

  const [, argPath, , pathMod, fsMod] = head
  const [, vscodeMod, uriVar, thenArg] = tail

  // Fix 2 and 3: decode the percent-encoded href and expand a leading ~/.
  // The decode only wins when the raw path does not exist, so a filename that
  // really contains a % still resolves.
  const prelude =
    `${MARKER}try{let _d=decodeURIComponent(${argPath});` +
    `if(_d!==${argPath}){` +
    `let _o=${pathMod}.isAbsolute(${argPath})?${argPath}:${pathMod}.join(this.cwd,${argPath});` +
    `if(!${fsMod}.existsSync(_o))${argPath}=_d}}catch{}` +
    `if(${argPath}.startsWith("~/")){` +
    `let _h=process.env.HOME||process.env.USERPROFILE;` +
    `if(_h)${argPath}=${pathMod}.join(_h,${argPath}.slice(2))}`

  let out = src.replace(HOST_HEAD, (m) => m + prelude)

  // Fix 1: hand media to vscode.open so the built-in preview takes the tab.
  // Plus: when showTextDocument rejects, send the file to vscode.open, which
  // lands on the same Open Anyway tab a click in the explorer gives. Reveal it
  // in the explorer only when even that fails.
  out = out.replace(
    HOST_TAIL,
    () =>
      `}catch{}if(/\\.(${MEDIA})$/i.test(${uriVar}.fsPath))` +
      `{${vscodeMod}.commands.executeCommand("vscode.open",${uriVar});return}` +
      `${vscodeMod}.window.showTextDocument(${uriVar})` +
      `.catch(()=>{Promise.resolve(${vscodeMod}.commands.executeCommand("vscode.open",${uriVar}))` +
      `.then(void 0,()=>${vscodeMod}.commands.executeCommand("revealInExplorer",${uriVar}));return null})` +
      `.then((${thenArg})=>{if(!${thenArg})return;`
  )

  if (out === src) throw new Error('host replacement produced no change')
  return out
}

// Fix 5: the one host the popup calls. No marker goes in here, the CSP is a
// header value and a comment inside it would void the whole policy.
function patchCsp(src) {
  const count = countMatches(src, PANEL_CSP)
  if (count === 0) throw new Error('panel CSP no longer matches, extension changed too much')
  if (count !== 1) throw new Error(`panel CSP matched ${count} times, expected exactly 1`)

  return src.replace(PANEL_CSP, () => `default-src 'none'; connect-src ${TRANSLATE_ORIGIN}; `)
}

function patchWebview(src) {
  const count = src.split(WEBVIEW_WHITELIST).length - 1
  if (count === 0) throw new Error('href folder whitelist no longer matches, webview changed too much')
  if (count !== 1) throw new Error(`href folder whitelist matched ${count} times, expected exactly 1`)

  // Fix 4: any href holding a path separator counts as a file path, so an
  // extensionless file opens too. /^/ matches every last segment, including "".
  return src.replace(WEBVIEW_WHITELIST, `${MARKER} /^/`)
}

// Fix 7: the editor's selection never rides along, and the chip that offered
// it is gone. Nothing here touches how an @ mention attaches a file.
function patchWebviewSelection(src) {
  const sends = countMatches(src, WEBVIEW_SEND_SELECTION)
  if (sends !== 1) throw new Error(`selection read matched ${sends} times, expected exactly 1`)

  const chips = countMatches(src, WEBVIEW_SELECTION_CHIP)
  if (chips !== 1) throw new Error(`selection chip matched ${chips} times, expected exactly 1`)

  return src
    .replace(
      WEBVIEW_SEND_SELECTION,
      (_m, _flag, sameAs) =>
        `if(${MARKER}!1&&!${sameAs}(this.lastSentSelection,this.selection.value))`
    )
    .replace(WEBVIEW_SELECTION_CHIP, () => `${MARKER}null,null`)
}

// Fix 8: the prompt cache countdown leaves the footer.
// The widget arrived in 2.1.268. A bundle with no promptCacheRecord anywhere in
// it is older than the feature and has nothing to hide, which is why this one
// is allowed to find nothing. A bundle that has one and still does not match
// has been rewritten, and that fails like everything else here.
function patchWebviewCacheIndicator(src) {
  if (!src.includes('promptCacheRecord')) return src

  const count = countMatches(src, WEBVIEW_CACHE_INDICATOR)
  if (count !== 1) throw new Error(`prompt cache indicator matched ${count} times, expected exactly 1`)

  return src.replace(WEBVIEW_CACHE_INDICATOR, () => `${MARKER}null`)
}

// Fix 6. The bundle is loaded as type="module", so this lands in module scope
// under strict mode, and the leading ; keeps it clear of whatever the bundle
// ends on.
function appendTranslator(src) {
  return `${src}\n;${MARKER}(${translatePopup.toString()})()\n`
}

// Runs inside the panel webview, never in node, so it may only use what a
// browser gives it, and its fetch only reaches out because patchCsp() opened
// connect-src to that one host.
function translatePopup() {
  if (window.__ccTranslateReady) return
  window.__ccTranslateReady = true

  // https://translate.googleapis.com/translate_a/single?client=gtx — no API key.
  // Payload: [[[translated, original, ...], ...], null, detectedSourceLanguage, ...]
  const ENDPOINT = 'https://translate.googleapis.com/translate_a/single'
  const MAX_CHARS = 1800
  const POPUP_ID = 'cc-translate-popup'
  const PRIMARY_TARGET = 'th'
  const FALLBACK_TARGET = 'en'

  const cache = new Map()
  let activeRange = null
  let pendingRequest = null
  let visible = false

  function getPopup() {
    const existing = document.getElementById(POPUP_ID)
    if (existing) return existing

    const popup = document.createElement('div')
    popup.id = POPUP_ID
    Object.assign(popup.style, {
      position: 'fixed',
      zIndex: '99999',
      maxWidth: 'min(420px, calc(100vw - 24px))',
      maxHeight: '40vh',
      overflowY: 'auto',
      padding: '8px 10px',
      borderRadius: '6px',
      fontSize: '13px',
      lineHeight: '1.6',
      whiteSpace: 'pre-wrap',
      color: 'var(--vscode-editorHoverWidget-foreground, #cccccc)',
      background: 'var(--vscode-editorHoverWidget-background, #252526)',
      border: '1px solid var(--vscode-editorHoverWidget-border, #454545)',
      boxShadow: '0 2px 8px rgba(0, 0, 0, 0.35)',
    })
    document.body.appendChild(popup)
    return popup
  }

  function hidePopup() {
    const popup = document.getElementById(POPUP_ID)
    if (popup) popup.style.display = 'none'
    visible = false
    activeRange = null
  }

  function positionPopup(popup) {
    if (!activeRange) return

    const rect = activeRange.getBoundingClientRect()
    const gone = (rect.width === 0 && rect.height === 0) || rect.bottom < 0 || rect.top > window.innerHeight
    if (gone) {
      popup.style.display = 'none'
      return
    }

    popup.style.display = 'block'
    const below = rect.bottom + 8
    const flip = below + popup.offsetHeight > window.innerHeight - 8
    popup.style.top = (flip ? Math.max(8, rect.top - popup.offsetHeight - 8) : below) + 'px'
    popup.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - popup.offsetWidth - 12)) + 'px'
  }

  function showPopup(message) {
    const popup = getPopup()
    popup.textContent = message
    visible = true
    popup.style.display = 'block'
    positionPopup(popup)
  }

  function repositionPopup() {
    if (!visible) return
    const popup = document.getElementById(POPUP_ID)
    if (popup) positionPopup(popup)
  }

  async function requestTranslation(text, target, signal) {
    const query = encodeURIComponent(text.slice(0, MAX_CHARS))
    const response = await fetch(ENDPOINT + '?client=gtx&sl=auto&dt=t&tl=' + target + '&q=' + query, { signal })
    if (!response.ok) throw new Error('translate returned ' + response.status)

    const payload = await response.json()
    return {
      text: payload[0].map((segment) => segment[0]).join(''),
      sourceLanguage: payload[2],
    }
  }

  async function translate(text, signal) {
    const primary = await requestTranslation(text, PRIMARY_TARGET, signal)
    if (primary.sourceLanguage !== PRIMARY_TARGET) return primary.text

    const fallback = await requestTranslation(text, FALLBACK_TARGET, signal)
    return fallback.text
  }

  // The composer is left alone, a selection there is on its way to being
  // retyped or deleted, not read.
  function isEditable(node) {
    const element = node && node.nodeType === 1 ? node : node && node.parentElement
    return !!(element && element.closest('input, textarea, [contenteditable=""], [contenteditable="true"]'))
  }

  function selectedText() {
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0) return null

    const text = selection.toString().trim()
    if (!text) return null

    const range = selection.getRangeAt(0)
    if (isEditable(range.commonAncestorContainer)) return null

    activeRange = range
    return text
  }

  async function onMouseUp(event) {
    if (event.target.closest && event.target.closest('#' + POPUP_ID)) return

    const text = selectedText()
    if (!text) {
      hidePopup()
      return
    }
    if (cache.has(text)) {
      showPopup(cache.get(text))
      return
    }

    if (pendingRequest) pendingRequest.abort()
    pendingRequest = new AbortController()
    showPopup('กำลังแปล...')

    try {
      const translated = await translate(text, pendingRequest.signal)
      cache.set(text, translated)
      showPopup(translated)
    } catch (error) {
      if (error.name !== 'AbortError') showPopup('แปลไม่สำเร็จ: ' + error.message)
    }
  }

  // Capture everywhere, the panel stops plenty of events on their way up.
  document.addEventListener('mouseup', onMouseUp, true)
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.key === 'Escape') hidePopup()
    },
    true
  )
  window.addEventListener('scroll', repositionPopup, true)
  window.addEventListener('resize', repositionPopup)
}

function syntaxCheck(source) {
  const tmp = path.join(os.tmpdir(), `ccpatch-check-${process.pid}.js`)
  fs.writeFileSync(tmp, source)
  try {
    execFileSync(process.execPath, ['--check', tmp], { stdio: 'pipe' })
  } finally {
    fs.unlinkSync(tmp)
  }
}

function findExtensionDirs() {
  const out = []
  for (const root of EXT_ROOTS) {
    if (!fs.existsSync(root)) continue
    for (const name of fs.readdirSync(root)) {
      if (name.startsWith('anthropic.claude-code-')) out.push(path.join(root, name))
    }
  }
  return out
}

function main() {
  const dryRun = process.argv.includes('--dry-run')
  const restore = process.argv.includes('--restore')
  const dirs = findExtensionDirs()

  if (dirs.length === 0) {
    console.error('no Claude Code extension found under ' + EXT_ROOTS.join(', '))
    process.exit(1)
  }

  let failed = 0
  for (const dir of dirs) {
    const label = path.basename(dir)
    const jobs = [
      { file: path.join(dir, 'extension.js'), apply: (src) => patchCsp(patchHost(src)), what: 'host' },
      {
        file: path.join(dir, 'webview', 'index.js'),
        apply: (src) =>
          appendTranslator(patchWebviewCacheIndicator(patchWebviewSelection(patchWebview(src)))),
        what: 'webview',
      },
    ]

    for (const job of jobs) {
      const tag = `${label} ${job.what}`
      if (!fs.existsSync(job.file)) {
        console.error(`${tag}  FAILED: ${job.file} is missing`)
        failed++
        continue
      }

      const backup = job.file + BACKUP_SUFFIX

      if (restore) {
        if (!fs.existsSync(backup)) {
          console.log(`${tag}  no backup, nothing to restore`)
          continue
        }
        fs.copyFileSync(backup, job.file)
        console.log(`${tag}  restored`)
        continue
      }

      let src = fs.readFileSync(job.file, 'utf8')
      if (src.includes(MARKER)) {
        console.log(`${tag}  already patched`)
        continue
      }
      if (MARKER_ANY.test(src)) {
        if (!fs.existsSync(backup)) {
          console.error(`${tag}  FAILED: carries an older patch with no backup to rebuild from`)
          failed++
          continue
        }
        src = fs.readFileSync(backup, 'utf8')
      }

      try {
        const out = job.apply(src)
        syntaxCheck(out)
        if (dryRun) {
          console.log(`${tag}  would patch`)
          continue
        }
        fs.writeFileSync(backup, src)
        fs.writeFileSync(job.file, out)
        console.log(`${tag}  patched`)
      } catch (err) {
        failed++
        console.error(`${tag}  FAILED: ${err.message}`)
      }
    }
  }

  if (failed > 0) process.exit(1)
  if (!dryRun && !restore) console.log('\nreload the VSCode window for this to take effect')
}

main()
