import{a8 as l,av as i}from"./index-BcB-ZebW.js";const d=`accept acceptcharset accesskey action allowfullscreen allowtransparency
alt async autocomplete autofocus autoplay capture cellpadding cellspacing challenge
charset checked classid classname colspan cols content contenteditable contextmenu
controls coords crossorigin data datetime default defer dir disabled download draggable
enctype form formaction formenctype formmethod formnovalidate formtarget frameborder
headers height hidden high href hreflang htmlfor for httpequiv icon id inputmode integrity
is keyparams keytype kind label lang list loop low manifest marginheight marginwidth max maxlength media
mediagroup method min minlength multiple muted name novalidate nonce open
optimum pattern placeholder poster preload radiogroup readonly rel required
reversed role rowspan rows sandbox scope scoped scrolling seamless selected
shape size sizes span spellcheck src srcdoc srclang srcset start step style
summary tabindex target title type usemap value width wmode wrap`,u=`onCopy onCut onPaste onCompositionend onCompositionstart onCompositionupdate onKeydown
    onKeypress onKeyup onFocus onBlur onChange onInput onSubmit onClick onContextmenu onDoubleclick onDblclick
    onDrag onDragend onDragenter onDragexit onDragleave onDragover onDragstart onDrop onMousedown
    onMouseenter onMouseleave onMousemove onMouseout onMouseover onMouseup onSelect onTouchcancel
    onTouchend onTouchmove onTouchstart onTouchstartPassive onTouchmovePassive onScroll onWheel onAbort onCanplay onCanplaythrough
    onDurationchange onEmptied onEncrypted onEnded onError onLoadeddata onLoadedmetadata
    onLoadstart onPause onPlay onPlaying onProgress onRatechange onSeeked onSeeking onStalled onSuspend onTimeupdate onVolumechange onWaiting onLoad onError`,r=`${d} ${u}`.split(/[\s\n]+/),m="aria-",p="data-";function s(e,o){return e.indexOf(o)===0}function f(e){let o=arguments.length>1&&arguments[1]!==void 0?arguments[1]:!1,n;o===!1?n={aria:!0,data:!0,attr:!0}:o===!0?n={aria:!0}:n=l({},o);const a={};return Object.keys(e).forEach(t=>{(n.aria&&(t==="role"||s(t,m))||n.data&&s(t,p)||n.attr&&(r.includes(t)||r.includes(t.toLowerCase())))&&(a[t]=e[t])}),a}const y=()=>i()&&window.document.documentElement,c=e=>{if(i()&&window.document.documentElement){const o=Array.isArray(e)?e:[e],{documentElement:n}=window.document;return o.some(a=>a in n.style)}return!1},g=(e,o)=>{if(!c(e))return!1;const n=document.createElement("div"),a=n.style[e];return n.style[e]=o,n.style[e]!==a};function w(e,o){return!Array.isArray(e)&&o!==void 0?g(e,o):c(e)}export{y as c,w as i,f as p};
