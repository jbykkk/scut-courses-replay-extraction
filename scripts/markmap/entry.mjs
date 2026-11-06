// The app supplies stylesheets locally and never uses Markmap's resource loader.
export { Transformer } from 'markmap-lib/no-plugins';
import { Markmap as UpstreamMarkmap } from 'markmap-view';
import { select } from 'd3';

export const Markmap = {
  create(svg, options) {
    const selection = select(svg);
    const append = selection.append;
    // Upstream always appends an inline <style>, even when its content is empty.
    // Keep that unused node detached so strict style-src 'self' remains intact.
    selection.append = function (name) {
      if (name === 'style') return select(svg.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'style'));
      return append.call(this, name);
    };
    return new UpstreamMarkmap(selection, { ...options, embedGlobalCSS: false, style: () => '' });
  },
};