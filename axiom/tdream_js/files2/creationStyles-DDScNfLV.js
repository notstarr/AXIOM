import{a as r}from"./vendor-react-Ra4BYCkX.js";const i={background:"linear-gradient(135deg,#5B70F1 0%,#FC98FF 100%)"},n={boxShadow:"0 12px 32px -12px rgba(91,112,241,.45)"},s={backgroundColor:"transparent"},t=`
/* Override the app-wide dark page background while the workbench is
   mounted. Without this, ANY horizontal overflow (or, in some browsers,
   the area to the right of the layout when content doesn't fully reach
   100vw) would leak the global #0F172A page background through, showing
   up as a hard black band on the right edge.

   IMPORTANT: index.css sets background-color: var(--bg-page) on BOTH
   :root (i.e. <html>) and <body>. Overriding only <body> is not
   enough — once a horizontal overflow gets clipped by an ancestor's
   overflow-x: hidden, the area beyond the body's painted box is the
   <html> element's background, which would still be #0F172A. So we
   override html as well.

   We use :has() with a graceful fallback: the [data-dw-creation]
   attribute is set/cleared on both <html> and <body> by the
   CreationStyles effect for browsers that don't support :has() yet. */
html:has(.dw-creation),
body:has(.dw-creation),
html[data-dw-creation='1'],
body[data-dw-creation='1'] {
  background-color: #F6F6F6 !important;
  /* Flat, glow-free page: the previous grape/peach radial
     gradients made the workbench look subtly busier than the rest
     of the platform (especially noticeable on the image-gen tab
     where the lighter central preview let the surrounding glow
     read through). Per the latest design call we want every
     workbench tab — video / image / audio — to share the exact
     same flat #F6F6F6 canvas as the home page and short-video
     workshop landing. */
  background-image: none !important;
}

/* ------------------------------------------------------------------
   Native page scrollbar (the vertical bar on the right of the window).
   Chrome/Linux & some other browsers paint the scrollbar gutter using
   a system-default dark color, which appears as a thin black band on
   the right edge of the workbench (and turns light-gray on hover —
   that is the OS-level scrollbar thumb hover). We therefore re-paint
   the entire scrollbar in the workbench's purple-tinted palette so it
   blends into the page background.

   We only target the workbench (data attribute / :has) so other pages
   keep their own scrollbar styling.
   ------------------------------------------------------------------ */

/* ----- Plan B: macOS-style overlay scrollbar -----
   At rest the thumb is fully transparent, so the gutter visually
   "disappears" into the page background. When the user hovers anywhere
   on <html> the thumb fades in as a slim purple pill. The track itself
   is always the page color (#F7F5FB) so it never shows the OS-default
   dark gutter — which was the original "black band" symptom. */

/* Firefox: only 'auto | thin | none' is supported for width, and
   'scrollbar-color' cannot be transitioned. We pick 'thin' and keep
   the thumb in a soft purple — it is already low-contrast enough. */
html:has(.dw-creation),
html[data-dw-creation='1'] {
  scrollbar-color: rgba(139, 92, 246, 0.25) transparent;
  scrollbar-width: thin;
}

/* WebKit (Chrome / Edge / Safari) — overlay-style behaviour. */
html[data-dw-creation='1']::-webkit-scrollbar,
html:has(.dw-creation)::-webkit-scrollbar {
  width: 10px;
  height: 10px;
  background-color: #F6F6F6; /* same as page bg → gutter is invisible */
}
html[data-dw-creation='1']::-webkit-scrollbar-track,
html:has(.dw-creation)::-webkit-scrollbar-track {
  background-color: #F6F6F6;
}
/* Thumb is invisible at rest. */
html[data-dw-creation='1']::-webkit-scrollbar-thumb,
html:has(.dw-creation)::-webkit-scrollbar-thumb {
  background-color: transparent;
  border-radius: 8px;
  border: 2px solid transparent;
  background-clip: padding-box;
  transition: background-color 0.2s ease;
}
/* Thumb fades in once the user moves the mouse anywhere over the page. */
html[data-dw-creation='1']:hover::-webkit-scrollbar-thumb,
html:has(.dw-creation):hover::-webkit-scrollbar-thumb {
  background-color: rgba(139, 92, 246, 0.35);
}
/* Stronger purple while actively hovering / dragging the thumb. */
html[data-dw-creation='1']::-webkit-scrollbar-thumb:hover,
html:has(.dw-creation)::-webkit-scrollbar-thumb:hover {
  background-color: rgba(139, 92, 246, 0.55);
}
html[data-dw-creation='1']::-webkit-scrollbar-thumb:active,
html:has(.dw-creation)::-webkit-scrollbar-thumb:active {
  background-color: rgba(139, 92, 246, 0.7);
}
html[data-dw-creation='1']::-webkit-scrollbar-corner,
html:has(.dw-creation)::-webkit-scrollbar-corner {
  background-color: #F6F6F6;
}

/* Slim purple scrollbar inside .dw-creation containers */
.dw-creation .nice-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
.dw-creation .nice-scroll::-webkit-scrollbar-thumb {
  background: rgba(167,139,250,.35); border-radius: 6px;
}
.dw-creation .nice-scroll::-webkit-scrollbar-track { background: transparent; }

/* Custom range slider with purple-fill on the left of the thumb. */
.dw-creation input[type='range'].grape-range {
  -webkit-appearance: none;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    #8b5cf6 0%,
    #8b5cf6 var(--val, 50%),
    rgba(15,10,26,.08) var(--val, 50%),
    rgba(15,10,26,.08) 100%
  );
  outline: none;
}
.dw-creation input[type='range'].grape-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  border: 2px solid #8b5cf6;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(76,29,149,.25);
}
.dw-creation input[type='range'].grape-range::-moz-range-thumb {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  border: 2px solid #8b5cf6;
  cursor: pointer;
}

/* Pulsing emerald dot used in "生成完成" status pills. */
@keyframes dwCreationPulseDot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(34,197,94,.45); }
  50%      { box-shadow: 0 0 0 6px rgba(34,197,94,0); }
}
.dw-creation .dot-pulse { animation: dwCreationPulseDot 1.8s ease-out infinite; }

/* ==================================================================
   ROUTE-A "skin re-paint" overrides for embedded legacy pages.

   The three legacy generation pages (VideoGeneration / ImageGeneration
   / VoiceStudio) were built dark-themed (#0a0a0f base, #1a1a24 panels,
   white text, cyan accents). When we embed them inside the unified
   Creation Workbench shell — which adopts the planner's "grape"
   visual language (light #F7F5FB background, frosted-white panels,
   purple-pink accents) — the dark colours create a visible visual
   seam.

   To avoid touching ~600 KB of legacy code, we re-paint those pages
   from the outside via high-specificity attribute selectors scoped
   under ".dw-creation". All overrides target Tailwind arbitrary-value
   class names like "bg-[#1a1a24]" exactly as they appear in the legacy
   markup. This is purely visual; no logic, no DOM structure, no class
   names in the legacy pages are modified.

   Maintenance notes:
   - If a legacy page introduces a NEW dark colour we do not list here,
     it will fall through and look dark. Add a rule below.
   - When you eventually migrate a panel to a proper "grape" component
     (Route B), the legacy class names disappear and these overrides
     become inert — they are safe to keep around.
   - Specificity: every selector starts with ".dw-creation" (one class)
     plus an attribute selector (one attribute = same as a class), so
     we win against single-class Tailwind utilities. Where utilities
     use the same specificity (e.g. "text-white"), source order wins
     and we are injected after Tailwind compiled CSS via a runtime
     <style> tag, so we still take precedence.
   ================================================================== */

/* ---- 1. Page / column backgrounds → make them transparent so the
        shell's #F7F5FB ambient backdrop shows through. ---- */
.dw-creation [class*="bg-[#0a0a0f]"],
.dw-creation [class*="bg-[#0d0d12]"],
.dw-creation [class*="bg-[#0d0d14]"],
.dw-creation [class*="bg-[#0d0d15]"] {
  background-color: transparent !important;
  /* Some legacy elements use gradient utilities that include #0a0a0f
     stops — neutralise those too. */
  background-image: none !important;
}

/* ---- 2. Frosted card surfaces.
        These were the dark panel fills (#1a1a24 / #1e1e2a / #0d0d15).
        Re-paint as the shared "frosted white" card surface. ---- */
.dw-creation [class*="bg-[#1a1a24]"],
.dw-creation [class*="bg-[#1e1e2a]"] {
  background-color: rgba(255, 255, 255, 0.85) !important;
  backdrop-filter: blur(14px) !important;
  -webkit-backdrop-filter: blur(14px) !important;
}

/* ---- 3. Secondary buttons / chip backgrounds.
        Originally darker steps (#2a2a34 / #3a3a44 hover) — turn into
        a light glass step that sits above the frosted card. ---- */
.dw-creation [class*="bg-[#2a2a34]"] {
  background-color: rgba(15, 10, 26, 0.04) !important;
  color: #1F1A2E !important;
}
.dw-creation [class*="bg-[#3a3a44]"],
.dw-creation [class*="hover:bg-[#3a3a44]"]:hover {
  background-color: rgba(15, 10, 26, 0.08) !important;
}

/* ---- 4. Borders.
        Convert white/10 & white/20 (designed to sit on a dark canvas)
        into faint grape-tinted borders that sit on a light canvas. ---- */
.dw-creation [class*="border-white/10"] {
  border-color: rgba(139, 92, 246, 0.12) !important;
}
.dw-creation [class*="border-white/20"] {
  border-color: rgba(139, 92, 246, 0.22) !important;
}
.dw-creation [class*="hover:border-white/20"]:hover {
  border-color: rgba(139, 92, 246, 0.35) !important;
}
/* Also covers "divide-white/10" if the legacy code uses it. */
.dw-creation [class*="divide-white/10"] > * + * {
  border-color: rgba(139, 92, 246, 0.12) !important;
}

/* ---- 5. Text colours.
        text-white was the default body colour on the dark theme.
        Map to ink-900. text-gray-300/400 stay readable on white but
        we nudge them to gray-500/600 so contrast feels right. ---- */
.dw-creation .text-white {
  color: #0E0A1A !important;
}
.dw-creation .text-gray-300 {
  color: #4B5563 !important;
}
.dw-creation .text-gray-400 {
  color: #6B7280 !important;
}
.dw-creation .text-gray-500 {
  color: #9CA3AF !important;
}
/* hover:text-white on a light canvas would invert to invisible; map it
   to a darker ink so hover stays readable. */
.dw-creation [class*="hover:text-white"]:hover {
  color: #0E0A1A !important;
}

/* ---- 5b. White-on-dark exception list.
        The blanket .text-white -> #0E0A1A remap above is correct for
        plain text that "leaked" from the legacy dark theme onto the
        new light canvas. But it ALSO catches icons that are
        intentionally drawn on a dark scrim — most notably the hover
        toolbar overlaid on each video / image generation card
        (RegenerateIcon / DownloadIcon / TrashIcon etc. sitting on a
        bg-black/60 wash that fades in on group-hover). When those
        icons turned ink-black they became nearly invisible against
        the dark scrim (red-box in the user's screenshot of the video
        history grid).

        Solution: opt those icons back into white when ANY ancestor
        carries a bg-black/<opacity> utility (the marker for "this is
        an intentionally dark scrim, not a stray dark surface"). The
        selector uses [class*="bg-black/"] because Tailwind compiles
        every variant — bg-black/40, bg-black/60, bg-black/[0.6] — to
        a class containing that literal substring.

        Specificity: this selector ([class*=...] .text-white) is
        equal-specificity to .dw-creation .text-white but higher in
        source order, and both rules are !important — so the
        cascade tie-breaker (later wins) restores white as desired. */
.dw-creation [class*="bg-black/"] .text-white,
.dw-creation [class*="bg-black/"] [class*="text-white"] {
  color: #ffffff !important;
}
.dw-creation [class*="bg-black/"] [class*="hover:text-white"]:hover {
  color: #ffffff !important;
}
/* Same-element variant. The descendant selectors above only fire
   when bg-black/<n> sits on an *ancestor*. But Tailwind buttons
   often combine both classes on a single element, e.g. the small
   "regenerate" pill in the top-right of every video history card:
   class="p-1.5 bg-black/60 backdrop-blur-sm rounded-lg text-white".
   Those slipped through and rendered with ink-black icons on a
   faint-grey scrim. Match them via attribute-AND on the same
   element so the icon stays white. */
.dw-creation [class*="bg-black/"][class*="text-white"] {
  color: #ffffff !important;
}
.dw-creation [class*="bg-black/"][class*="hover:text-white"]:hover {
  color: #ffffff !important;
}
/* placeholder colour — keep mid-gray on white. */
.dw-creation .placeholder-gray-500::placeholder,
.dw-creation [class*="placeholder-gray-500"]::placeholder {
  color: #9CA3AF !important;
}

/* ---- 6. Backgrounds keyed off "bg-white/<n>" (semi-transparent
        white-on-dark "lift" surfaces) — re-tint as faint grape so they
        remain visible on the now-white canvas. ---- */
.dw-creation [class*="bg-white/[0.04]"],
.dw-creation [class*="bg-white/5"] {
  background-color: rgba(139, 92, 246, 0.04) !important;
}
.dw-creation [class*="bg-white/10"] {
  background-color: rgba(139, 92, 246, 0.08) !important;
}
.dw-creation [class*="bg-white/20"] {
  background-color: rgba(139, 92, 246, 0.12) !important;
}
.dw-creation [class*="hover:bg-white/5"]:hover {
  background-color: rgba(139, 92, 246, 0.06) !important;
}
.dw-creation [class*="hover:bg-white/10"]:hover {
  background-color: rgba(139, 92, 246, 0.1) !important;
}
.dw-creation [class*="hover:bg-white/30"]:hover {
  background-color: rgba(139, 92, 246, 0.18) !important;
}

/* ---- 7. Cyan accent → grape accent.
        The legacy pages used cyan-500 as the primary focus / selected
        colour. Re-map every cyan-* utility we've seen so focus rings,
        active tab underlines, selected radio borders, etc. all turn
        purple. ---- */
.dw-creation [class*="border-cyan-500"] {
  border-color: #8B5CF6 !important;
}
.dw-creation [class*="ring-cyan-500"] {
  --tw-ring-color: #8B5CF6 !important;
}
.dw-creation [class*="bg-cyan-500"] {
  background-color: #8B5CF6 !important;
}
.dw-creation [class*="hover:bg-cyan-600"]:hover {
  background-color: #7C3AED !important;
}
.dw-creation .text-cyan-300,
.dw-creation .text-cyan-400,
.dw-creation [class*="text-cyan-3"],
.dw-creation [class*="text-cyan-4"] {
  color: #7C3AED !important;
}
.dw-creation [class*="hover:text-cyan-400"]:hover {
  color: #7C3AED !important;
}
.dw-creation [class*="bg-cyan-500/5"] {
  background-color: rgba(139, 92, 246, 0.05) !important;
}
.dw-creation [class*="bg-cyan-500/10"] {
  background-color: rgba(139, 92, 246, 0.1) !important;
}
.dw-creation [class*="bg-cyan-500/20"] {
  background-color: rgba(139, 92, 246, 0.2) !important;
}
.dw-creation [class*="bg-cyan-500/30"] {
  background-color: rgba(139, 92, 246, 0.3) !important;
}

/* ---- 8. Primary CTA gradient (green→emerald) → brand gradient. ---- */
.dw-creation [class*="from-green-500"][class*="to-emerald-500"] {
  background-image: linear-gradient(
    135deg,
    #5B70F1 0%,
    #FC98FF 100%
  ) !important;
}

/* ---- 9. Modal overlays.
        bg-black/<n> overlays were calibrated for a dark theme. On the
        light canvas they look unnecessarily heavy; tone them down a bit
        but keep them dark enough to enforce focus. ---- */
.dw-creation [class*="bg-black/80"] { background-color: rgba(15, 10, 26, 0.55) !important; }
.dw-creation [class*="bg-black/70"] { background-color: rgba(15, 10, 26, 0.45) !important; }
.dw-creation [class*="bg-black/60"] { background-color: rgba(15, 10, 26, 0.4) !important; }
.dw-creation [class*="bg-black/50"] { background-color: rgba(15, 10, 26, 0.35) !important; }
.dw-creation [class*="bg-black/40"] { background-color: rgba(15, 10, 26, 0.25) !important; }
.dw-creation [class*="bg-black/30"] { background-color: rgba(15, 10, 26, 0.18) !important; }
/* Hover variants for icon buttons etc. */
.dw-creation [class*="hover:bg-black/80"]:hover { background-color: rgba(15, 10, 26, 0.7) !important; }
.dw-creation [class*="hover:bg-black/60"]:hover { background-color: rgba(15, 10, 26, 0.5) !important; }

/* ---- 10. Card-like containers should pick up the unified rounded /
        shadow / border treatment.
        We can't safely paint EVERY rounded-xl on the page because the
        legacy code uses rounded-xl for inputs, chips, thumbnails etc.
        Instead we lift only the elements that already declared one of
        the dark surface fills (matched in #2 above) — they ARE the
        "panels", and they now deserve the frosted-card finish. ---- */
.dw-creation [class*="bg-[#1a1a24]"],
.dw-creation [class*="bg-[#1e1e2a]"] {
  border: 1px solid rgba(139, 92, 246, 0.1) !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.04),
    0 8px 24px -12px rgba(76, 29, 149, 0.08) !important;
}

/* ---- 11. Make sure native form controls inside re-painted panels
        don't keep their default dark-on-dark colours. ---- */
.dw-creation [class*="bg-[#1a1a24]"] input,
.dw-creation [class*="bg-[#1a1a24]"] textarea,
.dw-creation [class*="bg-[#1a1a24]"] select {
  color: #1F1A2E;
}

/* ---- 12. Active-state remap for selected pills / tabs that used
        bg-purple-600 as the on-dark accent. Keep the colour but
        sharpen the contrast against white. ---- */
.dw-creation .bg-purple-600,
.dw-creation [class*="hover:bg-purple-500"]:hover {
  background-color: #8B5CF6 !important;
}
.dw-creation [class*="shadow-purple-500/20"] {
  box-shadow: 0 8px 20px -8px rgba(139, 92, 246, 0.45) !important;
}

/* ---- 13. Video preview area — outer dark gradient frame.

   Original markup (VideoGeneration/components/VideoPreviewArea.tsx:69):
     <div class="flex-1 ... bg-gradient-to-br from-[#0a0a0f]
                 via-[#0d0d14] to-[#0a0a0f]">

   PREVIOUS approach: paint the outer frame as a frosted white card
   (rgba(255,255,255,0.85)) so the inner <video bg-black> wouldn't
   create a "dark seam" against the light page. In practice that
   white card was opaque enough — and not backdrop-blurred — that
   it COMPLETELY hid the page's grape/peach radial glow above and
   below the video, producing the obvious "white slabs" the user
   pointed out (see screenshot, top + bottom red boxes).

   NEW approach: keep the outer frame fully transparent so the
   demo-style page background (#F7F5FB + radial glow) shows through
   on all four sides of the video. The inner video element is
   wrapped in a "rounded-2xl overflow-hidden" div, so the black
   bg of the <video> is already clipped — there is no real "seam",
   just a clean rounded video sitting on the lit page. */
.dw-creation [class*="from-[#0a0a0f]"][class*="via-[#0d0d14]"][class*="to-[#0a0a0f]"] {
  background: transparent !important;
  background-image: none !important;
  border: none !important;
  box-shadow: none !important;
}

/* ---- 14. Primary "立即生成 / 消耗 N 积分生成" button.

   Two visual states need treatment:
     a) Enabled: from-green-500 → to-emerald-500 — already mapped to
        the grape gradient in rule #8, but we re-declare it here with
        higher specificity to defeat any future Tailwind reordering
        and to also lift the disabled-vs-enabled visual contrast.
     b) Disabled: bg-gray-700 + text-gray-400 — on the dark theme this
        was a dark grey; on the light canvas it now reads as a near-
        black slab, which is louder than the rest of the workbench.
        Re-tint to a soft grape-tinged disabled chip so the button
        still reads as "currently inactive" without dominating. ---- */
.dw-creation button[class*="from-green-500"][class*="to-emerald-500"] {
  background-image: linear-gradient(
    135deg,
    #5B70F1 0%,
    #FC98FF 100%
  ) !important;
  color: #ffffff !important;
  box-shadow: 0 12px 28px -12px rgba(91, 112, 241, 0.45) !important;
}
.dw-creation button[class*="bg-gray-700"][class*="cursor-not-allowed"],
.dw-creation [class*="bg-gray-700"][class*="cursor-not-allowed"] {
  /* Unified disabled CTA appearance across all three studio tabs
     (视频 / 图片 / 音频). Tinted with the brand primary so the
     button still reads as part of the same family, but the alpha
     is high enough to be clearly distinguishable from the active
     gradient and the label stays legible on white. */
  background-color: rgba(91, 112, 241, 0.12) !important;
  background-image: none !important;
  color: #A5A8B8 !important;
  border: 1px solid rgba(91, 112, 241, 0.20) !important;
  box-shadow: none !important;
}

/* ---- 14b. VoiceStudio (audio tab) primary CTA re-skin.

   Unlike VideoGeneration which uses 'from-green-500 to-emerald-500'
   (handled by rule #14) and ImageGeneration which uses
   '.ig-generate-btn' (handled by rule #19i), the three VoiceStudio
   sub-tabs (声音克隆 / BGM 生成 / 对白朗读) build their primary CTA
   with 'bg-gradient-to-r from-cyan-600 to-cyan-500', so neither of
   the previous selectors hits them — the buttons would render with
   the legacy cyan gradient even after the brand-color rollout.

   Map them to the new brand gradient. The selector is intentionally
   narrow (button + both from-/to- attribute selectors) so we don't
   accidentally re-color decorative cyan→cyan chips elsewhere on the
   page. ---- */
.dw-creation button[class*="from-cyan-600"][class*="to-cyan-500"] {
  background-image:
    linear-gradient(135deg, #5B70F1 0%, #FC98FF 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 12px 28px -12px rgba(91, 112, 241, 0.45) !important;
}
/* The "上传音频文件" CTA in the 对白朗读 tab uses a different gradient
   (from-blue-600 to-indigo-500). Treat it the same way so all CTAs
   in the audio tab speak the same brand language. */
.dw-creation button[class*="from-blue-600"][class*="to-indigo-500"] {
  background-image:
    linear-gradient(135deg, #5B70F1 0%, #FC98FF 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 12px 28px -12px rgba(91, 112, 241, 0.45) !important;
}

/* ---- 15. Left sidebar "card-ification" for the video tab.

   Goal: turn the left parameter column from one long flat strip into a
   stack of independent frosted cards, matching the planner's demo
   (creation-workbench.html). This is the KEY visual difference between
   the current state and the demo; pure colour overrides (rules #1–#14)
   already match the palette but the lack of card boundaries makes the
   left column feel like one big slab.

   Strategy:
     The legacy left sidebar has THREE structural parts:
       (a) Top model picker          → wrapped in <div class="p-4 border-b border-white/10">
       (b) Middle scrollable params  → wrapped in <div class="flex-1 overflow-y-auto p-4 space-y-5 custom-scrollbar">
       (c) Bottom generate button    → wrapped in <div class="p-4 border-t border-white/10">

     For (b), <ModelParamsSection> returns a Fragment whose top-level
     children become DIRECT children of the scroll container, so each
     parameter group (主体参考 / 提示词 / 分辨率 / 运镜 / 时长 / 错峰 /
     声音 / 尺寸 / 比例 / 音频) is already a distinct DOM block — we
     just need to paint each as a card.

     The selector ".space-y-5.custom-scrollbar" is unique to this
     container in the legacy markup (the right-side preview pane uses
     "p-6" instead of "space-y-5"), so it scopes safely.

   What this rule does NOT touch:
     - any nested children inside each card (chips, sliders, buttons)
     - the right-side preview / video container
     - history panel layout
   ---- */

/* ----- 15a. Lift the entire left sidebar so it sits on the page bg
     and lose the dark right-border that previously separated it from
     the central preview. ---- */
.dw-creation [class*="w-[420px]"][class*="border-r"] {
  border-right: 1px solid rgba(139, 92, 246, 0.08) !important;
  background-color: transparent !important;
  padding: 12px 12px 12px 12px !important;
  gap: 12px;
}

/* ----- 15b. Each of the three structural parts (top picker / middle
     scroll / bottom CTA) becomes a frosted card. We match by the
     direct-child combinator so we don't accidentally style nested
     blocks. ---- */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div {
  background-color: rgba(255, 255, 255, 0.85) !important;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid rgba(139, 92, 246, 0.10) !important;
  border-radius: 16px !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.04),
    0 8px 24px -12px rgba(76, 29, 149, 0.08) !important;
  margin-bottom: 12px;
}
/* Drop the legacy "border-b / border-t" hairlines because each div
   is now an independent card — the hairline would just be a stray
   line floating inside the white surface. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-b"],
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] {
  border-top-color: transparent !important;
  border-bottom-color: transparent !important;
  border-top-width: 1px !important;
  border-bottom-width: 1px !important;
  /* Keep the four-side border from rule 15b above — these specifically
     null out only the hairlines that pointed in/out of neighbouring
     sections in the original dark layout. */
}
/* The middle scroll container needs slightly different padding so its
   own scrollable list doesn't double-pad. Reset to a tighter inset. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="overflow-y-auto"] {
  padding: 12px !important;
}

/* ----- 15c. Inside the middle scrollable area, EACH top-level
     parameter group rendered by <ModelParamsSection> (and the inline
     "video type" tabs above it) becomes its own nested card. This is
     what produces the "stack of cards" look from the demo.

     Structure recap:
       div.space-y-5.custom-scrollbar       ← the scroll container
         > div                              ← group A (e.g. video type tabs)
         > div                              ← group B (e.g. 主体参考)
         > div                              ← group C (提示词)
         > ...

     We deliberately DO NOT use ".space-y-5 > *" because that also
     matches non-div elements (e.g. fragments rendering null) which
     would not visually need a card frame. Restricting to "> div" is
     safe because all parameter groups are wrapped in <div>. ---- */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div {
  background-color: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(139, 92, 246, 0.10);
  border-radius: 14px;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.03),
    0 4px 16px -8px rgba(76, 29, 149, 0.06);
  padding: 14px 14px;
}
/* The middle area itself is now a card surrounding many cards — to
   keep the nesting visually clean, drop the outer surface's solid
   white so only the inner cards read as panels. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="overflow-y-auto"] {
  background-color: transparent !important;
  border: none !important;
  box-shadow: none !important;
  margin-bottom: 0;
}

/* ----- 15d. Tighten section labels inside each parameter card so the
     <label> blocks don't visually compete with the card border. The
     labels in the legacy markup use "text-sm font-medium" — give them
     a subtle ink-700 colour and a small bottom margin. ---- */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div > label,
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div > div > label {
  color: #1F1A2E;
  letter-spacing: 0.01em;
}

/* ----- 15e. Bottom generate-button card: keep the inner row as a
     plain flex (no extra padding). The card padding from rule #15b
     already provides enough breathing room. ---- */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] {
  padding: 14px !important;
}
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] > div {
  /* The flex row inside the bottom card — keep as is. */
}

/* ----- 15f. Fix model dropdown clipping / stacking.

   Symptom: after rule 15b wrapped the model picker in a frosted card
   with backdrop-filter, opening the model dropdown made it appear
   BELOW the next card (e.g. inside "首尾帧参考图") instead of floating
   above everything as a popup.

   Root causes:
     1. backdrop-filter creates a new stacking context — the inner
        absolute z-50 dropdown can no longer escape the card's
        z-context to layer above sibling cards.
     2. Even without that, the top card's own border / shadow
        + each card's border-radius create implicit clipping the
        eye reads as "the menu got pushed down".

   Fix:
     - Strip backdrop-filter from the three structural cards (15b)
       so they no longer create a new stacking context. We keep the
       semi-transparent fill — at 92–95% opacity it reads as a
       frosted card without the blur effect.
     - Force overflow: visible on the three structural cards so any
       descendant absolute popup can paint outside the card body.
     - Bump z-index on the dropdown's [z-50] utility so it always
       wins against neighbour cards' implicit stacking. ---- */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  overflow: visible !important;
  background-color: rgba(255, 255, 255, 0.92) !important;
}
/* The middle scroll card MUST keep overflow-y-auto for its own
   inner scroll. Restore it after the blanket override above. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="overflow-y-auto"] {
  overflow-y: auto !important;
  overflow-x: hidden !important;
}
/* Lift the model-picker dropdown above sibling cards. */
.dw-creation [class*="w-[420px]"][class*="border-r"] [class*="z-50"] {
  z-index: 100 !important;
}

/* ==================================================================
   Rule 16. Reorder the left sidebar to match the planner demo
   (creation-workbench.html).

   Demo order (top → bottom):
     1) 创作描述 (prompt)
     2) 模型选择 (model picker)
     3) 视频设置 (everything else: 参考图模式, 主体参考, 分辨率,
        视频时长, 比例, ...)
     4) 立即生成 (CTA)

   Current React render order in <VideoGeneration/index.tsx>:
     a) 模型选择卡片              ('> div' with 'border-b')
     b) 中间滚动容器              ('> div' with 'overflow-y-auto')
        - inline 参考图模式 group (the {selectedModel?.platform !==
            'hunyuan' && <div>...} wrapper at index.tsx:~1475)
        - <ModelParamsSection> children (主体参考 / 提示词 /
            分辨率 / 时长 / ...)
     c) 立即生成卡片              ('> div' with 'border-t')

   Reordering plan (CSS-only, zero React structural change):
     - Make the LEFT-SIDEBAR FLEX (it already is flex-col) honour
       order on its three children; keep model-picker -> b -> CTA
       order unchanged for now (so model picker stays at the top of
       the sidebar's structural cards).
     - Inside the middle scroll container (b), turn it into a
       flex flex-col so children honour order. Promote the
       prompt card ([data-dw-prompt-card]) to order: -10 so it
       always renders FIRST inside the scroll area, ABOVE 主体参考
       and the inline 参考图模式 group.
     - Disable space-y-5 vertical spacing inside the scroll area
       (now it is a flex not a normal block flow with margin
       collapsing concerns); rely on rule 15c padding/margin
       on each card-div for spacing. We add an explicit gap so the
       cards still have breathing room.

   Effect: the top of the parameter column now reads
       提示词 -> 主体参考 -> 参考图模式 -> 分辨率 -> 时长 -> ...
   which, combined with the existing model picker above the scroll
   container, becomes:
       [模型选择] -> [提示词] -> [视频设置...] -> [立即生成]
   That matches the demo intent. (We cannot easily put the prompt
   ABOVE the model picker without restructuring JSX, since the
   picker is OUTSIDE the scroll container; route Y stops here.)
   ================================================================== */

/* 16a. Make the middle scroll container a flex column so children
       can be reordered via order. We keep overflow-y: auto
       (already enforced in 15f) for the inner scroll. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] {
  display: flex !important;
  flex-direction: column !important;
  gap: 12px;
}
/* space-y-5 adds margin-top to siblings; turn it off because we
   rely on gap now (margin-top + gap would double-space). */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > * + * {
  margin-top: 0 !important;
}

/* 16b. Promote the prompt card to the very top of the scroll area. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] [data-dw-prompt-card] {
  order: -10;
}
/* The prompt card lives INSIDE the <ModelParamsSection> Fragment, so
   its parent in the DOM is actually the scroll container directly
   (since the Fragment is unwrapped). The selector above is therefore
   sufficient — but we ALSO scope the rule below as a safety net in
   case React inserts an intermediate wrapper later. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > [data-dw-prompt-card] {
  order: -10;
}

/* 16c. Make sure the prompt card itself still gets the card frame
       from rule 15c. Since 15c targets "> div" and the prompt card
       IS a direct div child of the scroll container, this already
       applies. No extra rule needed.
       
       However, when the prompt card is an INDIRECT descendant
       (theoretical future-proofing), force the card frame here too. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] [data-dw-prompt-card] {
  background-color: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(139, 92, 246, 0.10);
  border-radius: 14px;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.03),
    0 4px 16px -8px rgba(76, 29, 149, 0.06);
  padding: 14px;
}

/* ==================================================================
   Rule 17. "Plan C" polish pass — bring the workbench visual density
   and rhythm closer to the planner demo (creation-workbench.html).

   Diagnosis (before this rule):
     - Left sidebar at 420px feels heavy; the demo's left column is
       ~360px (col-span-3 of a 12-col grid at 1440 viewport).
     - Cards have 14px padding everywhere — chunky compared to demo's
       p-4 (16px) on the OUTER and tighter rhythms inside.
     - Section titles render as default 14px medium — demo uses
       13px semibold with a slight letter-spacing.
     - Bottom CTA card has only the green-pill button; demo shows a
       small "预估消耗 → N 积分" row above the button and a
       "余额：xxx 积分" caption below it. We can't show real numbers
       from CSS, but a static informational caption immediately
       communicates "this is the cost / submit area" matching demo.
     - The footer (CreationFooter) needs the legacy page's bottom
       to not collide with it — VideoGeneration's history list etc.
       should leave 64px clear.

   We keep ALL changes scoped under .dw-creation so other pages
   stay untouched.
   ================================================================== */

/* 17a. Widen left sidebar to 540px so each parameter card has more
       breathing room (sliders, button rows, label/value pairs no
       longer feel cramped). The central preview area auto-shrinks
       to compensate, which is exactly what the user asked for:
       "make the parameter column larger, the preview smaller". */
.dw-creation [class*="w-[420px]"][class*="border-r"] {
  width: 540px !important;
  padding: 20px 18px !important;
  gap: 16px;
}
/* 17a-bis. Right history panel stays at 420px (the legacy width).
       It only shows thumbnails so it doesn't need extra room. The
       asymmetry between the two side columns is intentional now:
       parameters need width, history doesn't. */
.dw-creation [class*="w-[420px]"][class*="border-l"] {
  width: 420px !important;
  padding: 20px 18px !important;
  gap: 16px;
}

/* 17b. Slightly bigger gap between the three structural cards and a
       lighter shadow so each card reads as "floating" rather than
       "stacked tight". */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div {
  border-radius: 18px !important;
  border: 1px solid rgba(139, 92, 246, 0.08) !important;
  box-shadow:
    0 1px 1px rgba(15, 12, 30, 0.03),
    0 6px 18px -10px rgba(76, 29, 149, 0.07) !important;
  margin-bottom: 16px !important;
}

/* 17c. Inner parameter cards (rendered by ModelParamsSection inside
       the middle scroll area) — make them visually lighter than the
       three structural cards: smaller radius, no shadow, just a
       subtle border. This mirrors the demo where the "stack of
       small cards inside the section" rhythm has the inner cards
       feeling like content blocks rather than panels. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div {
  border-radius: 14px !important;
  border: 1px solid rgba(139, 92, 246, 0.07) !important;
  box-shadow: none !important;
  padding: 14px 14px !important;
  background-color: rgba(255, 255, 255, 0.65) !important;
}

/* 17d. Tighter gap between inner parameter cards. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] {
  gap: 12px !important;
}

/* 17e. Section labels (the <label> blocks inside each parameter card)
       — make them 13px semibold matching demo "text-sm font-semibold
       text-ink-900" header treatment. Originally they render as
       default 14px medium. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div > label,
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div > div > label,
.dw-creation [data-dw-prompt-card] > label {
  font-size: 13px !important;
  font-weight: 600 !important;
  color: #0E0A1A !important;
  letter-spacing: 0.01em !important;
  margin-bottom: 8px !important;
  display: block;
}

/* 17f. Square-button grids inside parameter cards (画面比例 / 分辨率 /
       时长 / 视频类型 etc.) — repaint the chips so they match demo
       style: 32px tall, soft surface, purple-tinted hover, selected
       state uses grape-500/10 + grape-700 text. These selectors target
       Tailwind utility patterns the legacy code uses for "chip-style"
       buttons. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-md"],
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-lg"] {
  border-radius: 8px !important;
}
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-md"]:not([class*="bg-cyan"]):not([class*="bg-purple"]):not(.bg-purple-600):not([class*="from-"]),
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-lg"]:not([class*="bg-cyan"]):not([class*="bg-purple"]):not(.bg-purple-600):not([class*="from-"]) {
  background-color: rgba(139, 92, 246, 0.04) !important;
  border: 1px solid rgba(139, 92, 246, 0.10) !important;
  color: #4B5563 !important;
  transition: all 150ms ease !important;
}
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-md"]:not([class*="bg-cyan"]):not([class*="bg-purple"]):not(.bg-purple-600):not([class*="from-"]):hover,
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="rounded-lg"]:not([class*="bg-cyan"]):not([class*="bg-purple"]):not(.bg-purple-600):not([class*="from-"]):hover {
  background-color: rgba(139, 92, 246, 0.10) !important;
  border-color: rgba(139, 92, 246, 0.25) !important;
  color: #1F1A2E !important;
}

/* 17g. Bottom CTA card (the "立即生成" pill area) — add an informational
       caption row ABOVE the button via ::before, mirroring the demo's
       "预估消耗 → N 积分" + "余额：xxx 积分 · 普通用户" lines.

       Because CSS can't read React state, the message is generic but
       still communicates the right semantics: the button itself
       already shows the live cost ("消耗 N 积分生成"), so the caption
       just frames the action. We position the ::before as a flex
       sibling above the button row by injecting flex-direction:
       column into the bottom card itself. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] {
  display: flex !important;
  flex-direction: column !important;
  gap: 10px !important;
  padding: 14px 16px 16px 16px !important;
}
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"]:not([data-wf-cta])::before {
  content: '预估消耗 · 立即生成';
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: #9CA3AF;
  font-weight: 500;
  letter-spacing: 0.02em;
  text-transform: none;
  padding: 0 2px;
}
/* Caption below the button. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"]::after {
  content: '生成结果将自动保存到「我的素材库」';
  display: block;
  font-size: 11px;
  color: #B6B0C2;
  text-align: center;
  margin-top: 2px;
  letter-spacing: 0.01em;
}

/* 17h. The primary "立即生成" button — make it taller (44px) with a
       slightly bigger radius so it dominates the bottom card the way
       the demo's full-width grape-gradient pill does. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] button[class*="from-green-500"],
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] button[class*="bg-gray-700"] {
  height: 44px !important;
  border-radius: 12px !important;
  font-size: 14px !important;
  font-weight: 600 !important;
  letter-spacing: 0.01em !important;
}
/* Cancel-during-generation variant (red theme) gets the same shape. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] button[class*="bg-red-500/10"] {
  height: 44px !important;
  border-radius: 12px !important;
  font-size: 14px !important;
  font-weight: 600 !important;
}

/* 17i. The "数量 · X 条" dropdown chip on the left of the CTA button —
       give it the same height as the primary button and a softer
       grape-tinted surface so the row reads as a single bar. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] .ig-input,
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] button.ig-input {
  height: 44px !important;
  border-radius: 12px !important;
  background-color: rgba(139, 92, 246, 0.05) !important;
  border: 1px solid rgba(139, 92, 246, 0.15) !important;
  color: #1F1A2E !important;
  font-size: 13px !important;
  font-weight: 500 !important;
}

/* 17j. Make sure the page's main column reserves space for the
       fixed CreationFooter (h-16 = 64px). The shell already adds
       pb-16 on the <section>, but we also force the LEGACY page's
       bottom history rail not to be obscured: the legacy
       page uses h-screen / 100vh-128px math; with the footer it
       should now use 100vh-192px effectively. We achieve this by
       trimming the top-level legacy container's height by 64px when
       embedded. */
.dw-creation [class*="h-[calc(100vh"][class*="-128px)"] {
  height: calc(100vh - 192px) !important;
}
/* Some legacy pages may have a hardcoded "h-screen" on the embedded
   container — bring those down too, leaving room for AppHeader (64) +
   CreationTabs (~64) + CreationFooter (64). */
.dw-creation > div > section > div[class*="h-screen"] {
  height: calc(100vh - 192px) !important;
}

/* 17k. Soften the structural-card top/bottom card hairlines — after
       17b they're already toned down, but the bottom CTA card had
       border-t which combined with our border now reads as double.
       Force border-top-color to match our card border so it merges. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"] {
  border-top-color: rgba(139, 92, 246, 0.08) !important;
}
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-b"] {
  border-bottom-color: rgba(139, 92, 246, 0.08) !important;
}

/* 17l. Subtle accent bar on the active selected chip — when a比例 /
       分辨率 / video-type chip is selected (legacy uses bg-cyan-500
       which we already remap to grape), reinforce with a stronger
       grape-tinged pill so it pops against the new soft surfaces. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="bg-cyan-500/20"],
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] button[class*="bg-cyan-500"][class*="text-white"] {
  background-color: rgba(139, 92, 246, 0.12) !important;
  border: 1px solid rgba(139, 92, 246, 0.45) !important;
  color: #6D28D9 !important;
  font-weight: 600 !important;
}

/* 17m. The model-picker top card (the one OUTSIDE the scroll area)
       — match its inner padding with the bottom card so the three
       structural cards look symmetric. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-b"] {
  padding: 14px 16px !important;
}

/* ==================================================================
   Rule 18. Fix the "squeezed middle" symptom — left column should
   scroll AS A WHOLE, not just its middle parameter card.

   Diagnosis (visible in user screenshot 2026-05-18):
     The four left-column structural cards stack vertically:
       (A) prompt card        ~190px (textarea h-32 + label + paste row)
       (B) model picker card  ~80px
       (C) parameters card    flex:1 + overflow-y-auto  ← squeezed!
       (D) bottom CTA card    ~160px (::before + button + ::after)
     On a 720px viewport (after AppHeader 64 + Tabs 64 + Footer 64),
     the column has only ~528px usable, leaving (C) just ~100-200px.
     Users see only 2-3 parameter rows and have to scroll inside (C),
     which breaks visual continuity and feels nothing like demo.

   Demo behaviour (creation-workbench.html):
     The whole left column (col-span-3) is a tall vertical strip whose
     four cards (创作描述 / 模型选择 / 视频设置 / 立即生成) flow
     naturally with space-y-4. The PAGE scrolls if the column is
     taller than the viewport — the parameter card itself is NEVER
     forced to scroll independently.

   Fix:
     • Disable flex:1 + overflow-y-auto on the middle parameter card,
       making it grow to its natural height.
     • Allow the left-column shell itself to scroll. We override the
       legacy flex flex-col so the four cards keep stacking but the
       overflow happens at the OUTER shell.
     • Trim a bit of breathing room: shorter prompt textarea (h-32 ->
       h-24), tighter card paddings.
     • Remove the over-eager ::after caption from the bottom CTA card
       (rule 17g) — combined with footer + saved-to-draft message it
       was triple-redundant.
   ================================================================== */

/* 18a. Make the LEFT COLUMN SHELL scroll, not its middle card. We
       remove the legacy flex flex-col baseline (which made the
       middle child use flex:1 to fill remaining height) and turn the
       shell into a normal block-flow column with overflow-y: auto. */
.dw-creation [class*="w-[420px]"][class*="border-r"] {
  display: block !important;
  overflow-y: auto !important;
  overflow-x: hidden !important;
  /* leave height unchanged — rule 17j already trims the parent
     wrapper to (100vh - 192px). The shell takes that full height
     and scrolls internally if needed. */
  scrollbar-width: thin;
  scrollbar-color: rgba(139, 92, 246, 0.25) transparent;
}
.dw-creation [class*="w-[420px]"][class*="border-r"]::-webkit-scrollbar {
  width: 6px;
}
.dw-creation [class*="w-[420px]"][class*="border-r"]::-webkit-scrollbar-thumb {
  background-color: rgba(139, 92, 246, 0.25);
  border-radius: 6px;
}
.dw-creation [class*="w-[420px]"][class*="border-r"]::-webkit-scrollbar-track {
  background: transparent;
}

/* 18b. The middle parameter card no longer needs to be a flex:1
       overflow container — let it grow with its content. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="overflow-y-auto"] {
  flex: none !important;
  overflow: visible !important;
  overflow-y: visible !important;
  overflow-x: visible !important;
  height: auto !important;
  max-height: none !important;
}

/* 18c. Inside the middle card, switch back from display flex (added
       by rule 16a so children can use order) to a plain block flow.
       Why we can drop the flex now: in route Z, the prompt card was
       hoisted OUT of this scroll container, so we no longer need
       order to reposition it. Children just stack naturally with
       gap from rule 17d. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] {
  display: flex !important;
  flex-direction: column !important;
  gap: 12px !important;
}

/* 18d. Trim the prompt textarea height: h-32 (128px) → 96px so the
       prompt card doesn't dominate the column. Most users write 1-3
       lines anyway and the textarea is resizable via mouse if not. */
.dw-creation [data-dw-prompt-card] textarea[class*="h-32"] {
  height: 88px !important;
  min-height: 88px !important;
}

/* 18e. Drop the redundant ::after caption from the bottom CTA card
       — the page footer already shows "✓ 生成的内容将自动保存到我的草稿"
       so duplicating it just below the button is noise. Keep ::before
       (预估消耗 · 立即生成) which is informative. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"]::after {
  content: none !important;
  display: none !important;
}

/* 18f. Tighten the bottom CTA card a bit so it doesn't loom large
       at the bottom of the column. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-t"]:not([data-wf-cta]) {
  padding: 12px 14px 14px 14px !important;
  gap: 8px !important;
}

/* 18g. Tighten the prompt host card padding too. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[data-dw-prompt-host] {
  padding: 14px !important;
}

/* 18h. Tighten model-picker card padding to match. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="border-b"]:not([data-dw-prompt-host]) {
  padding: 12px 14px !important;
}

/* 18i. Inner parameter cards: lighter padding so 5+ cards fit nicer. */
.dw-creation [class*="space-y-5"][class*="custom-scrollbar"] > div {
  padding: 14px !important;
}

/* 18i-fix. Align inner-card outer edges with the structural cards
   (model picker / bottom CTA) above and below.

   Why: the middle scroll container ships with Tailwind 'p-4' (16px
   horizontal padding) from the JSX className. The structural cards
   (model picker, prompt host, bottom CTA) are direct children of the
   420px panel and therefore extend edge-to-edge (0 → 420px).
   The inner parameter cards ("首尾帧参考图", "视频时长", ...) live
   INSIDE the scroll container, so their outer edges sit at 16px /
   404px — visibly narrower than the structural cards above by 16px on
   each side. This is what produced the "the small cards look smaller
   than the model card" mismatch.

   Fix: zero out the left/right padding on the scroll container so
   inner cards can extend edge-to-edge like their siblings. We keep
   vertical padding (top/bottom) so the first/last inner card still
   has breathing room from the structural card above/below. The
   scroll container itself is transparent (rule 15c), so its padding
   was purely cosmetic spacing for content — and that spacing is now
   provided by each inner card's own 14px padding instead. */
.dw-creation [class*="w-[420px]"][class*="border-r"] > div[class*="overflow-y-auto"] {
  padding-left: 0 !important;
  padding-right: 0 !important;
}

/* 18j. Make sure the embedded VideoGeneration's TWO-COLUMN flex
       parent (left sidebar + right preview) still respects its
       100vh-192px height after rule 18a turned the left side into
       block (which took the left out of the flex stretch). We force
       the outer flex parent to keep the right side stretched. */
.dw-creation [class*="h-[calc(100vh"] > [class*="w-[420px]"][class*="border-r"] {
  /* The left column is now block, but as a flex child of its parent
     it still gets the full height; setting align-self isn't needed
     since the parent uses default align-items stretch. The
     display block from 18a does NOT detach it from being a flex
     child — it just changes its own internal layout. */
  align-self: stretch;
}

/* 18k. The middle parameter card had flex:1 legacy — neutralise
       that completely with a backup catch-all in case Tailwind's
       JIT spits a different attribute pattern. */
.dw-creation [class*="w-[420px]"][class*="border-r"] [class*="flex-1"][class*="overflow-y-auto"] {
  flex: none !important;
  flex-grow: 0 !important;
  flex-shrink: 1 !important;
  height: auto !important;
}

/* 19. Grape-Range slider — clone of demo's input[type='range'].grape-range
      adapted for the dark workbench panel.

      Shape:
        • thin 4px pill-shaped track
        • gradient-fill from the start up to --val (set inline as %)
        • 14px white thumb with cyan ring (matches our cyan accent)

      Why we need the !important: the workbench panel ships its own
      dark <input> resets globally; we have to override base styles. */
.dw-creation input[type="range"].grape-range {
  -webkit-appearance: none !important;
  appearance: none !important;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  /* Two-stop gradient: filled portion in cyan→purple, rest faint white. */
  background: linear-gradient(
    90deg,
    #06b6d4 0%,
    #8b5cf6 var(--val, 50%),
    rgba(255, 255, 255, 0.08) var(--val, 50%),
    rgba(255, 255, 255, 0.08) 100%
  );
  outline: none;
  cursor: pointer;
  padding: 0;
  margin: 4px 0;
}
/* Webkit thumb */
.dw-creation input[type="range"].grape-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ffffff;
  border: 2px solid #06b6d4;
  cursor: pointer;
  box-shadow:
    0 2px 6px rgba(6, 182, 212, 0.35),
    0 0 0 4px rgba(6, 182, 212, 0.08);
  transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.dw-creation input[type="range"].grape-range::-webkit-slider-thumb:hover {
  transform: scale(1.1);
  box-shadow:
    0 2px 8px rgba(6, 182, 212, 0.5),
    0 0 0 6px rgba(6, 182, 212, 0.12);
}
/* Firefox thumb */
.dw-creation input[type="range"].grape-range::-moz-range-thumb {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #ffffff;
  border: 2px solid #06b6d4;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(6, 182, 212, 0.35);
}
/* Firefox track — Chromium uses background-image directly on input,
   but Firefox needs a separate track element styled. We re-apply the
   same gradient for parity. */
.dw-creation input[type="range"].grape-range::-moz-range-track {
  height: 4px;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    #06b6d4 0%,
    #8b5cf6 var(--val, 50%),
    rgba(255, 255, 255, 0.08) var(--val, 50%),
    rgba(255, 255, 255, 0.08) 100%
  );
}

/* ============================================================
   Section 18 — Light-theme overrides for the centre preview
   overlay UI inside <VideoPreviewArea>.

   The component is written for the legacy dark theme and uses
   hard-coded 'bg-black/60', 'bg-black/40', 'bg-cyan-500/20',
   'text-cyan-300', etc. On the new light page these read as
   harsh black/violet patches floating on top of the video.
   The screenshot the user sent highlights three offenders:

      (1) top-left  "1 / N" index badge      -> bg-black/60
      (2) top-right action buttons           -> bg-black/60
      (3) bottom info card (prompt + chips)  -> bg-black/40

   We rewrite all three so they look like pale glass cards,
   matching the rest of the workbench. Logic, structure and
   class names in the source file remain untouched.
   ============================================================ */

/* 18a. Top-left video index badge ("1 / N").
       Original: solid black/60 + white text. New: pale glass
       with grape-tinted border, dark text. */
.dw-creation [class*="absolute"][class*="top-4"][class*="left-4"][class*="bg-black/60"] {
  background: rgba(255, 255, 255, 0.78) !important;
  border: 1px solid rgba(139, 92, 246, 0.22) !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.06),
    0 6px 18px -10px rgba(76, 29, 149, 0.18) !important;
  color: #1f1933 !important;
}
/* Re-color the inner numerals: cyan→grape, gray→muted dark. */
.dw-creation [class*="absolute"][class*="top-4"][class*="left-4"][class*="bg-black/60"] .text-cyan-400 {
  color: #7c3aed !important;
}
.dw-creation [class*="absolute"][class*="top-4"][class*="left-4"][class*="bg-black/60"] .text-gray-400,
.dw-creation [class*="absolute"][class*="top-4"][class*="left-4"][class*="bg-black/60"] .text-gray-300 {
  color: #4b3a6b !important;
}

/* 18b. Top-right floating action buttons (regenerate / extend /
       fullscreen). Same recipe as 18a, plus hover state. */
.dw-creation [class*="absolute"][class*="top-4"][class*="right-4"] button[class*="bg-black/60"] {
  background: rgba(255, 255, 255, 0.78) !important;
  border: 1px solid rgba(139, 92, 246, 0.22) !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.06),
    0 6px 18px -10px rgba(76, 29, 149, 0.18) !important;
}
.dw-creation [class*="absolute"][class*="top-4"][class*="right-4"] button[class*="bg-black/60"]:hover {
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.18), rgba(217, 70, 239, 0.16)) !important;
  border-color: rgba(139, 92, 246, 0.45) !important;
  box-shadow:
    0 2px 4px rgba(15, 12, 30, 0.08),
    0 10px 24px -10px rgba(124, 58, 237, 0.32) !important;
}
/* Re-color the icon strokes inside those buttons. The buttons
       use 'text-white' / 'text-purple-400' / 'text-cyan-400',
       all of which need to read on a light background. */
.dw-creation [class*="absolute"][class*="top-4"][class*="right-4"] button[class*="bg-black/60"] svg {
  color: #6d28d9 !important;
}

/* 18c. Below-video info card (prompt text + model chip + duration
       + timestamp). Originally 'bg-black/40 backdrop-blur-xl
       border border-white/10'.
       PREVIOUSLY this used rgba(255,255,255,0.82) + the inherited
       'backdrop-blur-xl' (24px) which combined to look like a
       solid white slab — out of place next to the airy
       demo-style cards on the left/right. We now match the demo
       recipe exactly: 70% white + 14px blur + 140% saturate so
       the page's grape/peach radial glow shows through. */
.dw-creation [class*="mt-6"][class*="bg-black/40"][class*="rounded-2xl"] {
  background: rgba(255, 255, 255, 0.70) !important;
  border: 1px solid rgba(124, 58, 237, 0.10) !important;
  /* Override Tailwind's backdrop-blur-xl (24px) → demo-style
     softer 14px frost so the radial glow underneath remains
     visible. Vendor prefix kept for Safari. */
  -webkit-backdrop-filter: blur(14px) saturate(140%) !important;
  backdrop-filter: blur(14px) saturate(140%) !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.04),
    0 8px 24px -12px rgba(76, 29, 149, 0.10) !important;
}
/* Prompt body: light-theme readable. */
.dw-creation [class*="mt-6"][class*="bg-black/40"][class*="rounded-2xl"] .text-gray-200 {
  color: #1f1933 !important;
}
/* Inline meta (duration / timestamp). */
.dw-creation [class*="mt-6"][class*="bg-black/40"][class*="rounded-2xl"] .text-gray-400 {
  color: #4b3a6b !important;
}
.dw-creation [class*="mt-6"][class*="bg-black/40"][class*="rounded-2xl"] .text-gray-500 {
  color: #6b5b8a !important;
}
/* Model chip (the pill that says e.g. "HunyuanVideo I2V"). It
       lives inside the info card and uses 'bg-cyan-500/20
       text-cyan-300 border-cyan-500/30'. We swap to grape so
       it matches the brand palette used everywhere else. */
.dw-creation [class*="mt-6"][class*="bg-black/40"][class*="rounded-2xl"] [class*="bg-cyan-500/20"] {
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.16), rgba(217, 70, 239, 0.14)) !important;
  color: #6d28d9 !important;
  border: 1px solid rgba(139, 92, 246, 0.32) !important;
}

/* 18d. Video model selector — icon tile (the 40×40 square that
       holds each model's logo, both inside the dropdown trigger
       and inside every option row in the open dropdown).

       Origin: VideoGeneration/index.tsx renders
         <div class="w-10 h-10 rounded-lg
                     bg-gradient-to-br
                     from-cyan-500/20 to-purple-500/20 ...">
       which was tuned for the page's native dark theme
       (#1a1a24 cards). Once the page is embedded inside the
       Creation Workbench, the cards become near-white via
       Section 15 / 17 overrides, so a 20%-alpha cyan→purple
       gradient over white reads as a near-invisible pale wash
       and the model's logo inside (also tinted-white in some
       SVGs) gets lost — see planner screenshot 2026-05-27.

       Fix: inside .dw-creation only, give the icon tile a
       deeper grape gradient with a clear violet ring so it
       stands out from the surrounding frosted-white card, and
       lift the inner <img> with a soft drop-shadow so a
       light-on-light logo still has enough edge contrast.
       Standalone /video-generation route stays untouched. */
.dw-creation [class*="from-cyan-500/20"][class*="to-purple-500/20"] {
  background-image: linear-gradient(
    135deg,
    rgba(139, 92, 246, 0.28),
    rgba(217, 70, 239, 0.22)
  ) !important;
  border: 1px solid rgba(139, 92, 246, 0.45) !important;
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.55),
    0 1px 2px rgba(76, 29, 149, 0.10) !important;
}
/* The PNG/SVG logos served from /icons/* are authored as PURE WHITE
   pixels on a transparent canvas (they were designed for the dark
   #1a1a24 cards of the standalone /video-generation route, where
   white reads fine). On the workbench's light grape background the
   white silhouette disappears into the pale lilac tile — see planner
   screenshot 2026-05-27 (red-boxed 海螺 2.3 / Kling 2.6 rows).

   We can't recolor the asset itself (it's still needed white on the
   standalone dark route), so we re-tint it via a CSS filter chain:
     1. brightness(0)  →  collapse every channel to black, killing
                          the original white so the next stages have
                          a known starting color.
     2. saturate(100%) →  no-op safety; needed because some browsers
                          fold brightness(0) into desaturated grey.
     3. invert(20%)    →  lift black to a deep near-black violet.
     4. sepia(60%)     →  push neutral grey toward warm brown, which
                          combined with the next hue-rotate yields a
                          rich purple instead of a flat grey.
     5. hue-rotate(220deg) → rotate the warm tint into the violet /
                          indigo band that matches the surrounding
                          grape gradient and the workbench's primary.
     6. saturate(600%) →  re-saturate the result so the icon reads
                          as a vivid purple, not a washed-out mauve.
     7. drop-shadow    →  one-pixel grape shadow for crisp edges
                          against the pale tile.
   The whole chain stays inside .dw-creation, so the standalone
   /video-generation route still gets crisp white icons on dark. */
.dw-creation [class*="from-cyan-500/20"][class*="to-purple-500/20"] > img {
  filter:
    brightness(0)
    saturate(100%)
    invert(20%)
    sepia(60%)
    hue-rotate(220deg)
    saturate(600%)
    drop-shadow(0 1px 1px rgba(15, 12, 30, 0.18)) !important;
}

/* ============================================================
   Section 19. Image Generation panel — light-theme overrides.
   ============================================================
   ImageGeneration/index.tsx and its sub-components were originally
   designed against a dedicated DARK theme (#06060a / #08080e fills,
   white/gray text). When that page is mounted as the embedded image
   tab inside the unified Creation Workbench, we want it to read on
   the SAME light grape background the video tab uses.

   Strategy: scoped inside .dw-creation only, we
     a) neutralize the bespoke .ig-* background layers (transparent),
     b) re-skin the .ig-* card / input / option / history widgets to
        match the frosted-white grape look of the video tab,
     c) remap the most common dark Tailwind utility classes
        (text-white, text-gray-{200..600}, bg-black/*, bg-white/10,
        ring-white/[0.0x], bg-zinc-800) to readable grape values.

   Out of scope: this section never matches outside .dw-creation, so
   the standalone /image-generation route keeps its native dark look
   if anyone navigates there directly.
   ============================================================ */

/* ---- 19a. Page / panel / grid backgrounds → fully flat #F6F6F6
       (armored). After several rounds of "residual purple cast" in
       the centre preview area, we discovered the glow was leaking
       through compositing rather than via background-image alone:
       a parent layer in the workbench shell paints a faint grape
       radial that mixes with anything semi-transparent below it.
       Hence the armored recipe:

         (1) Triple-write the background (shorthand + longhand color
             + longhand image:none) — defensive against any cascade
             ordering that re-introduces the violet image.
         (2) 'isolation: isolate' creates a fresh stacking context
             on each surface so ancestor 'mix-blend-mode' / radial
             glow layers cannot composite onto the canvas.
         (3) The center preview (.ig-grid-bg) additionally pins an
             opaque #F6F6F6 fill via 'box-shadow: inset 0 0 0 9999px'
             as a last-resort visual seal — even if some descendant
             renders a translucent purple wash above the bg, the
             inset shadow keeps the visible canvas neutral.

       Why we don't simply edit index.css: the standalone
       /image-generation route still relies on its native dark theme
       there. Scoping the override to .dw-creation keeps both
       entrypoints independent. */
.dw-creation .ig-page-bg,
.dw-creation .ig-panel-left-bg,
.dw-creation .ig-panel-right-bg,
.dw-creation .ig-grid-bg {
  background: #F6F6F6 !important;
  background-image: none !important;
  background-color: #F6F6F6 !important;
  isolation: isolate !important;
}
.dw-creation .ig-grid-bg {
  /* Hard seal: an inset shadow the size of the element ensures
     the painted surface is solid #F6F6F6 regardless of any
     translucent layer that might still exist below the content. */
  box-shadow: inset 0 0 0 9999px #F6F6F6 !important;
}
/* Kill every decorative pseudo-layer (noise overlay, radial glow,
       grid lines) on these surfaces — they are the secondary source
       of the residual purple cast even when the base color is right. */
.dw-creation .ig-page-bg::before,
.dw-creation .ig-page-bg::after,
.dw-creation .ig-panel-left-bg::before,
.dw-creation .ig-panel-left-bg::after,
.dw-creation .ig-panel-right-bg::before,
.dw-creation .ig-panel-right-bg::after,
.dw-creation .ig-grid-bg::before,
.dw-creation .ig-grid-bg::after {
  content: none !important;
  display: none !important;
  background: none !important;
  background-image: none !important;
  mask-image: none !important;
  -webkit-mask-image: none !important;
}

/* ---- 19b. Header strip (.ig-header / .ig-header-enhanced).
       Originally a near-black blurred bar. */
.dw-creation .ig-header,
.dw-creation .ig-header-enhanced {
  background: rgba(255, 255, 255, 0.6) !important;
  border-bottom: 1px solid rgba(139, 92, 246, 0.12) !important;
  box-shadow: 0 1px 0 rgba(139, 92, 246, 0.04) !important;
}

/* ---- 19c. Panel border / column glow lines.
       The original draws a 1px dark gradient on the column edge —
       turn it off because Section 15 cards already give us edges. */
.dw-creation .ig-panel-border {
  border-color: transparent !important;
}
.dw-creation .ig-panel-border::after,
.dw-creation .ig-left-glow::after,
.dw-creation .ig-right-glow::after {
  content: none !important;
  display: none !important;
}

/* ---- 19d. Model selector card (.ig-model-card). */
.dw-creation .ig-model-card {
  background: rgba(255, 255, 255, 0.92) !important;
  border: 1px solid rgba(139, 92, 246, 0.22) !important;
  backdrop-filter: none !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.04),
    0 6px 18px -10px rgba(124, 58, 237, 0.18) !important;
}
.dw-creation .ig-model-card:hover {
  border-color: rgba(139, 92, 246, 0.4) !important;
  box-shadow:
    0 2px 6px rgba(15, 12, 30, 0.06),
    0 12px 28px -12px rgba(124, 58, 237, 0.28) !important;
}

/* ---- 19e. Dropdown panel + items. */
.dw-creation .ig-dropdown {
  background: rgba(255, 255, 255, 0.98) !important;
  border: 1px solid rgba(139, 92, 246, 0.22) !important;
  box-shadow:
    0 12px 32px rgba(76, 29, 149, 0.16),
    0 0 0 1px rgba(139, 92, 246, 0.06) !important;
}
.dw-creation .ig-dropdown-item:hover {
  background: rgba(139, 92, 246, 0.08) !important;
}
.dw-creation .ig-dropdown-item.selected {
  background: rgba(139, 92, 246, 0.12) !important;
  border-left: 2px solid rgba(139, 92, 246, 0.6) !important;
}

/* ---- 19f. Text input / textarea (.ig-input). */
.dw-creation .ig-input {
  background: rgba(255, 255, 255, 0.7) !important;
  border: 1px solid rgba(139, 92, 246, 0.2) !important;
  color: #1f1933 !important;
}
.dw-creation .ig-input:focus {
  background: rgba(255, 255, 255, 0.95) !important;
  border-color: rgba(139, 92, 246, 0.5) !important;
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.12) !important;
}
.dw-creation .ig-input::placeholder {
  color: #8a7aa3 !important;
}

/* ---- 19g. Aspect / size option buttons. */
.dw-creation .ig-option-btn {
  background: rgba(139, 92, 246, 0.04) !important;
  border: 1px solid rgba(139, 92, 246, 0.18) !important;
  color: #4b3a6b !important;
}
.dw-creation .ig-option-btn:hover {
  background: rgba(139, 92, 246, 0.08) !important;
  border-color: rgba(139, 92, 246, 0.32) !important;
  color: #2c1f4d !important;
}
.dw-creation .ig-option-btn.selected {
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.16), rgba(217, 70, 239, 0.12)) !important;
  border-color: rgba(139, 92, 246, 0.5) !important;
  color: #6d28d9 !important;
  box-shadow: 0 0 0 1px rgba(139, 92, 246, 0.2) inset !important;
}

/* ---- 19h. History cards. */
.dw-creation .ig-history-card {
  background: rgba(255, 255, 255, 0.78) !important;
  border: 1px solid rgba(139, 92, 246, 0.16) !important;
}
.dw-creation .ig-history-card:hover {
  background: rgba(255, 255, 255, 0.92) !important;
  border-color: rgba(139, 92, 246, 0.32) !important;
  box-shadow:
    0 2px 6px rgba(15, 12, 30, 0.04),
    0 10px 24px -12px rgba(124, 58, 237, 0.18) !important;
}
.dw-creation .ig-history-card.selected {
  border-color: rgba(139, 92, 246, 0.5) !important;
  box-shadow:
    0 0 0 1px rgba(139, 92, 246, 0.25),
    0 4px 18px rgba(124, 58, 237, 0.12) !important;
}

/* ---- 19i. Generate button — re-skin the legacy cyan-violet shimmer
       to the new brand gradient (#5B70F1 → #FC98FF) and drop the
       heavy dark glow so it sits on white. The legacy index.css
       defines '.ig-generate-btn' with a 200%-wide cyan→violet→cyan
       background that animates via 'ig-btn-shimmer'; we replace that
       background entirely while keeping the same animation timing
       (200% size + 3s loop) so the shimmer effect survives. */
.dw-creation .ig-generate-btn {
  background:
    linear-gradient(135deg, #5B70F1, #FC98FF, #5B70F1) !important;
  background-size: 200% 200% !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.06),
    0 12px 28px -10px rgba(91, 112, 241, 0.4) !important;
}
.dw-creation .ig-generate-btn:hover:not(:disabled) {
  box-shadow:
    0 2px 4px rgba(15, 12, 30, 0.08),
    0 18px 36px -12px rgba(91, 112, 241, 0.5) !important;
}
.dw-creation .ig-generate-btn:disabled {
  /* Match the unified disabled style used by VideoGeneration /
     VoiceStudio CTAs. Without this rule the legacy index.css would
     paint .ig-generate-btn:disabled as a near-black #27272a slab,
     which looks louder than the rest of the workbench on white. */
  background: rgba(91, 112, 241, 0.12) !important;
  background-image: none !important;
  color: #A5A8B8 !important;
  border: 1px solid rgba(91, 112, 241, 0.20) !important;
  box-shadow: none !important;
  animation: none !important;
}

/* ---- 19j. Tab indicator + small badge: keep grape, remove cyan. */
.dw-creation .ig-tab-active::after {
  background: linear-gradient(90deg, #8b5cf6, #d946ef) !important;
}
.dw-creation .ig-badge {
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.14), rgba(217, 70, 239, 0.12)) !important;
  border: 1px solid rgba(139, 92, 246, 0.28) !important;
  color: #6d28d9 !important;
}
.dw-creation .ig-divider {
  background: linear-gradient(
    90deg,
    transparent,
    rgba(139, 92, 246, 0.2) 20%,
    rgba(139, 92, 246, 0.2) 80%,
    transparent
  ) !important;
}

/* ---- 19k. Generic dark Tailwind text utilities used throughout
       the page body. We map the gray ramp to a grape-tinted neutral
       ramp so labels, hints and meta text stay readable on white.
       Scoped tightly via .dw-creation to avoid bleeding to other
       pages.

       NOTE: we deliberately exclude buttons / pills that already use
       a colored background (those keep their own text color via more
       specific rules — e.g. .ig-generate-btn keeps text-white). */
.dw-creation .ig-page-bg .text-white,
.dw-creation .ig-panel-left-bg .text-white,
.dw-creation .ig-panel-right-bg .text-white {
  color: #1f1933 !important;
}
.dw-creation .ig-page-bg .text-gray-200,
.dw-creation .ig-panel-left-bg .text-gray-200,
.dw-creation .ig-panel-right-bg .text-gray-200 {
  color: #1f1933 !important;
}
.dw-creation .ig-page-bg .text-gray-300,
.dw-creation .ig-panel-left-bg .text-gray-300,
.dw-creation .ig-panel-right-bg .text-gray-300 {
  color: #2c1f4d !important;
}
.dw-creation .ig-page-bg .text-gray-400,
.dw-creation .ig-panel-left-bg .text-gray-400,
.dw-creation .ig-panel-right-bg .text-gray-400 {
  color: #4b3a6b !important;
}
.dw-creation .ig-page-bg .text-gray-500,
.dw-creation .ig-panel-left-bg .text-gray-500,
.dw-creation .ig-panel-right-bg .text-gray-500 {
  color: #6b5b8a !important;
}
.dw-creation .ig-page-bg .text-gray-600,
.dw-creation .ig-panel-left-bg .text-gray-600,
.dw-creation .ig-panel-right-bg .text-gray-600 {
  color: #8a7aa3 !important;
}
/* Small dark tooltip pills used near copy/hint buttons. */
.dw-creation .ig-page-bg [class*="bg-zinc-800"],
.dw-creation .ig-panel-left-bg [class*="bg-zinc-800"],
.dw-creation .ig-panel-right-bg [class*="bg-zinc-800"] {
  background: rgba(255, 255, 255, 0.96) !important;
  border: 1px solid rgba(139, 92, 246, 0.22) !important;
  color: #2c1f4d !important;
  box-shadow: 0 6px 18px -8px rgba(76, 29, 149, 0.2) !important;
}
.dw-creation .ig-page-bg [class*="bg-zinc-800"] .text-gray-200,
.dw-creation .ig-panel-left-bg [class*="bg-zinc-800"] .text-gray-200,
.dw-creation .ig-panel-right-bg [class*="bg-zinc-800"] .text-gray-200,
.dw-creation .ig-page-bg [class*="bg-zinc-800"] .text-gray-300,
.dw-creation .ig-panel-left-bg [class*="bg-zinc-800"] .text-gray-300,
.dw-creation .ig-panel-right-bg [class*="bg-zinc-800"] .text-gray-300 {
  color: #2c1f4d !important;
}

/* ---- 19l. Half-transparent BLACK fills used for video preview
       wells / overlays in history cards. Map them to a soft grape
       tint so they look intentional on a light page. */
.dw-creation .ig-page-bg [class*="bg-black/20"],
.dw-creation .ig-panel-left-bg [class*="bg-black/20"],
.dw-creation .ig-panel-right-bg [class*="bg-black/20"] {
  background-color: rgba(139, 92, 246, 0.06) !important;
}
.dw-creation .ig-page-bg [class*="bg-black/40"],
.dw-creation .ig-panel-left-bg [class*="bg-black/40"],
.dw-creation .ig-panel-right-bg [class*="bg-black/40"] {
  background-color: rgba(15, 12, 30, 0.32) !important;
}
.dw-creation .ig-page-bg [class*="bg-black/50"],
.dw-creation .ig-panel-left-bg [class*="bg-black/50"],
.dw-creation .ig-panel-right-bg [class*="bg-black/50"] {
  background-color: rgba(15, 12, 30, 0.4) !important;
}
.dw-creation .ig-page-bg [class*="bg-black/60"],
.dw-creation .ig-panel-left-bg [class*="bg-black/60"],
.dw-creation .ig-panel-right-bg [class*="bg-black/60"] {
  background-color: rgba(15, 12, 30, 0.45) !important;
}

/* ---- 19m. Hover overlay action buttons (the 'bg-white/10
       text-white/80' chips that appear over a hovered video card).
       On a dark backdrop they read as translucent white; we keep
       them readable by giving them a stronger frosted look. */
.dw-creation .ig-page-bg [class*="bg-white/10"],
.dw-creation .ig-panel-left-bg [class*="bg-white/10"],
.dw-creation .ig-panel-right-bg [class*="bg-white/10"] {
  background-color: rgba(255, 255, 255, 0.85) !important;
  color: #4b3a6b !important;
}
.dw-creation .ig-page-bg [class*="bg-white/10"]:hover,
.dw-creation .ig-panel-left-bg [class*="bg-white/10"]:hover,
.dw-creation .ig-panel-right-bg [class*="bg-white/10"]:hover {
  background-color: rgba(255, 255, 255, 0.95) !important;
  color: #2c1f4d !important;
}
.dw-creation .ig-page-bg [class*="text-white/80"],
.dw-creation .ig-panel-left-bg [class*="text-white/80"],
.dw-creation .ig-panel-right-bg [class*="text-white/80"] {
  color: #4b3a6b !important;
}

/* ---- 19n. Faint white rings used as inner hairlines on cards
       (ring-white/[0.04..0.08]). Replace with a faint grape line
       so they remain visible on the light theme. */
.dw-creation .ig-page-bg [class*="ring-white/"],
.dw-creation .ig-panel-left-bg [class*="ring-white/"],
.dw-creation .ig-panel-right-bg [class*="ring-white/"] {
  --tw-ring-color: rgba(139, 92, 246, 0.18) !important;
}

/* ============================================================
   Section 20. Video Generation panel — leftover dark utilities.
   ============================================================
   The video tab's left column re-uses some Tailwind dark utility
   classes that were never refactored to .ig-* / .dw-* tokens, most
   notably:
     • the camera-control 3x3 grid buttons (bg-zinc-800 hover:bg-zinc-700
       border-white/10 hover:border-cyan-500/50, text-gray-400)
     • the bottom hint pill of the camera popover (bg-zinc-800
       border-t border-white/10, text-gray-500)
     • a few inline chips / textareas elsewhere in index.tsx that
       use bg-zinc-800 / bg-zinc-800/50 / bg-zinc-800/95.

   These render as black blocks on the light grape page (see screenshot
   at the bottom-left of the camera control panel). Section 19's
   matching rules were scoped to .ig-page-bg / .ig-panel-*-bg, which
   only exist on the image tab — so they did not catch these.

   Strategy: scoped on .dw-creation alone (no .ig-* prefix), remap the
   most common dark utilities globally for the workbench. Because the
   video page is the only OTHER consumer of these Tailwind classes
   inside .dw-creation, the blast radius is exactly the panels we want
   to fix.
   ============================================================ */

/* ---- 20a. Solid zinc fills → frosted white with grape edge. */
.dw-creation [class*="bg-zinc-800"] {
  background-color: rgba(255, 255, 255, 0.78) !important;
  color: #2c1f4d !important;
}
.dw-creation [class*="bg-zinc-800/50"] {
  background-color: rgba(255, 255, 255, 0.62) !important;
}
.dw-creation [class*="bg-zinc-800/95"] {
  background-color: rgba(255, 255, 255, 0.96) !important;
  box-shadow: 0 12px 28px -8px rgba(76, 29, 149, 0.22) !important;
}
.dw-creation [class*="bg-zinc-700"]:hover,
.dw-creation [class*="hover\\:bg-zinc-700"]:hover {
  background-color: rgba(139, 92, 246, 0.1) !important;
}

/* ---- 20b. White hairline borders (border-white/10) used as the
       1px outline on those cards. Map to a faint grape stroke. */
.dw-creation [class*="border-white/10"] {
  border-color: rgba(139, 92, 246, 0.2) !important;
}

/* ---- 20c. The cyan hover border on camera buttons. We keep the
       hover behaviour but in grape so it matches the brand. */
.dw-creation [class*="hover\\:border-cyan-500/50"]:hover {
  border-color: rgba(139, 92, 246, 0.5) !important;
}

/* ---- 20d. Generic dark Tailwind text utilities, NOT scoped to
       .ig-*-bg this time, so the video page picks them up too.
       We exclude any element that already sits on a colored
       background by NOT touching elements inside known accent
       containers (the explicit grape gradient pills already set
       their own color via more specific rules in Section 18). */
.dw-creation [class*="bg-zinc-800"] .text-gray-400,
.dw-creation [class*="bg-zinc-800"] .text-gray-500 {
  color: #4b3a6b !important;
}
.dw-creation [class*="bg-zinc-800"] .text-white,
.dw-creation [class*="bg-zinc-800"] .text-gray-200,
.dw-creation [class*="bg-zinc-800"] .text-gray-300 {
  color: #2c1f4d !important;
}
/* The cyan accent text used inside the small token chip (e.g.
       'bg-zinc-800 ... text-cyan-400'). Re-tone to grape. */
.dw-creation [class*="bg-zinc-800"] .text-cyan-400 {
  color: #6d28d9 !important;
}
/* The faint inner ring used on the camera popover (ring-white/[0.08]). */
.dw-creation [class*="bg-zinc-800/95"][class*="ring-white/"] {
  --tw-ring-color: rgba(139, 92, 246, 0.18) !important;
}

/* ---- 20e. Polish: when camera buttons are hovered they elevate
       icons via 'group-hover:text-white' / 'opacity-100'. On a
       light hovered background, white-on-light is invisible, so
       remap that final text color to deep grape. */
.dw-creation [class*="bg-zinc-800"] .group:hover .text-gray-400,
.dw-creation [class*="bg-zinc-800"] .group:hover [class*="text-gray-400"] {
  color: #2c1f4d !important;
}
.dw-creation [class*="bg-zinc-800"] [class*="group-hover\\:text-white"] {
  /* Make sure the post-hover state in those buttons stays readable. */
  color: inherit !important;
}
.dw-creation [class*="bg-zinc-800"] .group:hover [class*="group-hover\\:text-white"] {
  color: #2c1f4d !important;
}

/* ============================================================
   Section 21. ui/select.tsx — light-theme readability fixes.
   ============================================================
   The shadcn-style <Select> component (components/ui/select.tsx)
   was authored for the dark cyber theme:
     • <SelectTrigger> defaults to 'bg-cyber-surface text-cyber-
       text-main border-cyber-border', and most call sites layer
       a hard-coded 'bg-[#1a1a24] border-white/10' on top.
     • <SelectValue> always wraps its content in
       <span class="text-cyber-text-main"> (= pure #ffffff).
     • <SelectContent> renders via createPortal under <body>,
       hard-codes 'bg-[#1e1e2a] text-cyber-text-main' and the
       inner <SelectItem>s use 'text-gray-400' / 'text-cyan-400'.

   Symptom on the audio-tab "Voice Design" form: the trigger box
   got recolored to white by Section 20's dark-utility remap, but
   its inner text stayed white-on-white, so the language ("Chinese")
   was completely invisible (red box in the user's screenshot).

   Fix strategy:
     a) Inside .dw-creation, force every <SelectTrigger>'s text and
        every nested 'text-cyber-text-main' / 'text-white' span to
        a deep readable grape (#2c1f4d).
     b) Re-skin the trigger surface itself to a frosted white card
        with a faint grape outline so it looks like the rest of
        the workbench inputs.
     c) For the portal-rendered <SelectContent> we cannot use a
        '.dw-creation X' selector (it's not in the subtree); we
        instead key off the 'data-dw-creation' attribute the
        CreationStyles effect mirrors onto <body> + <html>. While
        the workbench is mounted, EVERY portal <SelectContent>
        (identified by its inline 'bg-[#1e1e2a]') gets the light
        treatment; on unmount the attribute is removed and the
        original dark dropdown is restored across the rest of the
        app.

   Out of scope: the standalone /voice-studio route (not embedded
   in the workbench) keeps its native dark <Select> palette.
   ============================================================ */

/* ---- 21a. Trigger surface (the visible select box).
       Catch all triggers regardless of which extra utility classes
       the call site layered on. We anchor on the inline dark fill
       'bg-[#1a1a24]' which appears on every Voice Studio Select. */
.dw-creation button[class*="bg-\\[\\#1a1a24\\]"] {
  background: rgba(255, 255, 255, 0.92) !important;
  border-color: rgba(139, 92, 246, 0.22) !important;
  color: #2c1f4d !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.04),
    inset 0 1px 0 rgba(255, 255, 255, 0.6) !important;
}
.dw-creation button[class*="bg-\\[\\#1a1a24\\]"]:hover {
  border-color: rgba(139, 92, 246, 0.42) !important;
  background: rgba(255, 255, 255, 0.96) !important;
}
/* Same fix for the hover variant some triggers spell explicitly. */
.dw-creation button[class*="hover\\:border-white/20"]:hover {
  border-color: rgba(139, 92, 246, 0.42) !important;
}

/* ---- 21b. The text token used by <SelectValue>. The component
       wraps every value in <span class="text-cyber-text-main">,
       which Tailwind compiles to color: #ffffff. Force it to a
       readable grape inside the workbench. */
.dw-creation .text-cyber-text-main {
  color: #2c1f4d !important;
}
/* Belt-and-braces: any direct <span> child of a SelectTrigger-like
   button inherits the same color. Some call sites pass plain text
   inside <SelectValue>, which lands directly on the button — this
   keeps that case readable too. */
.dw-creation button[class*="bg-\\[\\#1a1a24\\]"] > span,
.dw-creation button[class*="bg-\\[\\#1a1a24\\]"] {
  color: #2c1f4d !important;
}
/* The chevron <svg> at the end of every trigger uses
   'opacity-50' on currentColor. Now that currentColor is dark,
   the icon is visible; we just nudge opacity up slightly so it
   looks balanced against the lighter background. */
.dw-creation button[class*="bg-\\[\\#1a1a24\\]"] > svg {
  opacity: 0.7 !important;
}

/* ---- 21c. Portal-rendered <SelectContent>. Lives outside the
       .dw-creation subtree, so we scope on the data attribute that
       CreationStyles mirrors onto <body>/<html>. We target the
       hard-coded 'bg-[#1e1e2a]' fill present on every dropdown. */
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"] {
  background: rgba(255, 255, 255, 0.98) !important;
  border-color: rgba(139, 92, 246, 0.18) !important;
  color: #2c1f4d !important;
  box-shadow:
    0 12px 32px -8px rgba(76, 29, 149, 0.18),
    0 4px 12px -4px rgba(15, 12, 30, 0.08) !important;
}
/* SelectItem default state is 'text-gray-400' on the dark theme;
   on the light dropdown that grey reads fine but feels washed out.
   Bump it to a more confident neutral. */
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"] [class*="text-gray-400"] {
  color: #4b3a6b !important;
}
/* Hover state used by SelectItem ('hover:bg-white/[0.06]'). Replace
   with a soft grape tint so it is actually visible on a white menu. */
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"] [class*="hover\\:bg-white/\\[0\\.06\\]"]:hover {
  background-color: rgba(139, 92, 246, 0.08) !important;
  color: #2c1f4d !important;
}
/* Selected item: source uses 'bg-cyan-500/20 text-cyan-400'. Re-skin
   to grape-on-grape so it matches the workbench accent palette. */
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"] [class*="bg-cyan-500/20"] {
  background-color: rgba(139, 92, 246, 0.14) !important;
}
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"] [class*="text-cyan-400"] {
  color: #6d28d9 !important;
}
/* The 1px portal border ('border-white/10') maps to the same faint
   grape stroke we use on workbench cards. */
body[data-dw-creation="1"] [class*="bg-\\[\\#1e1e2a\\]"][class*="border-white/10"] {
  border-color: rgba(139, 92, 246, 0.18) !important;
}

/* ============================================================
   Section 22. Image Generation panel — parameter cards.
   ============================================================
   Goal: align ImageGeneration's left column with VideoGeneration so
   each parameter group (超级提示词 / 画面比例 / 分辨率 / 参考图 /
   生成数量 ...) sits inside its own frosted-white card, instead of
   bleeding into the page background as a flat list (the original
   look the user pointed out via screenshot).

   Why a separate section:
     - VideoGeneration's left column markup is
         <div w-[420px] border-r>
           > <div border-b>            ← model picker structural card
           > <div .space-y-5.custom-scrollbar>  ← params, each child becomes a card via Rule 17c
           > <div border-t>            ← bottom CTA structural card
       Rule 17c keys off ".space-y-5.custom-scrollbar > div" and
       paints every parameter section as a card.
     - ImageGeneration uses the bespoke .ig-* class system instead.
       The model picker is wrapped in <div .ig-model-card> (already
       a white card via Rule 19d), but the parameter sections live
       inside <div class="p-4 space-y-5"> WITHOUT the
       'custom-scrollbar' marker — so Rule 17c never matched, and
       each section rendered as a flat block on the page bg.

   This section paints those parameter sections with the same recipe
   used in Rule 17c (frosted white card, faint grape edge, no shadow)
   so the two tabs feel consistent. We anchor on the unique
   '.ig-panel-left-bg' class so we never bleed onto unrelated
   '.space-y-5' lists elsewhere on the workbench (history grids,
   modal forms, etc.).
   ============================================================ */

/* 22a. The OUTER param container: <div class="flex-1 overflow-y-auto
       custom-scrollbar"> wraps the whole "input form" column inside
       the left panel. Make it transparent so only the inner cards
       read as panels (mirrors Rule 15c's behaviour for the video
       tab). The container keeps its own scroll. */
.dw-creation .ig-panel-left-bg > [class*="overflow-y-auto"][class*="custom-scrollbar"] {
  background-color: transparent !important;
  border: none !important;
  box-shadow: none !important;
}

/* 22b. The INNER param container: <div class="p-4 space-y-5"> holds
       the actual parameter sections. Switch to flex column with gap
       so each child can render edge-to-edge with consistent rhythm.
       We turn off the legacy 'space-y-5' margin to avoid double
       spacing on top of our gap.

       Note on the selector: [class*="p-4"] would also match "px-4"
       and "pb-4" (substring contains 'p-4'), so we add the
       'space-y-5' anchor — only this one container has both. */
.dw-creation .ig-panel-left-bg [class*="overflow-y-auto"][class*="custom-scrollbar"] > [class*="space-y-5"] {
  display: flex !important;
  flex-direction: column !important;
  gap: 12px !important;
  padding: 0 !important; /* let inner cards extend edge-to-edge */
}
.dw-creation .ig-panel-left-bg [class*="overflow-y-auto"][class*="custom-scrollbar"] > [class*="space-y-5"] > * + * {
  margin-top: 0 !important;
}

/* 22c. Each direct child of the inner container becomes a parameter
       card. Recipe matches Rule 17c so the visual rhythm is
       identical to the video tab (rounded-14, faint grape edge,
       semi-transparent white fill, no heavy shadow). */
.dw-creation .ig-panel-left-bg [class*="overflow-y-auto"][class*="custom-scrollbar"] > [class*="space-y-5"] > div {
  background-color: rgba(255, 255, 255, 0.92) !important;
  border: 1px solid rgba(139, 92, 246, 0.10) !important;
  border-radius: 14px !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.03),
    0 4px 16px -8px rgba(76, 29, 149, 0.06) !important;
  padding: 14px !important;
}

/* 22d. The first decorative <div class="ig-divider" /> immediately
       inside the input form column is a 1px gradient line (defined
       by '.ig-divider' in index.css). With every neighbouring
       section now in its own card, the divider becomes a stray
       floating hairline that visually doesn't belong. Hide it. */
.dw-creation .ig-panel-left-bg [class*="overflow-y-auto"][class*="custom-scrollbar"] > .ig-divider {
  display: none !important;
}

/* 22e. The "数量 + 立即生成" row at the bottom of the input form is
       wrapped in <div class="px-4 pb-4"> as a SIBLING of the inner
       p-4.space-y-5 container — so rule 22c's "> div" doesn't
       match it. We give it a matching card so the bottom action
       row aligns with the parameter cards above. */
.dw-creation .ig-panel-left-bg [class*="overflow-y-auto"][class*="custom-scrollbar"] > [class*="px-4"][class*="pb-4"][class*="pt-2"] {
  background-color: rgba(255, 255, 255, 0.92) !important;
  border: 1px solid rgba(139, 92, 246, 0.10) !important;
  border-radius: 14px !important;
  box-shadow:
    0 1px 2px rgba(15, 12, 30, 0.03),
    0 4px 16px -8px rgba(76, 29, 149, 0.06) !important;
  padding: 14px !important;
  margin-top: 12px !important;
}

/* 22f. The model picker host (<div class="flex-shrink-0 p-4">) sits
       OUTSIDE the scrollable input form. It already contains an
       .ig-model-card with its own card frame (Rule 19d), so we
       just remove its outer padding to make the inner card flush
       with the parameter cards below — otherwise the model picker
       would look subtly inset relative to the rest. */
.dw-creation .ig-panel-left-bg > [class*="flex-shrink-0"] {
  padding: 0 !important;
  margin-bottom: 12px !important;
}

/* 22g. The 420px left panel itself: add modest padding so the
       cards don't run flush against the panel edge / right border.
       This mirrors the video tab's Rule 17a treatment. */
.dw-creation .ig-panel-left-bg {
  padding: 14px !important;
}

/* ============================================================
   Section 23. Model icon container — unified light-theme style.
   ============================================================
   The video/image generation pages use a dark gradient background
   (#1e1b4b → #312e81) for model icon containers, which looks out
   of place on the light workbench canvas. We remap them to a soft
   violet tint matching the workbench's grape palette, and invert
   the icon color so it reads as dark-on-light — consistent with
   how the audio tab's colored SVG icons appear on their light
   background.
   ============================================================ */

/* 23a. The model icon container div (video generation page).
       Original: linear-gradient(135deg, #1e1b4b, #312e81) — deep
       indigo, designed for a dark page. On the light workbench it
       looks like a heavy dark slab. Re-skin to a soft violet tint
       that matches the grape card borders used throughout. */
.dw-creation [style*="1e1b4b"] {
  background: linear-gradient(135deg, rgba(139,92,246,0.15), rgba(109,40,217,0.12)) !important;
  box-shadow: inset 0 0 0 1px rgba(139,92,246,0.25), 0 2px 6px rgba(109,40,217,0.1) !important;
}

/* 23b. The icon <img> inside the container. The SVG files use
       #E0E0E0 (near-white) strokes — on the new light-tinted
       background they are invisible. Invert + darken so they
       render as deep violet marks on the soft violet surface,
       matching the grape accent used everywhere else. */
.dw-creation [style*="1e1b4b"] img {
  filter: brightness(0) saturate(100%) invert(25%) sepia(60%) saturate(800%) hue-rotate(240deg) !important;
}

/* ============================================================
   Section 24. Native <audio> player — light-theme adaptation.
   ============================================================
   The browser's default <audio controls> renders with a dark
   (near-black) background in Chromium. On the light workbench
   canvas this creates a jarring dark rectangle. We force the
   light color-scheme so the native controls render in their
   light variant, and add rounded corners plus a subtle border
   so the player blends with the frosted-white card surfaces.
   ============================================================ */
.dw-creation audio {
  color-scheme: light !important;
  border-radius: 8px !important;
  background-color: #f3f4f6 !important;
  border: 1px solid rgba(139, 92, 246, 0.15) !important;
}
`,a="dw-creation-styles";function l(){return r.useEffect(()=>{if(typeof document>"u")return;let e=document.getElementById(a);return e||(e=document.createElement("style"),e.id=a,document.head.appendChild(e)),e.textContent!==t&&(e.textContent=t),document.body.setAttribute("data-dw-creation","1"),document.documentElement.setAttribute("data-dw-creation","1"),()=>{document.body.removeAttribute("data-dw-creation"),document.documentElement.removeAttribute("data-dw-creation")}},[]),null}export{l as C,i as a,n as g,s as p};
