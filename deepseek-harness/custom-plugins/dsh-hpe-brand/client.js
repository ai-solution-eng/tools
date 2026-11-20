/**
 * HPE brand — browser half.
 *
 * Restyles the Harness Web UI in HPE terms, using the HPE operations UI
 * (the Prometheus MCP "Ops Timeline" page) as the design reference:
 *
 *   accent        HPE green replaces the neutral accent and the DeepSeek blue:
 *                 the primary button, switches, checkboxes, the spinner, focus
 *                 rings, links, the selection wash, the slot caret and the dock
 *                 drop hint. Fills use #01a982 (the reference UI's `--hpe`);
 *                 anything that carries or sits under text uses the text-grade
 *                 step #017a5e, and dark mode uses the brighter #02c99a.
 *   brand mark    The HPE element — a sheared parallelogram, the same geometry
 *                 the reference UI draws with clip-path — replaces the fish in
 *                 the sidebar brand row (expanded and collapsed rail) and
 *                 before the blank-session headline.
 *   wordmark      "DeepSeek Harness" with the product word in HPE green,
 *                 mirroring the reference header's `<b>Product</b>` treatment.
 *
 * Three mechanisms, all sanctioned extension points:
 *
 *   1. `ctx.theme.overrideTokens(source, tokens)` stacks a *token layer* over
 *      the active theme. Only the tokens named below change; the base light and
 *      dark palettes stay untouched, the layer composes with other layers
 *      (later wins per token) and removing it restores what it covered. Each
 *      token carries its own light and dark value, so no scheme sniffing.
 *   2. `sidebar.brand.mark` / `sidebar.brand.name` / `conversation.hero.brand.mark`
 *      are SINGLE slots declared by the sidebar and conversation packages for
 *      exactly this purpose. A single slot renders one occupant and the first
 *      registration wins; replacing it leaves the surrounding chrome (the
 *      new-session button, the collapsed rail, the hero layout) alone.
 *   3. Styles ride along as a `<style>` element inside the rendered component,
 *      so unmounting the occupant removes them; colours come from
 *      `--dsw-alias-*` tokens, so light/dark and theme changes follow.
 *
 * The two wordmark words are proper nouns and stay untranslated in every
 * locale, which is why this module registers no locale dictionary — the
 * Plugin Manager metadata in `locale/*.json` is separate from runtime text.
 *
 * Nothing here touches the DOM outside the component, the app root, or another
 * plugin's markup.
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-hpe-brand',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** Package id, reused as the theme-layer source name. */
    const PACKAGE = '@local/dsh-hpe-brand'

    /**
     * HPE brand greens, taken from the reference operations UI: `--hpe` and
     * `--accent` are #01a982, `--accent-2` is #02c99a. #017a5e is the
     * text-grade step of the same hue — #01a982 reaches only 3:1 on white,
     * which is fine for a fill, a ring or a graphic but thin for lettering and
     * for a white label on top of it, so anything that carries or sits under
     * text uses the deeper step (5.3:1). #e6f7f1 / #123a30 are the pale and
     * dark tints of the same green, used for the "business" selection wash.
     */
    const GREEN = '#01a982'
    const GREEN_BRIGHT = '#02c99a'
    const GREEN_INK = '#017a5e'
    const GREEN_WASH = '#e6f7f1'
    const GREEN_WASH_DARK = '#123a30'

    /**
     * The accent tokens, each with its light and dark value.
     *
     * - `brand-primary` is the bright brand green: the brand mark, switches,
     *   checkbox accent and the loading spinner.
     * - `brand-primary-new-colorprimary-new-color` is the blue accent used by
     *   the slot caret and the dock drop hint; left alone it would reintroduce
     *   a second accent colour.
     * - `brand-text` is the palette's brand-coloured-text alias, used by the
     *   wordmark, so lettering carries the deeper green.
     * - `button-primary-fill` is the primary button's fill. It is overridden
     *   *separately* from `brand-primary` because its label is white: the
     *   bright green under white is 3:1, the text-grade green is 5.3:1, and
     *   the shared `label-primary-foreground` token cannot be flipped to dark
     *   ink without also inverting the contrast button.
     * - `button-primary-hover` must move with the fill, or the hover state
     *   falls back to the neutral palette and the button turns grey.
     * - `state-business-primary` is the focus ring and the interactive accent
     *   (it paints ~90 focus outlines and the border/colour of selected
     *   controls), so leaving it blue would leave the app half-rebranded.
     * - `link` colours every link.
     * - `state-business-tertiary` is the pale selection wash behind the hero
     *   badge and selected rows.
     */
    const TOKENS = {
      '--dsw-alias-brand-primary': { light: GREEN, dark: GREEN_BRIGHT },
      '--dsw-alias-brand-primary-new-colorprimary-new-color': { light: GREEN, dark: GREEN_BRIGHT },
      '--dsw-alias-brand-text': { light: GREEN_INK, dark: GREEN_BRIGHT },
      '--dsw-alias-button-primary-fill': { light: GREEN_INK, dark: GREEN_BRIGHT },
      '--dsw-alias-button-primary-hover': { light: GREEN, dark: GREEN },
      '--dsw-alias-state-business-primary': { light: GREEN_INK, dark: GREEN_BRIGHT },
      '--dsw-alias-link': { light: GREEN_INK, dark: GREEN_BRIGHT },
      '--dsw-alias-state-business-tertiary': { light: GREEN_WASH, dark: GREEN_WASH_DARK },
    }

    const CSS = [
      /* The wordmark sits inside the sidebar's 24px brand-name box, which
         already supplies 18px/600 type; only the two-tone colouring, the
         inline rhythm and the overflow behaviour are ours. The accent word
         owns the clipping, so a narrow sidebar ellipsises one word instead of
         cutting "Harness" mid-glyph. */
      '.dsh-hpe-brand{display:inline-flex;align-items:center;gap:.28em;min-width:0;white-space:nowrap}',
      '.dsh-hpe-brand__product{color:var(--dsw-alias-label-primary)}',
      '.dsh-hpe-brand__accent{color:var(--dsw-alias-brand-text);min-width:0;',
      'overflow:hidden;text-overflow:ellipsis}',
    ].join('')

    /**
     * The HPE element: a parallelogram with the top edge shifted right by a
     * quarter of its width. Geometry is the reference UI's clip-path
     * (`polygon(25% 0%, 100% 0%, 75% 100%, 0% 100%)`) expressed on a 24-unit
     * viewBox, so one drawing serves every requested size.
     *
     * The ink is inset one unit: the fish it replaces carries its own margins
     * inside the 24px seat, and a full-bleed slab would read larger than the
     * mark beside it.
     *
     * The fill is set as a *style* property, not the `fill` presentation
     * attribute: a presentation attribute does not parse `var()`, so
     * `fill="var(--x)"` paints black, while `style="fill:var(--x)"` resolves.
     *
     * @param {{ size?: number, className?: string }} props - slot owner props:
     *   `size` is the requested square edge, `className` the host's own
     *   geometry class (the hero passes one; the sidebar passes none).
     */
    function HpeMark(props) {
      const edge = typeof props.size === 'number' && props.size > 0 ? props.size : 24
      return h('svg', {
        className: props.className,
        viewBox: '0 0 24 24',
        width: edge,
        height: edge,
        'aria-hidden': true,
        focusable: false,
        style: { display: 'block', flex: 'none', overflow: 'visible' },
      }, h('path', {
        d: 'M6.9 1 H22.1 L17.1 23 H1.9 Z',
        style: { fill: 'var(--dsw-alias-brand-primary)' },
      }))
    }

    /**
     * The product wordmark beside the sidebar mark.
     *
     * The sidebar declares the seat and supplies the type (18px/600, 24px
     * line); this only splits the product name into a neutral first word and
     * an HPE-green second word, the way the reference header sets its product
     * line. It is not interactive — the surrounding brand row is the
     * new-session button and keeps its own behaviour.
     */
    function HpeWordmark() {
      return h(React.Fragment, null,
        h('style', { key: 'css' }, CSS),
        h('span', { key: 'wordmark', className: 'dsh-hpe-brand' },
          h('span', { className: 'dsh-hpe-brand__product' }, 'DeepSeek'),
          h('span', { className: 'dsh-hpe-brand__accent' }, 'Harness'),
        ),
      )
    }

    return {
      /* `slots` is the hard dependency. The theme service is looked up at
         apply time instead of being declared here: a hard `theme` entry leaves
         the whole plugin pending — and therefore silent — if the service is
         missing or later than this row. */
      inject: ['slots'],
      apply(ctx) {
        /* Register the seats FIRST, so nothing that follows can keep them off
           the page. */
        ctx.slots.inject('sidebar.brand.mark', () =>
          ctx.slots.register({ name: 'sidebar.brand.mark' }, HpeMark))

        ctx.slots.inject('sidebar.brand.name', () =>
          ctx.slots.register({ name: 'sidebar.brand.name' }, HpeWordmark))

        ctx.slots.inject('conversation.hero.brand.mark', () =>
          ctx.slots.register({ name: 'conversation.hero.brand.mark' }, HpeMark))

        /* The accent layer, attached last and without a throw path: the service
           is read straight off the context, and only a service that actually
           implements overrideTokens installs the layer. Disposing the effect —
           plugin unload, or a reload through HMR — restores the palette
           underneath. */
        const attach = (theme) => {
          if (theme === undefined || theme === null || typeof theme.overrideTokens !== 'function') return
          ctx.effect(() => theme.overrideTokens(PACKAGE, TOKENS), 'hpe-brand: accent tokens')
        }
        const theme = typeof ctx.get === 'function' ? ctx.get('theme') : undefined
        if (theme !== undefined) {
          attach(theme)
        } else if (typeof ctx.inject === 'function') {
          ctx.inject(['theme'], (themed) => attach(themed.theme))
        }
      },
    }
  },
})
